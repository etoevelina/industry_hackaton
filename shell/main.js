'use strict';
/**
 * Electron-оболочка «защищённого браузера» для локального прокторинга.
 *
 * Отвечает за:
 *  - kiosk-окно без рамки, поверх всех окон, с setContentProtection (окно выходит
 *    чёрным на скриншотах и в записи экрана — ключевой демо-эффект);
 *  - слой блокировки окружения (shell/lockdown.js) и журналирование каждой
 *    подавленной комбинации как SHELL_EVENT SHORTCUT_BLOCKED;
 *  - контроль мониторов: второй экран => MULTIPLE_DISPLAYS + блокирующий экран;
 *  - удержание фокуса и полного экрана: blur => WINDOW_BLUR + возврат фокуса,
 *    leave-full-screen => FULLSCREEN_EXIT + возврат в fullscreen;
 *  - WS-канал к сайдкару (shell/ipc.js). Сайдкар не поднялся — оболочка работает,
 *    HUD честно показывает «CV-канал недоступен»;
 *  - снимок ОКНА ЭКЗАМЕНА к инциденту (screen_evidence) по `event` с `screen: true` —
 *    только представление экзамена через capturePage(), никогда не рабочий стол;
 *  - финальный экран: где лежат отчёт и пакет, «Открыть отчёт» после конца сессии.
 *
 * Весь текст для пользователя — по-русски. Наружу (в интернет) не ходим: только loopback.
 */

const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { spawn } = require('child_process');
const {
  app, BrowserWindow, BrowserView, ipcMain, screen, globalShortcut, clipboard,
  session, Menu, nativeImage, shell: electronShell,
} = require('electron');

const { SidecarLink, EventKind, MsgType, DEFAULT_WS_HOST, DEFAULT_WS_PORT } = require('./ipc');
const { Lockdown, LOCK_SPECS, comboThrottle } = require('./lockdown');
const {
  isLockedState, canTransition, allowedFrom, lockPermissions,
  weakeningFlags, launchVerdict,
  normalizeExamProfile, examRequestVerdict, examDenyText, examDenyClass,
  examProfileHeadline, originRejectText, requestThrottle,
  examProfileCarriesRules,
  EXAM_VIEW_INSET_FALLBACK, normalizeExamViewInset, examViewRect,
  SCREEN_EVIDENCE, screenCaptureVerdict, isJpeg, jpegSize, screenShotCoordinator,
} = require('./state');

const ROOT_DIR = path.resolve(__dirname, '..');
const RENDERER_INDEX = path.join(__dirname, 'renderer', 'index.html');

/**
 * Есть ли интерфейс теста. Если есть — сессию открывает ТОЛЬКО renderer,
 * после экрана согласия и предполётной проверки. Автостарт допустим лишь
 * когда интерфейса нет и показан служебный экран оболочки: иначе прокторинг
 * начнёт писать доказательства до согласия студента.
 */
const HAS_RENDERER = fs.existsSync(RENDERER_INDEX);

/**
 * Аварийный выход для прокторa/жюри: иначе из kiosk-окна не выйти.
 * Регистрируется ПЕРВЫМ, до любых других хоткеев, и не снимается до самого
 * выхода процесса. Lockdown получает его в списке reserved и не трогает.
 */
const ADMIN_EXIT_ACCELERATOR = 'CommandOrControl+Alt+Shift+Q';

// Жизненный цикл оболочки — модель в shell/state.js, чтобы правило «блокировки
// только в exam и paused» проверялось без запуска Electron.

// ---------------------------------------------------------------------------
// Аргументы командной строки
// ---------------------------------------------------------------------------

function argFlag(name) {
  return process.argv.includes(`--${name}`);
}

function argValue(name, fallback) {
  const prefix = `--${name}=`;
  const found = process.argv.find((a) => a.startsWith(prefix));
  if (found) return found.slice(prefix.length);
  const idx = process.argv.indexOf(`--${name}`);
  if (idx !== -1 && process.argv[idx + 1] && !process.argv[idx + 1].startsWith('--')) {
    return process.argv[idx + 1];
  }
  return fallback;
}

/**
 * Все значения флага, а не первое. Белый список источников проктор пишет
 * несколькими записями (`--exam-origin a --exam-origin b`) либо одной через
 * запятую; argValue() взял бы только первую, то есть молча урезал бы список.
 */
function argValues(name) {
  const prefix = `--${name}=`;
  const out = [];
  const argv = process.argv;
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg.startsWith(prefix)) {
      out.push(arg.slice(prefix.length));
    } else if (arg === `--${name}` && argv[i + 1] && !argv[i + 1].startsWith('--')) {
      out.push(argv[i + 1]);
      i += 1;
    }
  }
  return out.reduce((acc, item) => acc.concat(String(item).split(/[\s,;]+/)), [])
    .map((s) => s.trim())
    .filter(Boolean);
}

const CLI = {
  spawnSidecar: argFlag('spawn-sidecar'),
  noKiosk: argFlag('no-kiosk'),              // отладочный режим: окно можно двигать
  // Блокировки не включаются НИКОГДА: ни глобальные хоткеи, ни очистка буфера
  // обмена, ни захват экрана окном. Для разработки и для прогона интерфейса
  // на чужой машине. Наблюдатели (мониторы) и запись событий продолжают работать.
  noLockdown: argFlag('no-lockdown'),
  // Аварийный клапан для сцены: проектор, подключённый расширением экрана, —
  // это второй монитор, и блокирующий экран закрывает собой весь показ, а снять
  // его нечем. Флаг оставляет событие MULTIPLE_DISPLAYS в журнале (честно), но
  // не блокирует экзамен. Ровно то, что обещает memory/demo-runbook.md.
  allowMultiDisplay: argFlag('allow-multi-display'),
  studentId: argValue('student-id', 'demo-student'),
  examId: argValue('exam-id', 'demo-exam'),
  studentName: argValue('student-name', 'Демо-студент'),
  wsHost: argValue('ws-host', DEFAULT_WS_HOST),
  wsPort: Number(argValue('ws-port', String(DEFAULT_WS_PORT))) || DEFAULT_WS_PORT,

  // --- профиль экзамена ----------------------------------------------------
  /*
   * ПРАВИЛА ЭКЗАМЕНА ОБОЛОЧКА НЕ ЗАДАЁТ. Единственный путь — файл профиля,
   * который читает ЯДРО. Этот флаг оболочка только ПЕРЕДАЁТ сайдкару, когда
   * запускает его сама, и ничего из него не разбирает.
   *
   * Почему так, а не «флаги как быстрый путь». Профиль из флагов оболочки
   * РАБОТАЛ: фильтр применялся, HUD писал «действовал профиль <хеш>», каждая
   * попытка выхода отбивалась. Но в подписанную цепочку он не попадал ВООБЩЕ:
   * ядро собирает запись по белому списку имён, флаговых ключей там нет, а
   * `_cmd_off_profile` при `profile.present == False` выбрасывает каждое
   * сообщение как `claimed_without_profile` — без события и без инцидента.
   * Получались два документа об одной сессии, противоречащие друг другу:
   * экран весь экзамен показывал действующий профиль, а подписанный отчёт —
   * «правила экзамена проктором не задавались» и ноль обращений. Улики
   * оставались только в stdout оболочки, то есть нигде.
   *
   * Чинится это не досылкой ключей в цепочку (ядро всё равно не считало бы по
   * ним хеш и не перепроверяло бы адреса), а тем, что у профиля ОДИН владелец.
   * Правила, которых нет у ядра, не действуют ни на что.
   */
  examProfilePath: argValue('exam-profile', ''),
  examProfilePubkey: argValue('exam-profile-pubkey', ''),

  /*
   * Флаги, которые раньше задавали правила на стороне оболочки. Оставлены
   * РАСПОЗНАВАЕМЫМИ намеренно: молча проигнорированный флаг хуже отвергнутого,
   * потому что проктор решит, что правила действуют. Запуск с ними не
   * применяет ничего, печатает отказ и уходит в журнал записью.
   */
  examUrl: argValue('exam-url', ''),
  examOrigins: argValues('exam-origin'),
  examPreset: argValue('exam-preset', ''),
  allowSearch: argFlag('allow-search'),
};

/**
 * Что флаги вообще разрешают блокировать (смысл флагов — в shell/state.js).
 * Разрешение — ещё не включение: оно всё равно требует состояния exam/paused.
 */
const LOCK_PERMISSIONS = lockPermissions(CLI);
/** Разрешён ли перехват глобальных сочетаний и очистка системного буфера. */
const SHORTCUT_LOCKS_ALLOWED = LOCK_PERMISSIONS.shortcuts;
/**
 * Разрешён ли захват экрана окном: alwaysOnTop, видимость на всех рабочих
 * столах, запрет закрытия, kiosk, fullscreen, принудительный возврат фокуса.
 */
const WINDOW_LOCKS_ALLOWED = LOCK_PERMISSIONS.window;

function log(...args) {
  // eslint-disable-next-line no-console
  console.log('[shell]', ...args);
}

// ---------------------------------------------------------------------------
// Режим запуска как часть доказательства (LAUNCH-01, LAUNCH-04)
// ---------------------------------------------------------------------------
/*
 * Флаги --no-lockdown, --no-kiosk и --allow-multi-display снимают защиту
 * машины, и этот факт НЕ попадал в подписанную цепочку вообще: состояние
 * защиты жило только в HUD, через protectionInfo(). Выключенные блокировки
 * были видны лишь по ОТСУТСТВИЮ событий SHORTCUT_BLOCKED и WINDOW_BLUR — то
 * есть ничем не отличались от честной сессии, в которой студент просто ничего
 * не нажимал.
 *
 * Переменные окружения PROCTOR_PYTHON (её resolvePython берёт ПЕРВОЙ),
 * PROCTOR_SIGNING_KEY и PROCTOR_SESSIONS_DIR позволяют подменить интерпретатор
 * ядра, переподписать отчёт и перенаправить сбор доказательств. Ни одна из
 * трёх здесь НЕ запрещается: на машине студента запрет бесполезен — запускает
 * он, и ядро у него в руках. Все три оставляют улику: уходят в SHELL_CONFIG, а
 * оттуда ядро пишет их в хеш-цепочку.
 */

/**
 * Переменные окружения, меняющие доказательную базу. Это пути, не секреты.
 * PROCTOR_WS_TOKEN сюда намеренно НЕ входит: он секрет канала, и его место не
 * в пакете доказательств, который забирает проктор.
 */
const EVIDENCE_ENV_VARS = Object.freeze([
  Object.freeze({
    name: 'PROCTOR_PYTHON',
    affects: 'интерпретатор, которым оболочка запускает сайдкар',
  }),
  Object.freeze({
    name: 'PROCTOR_SIGNING_KEY',
    affects: 'ключ, которым будет подписан отчёт',
  }),
  Object.freeze({
    name: 'PROCTOR_SESSIONS_DIR',
    affects: 'каталог, куда уедут доказательства',
  }),
]);

function existsQuiet(p) {
  try { return fs.existsSync(p); } catch (err) { return false; }
}

/** Экранирование для служебного экрана оболочки (он собирается строкой). */
function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Факты по переменным окружения. Значения читаются, содержимое файлов — нет. */
function envFacts() {
  return EVIDENCE_ENV_VARS.map((spec) => {
    const raw = String(process.env[spec.name] || '');
    const row = { name: spec.name, affects: spec.affects, set: Boolean(raw.trim()) };
    if (!row.set) return row;
    row.value = raw.slice(0, 500);
    try {
      row.resolved = path.resolve(raw);
      row.exists = existsQuiet(row.resolved);
    } catch (err) {
      row.resolved = '';
      row.exists = false;
    }
    return row;
  });
}

/**
 * Какой интерпретатор пойдёт в сайдкар и ОТКУДА он взят.
 *
 * Порядок приоритета не меняется: PROCTOR_PYTHON сильнее .venv, .venv сильнее
 * python3 из PATH. Выделено в функцию, чтобы источник попадал в SHELL_CONFIG и
 * тогда, когда оболочка сайдкар не запускала: заданная переменная остаётся
 * уликой независимо от того, успела ли она подействовать (`used`).
 */
function resolvePython() {
  const requested = String(process.env.PROCTOR_PYTHON || '').trim();
  if (requested) {
    return {
      requested,
      resolved: requested,
      source: 'env',
      exists: existsQuiet(requested),
      used: false,
    };
  }
  const venv = firstExisting([
    path.join(ROOT_DIR, '.venv', 'bin', 'python3'),
    path.join(ROOT_DIR, '.venv', 'bin', 'python'),
    path.join(ROOT_DIR, '.venv', 'Scripts', 'python.exe'),
  ]);
  if (venv) {
    return { requested: '', resolved: venv, source: 'venv', exists: true, used: false };
  }
  return { requested: '', resolved: 'python3', source: 'path', exists: false, used: false };
}

/**
 * Собрать фактическое состояние защиты машины для SHELL_CONFIG.
 *
 * Важно, что здесь спрашивается СИСТЕМА, а не наши же намерения:
 * auditHeldShortcuts() опрашивает globalShortcut.isRegistered по каждому
 * сочетанию, auditWindow() — свойства окна, state.contentProtectionApplied
 * хранит результат вызова setContentProtection. Иначе запись подтверждала бы
 * сама себя: «блокировки включены, потому что мы так решили».
 */
function shellConfigRecord(reason) {
  const label = `SHELL_CONFIG: ${reason || 'старт сессии'}`;
  const audit = auditHeldShortcuts(label);
  const windowInfo = auditWindow(label) || {};
  const python = state.sidecarPython || resolvePython();
  const verdict = launchVerdict(CLI);
  const profile = examProfileInfo();
  let displays = [];
  try {
    displays = screen.getAllDisplays().map((d) => ({
      id: d.id, internal: Boolean(d.internal), bounds: d.bounds, scale: d.scaleFactor,
    }));
  } catch (err) { /* screen доступен только после ready */ }

  return {
    platform: process.platform,
    shell_version: String(process.versions.electron || ''),
    exam_state: state.examState,
    renderer_present: HAS_RENDERER,
    sidecar_spawned_by_shell: Boolean(CLI.spawnSidecar),

    // --- флаги, снимающие защиту ---
    no_kiosk: Boolean(CLI.noKiosk),
    no_lockdown: Boolean(CLI.noLockdown),
    allow_multi_display: Boolean(CLI.allowMultiDisplay),

    // --- что ФАКТИЧЕСКИ применено ---
    locks_intended: Boolean(state.locksIntended),
    locks_active: Boolean(state.locksActive),
    window_locked: Boolean(state.windowLocked),
    clipboard_cleared: Boolean(lockdown.engaged),
    content_protection_requested: Boolean(state.contentProtection),
    // Трёхзначно: null — окна ещё нет и ответа на вопрос «применена ли» тоже.
    content_protection_applied: state.contentProtectionApplied,
    // Короткое имя того же факта: его читает шапка отчёта (storage/report.py).
    content_protection: state.contentProtectionApplied,
    system_wide: audit.others.length > 0 && process.platform === 'darwin',
    admin_exit: ADMIN_EXIT_ACCELERATOR,
    admin_exit_held: audit.held.indexOf(ADMIN_EXIT_ACCELERATOR) !== -1,

    // Реальный список удержанных сочетаний, без аварийного выхода: он держится
    // всегда и блокировкой не является. Пустой список при locks_active === true
    // означает, что не перехвачено НИ ОДНО сочетание, и ядро отмечает это
    // отдельной причиной непригодности.
    held_shortcuts: audit.others,
    held_shortcuts_count: audit.others.length,
    auditable_shortcuts_count: auditableAccelerators().length,

    display_count: state.displayCount,
    displays,
    window: windowInfo,

    // --- окружение и интерпретатор ---
    python: {
      requested: python.requested,
      resolved: python.resolved,
      source: python.source,
      used: Boolean(python.used),
    },
    env: envFacts(),
    argv_flags: process.argv.slice(1).filter((a) => a.startsWith('--')).slice(0, 32),

    // Вердикт по флагам. Ядро его НЕ принимает на слово и считает свой —
    // иначе сборка с --no-lockdown могла бы прислать «всё в порядке».
    weakened_by_flags: verdict.flags,

    // --- профиль экзамена ---------------------------------------------------
    /*
     * Хеш действовавшего профиля уходит сюда ВСЕГДА, без флага отключения.
     * «Профиля нет» — это тоже запись (`exam_profile_applied: false` плюс
     * готовая строка для шапки), а не отсутствие записи: отсутствие нельзя
     * отличить от оболочки, которая промолчала.
     *
     * ВАЖНО ПРО ЦЕПОЧКУ. Ядро собирает запись по белому списку имён
     * (SHELL_CONFIG_BOOLS / _TRISTATE / _STRINGS / _INTS в sidecar/main.py) и
     * всё незнакомое отбрасывает МОЛЧА. Из полей ниже в цепочку попадают
     * только те, чьи имена в этих списках есть; остальные доезжают до HUD и
     * служебного экрана, но не до подписанного отчёта.
     *
     * Правила экзамена при этом в цепочку кладёт НЕ оболочка: их читает,
     * хеширует и подписывает ядро по своему файлу профиля (запись
     * `control/exam_profile`). Поля ниже описывают не правила, а то, ЧТО
     * ОБОЛОЧКА С НИМИ СДЕЛАЛА, — и только это она и может засвидетельствовать.
     */
    /*
     * ТРИ ПОЛЯ, КОТОРЫЕ ЗНАЕТ ТОЛЬКО ОБОЛОЧКА, и без которых отчёт мог бы
     * печатать «действовал профиль <хеш>» по сессии, где фильтра не было.
     * Ядро принимает их по имени (SHELL_CONFIG_TRISTATE / _BOOLS / _INTS в
     * sidecar/main.py) — незнакомое имя оно отбрасывает молча, поэтому
     * добавлять поле здесь, не добавив имя там, бессмысленно.
     */
    // Трёхзначное, как content_protection_applied: `null` — страницу экзамена
    // не открывали, и спрашивать не о чем.
    exam_filter_installed: state.examFilterInstalled,
    /*
     * ГДЕ БЫЛА СТРАНИЦА LMS. Такой же факт о защите, как фильтр и захват
     * экрана, и так же трёхзначный: `null` — профиль без адреса, страницу
     * экзамена не открывали и спрашивать не о чем; `true` — чужая страница
     * была на экране; `false` — не была.
     *
     * Зачем в цепочке. Без этого поля по документу нельзя отличить сессию,
     * где студент работал в настоящем LMS под наблюдением, от сессии, где
     * представление так и не встало и он весь экзамен смотрел в наш мок-тест
     * (или, наоборот, где чужая страница накрыла собой согласие и калибровку).
     * Причина идёт рядом: «не прикреплено» без причины неотличимо от
     * «забыли прикрепить».
     */
    exam_view_attached: profile.examUrl ? Boolean(state.examViewAttached) : null,
    exam_view_reason: String(state.examViewReason || ''),
    // Откуда взято место под страницу экзамена: отчёт renderer по фактической
    // раскладке или запасная константа. Если запасная — HUD мог не совпасть
    // с отведённым ему местом, и это надо видеть в документе.
    exam_view_inset_source: state.examViewInsetSource,
    exam_view_inset_reason: state.examViewInsetReason,
    // Флаги правил подали оболочке, и она их отклонила. Сами имена уже лежат
    // в argv_flags; это поле называет случай словом, чтобы отчёт не разбирал
    // argv глазами.
    exam_profile_flags_refused: state.examProfileFlagsRefused.length > 0,
    // Отменённые скачивания с РАЗРЕШЁННОГО источника. Не инцидент и не выход
    // за профиль: масштаб нужен, чтобы «экзамен не состоялся из-за вложений»
    // читалось из документа, а не выяснялось на разборе.
    exam_downloads_cancelled: state.examDownloadsCancelled,

    exam_profile_applied: Boolean(profile.applied),
    exam_profile_hash: profile.hash,
    // Хеш того, что оболочка ФАКТИЧЕСКИ применяет. Отдельно от хеша ядра:
    // ядро хеширует объявленные правила, оболочка — действующие.
    exam_profile_applied_hash: profile.appliedHash,
    exam_profile_source: profile.source,
    exam_profile_exam_url: profile.examUrl,
    exam_profile_preset: profile.preset,
    exam_profile_search_allowed: Boolean(profile.allowSearch),
    exam_profile_broad: Boolean(profile.broad),
    exam_profile_origins_count: profile.originsCount,
    exam_profile_rejected_count: profile.rejected.length,
    exam_profile_blocked_navigation: state.blocked.navigation,
    exam_profile_blocked_iframe: state.blocked.iframe,
    exam_profile_blocked_subresource: state.blocked.subresource,
    // Готовая строка для ШАПКИ отчёта, рядом с вердиктом: «действовал профиль
    // <хеш>» либо «правила экзамена проктором не задавались».
    exam_profile_headline: profile.headline,
    exam_profile: Object.assign({}, profile, {
      // Список режем: в SHELL_CONFIG есть потолок размера, а белый список
      // проктора может быть длинным. Полное число остаётся в origins_count.
      origins: profile.origins.slice(0, 64),
      rejected: profile.rejected.slice(0, 32),
    }),
    // Виды событий, которых это ядро не знает. Честная оговорка: если факты
    // легли в цепочку видом-заменой, отчёт должен это сказать.
    unsupported_event_kinds: link.unsupportedKinds(),
  };
}

/**
 * Предупреждение о непригодности сессии. Собирается из флагов (они известны
 * до окна и до сайдкара) — печатается в консоль при старте и уходит в HUD.
 */
function launchWarningText() {
  const verdict = launchVerdict(CLI);
  if (verdict.examReady) return '';
  return 'СЕССИЯ НЕПРИГОДНА ДЛЯ НАСТОЯЩЕГО ЭКЗАМЕНА. Запуск с ослабляющими флагами: '
    + verdict.reasons.join('; ')
    + '. Факт записан в журнал и в хеш-цепочку сессии.';
}

/** Заметное предупреждение в консоль. Один раз, на старте. */
function warnWeakenedLaunch() {
  const text = launchWarningText();
  if (!text) {
    log('режим запуска: ослабляющих флагов нет');
    return false;
  }
  const line = '='.repeat(72);
  // eslint-disable-next-line no-console
  console.warn(`\n${line}\n[shell] ${text}\n${line}\n`);
  for (const spec of weakeningFlags(CLI)) {
    log(`  ${spec.flag} — ${spec.takes}`);
  }
  log('Для настоящего экзамена запускайте без этих флагов: npm run dev:kiosk');
  return true;
}

// Непойманное исключение в main-процессе Electron гасит всё приложение: kiosk-окно
// исчезает вместе с экзаменом. Источников много и все асинхронные — таймеры
// lockdown, события screen, обработчики окна. На показе лучше запись в лог и
// продолжение работы, чем пустой экран, поэтому процесс мы не роняем.
/**
 * Авария в main-процессе. Правило: окно живо — продолжаем экзамен, но
 * обязательно переподтверждаем аварийный выход (ошибка могла прийти из кода,
 * который его снял). Окна нет — значит интерфейса теста нет, и держать
 * перехваченной клавиатуру пользователя не за чем: освобождаем всё и уходим.
 */
function onFatal(label, err) {
  try {
    log(`${label}:`, (err && err.stack) || err);
  } catch (e) { /* консоль могла уйти */ }
  const win = state.win;
  const alive = Boolean(win && !win.isDestroyed());
  if (!alive) {
    log('окна теста нет — освобождаем машину и выходим');
    shutdown(`fatal:${label}`);
    return;
  }
  try { ensureAdminExit(); } catch (e) { /* ничего не поделать */ }
  log('окно теста живо — оболочка продолжает работу, аварийный выход переподтверждён');
}

process.on('uncaughtException', (err) => onFatal('НЕОБРАБОТАННАЯ ОШИБКА', err));
process.on('unhandledRejection', (reason) => onFatal('НЕОБРАБОТАННЫЙ REJECT', reason));

// Ctrl+C в терминале и kill: Electron по умолчанию уходит, не разворачивая
// наши блокировки. Перехватываем явно.
process.on('SIGINT', () => shutdown('sigint'));
process.on('SIGTERM', () => shutdown('sigterm'));
process.on('SIGHUP', () => shutdown('sighup'));

// Последняя линия: что бы ни случилось раньше, на выходе процесса глобальные
// сочетания отпущены. Синхронно, без таймеров — другого шанса не будет.
process.on('exit', () => releaseEverything('process_exit'));

// ---------------------------------------------------------------------------
// Состояние процесса
// ---------------------------------------------------------------------------

const state = {
  examState: 'idle',       // одно из SHELL_STATES; ведётся только через setExamState()
  examStateAt: Date.now(),
  locksIntended: false,    // экзамен идёт — блокировки ДОЛЖНЫ быть
  locksActive: false,      // блокировки ФАКТИЧЕСКИ включены (с --no-lockdown не станут)
  windowLocked: false,     // применён ли захват экрана к окну
  adminExitHeld: false,    // держим ли аварийный выход
  win: null,
  overlay: null,
  blockingReason: null,
  blockingTitle: '',
  blockingText: '',
  contentProtection: true,       // НАМЕРЕНИЕ: защита содержимого должна быть
  contentProtectionApplied: null, // ФАКТ: вызов прошёл (null — окна ещё нет)
  sidecarPython: null,           // чем фактически запущен сайдкар, если запускали
  displayCount: 1,
  sessionStarted: false,
  sessionAuto: false,
  sessionMeta: null,
  sessionSentAt: 0,        // когда последний раз отправляли session_start
  linkEpoch: 0,            // номер текущего соединения с сайдкаром
  sessionEpoch: -1,        // на каком соединении уже доливали session_start
  quitting: false,
  sidecarChild: null,
  rendererReady: false,
  lastVerdict: null,
  lastRisk: null,
  statusTimer: null,
  /**
   * Где лежат отчёт и пакет последней сессии — для финального экрана.
   * Заполняется из того, что ядро уже присылает: SESSION_STARTED (каталог
   * сессии), SESSION_ENDED (код, признак деградации, путь пакета) и status
   * (итог сборки пакета, причина деградации). Своих путей оболочка не
   * выдумывает. См. freshSessionReport().
   */
  sessionReport: freshSessionReport(),

  // --- профиль экзамена и страница LMS ------------------------------------
  examProfile: null,        // результат normalizeExamProfile(); null до старта
  examProfileHash: '',      // sha256 канонической формы — то, что ДЕЙСТВОВАЛО
  examProfileSource: 'none',
  examView: null,           // BrowserView со страницей экзамена
  examViewAttached: false,
  /**
   * Почему представление в текущем положении. Пишется ТОЛЬКО в
   * syncExamViewWithState() и уходит в SHELL_CONFIG: «не прикреплено» без
   * причины неотличимо от «забыли прикрепить», а это разные инциденты.
   */
  examViewReason: 'startup',
  /**
   * Отступ, доложенный renderer по своей фактической раскладке, и факт
   * проверки этого отчёта. null — ещё не докладывал, работает запасной.
   */
  examViewInset: null,
  examViewInsetSource: 'fallback',
  examViewInsetReason: 'not_reported',
  examSessionGuarded: false,
  /*
   * Встал ли фильтр запросов. ТРЁХЗНАЧНОЕ, как content_protection_applied:
   * `null` — страницу экзамена ещё не открывали и спрашивать не о чем,
   * `true` — onBeforeRequest установлен, `false` — установить не удалось.
   * Свернуть `null` в `false` было бы подлогом в сторону обвинения, а в
   * `true` — подлогом в сторону «всё работало».
   */
  examFilterInstalled: null,
  /** Флаги правил, поданные оболочке и отклонённые. Факт для цепочки. */
  examProfileFlagsRefused: [],
  /** Скачивания, отменённые с РАЗРЕШЁННОГО источника: не выход за профиль. */
  examDownloadsCancelled: 0,
  examProfileRawSignature: '',
  examLastUrl: '',
  examLoadError: '',
  blocked: {                // счётчики для SHELL_CONFIG и HUD
    navigation: 0,
    iframe: 0,
    subresource: 0,
    suppressed: 0,
  },
};

/**
 * Раздел сессии для страницы экзамена. БЕЗ префикса `persist:` — значит
 * непостоянный: cookie, localStorage и кеш страницы экзамена живут только до
 * выхода из оболочки. Это не придирка к чистоте: сохранённая сессия Moodle
 * означала бы, что следующий студент за этой машиной садится под логином
 * предыдущего, а наша оболочка — именно та, которую запускают по очереди.
 */
const EXAM_PARTITION = 'exam-lms';

/*
 * Куда вписать страницу экзамена внутри окна — считает shell/state.js
 * (EXAM_VIEW_INSET_FALLBACK / normalizeExamViewInset / examViewRect), а
 * фактическую раскладку присылает renderer: см. ipcMain 'proctor:exam-view-inset'
 * и applyExamViewInset() ниже. Константу здесь больше не держим — ровно она и
 * накрывала HUD на окне уже 1100px, где панель прокторинга уезжает вниз.
 */

const link = new SidecarLink({ host: CLI.wsHost, port: CLI.wsPort, logger: (m) => log('ipc:', m) });

/** Троттлинг событий оболочки: одна комбинация — не чаще раза в 400 мс. */
const allowCombo = comboThrottle(400);
/** Производные события (paste/devtools) — реже, чтобы не дублировать сигнал. */
const allowDerived = comboThrottle(1500);
/** Потеря фокуса и выход из fullscreen — раз в секунду, иначе ping-pong. */
const allowWindowState = comboThrottle(1000);

/*
 * Дедупликация блокировок запросов. Два разных окна, потому что это факты
 * разного смысла:
 *
 *  - навигация — осознанная попытка студента. Ключ — ПОЛНЫЙ адрес, окно
 *    короткое: оно нужно лишь затем, чтобы одна попытка не дала две записи
 *    (onBeforeRequest и will-navigate срабатывают по одному действию). Два
 *    разных адреса на одном домене — два разных факта, и склеивать их нельзя:
 *    в журнале должно остаться, что именно пытались открыть;
 *  - подзапросы — обращения самой страницы. Ключ — origin, окно длиннее:
 *    одна страница Moodle с десятком заблокированных картинок иначе залила бы
 *    журнал десятком одинаковых записей. Количество не теряется: подавленные
 *    повторы считаются и уходят в журнал полем `repeats`.
 */
const allowBlockedNavigation = requestThrottle(1200, 256);
const allowBlockedSubresource = requestThrottle(5000, 512);

const lockdown = new Lockdown({
  // Аварийный выход не должен попасть в управление Lockdown: иначе его снял бы
  // первый же release(). Держит его только main-процесс.
  reserved: [ADMIN_EXIT_ACCELERATOR],
  logger: (m) => log('lockdown:', m),
  onShortcutBlocked: (combo, meta) => {
    link.sendShellEvent(EventKind.SHORTCUT_BLOCKED, {
      combo,
      lock: meta.lock,
      label: meta.label,
      source: meta.source,
      platform: meta.platform,
    });
    log('подавлена комбинация', combo);
  },
  onClipboardCleared: (info) => {
    // Непустой буфер во время теста — след копирования. Сам текст наружу не уходит.
    if (!allowDerived('clipboard-clear')) return;
    link.sendShellEvent(EventKind.CLIPBOARD_PASTE, {
      reason: 'clipboard_nonempty_cleared',
      length: info.length,
      source: 'clipboard',
    });
  },
  onDisplays: (info) => handleDisplays(info, 'poll'),
});

// ---------------------------------------------------------------------------
// Передача в renderer
// ---------------------------------------------------------------------------

function sendToRenderer(channel, payload) {
  const win = state.win;
  if (!win || win.isDestroyed()) return;
  const wc = win.webContents;
  if (!wc || wc.isDestroyed()) return;
  try {
    wc.send(channel, payload);
  } catch (err) {
    log('send в renderer не удался:', err && err.message);
  }
}

function shellStatus() {
  const sidecar = link.info();
  return {
    platform: process.platform,
    sidecar,
    cvAvailable: sidecar.connected,
    // строка готова к выводу в HUD как есть
    cvMessage: sidecar.connected
      ? 'CV-канал активен'
      : (sidecar.available ? 'CV-канал недоступен' : 'CV-канал недоступен (не установлен ws)'),
    contentProtection: state.contentProtection,
    displayCount: state.displayCount,
    blocking: state.blockingReason
      ? { reason: state.blockingReason, title: state.blockingTitle, text: state.blockingText }
      : null,
    session: {
      started: state.sessionStarted,
      auto: state.sessionAuto,
      meta: state.sessionMeta,
    },
    lastVerdict: state.lastVerdict,
    lastRisk: state.lastRisk,
    lockdown: lockdown.capabilities(),
    adminExit: ADMIN_EXIT_ACCELERATOR,
    examState: state.examState,
    protection: protectionInfo(),
    // Правила экзамена для HUD: студент обязан видеть, какие источники ему
    // разрешены, а не узнавать об этом из блокировки на середине задания.
    examProfile: examProfileInfo(),
    // Финальный экран: где отчёт и пакет, код сессии, деградация передачи.
    sessionReport: sessionReportInfo(),
  };
}

function broadcastShellStatus() {
  sendToRenderer('proctor:shell-status', shellStatus());
}

// ---------------------------------------------------------------------------
// Жизненный цикл блокировок
// ---------------------------------------------------------------------------

/**
 * Что показать студенту: включён ли защищённый режим и что именно он забирает.
 * Отдаётся и в shell-status (для HUD), и отдельным событием при каждом
 * изменении — студент обязан понимать, в каком режиме его машина.
 */
function protectionInfo() {
  const accelerators = lockdown.engaged ? lockdown.registeredAccelerators() : [];
  // Вердикт по флагам считается один раз: protectionInfo() зовётся из
  // shellStatus() раз в секунду.
  const launch = launchVerdict(CLI);
  let reason;
  if (CLI.noLockdown) reason = 'выключен флагом --no-lockdown';
  else if (state.locksActive) reason = `экзамен идёт (состояние «${state.examState}»)`;
  else reason = `экзамен не идёт (состояние «${state.examState}»)`;
  return {
    active: state.locksActive,
    windowLocked: state.windowLocked,
    examState: state.examState,
    reason,
    accelerators,
    clipboardCleared: lockdown.engaged,
    systemWide: accelerators.length > 0 && process.platform === 'darwin',
    disabledByFlag: Boolean(CLI.noLockdown),
    noKiosk: Boolean(CLI.noKiosk),
    allowMultiDisplay: Boolean(CLI.allowMultiDisplay),
    adminExit: ADMIN_EXIT_ACCELERATOR,
    // Пригоден ли ЗАПУСК для настоящего экзамена. Отдельно от `active`:
    // блокировки могут быть включены, а сессия всё равно непригодна —
    // например, при --no-kiosk окно не удерживается, хотя сочетания
    // перехватываются, и «защищённый режим ВКЛЮЧЁН» было бы полуправдой.
    examReady: launch.examReady,
    weakenedBy: launch.flags,
    warning: launch.examReady ? '' : launchWarningText(),
    contentProtectionApplied: state.contentProtectionApplied,
    // Профиль экзамена рядом с состоянием защиты, но ОТДЕЛЬНЫМ полем и с
    // оговоркой: он не входит в `active` и не делает сессию «защищённой».
    examProfile: examProfileInfo(),
  };
}

function broadcastProtection() {
  sendToRenderer('proctor:protection', protectionInfo());
}

/**
 * Показать студенту и проктору, что сессия непригодна для экзамена.
 *
 * Канал — `proctor:error`: renderer подписан на него через preload и выводит
 * сообщение видимым уведомлением HUD. Своего канала для этого не заводим
 * намеренно: список каналов main -> renderer объявлен в shell/preload.js, и
 * сообщение по неизвестному каналу renderer просто не получил бы — то есть
 * предупреждение молча не дошло бы до того, кому оно адресовано.
 *
 * `fatal: false` обязателен: по fatal renderer помечает канал наблюдения
 * сломанным, а канал здесь в полном порядке — непригоден режим запуска.
 */
function pushLaunchWarningToHud() {
  const text = launchWarningText();
  if (!text) return false;
  sendToRenderer('proctor:error', {
    code: 'shell_launch_weakened',
    message: text,
    fatal: false,
  });
  return true;
}

/**
 * Все сочетания, которые оболочка в принципе способна занять. Нужен не для
 * работы, а для проверки: по этому списку можно спросить систему, держим ли мы
 * что-нибудь прямо сейчас, и напечатать ответ в журнал. Без такой строки
 * «блокировок нет» — утверждение на слово, а не факт.
 */
function auditableAccelerators() {
  const out = new Set([ADMIN_EXIT_ACCELERATOR]);
  for (const spec of LOCK_SPECS) {
    if (spec.kind !== 'shortcut') continue;
    const list = spec.accelerators && (
      Object.prototype.hasOwnProperty.call(spec.accelerators, process.platform)
        ? spec.accelerators[process.platform]
        : spec.accelerators.default
    );
    if (Array.isArray(list)) for (const acc of list) out.add(acc);
  }
  return [...out];
}

/**
 * Спросить систему, какие из наших сочетаний заняты прямо сейчас,
 * и напечатать результат. Вызывается на старте и при снятии блокировок.
 */
function auditHeldShortcuts(label) {
  const held = [];
  for (const acc of auditableAccelerators()) {
    try {
      if (globalShortcut.isRegistered(acc)) held.push(acc);
    } catch (err) { /* модуль мог уйти */ }
  }
  const others = held.filter((a) => a !== ADMIN_EXIT_ACCELERATOR);
  log(`АУДИТ ХОТКЕЕВ [${label}]: занято всего ${held.length}`
    + `; аварийный выход ${held.includes(ADMIN_EXIT_ACCELERATOR) ? 'держим' : 'НЕ держим'}`
    + `; блокирующих сочетаний ${others.length}`
    + `${others.length ? ` — ${others.join(', ')}` : ' (ни одно системное сочетание не перехвачено)'}`);
  return { held, others };
}

/** Фактические свойства окна — чтобы «окно обычное» было проверяемо по журналу. */
function auditWindow(label) {
  const win = state.win;
  if (!win || win.isDestroyed()) {
    log(`АУДИТ ОКНА [${label}]: окна нет`);
    return null;
  }
  const ask = (fn, fallback) => {
    try { return fn(); } catch (err) { return fallback; }
  };
  const info = {
    alwaysOnTop: ask(() => win.isAlwaysOnTop(), null),
    closable: ask(() => win.isClosable(), null),
    minimizable: ask(() => win.isMinimizable(), null),
    movable: ask(() => win.isMovable(), null),
    kiosk: ask(() => win.isKiosk(), null),
    fullscreen: ask(() => win.isFullScreen(), null),
  };
  log(`АУДИТ ОКНА [${label}]: alwaysOnTop=${info.alwaysOnTop} closable=${info.closable} `
    + `minimizable=${info.minimizable} movable=${info.movable} `
    + `kiosk=${info.kiosk} fullscreen=${info.fullscreen}`);
  return info;
}

/**
 * Аварийный выход. Регистрируется первым и перерегистрируется после любого
 * release(): если его всё же кто-то снял, машина не должна остаться без
 * способа выйти из экзамена.
 */
function ensureAdminExit() {
  if (state.quitting) return state.adminExitHeld;
  try {
    if (globalShortcut.isRegistered(ADMIN_EXIT_ACCELERATOR)) {
      state.adminExitHeld = true;
      return true;
    }
  } catch (err) { /* спросим регистрацией */ }
  try {
    const ok = Boolean(globalShortcut.register(
      ADMIN_EXIT_ACCELERATOR, () => shutdown('admin_exit'),
    ));
    if (ok && !state.adminExitHeld) log('аварийный выход прокторa:', ADMIN_EXIT_ACCELERATOR);
    if (!ok) log('ВНИМАНИЕ: аварийный выход не зарегистрирован, сочетание занято системой');
    state.adminExitHeld = ok;
    return ok;
  } catch (err) {
    log('не удалось зарегистрировать аварийный выход:', err && err.message);
    state.adminExitHeld = false;
    return false;
  }
}

/**
 * Захват экрана окном. Применяется ТОЛЬКО на время экзамена: alwaysOnTop,
 * видимость над чужим fullscreen, запрет закрытия и свёртывания, kiosk.
 * Снимается при выходе из экзамена — окно снова ведёт себя как обычное.
 */
function applyWindowLocks(on) {
  const win = state.win;
  const want = Boolean(on) && WINDOW_LOCKS_ALLOWED;
  if (!win || win.isDestroyed()) {
    state.windowLocked = false;
    return false;
  }
  const safe = (label, fn) => {
    try { fn(); } catch (err) { log(`окно: ${label} не поддержано — ${err && err.message}`); }
  };

  if (want) {
    safe('setAlwaysOnTop', () => win.setAlwaysOnTop(true, 'screen-saver', 1));
    safe('setVisibleOnAllWorkspaces', () => win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true }));
    safe('setClosable', () => win.setClosable(false));
    safe('setMinimizable', () => win.setMinimizable(false));
    safe('setMovable', () => win.setMovable(false));
    safe('setFullScreen', () => win.setFullScreen(true));
    safe('setKiosk', () => win.setKiosk(true));
    safe('focus', () => { win.show(); win.focus(); win.moveTop(); });
  } else {
    safe('setKiosk', () => win.setKiosk(false));
    safe('setFullScreen', () => win.setFullScreen(false));
    safe('setAlwaysOnTop', () => win.setAlwaysOnTop(false));
    safe('setVisibleOnAllWorkspaces', () => win.setVisibleOnAllWorkspaces(false));
    safe('setClosable', () => win.setClosable(true));
    safe('setMinimizable', () => win.setMinimizable(true));
    safe('setMovable', () => win.setMovable(true));
  }
  state.windowLocked = want;
  return want;
}

/**
 * Единственная точка включения и выключения блокировок. Никто, кроме
 * setExamState() и аварийных путей, её не зовёт.
 */
function applyLocksForState(reason) {
  // Намерение (экзамен идёт) и факт (что реально включено) различаются: с
  // --no-lockdown намерение есть, а включать нечего. Путать их нельзя, иначе
  // HUD напишет «защищённый режим включён» там, где не включено ничего.
  const want = isLockedState(state.examState);

  if (want && !state.locksIntended) {
    state.locksIntended = true;
    ensureAdminExit();                       // ПЕРВЫМ, до любых других хоткеев
    if (SHORTCUT_LOCKS_ALLOWED) {
      const caps = lockdown.engage();
      log(`блокировки (${caps.platformLabel}): полных ${caps.summary.supported}, `
        + `частичных ${caps.summary.partial}, недоступных ${caps.summary.unsupported}`);
    } else {
      log('перехват сочетаний и очистка буфера НЕ включены: флаг --no-lockdown');
    }
    applyWindowLocks(true);
    state.locksActive = lockdown.engaged || state.windowLocked;
    log(`${state.locksActive ? 'защищённый режим ВКЛЮЧЁН' : 'экзамен идёт, но блокировок НЕТ'} `
      + `(${reason || state.examState}); `
      + `хоткеи: ${lockdown.engaged ? 'перехватываются' : 'не перехватываются'}, `
      + `окно: ${state.windowLocked ? 'поверх всех, не закрывается' : 'обычное'}`);
    auditHeldShortcuts(`после включения: ${reason || '—'}`);
  } else if (!want && state.locksIntended) {
    releaseLocks(reason || `состояние ${state.examState}`);
  } else if (want) {
    // уже включено: подстрахуем аварийный выход и перехват после возврата фокуса
    ensureAdminExit();
    lockdown.reregister();
    state.locksActive = lockdown.engaged || state.windowLocked;
  }

  broadcastProtection();
  broadcastShellStatus();
  return state.locksActive;
}

/**
 * Снятие блокировок. Идемпотентно и безопасно для вызова из любого
 * аварийного пути — в том числе когда окно уже уничтожено.
 */
function releaseLocks(reason) {
  // Было ли что снимать: нужно только для журнала, снятие выполняется всегда.
  const hadSomething = state.locksIntended || state.locksActive
    || lockdown.engaged || state.windowLocked;
  state.locksIntended = false;
  try {
    lockdown.release(reason);
  } catch (err) {
    log('release блокировок не удался:', err && err.message);
  }
  try {
    applyWindowLocks(false);
  } catch (err) {
    log('снятие захвата окна не удалось:', err && err.message);
  }
  state.locksActive = false;
  state.windowLocked = false;
  // Аварийный выход мог быть снят чем угодно — возвращаем его на место.
  ensureAdminExit();
  if (hadSomething) {
    log(`защищённый режим СНЯТ (${reason || 'без причины'}); машина снова у пользователя`);
    auditHeldShortcuts(`после снятия: ${reason || '—'}`);
    auditWindow(`после снятия: ${reason || '—'}`);
  }
  broadcastProtection();
}

/**
 * Проверка и применение перехода. Renderer присылает состояние, но решение
 * принимается здесь: неизвестное имя и недопустимый переход отвергаются,
 * и локдаун по ним не включается.
 *
 * @returns {{ok:boolean, state:string, reason?:string}}
 */
function setExamState(next, source) {
  const want = typeof next === 'string' ? next.trim().toLowerCase() : '';
  const verdict = canTransition(state.examState, want);
  if (!verdict.ok) {
    log(`переход отвергнут (${verdict.reason}): ${state.examState} -> «${String(next)}» `
      + `(источник ${source}); допустимо: ${allowedFrom(state.examState).join(', ') || 'ничего'}`);
    return { ok: false, state: state.examState, reason: verdict.reason };
  }
  if (verdict.reason === 'unchanged') {
    return { ok: true, state: state.examState, reason: 'unchanged' };
  }

  const prev = state.examState;
  state.examState = want;
  state.examStateAt = Date.now();
  log(`состояние оболочки: ${prev} -> ${want} (источник ${source})`);
  applyLocksForState(`${prev}->${want}`);
  // Страница экзамена живёт ровно столько же, сколько блокировки: чужой LMS
  // появляется на экране только в exam/paused и уходит вместе с ними.
  syncExamViewWithState(`${prev}->${want}`);
  // Блокирующий экран живёт по тому же правилу: внутри экзамена он законен,
  // вне — тупик. Пересматриваем его на каждом переходе, иначе монитор,
  // подключённый до экзамена, так и не поднял бы оверлей внутри него.
  syncBlockingWithState(`${prev}->${want}`);
  return { ok: true, state: state.examState, previous: prev };
}

// ---------------------------------------------------------------------------
// Блокирующий экран (нативное оверлей-окно, не зависит от renderer)
// ---------------------------------------------------------------------------

/**
 * Строка в JS-литерал для инлайн-скрипта. '<' экранируем в <, иначе текст
 * вида '</script>' (а его может прислать renderer через setBlocking) разорвёт тег.
 */
function jsString(value) {
  return JSON.stringify(String(value)).replace(/</g, '\\u003c');
}

function blockingHtml(title, text) {
  const html = `<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Тест приостановлен</title><style>
html,body{margin:0;height:100%;background:#0b0f14;color:#e6edf3;
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
.wrap{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;
text-align:center;padding:48px;box-sizing:border-box;}
.mark{width:72px;height:72px;border-radius:50%;border:3px solid #f0883e;color:#f0883e;
display:flex;align-items:center;justify-content:center;font-size:40px;margin-bottom:28px;}
h1{font-size:28px;margin:0 0 14px;font-weight:600;}
p{margin:0;max-width:640px;color:#9fb0c0;font-size:17px;}
.hint{margin-top:32px;font-size:14px;color:#6b7c8c;}
</style></head><body><div class="wrap">
<div class="mark">!</div><h1></h1><p></p>
<div class="hint">Экран снимется автоматически, как только нарушение будет устранено.</div>
</div><script>
document.querySelector('h1').textContent = ${jsString(title)};
document.querySelector('p').textContent = ${jsString(text)};
</script></body></html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

/**
 * Блокирующий экран — нативное окно поверх всего, которое ГЛУШИТ ВВОД
 * (before-input-event отменяется целиком). Это законный инструмент ВНУТРИ
 * экзамена: тест приостановлен, и трогать его нельзя, пока нарушение не
 * устранено.
 *
 * Вне экзамена он — тупик. На экране согласия, предполётной проверке и
 * калибровке студент обязан что-то нажимать: прочитать правила, ввести имя,
 * отключить лишний монитор и повторить проверки, откалиброваться. Оверлей,
 * вставший до экзамена, не оставляет ни одного действия: ни кнопки, ни
 * поля, ни клавиатуры — ровно то, что заказчик описала словами «я там
 * никуда нажимать не могу, ничего делать не могу».
 *
 * Поэтому до экзамена он не ставится. Само НАБЛЮДЕНИЕ при этом не слабеет:
 * событие уходит в журнал тем же вызовом (см. handleDisplays), предполётная
 * проверка валит соответствующий пункт, и кнопка «Начать тест» остаётся
 * заблокированной — путь к экзамену закрыт, а путь к исправлению открыт.
 */
function showBlocking(reason, title, text) {
  if (!isLockedState(state.examState)) {
    log(`блокирующий экран НЕ показан (${reason}): состояние ${state.examState}, `
      + 'экзамен не идёт. Факт остаётся в журнале и в предполётной проверке; '
      + 'оверлей до экзамена отнял бы у студента возможность что-либо нажать');
    return;
  }
  state.blockingReason = reason;
  state.blockingTitle = title;
  state.blockingText = text;
  sendToRenderer('proctor:blocking', { active: true, reason, title, text });

  if (state.overlay && !state.overlay.isDestroyed()) {
    state.overlay.loadURL(blockingHtml(title, text));
    state.overlay.show();
    state.overlay.focus();
    state.overlay.moveTop();
    broadcastShellStatus();
    return;
  }

  // Оверлей не делаем fullscreen: на macOS это создало бы отдельный Space и
  // увело окно из-под kiosk-окна. Вместо этого — точные границы экрана,
  // уровень screen-saver и видимость над чужим fullscreen.
  const primary = screen.getPrimaryDisplay();
  const overlay = new BrowserWindow({
    x: primary.bounds.x,
    y: primary.bounds.y,
    width: primary.bounds.width,
    height: primary.bounds.height,
    frame: false,
    show: false,
    movable: false,
    resizable: false,
    minimizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    backgroundColor: '#0b0f14',
    title: 'Тест приостановлен',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      devTools: false,
      sandbox: true,
    },
  });
  overlay.setAlwaysOnTop(true, 'screen-saver', 2);
  try {
    overlay.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  } catch (err) { /* не все платформы */ }
  try {
    overlay.setContentProtection(state.contentProtection);
  } catch (err) { /* платформа не поддерживает */ }
  overlay.loadURL(blockingHtml(title, text));
  overlay.once('ready-to-show', () => {
    if (!overlay.isDestroyed()) {
      overlay.show();
      overlay.focus();
      overlay.moveTop();
    }
  });
  overlay.webContents.on('before-input-event', (e) => e.preventDefault());
  state.overlay = overlay;
  broadcastShellStatus();
}

function hideBlocking(reason) {
  // снимаем только тот экран, который сами же и поставили
  if (reason && state.blockingReason && state.blockingReason !== reason) return;
  state.blockingReason = null;
  state.blockingTitle = '';
  state.blockingText = '';
  if (state.overlay && !state.overlay.isDestroyed()) {
    state.overlay.destroy();
  }
  state.overlay = null;
  sendToRenderer('proctor:blocking', { active: false, reason: reason || null });
  const win = state.win;
  if (win && !win.isDestroyed()) {
    win.show();
    win.focus();
  }
  broadcastShellStatus();
}

// ---------------------------------------------------------------------------
// Мониторы
// ---------------------------------------------------------------------------

// Зовётся из таймера lockdown и из событий screen. Создание оверлей-окна внутри
// может бросить (например, когда приложение уже уходит в quit), а исключение из
// таймера без обработчика гасит main-процесс — поэтому обёрнуто целиком.
function handleDisplays(info, source) {
  try {
    const count = info && typeof info.count === 'number' ? info.count : screen.getAllDisplays().length;
    const displays = (info && info.displays) || screen.getAllDisplays().map((d) => ({
      id: d.id, internal: Boolean(d.internal), bounds: d.bounds, scale: d.scaleFactor,
    }));
    state.displayCount = count;

    if (count > 1 && CLI.allowMultiDisplay) {
      link.sendShellEvent(EventKind.MULTIPLE_DISPLAYS, { count, displays, source,
        allowed: true });
      hideBlocking('multiple_displays');
      log(`экранов ${count}, но блокировка снята флагом --allow-multi-display`);
    } else if (count > 1) {
      link.sendShellEvent(EventKind.MULTIPLE_DISPLAYS, { count, displays, source });
      showBlocking(
        'multiple_displays',
        'Обнаружено несколько экранов',
        `Подключено экранов: ${count}. Отключите внешние мониторы, проекторы и приставки захвата — тест продолжится автоматически.`,
      );
    } else {
      hideBlocking('multiple_displays');
    }
    broadcastShellStatus();
  } catch (err) {
    log('обработка мониторов не удалась:', err && err.message);
  }
}

/**
 * Привести блокирующий экран в соответствие состоянию оболочки.
 *
 * Нужна, потому что showBlocking() до экзамена отказывает: лишний монитор,
 * подключённый НА СОГЛАСИИ, оверлея не поднимет — и, не будь этой функции,
 * не поднял бы его и на входе в экзамен, где он как раз обязан стоять.
 * Событие в журнал здесь НЕ дублируется: его уже отправил handleDisplays,
 * а повторная запись об одном и том же мониторе засоряла бы доказательную
 * базу. Решение принимается по уже известному числу экранов.
 */
function syncBlockingWithState(reason) {
  if (state.quitting) return;
  const multi = state.displayCount > 1 && !CLI.allowMultiDisplay;
  if (multi && isLockedState(state.examState)) {
    showBlocking(
      'multiple_displays',
      'Обнаружено несколько экранов',
      `Подключено экранов: ${state.displayCount}. Отключите внешние мониторы, `
      + 'проекторы и приставки захвата — тест продолжится автоматически.',
    );
  } else if (!isLockedState(state.examState)) {
    // Вышли из экзамена (в том числе к отчёту) — оверлей снимаем: на экране
    // отчёта нажимать тоже надо.
    hideBlocking('multiple_displays');
  }
  log(`блокирующий экран по состоянию ${state.examState} (${reason || '—'}): `
    + `${state.blockingReason ? `стоит (${state.blockingReason})` : 'снят'}`);
}

// ---------------------------------------------------------------------------
// Подавление комбинаций внутри окна (before-input-event)
// ---------------------------------------------------------------------------

/**
 * Разбор нажатия. Возвращает null, если комбинация разрешена, либо
 * {combo, reason, kinds:[EventKind]} для подавления.
 *
 * Логика: mod = Ctrl (Win/Linux) либо Cmd (macOS) — одна таблица на обе платформы.
 */
function classifyInput(input) {
  if (!input || input.type !== 'keyDown') return null;

  const rawKey = typeof input.key === 'string' ? input.key : '';
  const key = rawKey.toLowerCase();
  const code = typeof input.code === 'string' ? input.code : '';
  const ctrl = Boolean(input.control);
  const meta = Boolean(input.meta);
  const alt = Boolean(input.alt);
  const shift = Boolean(input.shift);
  const mod = ctrl || meta;
  const isDarwin = process.platform === 'darwin';

  const parts = [];
  if (ctrl) parts.push('Ctrl');
  if (meta) parts.push(isDarwin ? 'Cmd' : 'Super');
  if (alt) parts.push('Alt');
  if (shift) parts.push('Shift');
  let name;
  if (code === 'Space' || rawKey === ' ') name = 'Space';
  else if (code === 'Tab' || key === 'tab') name = 'Tab';
  else if (rawKey.length === 1) name = rawKey.toUpperCase();
  else if (rawKey) name = rawKey;
  else name = code || '?';
  const combo = parts.concat([name]).join('+');

  const isFn = (n) => key === n.toLowerCase() || code === n;

  // 1. DevTools
  if (isFn('F12') || (mod && shift && ['i', 'j', 'c'].includes(key))) {
    return { combo, reason: 'devtools', kinds: [EventKind.SHORTCUT_BLOCKED, EventKind.DEVTOOLS_ATTEMPT] };
  }
  // 2. Буфер обмена
  if (mod && !alt && !shift && ['c', 'v', 'x'].includes(key)) {
    const kinds = [EventKind.SHORTCUT_BLOCKED];
    if (key === 'v') kinds.push(EventKind.CLIPBOARD_PASTE);
    return { combo, reason: 'clipboard', kinds };
  }
  // 3. Печать / сохранение / выделить всё / перезагрузка
  if (mod && !alt && (key === 'r' || (!shift && ['p', 's', 'a'].includes(key)))) {
    return { combo, reason: 'page_action', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  // 4. Новое окно / вкладка / закрытие
  if (mod && !alt && !shift && ['n', 't', 'w'].includes(key)) {
    return { combo, reason: 'new_window', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  // 5. Выход из приложения
  if (alt && isFn('F4')) {
    return { combo, reason: 'app_quit', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  if (meta && key === 'q') {
    return { combo, reason: 'app_quit', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  // 6. Свернуть / скрыть / системный поиск (macOS)
  if (meta && ['m', 'h'].includes(key)) {
    return { combo, reason: 'minimize_hide', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  if (meta && name === 'Space') {
    return { combo, reason: 'os_launcher', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  // 7. Полный экран
  if (isFn('F11') || (meta && ctrl && key === 'f')) {
    return { combo, reason: 'fullscreen_toggle', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  // 8. Перебор вкладок
  if (mod && name === 'Tab') {
    return { combo, reason: 'tab_cycle', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  // 9. Снимок экрана на macOS (Cmd+Shift+3/4/5)
  if (meta && shift && ['3', '4', '5'].includes(key)) {
    return { combo, reason: 'screenshot_key', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  return null;
}

function reportBlockedCombo(verdict, source) {
  const { combo, reason, kinds } = verdict;
  for (const kind of kinds) {
    const allow = kind === EventKind.SHORTCUT_BLOCKED ? allowCombo : allowDerived;
    if (!allow(`${kind}:${combo}`)) continue;
    link.sendShellEvent(kind, { combo, reason, source });
  }
}

// ===========================================================================
// ПРОФИЛЬ ЭКЗАМЕНА: страница LMS и фильтр на уровне сетевых запросов
// ===========================================================================
/*
 * Что здесь происходит и почему именно так.
 *
 * До этого оболочка грузила только локальный мок-тест через loadFile, и
 * открыть настоящий Moodle или Платонус было нельзя вообще. Профиль экзамена
 * задаёт две вещи: какой адрес открывать и какие источники разрешены.
 *
 * ПРОФИЛЬ — УЛИКА, А НЕ ЗАЩИТА. Фильтр живёт в процессе, который запускает
 * сам студент, и обойти его подменой сборки можно. Нигде не утверждается
 * обратное. Ценность в том, что каждая попытка выхода за белый список
 * попадает в журнал ПОЛНЫМ адресом, а хеш действовавшего профиля уходит в
 * подписанную цепочку ВСЕГДА — без флага «проверять». Ровно та ошибка, на
 * которой развалился Safe Exam Browser: подпись конфигурации была, а сверка
 * Config Key в Moodle включалась отдельной галочкой, выключенной по
 * умолчанию. Защиту сделали опциональной — и её не стало. У нас проверки,
 * которую можно забыть включить, нет.
 *
 * ИЗОЛЯЦИЯ. Страница экзамена — СТОРОННЕЕ содержимое внутри прокторинга.
 * Она живёт в отдельном BrowserView со своим разделом сессии, без нашего
 * preload, с contextIsolation и без nodeIntegration: ни сам LMS, ни то, что
 * в него внедрено, не должно видеть window.proctor. Иначе чужой скрипт мог
 * бы звать наш API — например открывать и закрывать сессию наблюдения.
 *
 * ФИЛЬТР НА ЗАПРОСАХ, А НЕ НА НАВИГАЦИИ. will-navigate — лишь один канал;
 * iframe, fetch, XHR, WebSocket, картинки и скрипты идут мимо него. Поэтому
 * решение принимает webRequest.onBeforeRequest в сессии страницы экзамена, а
 * will-navigate и обработчик открытия окон используют ТУ ЖЕ чистую функцию
 * из shell/state.js — чтобы два канала не могли разойтись в правилах.
 *
 * ЧЕГО `onBeforeRequest` НЕ ВИДИТ — назвать обязательно, иначе этот
 * комментарий сам становится ложной гарантией.
 *
 *  - WebRTC: ICE/STUN/TURN идут UDP-сокетами, события запроса не возникает, а
 *    data-канал разрешения не требует (`setPermissionRequestHandler` здесь не
 *    помогает). Поэтому на страницу экзамена отдельно ставится
 *    `setWebRTCIPHandlingPolicy('disable_non_proxied_udp')` — см.
 *    hardenExamContents().
 *  - preconnect и dns-prefetch: это подсказки сетевому стеку, а не запросы;
 *    фильтр их не видит и видеть не может.
 *
 * Ни один из этих каналов не даёт дороги к `window.proctor` и к ядру, и
 * сигнализация WebRTC всё равно пошла бы через разрешённый origin. Но это
 * исходящие каналы, не покрытые фильтром, и в журнале их обращений нет.
 *
 * НАШ ИНТЕРФЕЙС ФИЛЬТР НЕ ТРОГАЕТ. Он стоит только на разделе EXAM_PARTITION;
 * локальный renderer и канал ws://127.0.0.1 живут в defaultSession и о
 * фильтре не знают.
 */

/** sha256 в hex с префиксом, как его читает отчёт. */
function sha256Label(text) {
  try {
    const hex = crypto.createHash('sha256').update(String(text), 'utf8').digest('hex');
    return `sha256:${hex}`;
  } catch (err) {
    log('не удалось посчитать хеш профиля:', err && err.message);
    return '';
  }
}

/** Короткая форма хеша для шапки и HUD: полный хеш там не читается. */
function shortHash(label) {
  const s = String(label || '');
  const idx = s.indexOf(':');
  if (idx === -1) return s.slice(0, 12);
  return `${s.slice(0, idx + 1)}${s.slice(idx + 1, idx + 13)}`;
}

/**
 * Флаги правил, поданные оболочке. Имена — для отказа и для журнала.
 *
 * Применить их нельзя (почему — у CLI.examProfilePath), но и промолчать
 * нельзя: человек у машины обязан увидеть, что заданные им правила НЕ
 * действуют, прежде чем начнёт экзамен.
 */
function refusedProfileFlags() {
  const named = [];
  if (CLI.examUrl) named.push('--exam-url');
  if (CLI.examOrigins.length) named.push('--exam-origin');
  if (CLI.examPreset) named.push('--exam-preset');
  if (CLI.allowSearch) named.push('--allow-search');
  return named;
}

/**
 * Отказ по флагам правил: печать в консоль и запись факта.
 *
 * Сам факт уходит в цепочку двумя путями, и оба не зависят от этой функции:
 * `argv_flags` в SHELL_CONFIG перечисляет флаги запуска дословно, а
 * `exam_profile_flags_refused` называет случай словом. Если оболочку
 * пересобрали так, что отказа нет, в цепочке всё равно останется argv.
 */
function warnRefusedProfileFlags() {
  const named = refusedProfileFlags();
  if (!named.length) return named;
  log('='.repeat(72));
  log(`ФЛАГИ ПРАВИЛ ЭКЗАМЕНА ОТКЛОНЕНЫ: ${named.join(', ')}`);
  log('Оболочка правил экзамена НЕ задаёт. Профиль, которого нет у ядра, не');
  log('попадает в подписанную цепочку: HUD показывал бы «действовал профиль»,');
  log('а отчёт — «правила экзамена проктором не задавались», и каждая попытка');
  log('выхода за белый список молча отбрасывалась бы ядром как расхождение.');
  log('');
  log('Рабочий путь один — файл профиля, который читает ЯДРО:');
  log('  1) заготовка:  python sidecar/main.py --exam-profile-example ksu > exam-profile.json');
  log('  2) правка руками: адрес экзамена и КОНКРЕТНЫЕ источники (не класс .edu.kz)');
  log('  3) запуск:     bash scripts/run-dev.sh --exam-profile exam-profile.json');
  log('Поиск на экзаменах, где он разрешён правилами: --allow-search — сайдкару.');
  log('='.repeat(72));
  return named;
}

/**
 * Профиль, действующий прямо сейчас. Пока ни ядро, ни флаги ничего не
 * задали — это пустой профиль, и фильтр работает в режиме «только локальный
 * интерфейс»: страница экзамена не создаётся, внешние запросы запрещены все.
 */
function currentExamProfile() {
  if (!state.examProfile) {
    state.examProfile = normalizeExamProfile(null, {
      source: 'none', controlPort: String(CLI.wsPort),
    });
    state.examProfileHash = sha256Label(state.examProfile.canonical);
    state.examProfileSource = 'none';
  }
  return state.examProfile;
}

/**
 * Применить профиль.
 *
 * Источник ядра сильнее флагов: файл профиля читает сайдкар, и именно его
 * хеш ядро кладёт в цепочку. Профиль из флагов работает, пока ядро своего не
 * прислало, и замена фиксируется отдельной записью SHELL_CONFIG — смена
 * правил посреди сессии это изменение условий наблюдения, а не деталь.
 */
function applyExamProfile(raw, source) {
  // Ядро шлёт status ~2 Гц, и профиль может приезжать в каждом сообщении.
  // Разбирать и хешировать одно и то же дважды в секунду не нужно.
  let signature = '';
  try { signature = `${source}|${JSON.stringify(raw)}`; } catch (err) { signature = ''; }
  if (signature && signature === state.examProfileRawSignature) return state.examProfile;
  if (signature) state.examProfileRawSignature = signature;

  /*
   * СВОДКА — НЕ ПРАВИЛА.
   *
   * Ядро кладёт профиль в два разных сообщения, и они разной природы.
   * В `hello` (и в control-записи) лежит ПЕРЕЧЕНЬ: `allowed_origins`,
   * `wildcard_origins`. В `status`, который приходит ~2 Гц, лежит СВОДКА по
   * тому же профилю, и там поле `origins` — это КОЛИЧЕСТВО источников
   * (`"origins": 1`), а перечня нет вовсе.
   *
   * Что из этого выходило на живом запуске 08.10: сводка проходила проверку
   * «профиль не пуст» (адрес-то в ней есть), подменяла собой разобранный
   * перечень — и белый список проктора молча схлопывался до одного origin,
   * выведенного из адреса экзамена, плюс мусорная запись «1» из счётчика.
   * То есть каждые полсекунды действующие правила подменялись их огрызком,
   * а хеш применённых правил скакал между двумя значениями, и в отчёте было
   * не понять, что именно действовало.
   *
   * Поэтому: сообщение без перечня не заменяет перечень, который уже есть.
   * Направление отказа то же, что у пустого профиля ниже, — сохраняем то, что
   * прокторa и объявил.
   */
  if (!examProfileCarriesRules(raw) && state.examProfile && state.examProfile.applied) {
    return state.examProfile;
  }

  const next = normalizeExamProfile(raw, {
    source,
    // Порт канала к ядру — чтобы ни запись белого списка, ни адрес экзамена
    // не могли направить страницу экзамена на наш собственный сокет.
    controlPort: String(CLI.wsPort),
  });

  /*
   * Пустым профилем действующие правила не затираются.
   *
   * Сообщение, в котором правил нет, не то же самое, что решение проктора
   * правил не ставить: ядро могло перезапуститься, не дочитав файл. Молча
   * обменять работающий белый список на «разрешено только локальное» значило
   * бы посреди экзамена закрыть студенту его же LMS, а обратная подстановка
   * открыла бы мок-тест вместо настоящего теста. Поэтому понижение
   * отклоняется, а факт остаётся в журнале оболочки.
   */
  if (!next.applied && state.examProfile && state.examProfile.applied) {
    log(`профиль экзамена от «${source}» пуст — действующие правила сохранены `
      + `(${shortHash(state.examProfileHash)}); пустое сообщение не отменяет правил`);
    return state.examProfile;
  }
  const hash = sha256Label(next.canonical);
  const prev = state.examProfile;
  const changed = !prev || prev.canonical !== next.canonical;

  state.examProfile = next;
  state.examProfileHash = hash;
  state.examProfileSource = next.applied ? source : (next.rejected.length ? source : 'none');

  if (!changed) return next;

  if (!next.applied) {
    log(`профиль экзамена: правила не заданы (источник ${source}) — `
      + 'страница экзамена остаётся локальным мок-тестом');
  } else {
    log(`профиль экзамена применён (источник ${source}): ${shortHash(hash)}; `
      + `адрес ${next.examUrl || 'не задан'}; источников ${next.origins.length}; `
      + `поиск ${next.allowSearch ? 'РАЗРЕШЁН правилами экзамена' : 'запрещён'}`
      + `${next.preset ? `; пресет ${next.preset}` : ''}`);
    for (const entry of next.origins) {
      log(`  разрешено: ${entry.origin}${entry.subdomains ? ' и поддомены' : ''}`
        + ` (${entry.from})${entry.insecure ? ' — ВНИМАНИЕ: http, пароль уйдёт открытым' : ''}`);
    }
    if (next.broad) {
      log('  ВНИМАНИЕ: разрешён класс доменов, а не конкретные источники. '
        + 'Под него попадают форумы, библиотеки и файлообменники вуза. '
        + 'Факт отмечен в записи профиля.');
    }
  }
  for (const bad of next.rejected) {
    log(`  НЕ ПРИНЯТО «${bad.raw}» (${bad.field}): ${originRejectText(bad.reason)}`);
  }

  /*
   * ПРИХОД ПРОФИЛЯ НИЧЕГО НЕ ПОКАЗЫВАЕТ.
   *
   * Профиль приезжает в `hello`, то есть сразу при старте, когда на экране
   * согласие. Раньше здесь стоял собственный вызов attachExamView(), и это
   * было ВТОРОЕ место, где решалось, показывать ли чужую страницу: решение
   * «профиль пришёл» обходило проверку состояния. Теперь приход профиля лишь
   * ГОТОВИТ представление (создаёт раздел сессии и ставит фильтр, не
   * добавляя ничего в окно), а показать или снять решает одна функция —
   * syncExamViewWithState() по состоянию экзамена.
   */
  if (next.examUrl) prepareExamView(`profile_${source}`);
  if (state.examViewAttached && next.examUrl) {
    // Экзамен идёт, адрес сменился — переоткрываем по новому.
    loadExamUrl('profile_changed');
  }
  // Адрес из профиля пропал — представление снимет эта же функция: решение
  // «быть на экране» учитывает и состояние, и наличие адреса.
  syncExamViewWithState('profile_changed');

  // Условия наблюдения изменились: ядро обязано узнать об этом записью, а не
  // по косвенным признакам. Если сессии ещё нет, запись уйдёт в openSession().
  if (state.sessionStarted) {
    link.sendShellConfig(shellConfigRecord(`профиль экзамена (${source})`));
  }
  broadcastProtection();
  broadcastShellStatus();
  return next;
}

/**
 * Снимок профиля для HUD, SHELL_CONFIG и служебного экрана.
 *
 * ДВА ХЕША, И ЭТО НЕ ДУБЛЬ. `hash` — хеш ЯДРА по файлу профиля: он
 * авторитетный, стоит в шапке отчёта и в цепочке, и отвечает на вопрос «какие
 * правила объявлены». `appliedHash` — наш собственный хеш по канонической
 * форме того, что оболочка ФАКТИЧЕСКИ применяет к запросам, и отвечает на
 * другой вопрос — «какие правила действуют». Сравнивать их между собой
 * нельзя: они считаются по разным данным и разным канонам, и «не совпали» тут
 * означало бы не подмену, а разные вопросы. Поэтому оба лежат рядом, а вывод
 * по ним делает проверяющий.
 */
function examProfileInfo() {
  const p = currentExamProfile();
  const coreHash = p.coreHash || '';
  return {
    applied: Boolean(p.applied),
    source: state.examProfileSource,
    label: p.label,
    // Авторитетный хеш — от ядра; своего подставляем только когда профиль
    // пришёл из флагов и ядро файла не читало.
    hash: coreHash || state.examProfileHash,
    hashShort: coreHash ? (p.coreHashShort || shortHash(coreHash))
      : shortHash(state.examProfileHash),
    coreHash,
    coreSource: p.coreSource,
    coreSigned: Boolean(p.coreSigned),
    coreSignatureState: p.coreSignatureState,
    institution: p.institution,
    examId: p.examId,
    coreWarnings: p.warnings,
    // Хеш того, что применяет САМА оболочка.
    appliedHash: state.examProfileHash,
    appliedHashShort: shortHash(state.examProfileHash),
    examUrl: p.examUrl,
    examOrigin: p.examOrigin,
    origins: p.origins.map((e) => `${e.origin}${e.subdomains ? '/*' : ''}`),
    originsCount: p.origins.length,
    preset: p.preset,
    presetLabel: p.presetLabel,
    broad: Boolean(p.broad),
    insecureOrigins: p.insecure,
    localOrigins: p.local,
    controlPort: p.controlPort,
    allowSearch: Boolean(p.allowSearch),
    allowSearchBy: p.allowSearchBy,
    rejected: p.rejected.map((r) => ({
      raw: r.raw, field: r.field, reason: r.reason, text: originRejectText(r.reason),
    })),
    // Запрет, который не снимается ничем: канал оболочки к ядру.
    controlChannelDenied: true,
    headline: examProfileHeadline(p, shortHash(state.examProfileHash)),
    /*
     * Встал ли фильтр. Трёхзначное, и это не придирка: профиль с белым списком,
     * но БЕЗ адреса экзамена — рабочее состояние ядра (`present: true`), при
     * котором `attachExamView` не создаёт view, `examSession()` не зовётся и
     * `onBeforeRequest` не ставится вовсе. Студент остаётся в локальном
     * мок-тесте, фильтра нет ни одного, а HUD без этого поля печатал бы
     * «действовал профиль <хеш>».
     */
    filterInstalled: state.examFilterInstalled,
    // Правила есть, а открывать по ним нечего: в профиле нет exam_url.
    rulesWithoutUrl: Boolean(p.applied && !p.examUrl),
    viewAttached: Boolean(state.examViewAttached),
    // Почему представление в этом положении — та же строка, что уходит в
    // SHELL_CONFIG, чтобы экран и документ не расходились.
    viewReason: String(state.examViewReason || ''),
    viewInsetSource: state.examViewInsetSource,
    viewInsetReason: state.examViewInsetReason,
    lastUrl: state.examLastUrl,
    loadError: state.examLoadError,
    blocked: Object.assign({}, state.blocked),
    // Единственная честная формулировка. Обойти фильтр можно; нельзя обойти
    // его, не оставив записи, — и именно это мы обещаем.
    note: 'профиль экзамена — улика, а не защита: он не делает обход невозможным, '
      + 'он делает каждую попытку записанной',
  };
}

// ------------------------------------------------- фильтр на сетевых запросах

/**
 * Записать блокировку как факт для ядра.
 *
 * Полный адрес — обязательно: ценность не в запрете, а в доказательстве
 * попытки. Класс ресурса отделяет осознанное действие студента от обращения
 * самой страницы, и это разные виды событий с разным весом.
 */
function reportBlockedRequest(verdict, channel, rawUrl) {
  const cls = verdict.cls;
  const deliberate = Boolean(verdict.deliberate);
  const url = String(rawUrl || '').slice(0, 1000);

  // Навигация — по полному адресу, подзапросы — по origin и классу.
  const admit = deliberate
    ? allowBlockedNavigation(`nav:${url}`)
    : allowBlockedSubresource(`${cls}:${verdict.origin || url}`);

  if (cls === 'navigation') state.blocked.navigation += 1;
  else if (cls === 'iframe') state.blocked.iframe += 1;
  else state.blocked.subresource += 1;

  // Счётчики выше считают ВСЕ блокировки, в том числе склеенные
  // дедупликацией: в отчёте должен стоять масштаб, а не число записей.
  if (!admit.ok) {
    state.blocked.suppressed += 1;
    return false;
  }

  /*
   * Каким видом уходит факт.
   *
   * Отказы `profile` и `absolute` уходят ОДНИМИ И ТЕМИ ЖЕ видами —
   * NAVIGATION_OFF_PROFILE / SUBRESOURCE_OFF_PROFILE. Разница между ними не в
   * виде события, а в том, чем ядро убеждается в отказе: по `profile` оно
   * перепроверяет хост по своей копии белого списка, а `absolute` проверяет
   * само по форме адреса (нет сетевого хоста — ни одна запись белого списка
   * его разрешить не может; петля на порт самого ядра — ядро знает свой порт).
   *
   * Раньше `absolute` уходил видом SHORTCUT_BLOCKED, потому что ядро на
   * адресе без хоста отвечало `host_allowed("") == True` и записывало
   * расхождение вместо инцидента. Побочный эффект был хуже причины:
   * `file:///Users/stud/shpora.html` — самая вероятная настоящая шпаргалка —
   * лежал в журнале под подписью «Заблокированное сочетание клавиш», в канале
   * `shell_keys`, весил 5 вместо 20 и НАМЕРЕННО исключался из подраздела
   * попыток в отчёте. Адрес сохранялся, но под чужим именем и не там, где его
   * будут искать. Исправлено в ядре (`_cmd_off_profile`), а не обходом здесь.
   *
   * `hard` остаётся для адреса, который не разобрался вообще: сослаться не на
   * что, и запись остаётся технической.
   */
  const denyClass = examDenyClass(verdict.reason);
  let kind;
  if (denyClass === 'profile' || denyClass === 'absolute') {
    kind = deliberate ? EventKind.NAVIGATION_OFF_PROFILE : EventKind.SUBRESOURCE_OFF_PROFILE;
  } else if (denyClass === 'none' && !deliberate) {
    // Правил нет, нарушить их нельзя: обвинение по несуществующим правилам
    // хуже молчания. Остаётся запись в логе оболочки — и этого достаточно.
    log(`подзапрос отклонён без профиля (не инцидент): ${url}`);
    return false;
  } else {
    kind = EventKind.SHORTCUT_BLOCKED;
  }
  const profile = currentExamProfile();
  link.sendShellEvent(kind, {
    url,
    // Для вида SHORTCUT_BLOCKED ядро читает `combo` — заполняем осмысленно,
    // чтобы в отчёте стояла не пустая строка. Остаётся только для `hard`.
    combo: denyClass === 'hard' ? (deliberate ? 'NAVIGATION' : 'SUBRESOURCE')
      : undefined,
    deny_class: denyClass,
    origin: verdict.origin,
    host: verdict.host,
    scheme: verdict.scheme,
    resource_class: cls,
    resource_type: verdict.resourceType || '',
    deliberate,
    reason: verdict.reason,
    reason_text: examDenyText(verdict.reason),
    note: verdict.note || '',
    matched_rule: verdict.matched || '',
    channel,
    // Сколько таких же было подавлено дедупликацией с прошлой записи: иначе
    // «десяток картинок» превратился бы в одну и потерял масштаб.
    repeats: admit.suppressed,
    profile_applied: Boolean(profile.applied),
    profile_hash: state.examProfileHash,
    source: 'exam_view',
  });

  log(`${deliberate ? 'ПОПЫТКА ПЕРЕХОДА' : 'подзапрос'} заблокирован (${verdict.reason}) `
    + `[${cls}/${channel}]: ${url}${admit.suppressed ? ` (+${admit.suppressed} повторов)` : ''}`);

  // Осознанную попытку показываем человеку: иначе клик по ссылке выглядит как
  // сломанный LMS. Подзапросы не показываем — это шум чужой вёрстки.
  if (deliberate) {
    sendToRenderer('proctor:error', {
      code: 'exam_source_blocked',
      message: `Источник не входит в правила экзамена (${examDenyText(verdict.reason)}): `
        + `${url}. Попытка записана в журнал сессии.`,
      fatal: false,
    });
  }
  return true;
}

/** Решение по одному запросу — общая точка для всех каналов. */
function decideExamRequest(url, resourceType, channel) {
  const verdict = examRequestVerdict(currentExamProfile(), url, resourceType);
  verdict.resourceType = resourceType || '';
  if (verdict.allow) return verdict;
  try {
    reportBlockedRequest(verdict, channel, url);
  } catch (err) {
    log('запись блокировки не удалась:', err && err.message);
  }
  return verdict;
}

/**
 * Сессия страницы экзамена со всеми запретами. Создаётся лениво и
 * обустраивается один раз.
 */
function examSession() {
  let ses;
  try {
    ses = session.fromPartition(EXAM_PARTITION);
  } catch (err) {
    log('раздел сессии экзамена недоступен:', err && err.message);
    return null;
  }
  if (state.examSessionGuarded) return ses;
  state.examSessionGuarded = true;

  // --- фильтр запросов ------------------------------------------------------
  // Без фильтра по схемам: слушатель должен видеть ВСЕ запросы, включая
  // file:// и ws://. Фильтр по шаблонам оставил бы ровно те каналы, через
  // которые и уходят — file: и WebSocket.
  try {
    ses.webRequest.onBeforeRequest((details, callback) => {
      let cancel = false;
      try {
        const verdict = decideExamRequest(details.url, details.resourceType, 'webRequest');
        cancel = !verdict.allow;
      } catch (err) {
        // Исключение в фильтре не должно открыть дорогу: отказ по умолчанию.
        log('фильтр запросов упал, запрос отклонён:', err && err.message);
        cancel = true;
      }
      callback({ cancel });
    });
    state.examFilterInstalled = true;
    log(`фильтр источников установлен на раздел сессии «${EXAM_PARTITION}»`);
  } catch (err) {
    /*
     * ФИЛЬТР НЕ ВСТАЛ — СТРАНИЦУ НЕ ОТКРЫВАЕМ ВООБЩЕ.
     *
     * Раньше здесь печаталась строка в локальный лог и возвращался раздел
     * сессии: `attachExamView` продолжал, создавал view и грузил LMS. Без
     * `onBeforeRequest` оставались только `will-navigate`/`will-redirect` и
     * обработчик открытия окон, то есть один main frame; iframe, fetch, XHR,
     * WebSocket, скрипты и картинки не фильтровались и НЕ ЗАПИСЫВАЛИСЬ. При
     * этом в цепочке стоял профиль ядра с хешем, и шапка отчёта печатала
     * «действовал профиль <хеш>» — документ утверждал правила, которых не
     * применял никто. Вторая попытки не было бы за всю сессию: `examSession()`
     * зовётся один раз, только при `state.examView === null`.
     *
     * Правильная реакция уже написана двадцатью строками ниже, для случая
     * «раздел сессии недоступен»: чужой сайт без фильтра не грузим. Здесь то
     * же самое, и факт уходит наружу записью, а не остаётся в stdout.
     */
    state.examFilterInstalled = false;
    log('ФИЛЬТР ЗАПРОСОВ НЕ УСТАНОВЛЕН:', err && err.message);
    log('страница экзамена НЕ будет открыта: грузить чужой сайт без фильтра '
      + 'нельзя — обращения наружу не попали бы ни в журнал, ни в цепочку, '
      + 'а отчёт утверждал бы, что правила действовали');
    state.examSessionGuarded = false;
    return null;
  }

  // --- разрешения: странице экзамена не положено ничего ---------------------
  // В defaultSession камера и микрофон разрешены — их просит НАШ renderer для
  // превью и калибровки. Стороннему LMS они не нужны ни для чего, а запрос
  // камеры со страницы экзамена это попытка получить кадр в обход нашего
  // захвата. Поэтому здесь запрещено всё, без исключений.
  try {
    ses.setPermissionRequestHandler((wc, permission, callback) => {
      log(`страница экзамена просила «${permission}» — отказано`);
      callback(false);
    });
    ses.setPermissionCheckHandler(() => false);
  } catch (err) {
    log('разрешения раздела экзамена:', err && err.message);
  }
  try {
    ses.setDisplayMediaRequestHandler((request, callback) => callback({}));
  } catch (err) { /* не во всех сборках */ }

  /*
   * --- загрузки: канал выхода из оболочки ----------------------------------
   *
   * Скачанный файл открывается чужой программой ВНЕ окна экзамена, поэтому
   * загрузки отменяются все. Но ОБВИНЕНИЕ зависит от источника, и раньше не
   * зависело: обработчик звал reportBlockedRequest с жёстко вписанным
   * `reason: 'not_in_whitelist'` и `deliberate: true`. На штатном вложении в
   * Moodle с разрешённого moodle.ksu.edu.kz получалось худшее из возможного:
   * файл не скачался, студенту показано «Источник не входит в правила
   * экзамена», а ядро, перепроверив хост и найдя его РАЗРЕШЁННЫМ, выбрасывало
   * событие как `disputed` — в отчёте не оставалось ничего, зато скрытый
   * счётчик «оболочку поправили» рос от нормальной работы LMS.
   *
   * Теперь источник решает: выход за белый список — это выход за белый список,
   * а отменённое скачивание с разрешённого источника — свой отдельный факт.
   * Он не инцидент (студент не нарушал правил) и уходит в цепочку счётчиком в
   * SHELL_CONFIG, а человеку у машины говорится правду: дело не в источнике.
   */
  try {
    ses.on('will-download', (event, item) => {
      let url = '';
      try { url = item.getURL(); } catch (err) { url = ''; }
      event.preventDefault();

      const verdict = examRequestVerdict(currentExamProfile(), url, 'mainFrame');
      if (verdict.allow) {
        state.examDownloadsCancelled += 1;
        log(`скачивание отменено (источник РАЗРЕШЁН, не выход за профиль): ${url}`);
        sendToRenderer('proctor:error', {
          code: 'exam_download_blocked',
          message: 'Скачивание файлов во время экзамена отключено: файл открылся бы '
            + 'вне окна наблюдения. Источник правилам экзамена не противоречит — '
            + 'это не нарушение. Откройте материал в самой странице теста.',
          fatal: false,
        });
        return;
      }
      verdict.resourceType = 'download';
      verdict.note = verdict.note
        || 'скачивание файла со страницы экзамена: файл открылся бы вне окна';
      reportBlockedRequest(verdict, 'will-download', url);
    });
  } catch (err) { /* события может не быть */ }

  return ses;
}

/**
 * Ужесточение webContents СТРАНИЦЫ ЭКЗАМЕНА.
 *
 * Отдельно от hardenWebContents(): там will-navigate запрещает навигацию
 * ЦЕЛИКОМ, что для нашего локального интерфейса правильно (он одностраничный),
 * а настоящий LMS таким обработчиком был бы сломан на первом же входе в
 * систему. Здесь решение принимает тот же examRequestVerdict().
 */
function hardenExamContents(wc) {
  if (!wc || wc.isDestroyed()) return;
  if (hardenedContents.has(wc)) return;
  hardenedContents.add(wc);

  wc.on('context-menu', (e) => e.preventDefault());

  /*
   * WebRTC — единственный исходящий канал, которого фильтр запросов не видит
   * по устройству: ICE/STUN/TURN идут UDP-сокетами, события `onBeforeRequest`
   * не возникает, а data-канал не требует разрешения, так что и
   * `setPermissionRequestHandler` его не закрывает. Политика
   * `disable_non_proxied_udp` оставляет WebRTC только проксированный путь, а
   * прокси у раздела экзамена нет, — то есть UDP наружу не идёт.
   *
   * Новой улики этим НЕ появляется: канал закрывается, но обращения по нему
   * не видно и записать их нечем. Так и сказано в docs/LIMITATIONS.md.
   */
  try {
    wc.setWebRTCIPHandlingPolicy('disable_non_proxied_udp');
  } catch (err) {
    log('политика WebRTC для страницы экзамена не применена:', err && err.message);
  }

  // Новые окна странице экзамена не положены: окно вне нашего BrowserView
  // выпало бы и из фильтра, и из kiosk-окна. Переход в этом же окне студент
  // всегда может сделать сам — он и проверяется фильтром.
  wc.setWindowOpenHandler(({ url }) => {
    decideExamRequest(url, 'mainFrame', 'window-open');
    return { action: 'deny' };
  });

  wc.on('will-navigate', (e, url) => {
    const verdict = decideExamRequest(url, 'mainFrame', 'will-navigate');
    if (!verdict.allow) e.preventDefault();
  });

  // Редирект — тот же переход, только решение принял сервер. Разрешённый
  // источник, редиректящий на запрещённый, обязан быть остановлен здесь.
  wc.on('will-redirect', (e, url) => {
    const verdict = decideExamRequest(url, 'mainFrame', 'will-redirect');
    if (!verdict.allow) e.preventDefault();
  });

  wc.on('will-attach-webview', (e) => e.preventDefault());

  // Сочетания внутри страницы экзамена подавляются по той же таблице, что и
  // в нашем интерфейсе: буфер обмена, печать, devtools, новые окна.
  wc.on('before-input-event', (e, input) => {
    const verdict = classifyInput(input);
    if (!verdict) return;
    if (!isLockedState(state.examState) && verdict.reason !== 'devtools') return;
    e.preventDefault();
    reportBlockedCombo(verdict, 'exam-view');
  });

  wc.on('devtools-opened', () => {
    try { wc.closeDevTools(); } catch (err) { /* уже закрыты */ }
    if (allowDerived('devtools-opened-exam')) {
      link.sendShellEvent(EventKind.DEVTOOLS_ATTEMPT, {
        reason: 'devtools_opened', source: 'exam_view',
      });
    }
  });

  wc.on('did-navigate', (e, url) => {
    state.examLastUrl = String(url || '').slice(0, 1000);
    state.examLoadError = '';
    broadcastShellStatus();
  });

  wc.on('did-fail-load', (e, code, description, url, isMainFrame) => {
    if (!isMainFrame) return;
    // -3 (ERR_ABORTED) — наш же отказ фильтра, он уже записан отдельно.
    if (code === -3) return;
    state.examLoadError = `${description || 'ошибка загрузки'} (${code})`;
    log(`страница экзамена не загрузилась: ${url} — ${state.examLoadError}`);
    sendToRenderer('proctor:error', {
      code: 'exam_page_failed',
      message: `Страница экзамена не открылась: ${state.examLoadError}. `
        + 'Проверьте адрес в профиле и связь с LMS.',
      fatal: false,
    });
    broadcastShellStatus();
  });

  wc.on('render-process-gone', (e, details) => {
    log('страница экзамена упала:', details && details.reason);
  });
}

// ------------------------------------------------------- BrowserView с LMS

/**
 * Принять отчёт renderer о его фактической раскладке.
 *
 * Отчёт — это ответ на вопрос «какой прямоугольник наша вёрстка оставила
 * свободным», и единственный, кто знает ответ, — сама вёрстка: раскладка
 * меняется по медиа-запросу, а не по нашим константам. Проверку присланного
 * делает shell/state.js, здесь только применение и журнал.
 */
function applyExamViewInset(raw) {
  const win = state.win;
  const size = win && !win.isDestroyed()
    ? win.getContentBounds() : { width: 0, height: 0 };
  const verdict = normalizeExamViewInset(raw, size);
  const prev = state.examViewInset;
  state.examViewInset = verdict.inset;
  /*
   * Источник отступа — тот, чей прямоугольник ФАКТИЧЕСКИ применён, а не
   * «прошла ли проверка». При `window_too_small` проверка не прошла, но
   * применяется всё равно отчёт renderer: окно просто меньше, чем сумма
   * нашего интерфейса, и подменять отчёт константой тут было бы хуже —
   * константа оставила бы страницу экзамена ПОВЕРХ съехавшего HUD.
   */
  const usedRenderer = verdict.ok || verdict.reason === 'window_too_small';
  state.examViewInsetSource = usedRenderer ? 'renderer' : 'fallback';
  state.examViewInsetReason = verdict.reason;
  if (!verdict.ok && verdict.reason !== 'window_too_small') {
    // Отчёт отвергнут: берём запасной отступ и говорим об этом вслух.
    // Молчать нельзя — отвергнутый отчёт означает, что место под HUD
    // посчитано не по вёрстке, и он может не совпасть с панелью на экране.
    log(`отчёт о раскладке отвергнут (${verdict.reason}): `
      + 'место под страницу экзамена считаем запасным отступом '
      + `${JSON.stringify(EXAM_VIEW_INSET_FALLBACK)}`);
  }
  const changed = !prev || prev.top !== verdict.inset.top
    || prev.right !== verdict.inset.right
    || prev.bottom !== verdict.inset.bottom
    || prev.left !== verdict.inset.left;
  if (changed) layoutExamView();
  return verdict;
}

/** Границы страницы экзамена внутри окна. */
function examViewBounds() {
  const win = state.win;
  if (!win || win.isDestroyed()) return { x: 0, y: 0, width: 0, height: 0 };
  const size = win.getContentBounds();
  // Служебный экран оболочки (renderer не установлен) рамки наблюдения не
  // рисует — там отводить место под неё не нужно.
  if (!HAS_RENDERER) {
    return { x: 0, y: 0, width: size.width, height: size.height };
  }
  // Пока renderer не доложил раскладку — запасной отступ. Он уже и так
  // резервирует место; ошибка в нём безопасна в одну сторону (лишняя пустота),
  // а не в другую (накрытый HUD).
  const inset = state.examViewInset || EXAM_VIEW_INSET_FALLBACK;
  return examViewRect(inset, size);
}

function layoutExamView() {
  const view = state.examView;
  if (!view || !state.examViewAttached) return;
  try { view.setBounds(examViewBounds()); } catch (err) { /* окно уходит */ }
}

function loadExamUrl(reason) {
  const view = state.examView;
  const profile = currentExamProfile();
  if (!view || !profile.examUrl) return false;
  log(`страница экзамена: открываем ${profile.examUrl} (${reason})`);
  state.examLoadError = '';
  try {
    view.webContents.loadURL(profile.examUrl);
    return true;
  } catch (err) {
    state.examLoadError = String((err && err.message) || 'loadURL не удался');
    log('открыть страницу экзамена не удалось:', state.examLoadError);
    return false;
  }
}

/**
 * ПОДГОТОВИТЬ страницу экзамена, НЕ показывая её.
 *
 * Отдельно от attachExamView() намеренно. Профиль приезжает в `hello`, то есть
 * сразу при старте, когда на экране согласие, — и «профиль пришёл» не должно
 * означать «покажи чужой сайт». Здесь создаётся BrowserView и ставится фильтр
 * запросов, но в окно он НЕ добавляется: ни пикселя на экране, ни одного
 * обращения в сеть (адрес грузится только при прикреплении).
 *
 * Зачем вообще готовить заранее: создание раздела сессии и установка фильтра —
 * единственное, что может не получиться, и узнать об этом надо до экзамена, а
 * не в момент, когда студент уже нажал «К экзамену».
 */
function prepareExamView(reason) {
  const profile = currentExamProfile();
  if (!profile.examUrl) return null;            // профиль без адреса — мок-тест
  if (state.examView) return state.examView;

  const ses = examSession();
  if (!ses) {
    log('страница экзамена НЕ подготовлена: раздел сессии недоступен, '
      + 'грузить чужой сайт без фильтра нельзя');
    return null;
  }
  let view;
  try {
    view = new BrowserView({
      webPreferences: {
        // Наш preload здесь НЕ подключается сознательно: window.proctor не
        // должен существовать на стороннем сайте.
        partition: EXAM_PARTITION,
        contextIsolation: true,
        nodeIntegration: false,
        nodeIntegrationInSubFrames: false,
        sandbox: true,
        webSecurity: true,
        allowRunningInsecureContent: false,
        experimentalFeatures: false,
        webviewTag: false,
        devTools: false,
        plugins: false,
        spellcheck: false,
        safeDialogs: true,
        backgroundThrottling: false,
      },
    });
  } catch (err) {
    log('BrowserView для экзамена не создан:', err && err.message);
    return null;
  }
  // Фон страницы экзамена — БЕЛЫЙ, а не цвет нашей тёмной оболочки.
  // Веб-страница по стандарту рисуется на белом холсте: её собственный фон
  // прозрачен, пока CSS не скажет иного. Подставив сюда #0b0f14, мы заставляли
  // любую страницу БЕЗ явного background отрисовываться тёмным, а её текст
  // остаётся тёмным — получался чёрный текст на чёрном фоне, и прочитать
  // билет было нельзя. Поймано на живом запуске 08.10.
  // Moodle свой фон задаёт, но множество внутренних страниц вузов — нет,
  // и цена ошибки здесь — сорванный экзамен, а не косметика.
  try { view.setBackgroundColor('#ffffff'); } catch (e) { /* не критично */ }
  state.examView = view;
  hardenExamContents(view.webContents);
  log(`страница экзамена подготовлена, но НЕ показана (${reason || '—'}); `
    + `состояние ${state.examState}`);
  return view;
}

/**
 * Показать страницу экзамена.
 *
 * ЕДИНСТВЕННЫЙ ЗВАТЕЛЬ — syncExamViewWithState(), и только он проверяет
 * состояние. Прямой вызов из других мест как раз и давал второе место
 * принятия решения: по одному пути представление появлялось в exam, по
 * другому — сразу после прихода профиля, то есть на экране согласия, и
 * накрывало собой согласие, предполётную проверку и калибровку.
 */
function attachExamView(reason) {
  const profile = currentExamProfile();
  if (!profile.examUrl) return false;           // профиль без адреса — мок-тест
  const win = state.win;
  if (!win || win.isDestroyed()) return false;
  if (state.examViewAttached) return true;
  if (!prepareExamView(reason)) return false;

  try {
    win.addBrowserView(state.examView);
  } catch (err) {
    log('страница экзамена не прикреплена к окну:', err && err.message);
    return false;
  }
  state.examViewAttached = true;
  layoutExamView();
  loadExamUrl(reason || 'attach');
  log(`страница экзамена прикреплена (${reason || '—'}); `
    + `профиль ${shortHash(state.examProfileHash)}; `
    + `место под неё ${JSON.stringify(examViewBounds())} `
    + `(отступ ${state.examViewInsetSource})`);
  broadcastShellStatus();
  return true;
}

/** Убрать страницу экзамена с экрана (экзамен закончился или сменился адрес). */
function detachExamView(reason) {
  const win = state.win;
  const view = state.examView;
  if (!view) return;
  /*
   * Уже снято — выходим молча. Идемпотентность здесь не стиль, а требование:
   * syncExamViewWithState() зовётся на каждом переходе, на готовности окна, на
   * возврате фокуса и на каждой смене профиля, то есть многократно на одном
   * экране согласия. Без этой проверки каждый такой вызов писал бы в журнал
   * «страница экзамена снята» и гонял бы подготовленное представление на
   * about:blank, а по журналу было бы не понять, что именно происходило.
   */
  if (!state.examViewAttached) return;
  if (win && !win.isDestroyed()) {
    try { win.removeBrowserView(view); } catch (err) { /* уже снято */ }
  }
  state.examViewAttached = false;
  // Содержимое уводим на пустую страницу: иначе чужой LMS продолжал бы жить
  // за кадром — таймеры, WebSocket, обращения к сети — уже после экзамена.
  try { view.webContents.loadURL('about:blank'); } catch (err) { /* уже мертво */ }
  log(`страница экзамена снята (${reason || '—'})`);
  broadcastShellStatus();
}

/** Полное уничтожение — на путях выхода. */
function destroyExamView(reason) {
  const view = state.examView;
  if (!view) return;
  detachExamView(reason);
  state.examView = null;
  try {
    if (view.webContents && !view.webContents.isDestroyed()) view.webContents.close();
  } catch (err) { /* electron уже уходит */ }
}

/**
 * Привести страницу экзамена в соответствие состоянию оболочки.
 *
 * ЕДИНСТВЕННОЕ МЕСТО, где решается, быть представлению на экране или нет.
 * Правило одно и то же для блокировок и для чужой страницы: только exam и
 * paused (LOCKED_STATES в shell/state.js). На согласии, предполётной
 * проверке, калибровке и отчёте экран принадлежит нашему интерфейсу — там
 * студент читает правила, проверяет окружение и калибруется, и накрывать это
 * страницей LMS значит отнять у него возможность дойти до экзамена.
 *
 * Все пути — приход и смена профиля, готовность окна, переподключение к ядру,
 * возврат фокуса, изменение размера, выход из паузы, блокирующий экран —
 * зовут ЭТУ функцию, а не attachExamView() напрямую. Иначе появляется второе
 * решение, и одно из них однажды оказывается неверным.
 */
function syncExamViewWithState(reason) {
  if (state.quitting) return;
  const hasUrl = Boolean(currentExamProfile().examUrl);
  const want = isLockedState(state.examState) && hasUrl;
  const label = reason || '—';
  if (!hasUrl && isLockedState(state.examState)) {
    // Правила без адреса — рабочее состояние: студент остаётся в локальном
    // мок-тесте. Снять уже показанную страницу обязательно: адрес мог
    // ИСЧЕЗНУТЬ из профиля посреди экзамена, и тогда на экране остался бы
    // чужой сайт, которого действующие правила уже не называют.
    detachExamView(`${label}: в профиле нет адреса экзамена`);
    state.examViewReason = `экзамен идёт, но в профиле нет адреса (${label})`;
    broadcastShellStatus();
    return;
  }
  if (want) {
    const ok = attachExamView(label);
    state.examViewReason = ok
      ? `экзамен идёт (${label})`
      : `экзамен идёт, но представление не прикреплено (${label})`;
  } else {
    // Готовим заранее, но не показываем: место под профиль уже известно, а
    // раздел сессии и фильтр запросов лучше проверить до экзамена.
    prepareExamView(label);
    detachExamView(label);
    state.examViewReason = `вне экзамена, состояние ${state.examState} (${label})`;
  }
  broadcastShellStatus();
}

// ---------------------------------------------------------------------------
// Главное окно
// ---------------------------------------------------------------------------

function fallbackRendererHtml() {
  // Используется, только если shell/renderer/index.html ещё не положен:
  // оболочка не должна показывать пустой экран на демо.
  // Предупреждение о режиме запуска вставляется в разметку, а не досылается
  // событием: на этом экране нет HUD, которому можно было бы его показать, а
  // зависеть от прихода сообщения такое предупреждение не должно.
  const warning = launchWarningText();
  const banner = warning
    ? `<div class="alarm"><b>Внимание.</b> ${escapeHtml(warning)}</div>`
    : '';
  const html = `<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Защищённый браузер</title><style>
html,body{margin:0;height:100%;background:#0b0f14;color:#e6edf3;
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
.wrap{padding:40px;max-width:900px;margin:0 auto;}
h1{font-size:22px;margin:0 0 6px;}
.sub{color:#7d8d9c;margin-bottom:28px;}
.card{background:#111823;border:1px solid #1e2a38;border-radius:12px;padding:18px 20px;margin-bottom:14px;}
.row{display:flex;justify-content:space-between;gap:16px;padding:4px 0;}
.k{color:#9fb0c0;}
.ok{color:#56d364;} .bad{color:#f85149;} .warn{color:#f0883e;}
.alarm{background:#3d1013;border:1px solid #f85149;border-radius:12px;
padding:16px 20px;margin-bottom:20px;color:#ffd7d5;}
button{background:#1f6feb;border:0;color:#fff;padding:9px 16px;border-radius:8px;
font-size:14px;cursor:pointer;}
</style></head><body><div class="wrap">
<h1>Защищённый браузер прокторинга</h1>
<div class="sub">Интерфейс теста ещё не установлен (shell/renderer/index.html). Это служебный экран оболочки.</div>
${banner}
<div class="card">
<div class="row"><span class="k">CV-канал</span><span id="cv" class="bad">проверяем…</span></div>
<div class="row"><span class="k">Экранов</span><span id="disp">—</span></div>
<div class="row"><span class="k">Защита содержимого</span><span id="cp">—</span></div>
<div class="row"><span class="k">Блокировки окружения</span><span id="locks">—</span></div>
<div class="row"><span class="k">Режим запуска</span><span id="launch">—</span></div>
<div class="row"><span class="k">Risk-score</span><span id="risk">—</span></div>
</div>
<button id="toggle">Переключить защиту содержимого</button>
</div><script>
function paint(s){
  var cv=document.getElementById('cv');
  cv.textContent=s.cvMessage; cv.className=s.cvAvailable?'ok':'bad';
  document.getElementById('disp').textContent=s.displayCount;
  document.getElementById('cp').textContent=s.contentProtection?'включена':'выключена';
  var l=s.lockdown&&s.lockdown.summary;
  document.getElementById('locks').textContent=l?(l.supported+' полных / '+l.partial+' частичных / '+l.unsupported+' недоступных ('+s.lockdown.platformLabel+')'):'—';
  var p=s.protection||{};
  var lau=document.getElementById('launch');
  var weak=(p.weakenedBy||[]).join(' ');
  lau.textContent=p.examReady?'пригоден для экзамена':('ослаблен: '+(weak||'см. предупреждение'));
  lau.className=p.examReady?'ok':'bad';
}
if(window.proctor){
  window.proctor.onShellStatus(paint);
  window.proctor.shellStatus().then(paint);
  window.proctor.onRisk(function(m){document.getElementById('risk').textContent=Math.round(m.score)+' ('+m.level+')';});
  document.getElementById('toggle').addEventListener('click',function(){window.proctor.toggleContentProtection();});
}
</script></body></html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

function createWindow() {
  const primary = screen.getPrimaryDisplay();
  // Окно создаётся ОБЫЧНЫМ. Ни kiosk, ни alwaysOnTop, ни closable:false на
  // старте: до начала экзамена пользователь владеет своей машиной. Захват
  // экрана накладывается позже, в applyWindowLocks(true), и снимается при
  // выходе из состояния exam. Рамка есть, когда захват запрещён флагами —
  // иначе окно нельзя ни перетащить, ни закрыть штатной кнопкой.
  const win = new BrowserWindow({
    width: primary.workAreaSize.width,
    height: primary.workAreaSize.height,
    show: false,
    frame: !WINDOW_LOCKS_ALLOWED,
    kiosk: false,
    fullscreen: false,
    alwaysOnTop: false,
    movable: true,
    resizable: true,
    minimizable: true,
    maximizable: true,
    closable: true,
    skipTaskbar: false,
    autoHideMenuBar: true,
    backgroundColor: '#0b0f14',
    title: 'Защищённый браузер прокторинга',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),
      devTools: false,
      sandbox: false,
      spellcheck: false,
      webviewTag: false,
      backgroundThrottling: false,
    },
  });
  state.win = win;

  // Ключевой демо-момент: окно исключается из захвата экрана. Это не
  // блокировка машины — влияет только на вид нашего окна в чужих записях.
  applyContentProtection(true);

  win.once('ready-to-show', () => {
    win.show();
    win.focus();
    // Захват экрана накладываем, только если экзамен уже идёт (перезапуск окна).
    applyLocksForState('window-ready');
    syncExamViewWithState('window-ready');
    auditWindow(`окно показано, состояние ${state.examState}`);
    auditHeldShortcuts(`окно показано, состояние ${state.examState}`);
  });

  // --- удержание фокуса -----------------------------------------------------
  // Факт ухода фокуса пишется в доказательную базу ВСЕГДА (это наблюдение),
  // а силой возвращается окно только во время экзамена.
  win.on('blur', () => {
    if (state.quitting) return;
    // оверлей блокирующего экрана сам забирает фокус — это не нарушение
    if (state.blockingReason) return;
    if (!isLockedState(state.examState)) return;   // до экзамена фокус не наш
    if (allowWindowState('window-blur')) {
      link.sendShellEvent(EventKind.WINDOW_BLUR, {
        reason: 'focus_lost',
        platform: process.platform,
      });
      log('потеря фокуса — возвращаем окно');
    }
    // возврат фокуса немедленно, но только если захват окна разрешён флагами
    if (!win.isDestroyed() && state.windowLocked) {
      win.show();
      win.focus();
      win.moveTop();
    }
  });

  // --- удержание fullscreen -------------------------------------------------
  win.on('leave-full-screen', () => {
    if (state.quitting || !state.windowLocked) return;
    if (allowWindowState('fullscreen-exit')) {
      link.sendShellEvent(EventKind.FULLSCREEN_EXIT, { reason: 'left_fullscreen' });
    }
    if (!win.isDestroyed()) {
      win.setFullScreen(true);
      win.setKiosk(true);
      win.focus();
    }
  });

  win.on('minimize', () => {
    if (state.quitting || !state.windowLocked) return;
    if (allowWindowState('window-minimize')) {
      link.sendShellEvent(EventKind.WINDOW_BLUR, { reason: 'minimized' });
    }
    win.restore();
    win.focus();
  });

  // Окно теста нельзя закрыть обычным способом — но ТОЛЬКО пока идёт экзамен.
  // На экране согласия и в отладочном режиме окно закрывается как у всех.
  win.on('close', (e) => {
    if (state.quitting) return;
    if (!state.windowLocked) {
      // снимаем блокировки до того, как окно исчезнет
      releaseLocks('window_close');
      return;
    }
    e.preventDefault();
    if (allowCombo('window-close-attempt')) {
      link.sendShellEvent(EventKind.SHORTCUT_BLOCKED, {
        combo: 'WINDOW_CLOSE',
        reason: 'window_close_request',
        source: 'window',
      });
    }
    win.show();
    win.focus();
  });

  // Размер окна поменялся (поворот экрана, выход из fullscreen, --no-kiosk):
  // страница экзамена обязана остаться в своих границах, иначе она либо
  // закроет рамку наблюдения, либо оставит чёрную полосу.
  win.on('resize', () => layoutExamView());
  win.on('enter-full-screen', () => layoutExamView());
  win.on('leave-full-screen', () => layoutExamView());

  // Требование гарантии освобождения: окно умерло любым способом — хоткеев
  // и таймеров после него не остаётся.
  win.on('closed', () => {
    state.win = null;
    destroyExamView('window_closed');
    if (!state.quitting) releaseLocks('window_closed');
  });

  hardenWebContents(win.webContents);

  win.webContents.on('did-finish-load', () => {
    state.rendererReady = true;
    broadcastShellStatus();
    broadcastProtection();
    pushLaunchWarningToHud();
    if (state.blockingReason) {
      sendToRenderer('proctor:blocking', {
        active: true,
        reason: state.blockingReason,
        title: state.blockingTitle,
        text: state.blockingText,
      });
    }
  });

  if (HAS_RENDERER) {
    win.loadFile(RENDERER_INDEX);
  } else {
    log('shell/renderer/index.html не найден — служебный экран оболочки');
    win.loadURL(fallbackRendererHtml());
  }
  return win;
}

function applyContentProtection(on) {
  state.contentProtection = Boolean(on);
  // Electron не умеет спросить, применена ли защита: getter-а нет. Поэтому
  // фиксируем результат вызова. Без этого в SHELL_CONFIG уходило бы НАМЕРЕНИЕ
  // («мы хотели защиту») вместо факта, а на платформе, где setContentProtection
  // не поддержан, окно спокойно попадает в запись экрана при «включённой»
  // защите — ровно тот случай, ради которого запись и заводится.
  let applied = false;
  let touched = false;
  for (const w of [state.win, state.overlay]) {
    if (!w || w.isDestroyed()) continue;
    touched = true;
    try {
      w.setContentProtection(state.contentProtection);
      applied = true;
    } catch (err) {
      applied = false;
      log('setContentProtection не поддержан:', err && err.message);
      break;
    }
  }
  state.contentProtectionApplied = touched ? (applied && state.contentProtection) : null;
  return state.contentProtection;
}

// ---------------------------------------------------------------------------
// Ужесточение webContents
// ---------------------------------------------------------------------------

/** Одно и то же webContents приходит и из createWindow, и из web-contents-created. */
const hardenedContents = new WeakSet();

function hardenWebContents(wc) {
  if (!wc || wc.isDestroyed()) return;
  if (hardenedContents.has(wc)) return;
  hardenedContents.add(wc);

  wc.on('context-menu', (e) => e.preventDefault());

  wc.setWindowOpenHandler(({ url }) => {
    if (allowCombo(`window-open:${url}`)) {
      link.sendShellEvent(EventKind.SHORTCUT_BLOCKED, {
        combo: 'WINDOW_OPEN',
        reason: 'window_open_denied',
        source: 'webContents',
        url,
      });
    }
    return { action: 'deny' };
  });

  wc.on('will-navigate', (e, url) => {
    const current = wc.getURL();
    if (url === current) return;
    e.preventDefault();
    if (allowCombo(`navigate:${url}`)) {
      link.sendShellEvent(EventKind.SHORTCUT_BLOCKED, {
        combo: 'NAVIGATION',
        reason: 'navigation_denied',
        source: 'webContents',
        url,
      });
    }
    log('навигация запрещена:', url);
  });

  wc.on('will-redirect', (e) => e.preventDefault());

  wc.on('will-attach-webview', (e) => e.preventDefault());

  wc.on('before-input-event', (e, input) => {
    const verdict = classifyInput(input);
    if (!verdict) return;
    // Вне экзамена глушим только попытку открыть devtools (это защита самой
    // оболочки от подмены). Всё остальное — Cmd+C, Cmd+W, Cmd+Q — на экране
    // согласия принадлежит пользователю.
    if (!isLockedState(state.examState) && verdict.reason !== 'devtools') return;
    e.preventDefault();
    reportBlockedCombo(verdict, 'before-input-event');
  });

  wc.on('devtools-opened', () => {
    try { wc.closeDevTools(); } catch (err) { /* уже закрыты */ }
    if (allowDerived('devtools-opened')) {
      link.sendShellEvent(EventKind.DEVTOOLS_ATTEMPT, { reason: 'devtools_opened', source: 'webContents' });
    }
  });

  wc.on('render-process-gone', (e, details) => {
    log('renderer упал:', details && details.reason);
  });
}

// ---------------------------------------------------------------------------
// Сайдкар дочерним процессом (только по флагу --spawn-sidecar)
// ---------------------------------------------------------------------------

function firstExisting(paths) {
  for (const p of paths) {
    try { if (fs.existsSync(p)) return p; } catch (err) { /* нет доступа */ }
  }
  return null;
}

function spawnSidecar() {
  const entry = firstExisting([
    path.join(ROOT_DIR, 'sidecar', 'main.py'),
    path.join(ROOT_DIR, 'sidecar', 'server.py'),
    path.join(ROOT_DIR, 'sidecar', 'app.py'),
    path.join(ROOT_DIR, 'sidecar', '__main__.py'),
  ]);
  if (!entry) {
    log('точка входа сайдкара не найдена — запустите его вручную');
    return;
  }
  // Приоритет не меняется (PROCTOR_PYTHON > .venv > PATH), но теперь известен
  // ИСТОЧНИК выбора, и он уходит в SHELL_CONFIG: подмена интерпретатора ядра
  // через переменную окружения перестаёт быть невидимой.
  const py = resolvePython();
  const python = py.resolved;
  state.sidecarPython = Object.assign({}, py, { used: true });
  if (py.source === 'env') {
    log('ВНИМАНИЕ: интерпретатор сайдкара задан переменной PROCTOR_PYTHON:', python,
      py.exists ? '' : '(путь не существует)');
    log('это значение уйдёт в журнал сессии записью launch_env');
  }

  /*
   * Флаги профиля ПЕРЕДАЮТСЯ ядру как есть, не разбираясь. Оболочка не знает,
   * что в файле, не считает его хеш и не применяет его сама: правила читает,
   * хеширует и кладёт в цепочку ядро, и оно же присылает оболочке готовый
   * действующий список. Один владелец правил вместо двух.
   */
  const args = [entry];
  if (CLI.examProfilePath) args.push('--exam-profile', CLI.examProfilePath);
  if (CLI.examProfilePubkey) {
    args.push('--exam-profile-pubkey', CLI.examProfilePubkey);
  }

  log('запускаем сайдкар:', python, args.join(' '));
  let child;
  try {
    child = spawn(python, args, {
      cwd: ROOT_DIR,
      env: Object.assign({}, process.env, { PYTHONUNBUFFERED: '1' }),
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (err) {
    log('сайдкар не запустился:', err && err.message);
    return;
  }
  state.sidecarChild = child;
  child.stdout.on('data', (d) => log('sidecar:', String(d).trimEnd()));
  child.stderr.on('data', (d) => log('sidecar!', String(d).trimEnd()));
  child.on('exit', (code) => {
    log('сайдкар завершился, код', code);
    state.sidecarChild = null;
  });
  child.on('error', (err) => log('сайдкар: ошибка процесса', err && err.message));
}

function killSidecar() {
  const child = state.sidecarChild;
  if (!child) return;
  state.sidecarChild = null;
  try { child.kill('SIGTERM'); } catch (err) { /* уже мёртв */ }
}

// ---------------------------------------------------------------------------
// Снимок окна экзамена к инциденту (docs/CONTRACT.md, «Снимок окна экзамена»)
// ---------------------------------------------------------------------------
/*
 * Ядро присылает `event` с `screen: true` к каждому неслужебному событию,
 * пока сессия пишется. Оболочка отвечает `screen_evidence`: снимком ТОЛЬКО
 * представления экзамена — страницы LMS, если тест идёт в ней, иначе
 * содержимого нашего окна — через webContents.capturePage(). Рабочий стол,
 * другие окна и desktopCapturer не используются НИКОГДА: снимок нужен, чтобы
 * показать вопрос и ответ в момент инцидента, а не то, что ещё открыто на
 * машине. Об этом же сказано студенту на экране согласия.
 *
 * capturePage() берёт кадр из компоновщика самого окна, поэтому
 * setContentProtection на снимок не влияет: защищено окно от ЧУЖОЙ записи
 * экрана, а не от собственной.
 *
 * Когда снимать, как не снимать одно и то же трижды и как прочитать размер
 * JPEG — в shell/state.js (screenCaptureVerdict, screenShotCoordinator,
 * jpegSize), там это проверяется без Electron.
 */

/** Тип сообщения. ipc.js его может ещё не знать — имя из контракта. */
const SCREEN_EVIDENCE_TYPE = MsgType.SCREEN_EVIDENCE || 'screen_evidence';

function nowEpochSec() {
  return Date.now() / 1000;
}

/** Одна строка без управляющих символов: текст ошибки ляжет в отчёт. */
function oneLine(value, limit) {
  const text = String(value == null ? '' : value).replace(/[\u0000-\u001f\u007f]+/g, ' ')
    .replace(/\s+/g, ' ').trim();
  return text.slice(0, limit || SCREEN_EVIDENCE.errorMaxChars);
}

/**
 * Что снимать. Страница LMS — только если она ПРИКРЕПЛЕНА к окну, то есть
 * сейчас на экране (syncExamViewWithState держит её только в exam/paused).
 * Иначе тест идёт в нашем окне, и снимается его содержимое.
 */
function screenEvidenceTarget() {
  const view = state.examView;
  if (view && state.examViewAttached) {
    const wc = view.webContents;
    if (wc && !wc.isDestroyed()) return { wc, source: 'exam_view' };
  }
  const win = state.win;
  if (win && !win.isDestroyed()) {
    const wc = win.webContents;
    if (wc && !wc.isDestroyed()) return { wc, source: 'main_window' };
  }
  return null;
}

/**
 * Уменьшить до ширины SCREEN_EVIDENCE.maxWidth и закодировать JPEG.
 *
 * Второй проход — для HiDPI: getSize() отдаёт ширину в DIP, а toJPEG()
 * может закодировать представление 2x, и в байтах окажется вдвое больше
 * пикселей. Ширину проверяем по готовому JPEG (jpegSize), и если она всё ещё
 * больше предела — перечитываем байты как картинку масштаба 1 и уменьшаем.
 */
function encodeScreenJpeg(image) {
  const maxWidth = SCREEN_EVIDENCE.maxWidth;
  let img = image;
  let out = null;
  for (let pass = 0; pass < 2; pass += 1) {
    const size = img.getSize();
    if (size.width > maxWidth) img = img.resize({ width: maxWidth, quality: 'good' });
    const jpeg = img.toJPEG(SCREEN_EVIDENCE.jpegQuality);
    const dims = jpegSize(jpeg) || img.getSize();
    out = { jpeg, width: dims.width, height: dims.height };
    if (dims.width <= maxWidth) break;
    img = nativeImage.createFromBuffer(jpeg, { scaleFactor: 1 });
    if (img.isEmpty()) break;
  }
  return out;
}

/**
 * Снять представление экзамена. Не бросает: неудача — это `{error}`, и она
 * уходит ядру причиной, а отчёт пишет «снимок окна не получен: <причина>».
 */
async function captureExamView() {
  const target = screenEvidenceTarget();
  if (!target) {
    return { error: 'окна экзамена нет', source: 'main_window', capturedAt: nowEpochSec() };
  }
  const { wc, source } = target;
  let image;
  try {
    image = await wc.capturePage();
  } catch (err) {
    return {
      error: `capturePage не удался: ${oneLine((err && err.message) || err, 120)}`,
      source,
      capturedAt: nowEpochSec(),
    };
  }
  const capturedAt = nowEpochSec();
  if (!image || image.isEmpty()) {
    return { error: 'снимок пустой: окно экзамена скрыто или свёрнуто', source, capturedAt };
  }
  let encoded;
  try {
    encoded = encodeScreenJpeg(image);
  } catch (err) {
    return {
      error: `JPEG не собран: ${oneLine((err && err.message) || err, 120)}`,
      source,
      capturedAt,
    };
  }
  if (!encoded || !isJpeg(encoded.jpeg)) {
    return { error: 'кодировщик вернул не JPEG', source, capturedAt };
  }
  if (encoded.jpeg.length > SCREEN_EVIDENCE.maxBytes) {
    return {
      error: `снимок ${encoded.jpeg.length} байт больше предела ${SCREEN_EVIDENCE.maxBytes}`,
      source,
      capturedAt,
    };
  }
  return {
    source,
    capturedAt,
    width: encoded.width,
    height: encoded.height,
    bytes: encoded.jpeg.length,
    data_b64: encoded.jpeg.toString('base64'),
  };
}

/**
 * Не больше одного снимка в полёте; события в пределах 1.5 с — те же байты.
 * capturePage(), не ответивший за 4 с, уходит ядру причиной «capturePage не
 * ответил за 4 с» и снимается с полёта: следующее событие снимает заново.
 */
const screenShots = screenShotCoordinator(() => captureExamView(), {
  reuseMs: SCREEN_EVIDENCE.reuseMs,
  timeoutMs: SCREEN_EVIDENCE.captureTimeoutMs,
});

/**
 * Ответ ядру по одному событию. Каждое событие получает СВОЁ сообщение со
 * своим event_id, даже если байты общие с соседним.
 *
 * Только по живому каналу: в очередь ipc.js снимок не ставим. Очередь
 * рассчитана на короткие события, а не на сотни килобайт, и ядро всё равно
 * не примет снимок позже 60 с после события.
 */
function sendScreenEvidence(eventId, kind, shot) {
  const s = shot || {};
  const payload = {
    event_id: eventId,
    mime: 'image/jpeg',
    captured_at: Number.isFinite(s.capturedAt) ? s.capturedAt : nowEpochSec(),
    source: s.source === 'exam_view' ? 'exam_view' : 'main_window',
  };
  if (s.data_b64) {
    payload.data_b64 = s.data_b64;
    payload.width = s.width;
    payload.height = s.height;
  } else {
    payload.error = oneLine(s.error || 'снимок не получен') || 'снимок не получен';
  }
  if (!link.connected) {
    log(`снимок окна экзамена к ${kind} не отправлен: нет связи с ядром`);
    return false;
  }
  const sent = link.send(SidecarLink.envelope(SCREEN_EVIDENCE_TYPE, payload));
  log(payload.data_b64
    ? `снимок окна экзамена к ${kind} (${eventId.slice(0, 8)}): ${payload.source} `
      + `${payload.width}x${payload.height}, ${Math.round((s.bytes || 0) / 1024)} КБ`
    : `снимка окна экзамена к ${kind} (${eventId.slice(0, 8)}) нет: ${payload.error}`);
  return sent;
}

/**
 * Событие ядра с `screen: true`. Снимаем только внутри активной сессии и
 * только пока экзамен на экране (exam/paused); иначе отвечаем причиной.
 * Сессии нет вовсе — не снимаем и не отвечаем: ядро такой снимок не примет.
 */
function requestScreenEvidence(ev) {
  const eventId = ev && typeof ev.id === 'string' ? ev.id.trim().slice(0, 64) : '';
  if (!eventId) return;
  const kind = oneLine(ev.kind || '?', 64);
  const verdict = screenCaptureVerdict({
    sessionStarted: state.sessionStarted,
    examState: state.examState,
    quitting: state.quitting,
  });
  if (!verdict.capture) {
    if (!verdict.error) {
      log(`снимок окна экзамена к ${kind} не снят: сессия не открыта`);
      return;
    }
    const target = screenEvidenceTarget();
    sendScreenEvidence(eventId, kind, {
      error: verdict.error,
      source: target ? target.source : 'main_window',
      capturedAt: nowEpochSec(),
    });
    return;
  }
  screenShots.take()
    .then((shot) => sendScreenEvidence(eventId, kind, shot))
    .catch((err) => log('снимок окна экзамена не отправлен:', err && err.message));
}

// ---------------------------------------------------------------------------
// Финальный экран: где отчёт и пакет
// ---------------------------------------------------------------------------
/*
 * Ядро собирает report.html в каталоге сессии ПОСЛЕ SESSION_ENDED, затем
 * пакет `<сессия>.proctor.zip` рядом с каталогом. Оболочка запоминает пути из
 * того, что ядро уже присылает, и раз в секунду отдаёт renderer готовность
 * файлов (shellStatus().sessionReport). Открыть отчёт можно только после
 * конца сессии, когда машина отпущена: shell.openPath уводит фокус в браузер,
 * и во время экзамена это был бы законный выход из kiosk-окна.
 */

/** Имя отчёта в каталоге сессии (report_filename в sidecar/config.py). */
const REPORT_FILENAME = 'report.html';
const PACKAGE_SUFFIX = '.proctor.zip';

function freshSessionReport() {
  return {
    sessionDir: '',
    packagePath: '',
    packageOk: null,         // null — пакет ещё не собирался или итог не пришёл
    packageMessage: '',
    sessionCode: '',
    degradedHandover: false,
    handoverReason: '',
    ended: false,
    endReason: '',
  };
}

function textField(value, limit) {
  return typeof value === 'string' ? value.trim().slice(0, limit || 4096) : '';
}

/** Абсолютный путь из сообщения ядра; относительный считаем от корня репозитория. */
function absFromSidecar(p) {
  const raw = textField(p);
  if (!raw || raw.indexOf('\u0000') !== -1) return '';
  return path.isAbsolute(raw) ? path.normalize(raw) : path.resolve(ROOT_DIR, raw);
}

function fileReady(p) {
  if (!p) return false;
  try {
    const st = fs.statSync(p);
    return st.isFile() && st.size > 0;
  } catch (err) {
    return false;
  }
}

/** SESSION_STARTED / SESSION_ENDED: каталог, код, пакет. */
function noteSessionReportFromEvent(ev) {
  if (!ev || typeof ev !== 'object') return;
  const d = ev.detail && typeof ev.detail === 'object' ? ev.detail : {};
  if (ev.kind === EventKind.SESSION_STARTED) {
    state.sessionReport = Object.assign(freshSessionReport(), {
      sessionDir: absFromSidecar(d.session_dir),
      sessionCode: textField(d.session_code_display, 32),
      degradedHandover: Boolean(d.degraded_handover),
    });
    broadcastShellStatus();
    return;
  }
  if (ev.kind !== EventKind.SESSION_ENDED) return;
  const r = state.sessionReport;
  r.ended = true;
  r.endReason = textField(d.reason, 200);
  if (textField(d.session_code_display)) r.sessionCode = textField(d.session_code_display, 32);
  if (typeof d.degraded_handover === 'boolean') r.degradedHandover = d.degraded_handover;
  const pkg = absFromSidecar(d.package_path);
  if (pkg) r.packagePath = pkg;
  // SESSION_STARTED мог пройти мимо (оболочку перезапустили посреди сессии):
  // каталог сессии лежит рядом с пакетом под тем же именем.
  if (!r.sessionDir && pkg.endsWith(PACKAGE_SUFFIX)) {
    r.sessionDir = pkg.slice(0, -PACKAGE_SUFFIX.length);
  }
  log(`сессия закрыта ядром: каталог ${r.sessionDir || '—'}, пакет ${r.packagePath || '—'}, `
    + `код ${r.sessionCode || '—'}${r.degradedHandover ? ', ПЕРЕДАЧА ДЕГРАДИРОВАНА' : ''}`);
  broadcastShellStatus();
}

/** status: итог сборки пакета и причина деградации передачи. */
function noteSessionReportFromStatus(msg) {
  const r = state.sessionReport;
  if (!msg || typeof msg !== 'object') return;
  const handover = msg.handover && typeof msg.handover === 'object' ? msg.handover : null;
  if (handover && handover.degraded_handover) {
    r.handoverReason = textField(handover.reason || handover.message, 400);
  }
  if (!r.ended) return;
  if (!r.sessionCode && textField(msg.session_code_display)) {
    r.sessionCode = textField(msg.session_code_display, 32);
  }
  const pkg = msg.package && typeof msg.package === 'object' ? msg.package : null;
  if (!pkg) return;
  const pkgPath = absFromSidecar(pkg.package);
  // Итог чужого пакета (пересборка другой сессии) к этой не относится.
  if (pkgPath && r.packagePath && pkgPath !== r.packagePath) return;
  if (pkgPath) r.packagePath = pkgPath;
  r.packageOk = Boolean(pkg.ok);
  r.packageMessage = r.packageOk ? '' : textField(pkg.reason || pkg.message, 400);
}

/** То, что видит финальный экран. Готовность файлов — по диску, не на слово. */
function sessionReportInfo() {
  const r = state.sessionReport || freshSessionReport();
  const reportPath = r.sessionDir ? path.join(r.sessionDir, REPORT_FILENAME) : '';
  const reportReady = fileReady(reportPath);
  const examOver = !isLockedState(state.examState) && !state.locksIntended && !state.locksActive;
  return {
    sessionDir: r.sessionDir,
    reportPath,
    reportReady,
    packagePath: r.packagePath,
    packageReady: fileReady(r.packagePath),
    packageOk: r.packageOk,
    packageMessage: r.packageMessage,
    sessionCode: r.sessionCode,
    degradedHandover: Boolean(r.degradedHandover),
    handoverReason: r.handoverReason,
    ended: Boolean(r.ended),
    canOpen: Boolean(r.ended && reportReady && examOver),
  };
}

/**
 * Открыть <каталог сессии>/report.html браузером по умолчанию. Только после
 * конца сессии и снятия блокировок — проверка здесь, а не в renderer:
 * кнопку можно нажать и из подменённой страницы.
 */
async function openSessionReport() {
  if (isLockedState(state.examState) || state.locksIntended || state.locksActive) {
    return {
      ok: false,
      reason: 'exam_running',
      message: 'Отчёт открывается только после завершения экзамена.',
    };
  }
  const info = sessionReportInfo();
  if (!info.ended) {
    return {
      ok: false,
      reason: 'session_not_ended',
      message: 'Ядро ещё не закрыло сессию: отчёт появится после её завершения.',
    };
  }
  if (!info.reportPath) {
    return {
      ok: false,
      reason: 'no_session_dir',
      message: 'Каталог сессии неизвестен: ядро его не сообщило.',
    };
  }
  if (!info.reportReady) {
    return {
      ok: false,
      reason: 'report_not_ready',
      message: `Отчёт ещё собирается: ${info.reportPath}`,
      path: info.reportPath,
    };
  }
  let err = '';
  try {
    err = await electronShell.openPath(info.reportPath);
  } catch (e) {
    err = (e && e.message) || 'openPath не удался';
  }
  if (err) {
    log('отчёт не открылся:', info.reportPath, err);
    return {
      ok: false,
      reason: 'open_failed',
      message: `Не удалось открыть отчёт: ${oneLine(err, 200)}. Файл: ${info.reportPath}`,
      path: info.reportPath,
    };
  }
  log('отчёт открыт браузером по умолчанию:', info.reportPath);
  return { ok: true, path: info.reportPath };
}

// ---------------------------------------------------------------------------
// IPC из renderer
// ---------------------------------------------------------------------------

function registerIpc() {
  // Переключатель защиты содержимого — для показа эффекта на демо.
  ipcMain.handle('toggle-content-protection', (e, value) => {
    const next = typeof value === 'boolean' ? value : !state.contentProtection;
    applyContentProtection(next);
    broadcastShellStatus();
    log('защита содержимого:', state.contentProtection ? 'включена' : 'выключена');
    return state.contentProtection;
  });

  ipcMain.handle('proctor:capabilities', () => ({
    lockdown: lockdown.capabilities(),
    matrix: Lockdown.matrix(),
    shell: shellStatus(),
  }));

  ipcMain.handle('proctor:shell-status', () => shellStatus());

  /**
   * Состояние сессии из renderer: окно теста знает, на каком экране студент.
   * Доверия к значению нет — setExamState() проверяет имя и допустимость
   * перехода, и только после этого включает или снимает блокировки.
   */
  ipcMain.handle('proctor:exam-state', (e, next) => setExamState(next, 'renderer'));

  ipcMain.handle('proctor:protection', () => protectionInfo());

  ipcMain.handle('proctor:session-start', (e, meta) => {
    const payload = {
      student_id: (meta && (meta.student_id || meta.studentId)) || CLI.studentId,
      exam_id: (meta && (meta.exam_id || meta.examId)) || CLI.examId,
      student_name: (meta && (meta.student_name || meta.studentName)) || CLI.studentName,
    };
    openSession(payload, false);
    return payload;
  });

  ipcMain.handle('proctor:session-end', (e, reason) => {
    link.sendSessionEnd(typeof reason === 'string' ? reason : 'renderer_request');
    state.sessionStarted = false;
    state.sessionSentAt = 0;
    screenShots.reset();
    // Сессия закончилась — машина обязана освободиться, даже если renderer
    // забыл прислать состояние finished.
    if (isLockedState(state.examState)) setExamState('finished', 'session-end');
    broadcastShellStatus();
    return true;
  });

  ipcMain.handle('proctor:calibrate', (e, stage, point) => link.sendCalibrate(stage, point));

  // Второй аргумент несёт actor/reason решений проктора. Без него команды
  // proctor_lock / proctor_release отклоняются сайдкаром (actor_required):
  // запись в журнале обязана называть человека, который решил.
  ipcMain.handle('proctor:command', (e, name, opts) => link.sendCommand(name, opts || {}));

  // Телеметрия — поток, ответа не ждём (ipcMain.on, не handle).
  ipcMain.on('proctor:telemetry', (e, msg) => {
    link.sendTelemetry(msg || {});
  });

  /*
   * Отчёт renderer о СВОЕЙ раскладке: какой прямоугольник окна он оставил
   * свободным под страницу экзамена. Поток, не запрос: приходит на каждом
   * изменении размера, и ответа ждать незачем.
   *
   * Почему не считаем сами. Раскладка задана CSS и меняется медиа-запросом:
   * шире 1100px HUD — правая колонка, уже — нижняя полоса во всю ширину, а
   * шапка вырастает со 72px до ~125px. Константа в main.js это повторить не
   * может и на живом запуске 08.10 не повторила: на окне 1000x800 страница
   * LMS накрыла HUD целиком, вместе с кнопкой паузы.
   *
   * Присланному не доверяем на слово: проверку делает
   * normalizeExamViewInset() в shell/state.js, и отчёт, который не оставил
   * места ни шапке, ни одной из полос HUD, отвергается с записью в журнал.
   */
  ipcMain.on('proctor:exam-view-inset', (e, inset) => {
    applyExamViewInset(inset);
  });

  // Renderer может попросить свой блокирующий экран (например по verdict=pause).
  ipcMain.handle('proctor:set-blocking', (e, payload) => {
    const p = payload || {};
    if (p.active) {
      showBlocking(
        String(p.reason || 'renderer'),
        String(p.title || 'Тест приостановлен'),
        String(p.text || 'Вернитесь в рамку кадра и дождитесь разрешения.'),
      );
    } else {
      hideBlocking(p.reason ? String(p.reason) : null);
    }
    return state.blockingReason;
  });

  /*
   * «Открыть отчёт» на финальном экране: <каталог сессии>/report.html
   * браузером по умолчанию. Путь renderer не передаёт и передать не может —
   * он берётся из того, что сообщило ядро, а условия (сессия закрыта,
   * блокировки сняты, файл на месте) проверяет openSessionReport().
   */
  ipcMain.handle('proctor:open-report', () => openSessionReport());

  // Запрос выхода из оболочки (кнопка «завершить тест» в renderer).
  ipcMain.handle('proctor:exit', (e, reason) => {
    shutdown(typeof reason === 'string' ? reason : 'renderer_exit');
    return true;
  });
}

// ---------------------------------------------------------------------------
// Канал сайдкара -> renderer
// ---------------------------------------------------------------------------

/**
 * Открыть сессию в сайдкаре и запомнить, что именно и когда отправили.
 * Единая точка: из renderer, из автостарта и из восстановления после перезапуска
 * сайдкара — иначе состояние оболочки и состояние ядра расходятся.
 */
function openSession(payload, auto) {
  state.sessionStarted = true;
  state.sessionAuto = Boolean(auto);
  state.sessionMeta = payload;
  state.sessionSentAt = Date.now();
  // Снимок окна прошлой сессии новой не достаётся даже в окне повтора 1.5 с.
  screenShots.reset();
  link.sendSessionStart(payload);
  // Режим запуска уходит СРАЗУ ЗА session_start и по тому же сокету: порядок
  // сообщений в канале сохраняется, поэтому ядро успевает открыть цепочку и
  // записывает режим рядом с политикой и правилами оценки, до первого
  // наблюдения. Отправляется на каждое открытие сессии, включая повторное
  // после разрыва связи: у новой цепочки своя запись о режиме, и наследовать
  // прежнюю она не должна.
  link.sendShellConfig(shellConfigRecord(`сессия ${payload && payload.exam_id}`));
  broadcastShellStatus();
}

function wireSidecar() {
  const channels = {
    [MsgType.HELLO]: 'proctor:hello',
    [MsgType.STATUS]: 'proctor:status',
    [MsgType.EVENT]: 'proctor:event',
    [MsgType.RISK]: 'proctor:risk',
    [MsgType.CALIBRATION]: 'proctor:calibration',
    [MsgType.VERDICT]: 'proctor:verdict',
    [MsgType.ERROR]: 'proctor:error',
  };

  /**
   * Профиль из любого сообщения ядра.
   *
   * Контракт на этот счёт ещё дописывается (файл профиля читает сайдкар), и
   * форма может приехать в hello либо отдельным сообщением, под одним из
   * нескольких имён. Поэтому смотрим каждое сообщение и читаем оборонительно:
   * ошибка здесь означала бы, что экзамен открылся не по тем правилам.
   */
  function pickProfileFrom(msg) {
    if (!msg || typeof msg !== 'object') return undefined;
    for (const key of ['exam_profile', 'examProfile', 'exam_rules', 'profile']) {
      const value = msg?.[key];
      if (value && typeof value === 'object') return value;
    }
    // Профиль мог приехать вложенным в policy — там уже живут правила оценки.
    const policy = msg?.policy;
    if (policy && typeof policy === 'object') {
      for (const key of ['exam_profile', 'examProfile', 'exam_rules']) {
        const value = policy?.[key];
        if (value && typeof value === 'object') return value;
      }
    }
    return undefined;
  }

  link.onMessage((msg) => {
    const channel = channels[msg.type];
    if (channel) sendToRenderer(channel, msg);

    const raw = pickProfileFrom(msg);
    if (raw !== undefined) applyExamProfile(raw, 'sidecar');

    if (msg.type === MsgType.RISK) {
      state.lastRisk = { score: msg.score, level: msg.level, action: msg.action };
    }
    if (msg.type === MsgType.VERDICT) {
      state.lastVerdict = { action: msg.action, reason: msg.reason, score: msg.score };
    }
    if (msg.type === MsgType.STATUS) noteSessionReportFromStatus(msg);
    if (msg.type === MsgType.EVENT) {
      const ev = msg.event && typeof msg.event === 'object' ? msg.event : null;
      noteSessionReportFromEvent(ev);
      // Строго `true`: ядро прежней версии поля не шлёт, и снимать по нему
      // окно было бы самодеятельностью оболочки.
      if (msg.screen === true) requestScreenEvidence(ev);
    }
  });

  link.onType(MsgType.HELLO, () => {
    log('сайдкар сообщил возможности:', JSON.stringify(link.capabilities));
    broadcastShellStatus();
    // Автостарт сессии — ТОЛЬКО когда интерфейса теста нет (служебный экран
    // оболочки): там некому нажать «начать», и без сессии доказательная база
    // не откроется. Если renderer есть, сессию открывает он сам — после экрана
    // согласия и предполётной проверки. Автостарт при живом renderer означал бы
    // запись доказательств (кадры с лицом студента) до того, как студент дал
    // согласие, а это прямо противоречит docs/LIMITATIONS.md («Этика и
    // приватность», п. 3) и README.
    if (HAS_RENDERER) return;
    setTimeout(() => {
      if (state.sessionStarted || state.quitting) return;
      const payload = {
        student_id: CLI.studentId,
        exam_id: CLI.examId,
        student_name: CLI.studentName,
      };
      openSession(payload, true);
      log('сессия открыта автоматически:', JSON.stringify(payload));
    }, 3000);
  });

  // Восстановление сессии. Сайдкар может перезапуститься или потерять соединение
  // (watchdog в ipc.js рвёт молчащий сокет) посреди демо. Оболочка при этом
  // по-прежнему считает, что экзамен идёт, и никогда больше не пришлёт
  // session_start: ядро остаётся без сессии — доказательства не пишутся,
  // вердикты подавляются («вердикта нет: сессии нет»), отчёт выходит пустым.
  // Поэтому сверяем состояния по полю `state` из status и доливаем session_start.
  link.onType(MsgType.STATUS, (msg) => {
    if (state.quitting || !state.sessionStarted || !state.sessionMeta) return;
    const sidecarState = msg && typeof msg.state === 'string' ? msg.state : '';
    if (sidecarState !== 'idle') return;           // сессия у ядра есть — не трогаем
    // status мог быть отправлен до того, как ядро обработало наш session_start
    if (Date.now() - state.sessionSentAt < 5000) return;
    // Ровно одна попытка на соединение: второй session_start по тому же каналу
    // закрыл бы только что открытую сессию и завёл второй каталог с пустым отчётом.
    if (state.sessionEpoch === state.linkEpoch) return;
    state.sessionEpoch = state.linkEpoch;
    log('ядро без сессии — повторно открываю сессию после разрыва');
    openSession(state.sessionMeta, state.sessionAuto);
  });

  link.on('open', () => {
    state.linkEpoch += 1;
    /*
     * Переподключение к ядру — отдельный путь, на котором представление
     * умеет всплыть не вовремя: после разрыва ядро заново присылает `hello`
     * с профилем, и «профиль пришёл» на экране согласия не должно значить
     * «покажи LMS». Решение принимает та же одна функция, по состоянию.
     */
    syncExamViewWithState('link-reopen');
    broadcastShellStatus();
  });
  link.on('close', () => broadcastShellStatus());
  link.on('unavailable', () => broadcastShellStatus());
  link.on('socket-error', () => broadcastShellStatus());
}

// ---------------------------------------------------------------------------
// Завершение
// ---------------------------------------------------------------------------

/**
 * Полное освобождение машины. Идемпотентно, не бросает и не зависит ни от
 * окна, ни от сайдкара, ни от состояния сессии. Вызывается со ВСЕХ путей
 * выхода, включая аварийные: упавшее приложение не должно оставить
 * пользователя с перехваченными системными сочетаниями.
 *
 * Здесь — и только здесь — снимается аварийный выход: он держится до самого
 * конца процесса.
 */
let fullyReleased = false;
function releaseEverything(reason) {
  if (fullyReleased) return;
  fullyReleased = true;
  try { log('полное освобождение машины:', reason); } catch (err) { /* консоль закрыта */ }

  if (state.statusTimer) {
    try { clearInterval(state.statusTimer); } catch (err) { /* уже */ }
    state.statusTimer = null;
  }
  try { lockdown.dispose(reason); } catch (err) { /* electron мог уйти */ }
  // Страница экзамена уходит вместе с машиной: иначе чужой LMS остался бы
  // жить в процессе (таймеры, WebSocket) уже после освобождения.
  try { destroyExamView(reason); } catch (err) { /* окна может не быть */ }
  try { applyWindowLocks(false); } catch (err) { /* окна может не быть */ }
  state.locksIntended = false;
  state.locksActive = false;
  state.windowLocked = false;
  // Теперь можно отпустить и аварийный выход — процесс всё равно уходит.
  try { globalShortcut.unregister(ADMIN_EXIT_ACCELERATOR); } catch (err) { /* нечего */ }
  try { globalShortcut.unregisterAll(); } catch (err) { /* нечего снимать */ }
  state.adminExitHeld = false;
  try {
    const left = globalShortcut.isRegistered(ADMIN_EXIT_ACCELERATOR);
    log(`проверка: аварийный выход ${left ? 'ВСЁ ЕЩЁ занят' : 'освобождён'}`);
  } catch (err) { /* модуль уже недоступен */ }
}

function shutdown(reason) {
  if (state.quitting) return;
  state.quitting = true;
  log('завершение оболочки:', reason);

  try { link.sendSessionEnd(reason); } catch (err) { /* канала может не быть */ }

  releaseEverything(reason);
  try { clipboard.clear(); } catch (err) { /* нет буфера */ }
  killSidecar();

  // даём 250 мс на доставку session_end и выходим жёстко, минуя before-quit
  setTimeout(() => {
    link.stop();
    for (const w of BrowserWindow.getAllWindows()) {
      try { w.destroy(); } catch (err) { /* уже уничтожено */ }
    }
    app.exit(0);
  }, 250);
}

// ---------------------------------------------------------------------------
// Старт
// ---------------------------------------------------------------------------

if (!app.requestSingleInstanceLock()) {
  log('оболочка уже запущена — второй экземпляр закрывается');
  app.exit(0);
} else {
  app.on('second-instance', () => {
    const win = state.win;
    if (win && !win.isDestroyed()) {
      win.show();
      win.focus();
    }
  });

  // Снимаем системное меню: вместе с ним уходят штатные акселераторы
  // (Cmd+Q, Cmd+W, Cmd+M, Cmd+H, Ctrl+R и прочие). Это меню НАШЕГО приложения,
  // не системное, но в отладочном режиме оно нужно: без него из обычного окна
  // не выйти по Cmd+Q, а в этом режиме окно обязано вести себя как обычное.
  if (WINDOW_LOCKS_ALLOWED) Menu.setApplicationMenu(null);

  app.on('window-all-closed', () => {
    if (!state.quitting) shutdown('window_all_closed');
  });

  app.on('before-quit', (e) => {
    if (state.quitting) {
      // уже уходим — но убедимся, что машина отпущена
      releaseEverything('before_quit_while_quitting');
      return;
    }
    e.preventDefault();
    shutdown('before_quit');
  });

  // will-quit отменить нельзя: это последняя точка, где electron ещё жив
  // и globalShortcut ещё отвечает.
  app.on('will-quit', () => releaseEverything('will_quit'));
  app.on('quit', () => releaseEverything('quit'));

  // Повторная регистрация хоткеев при возврате фокуса приложению.
  // Lockdown.reregister() сам молчит, если блокировки не включены, так что на
  // экране согласия возврат фокуса ничего не захватывает.
  app.on('browser-window-focus', () => {
    if (state.quitting) return;
    ensureAdminExit();
    lockdown.reregister();
  });

  app.on('browser-window-blur', () => {
    if (state.quitting || state.blockingReason) return;
    if (!isLockedState(state.examState)) return;   // вне экзамена фокус не наш
    // Отдельно от win.on('blur'): ловим случай, когда фокус ушёл с любого окна
    // оболочки, включая оверлей. Троттлинг общий, дубля в журнале не будет.
    if (allowWindowState('window-blur')) {
      link.sendShellEvent(EventKind.WINDOW_BLUR, { reason: 'app_window_blur' });
    }
    const win = state.win;
    if (win && !win.isDestroyed() && !state.blockingReason && state.windowLocked) {
      win.show();
      win.focus();
    }
  });

  /*
   * Ужесточение по ТИПУ содержимого. Разделение обязательно: generic
   * hardenWebContents() запрещает навигацию целиком, и будь он применён к
   * странице экзамена, настоящий LMS сломался бы на первом же входе в
   * систему — вход это переход. Тип 'browserView' здесь ровно один: наша
   * страница экзамена, и у неё свой обработчик, принимающий решение по
   * профилю. Событие приходит раньше, чем мы успеваем пометить webContents
   * своим флагом, поэтому различаем по getType(), а не по собственной метке.
   */
  app.on('web-contents-created', (e, wc) => {
    let type = '';
    try { type = wc.getType(); } catch (err) { type = ''; }
    if (type === 'browserView') hardenExamContents(wc);
    else hardenWebContents(wc);
  });

  app.whenReady().then(() => {
    // Разрешаем только камеру/микрофон (превью в renderer), всё прочее — запрет.
    try {
      const ses = session.defaultSession;
      ses.setPermissionRequestHandler((wc, permission, callback) => {
        callback(permission === 'media');
      });
      ses.setPermissionCheckHandler((wc, permission) => permission === 'media');
    } catch (err) {
      log('permission handler:', err && err.message);
    }

    // ПЕРВЫМ делом — аварийный выход, до любых других хоткеев и до окна.
    ensureAdminExit();

    // Профиль экзамена: до прихода правил от ядра — пустой, то есть прежнее
    // поведение, локальный мок-тест. Своих правил оболочка не выдумывает.
    state.examProfileFlagsRefused = warnRefusedProfileFlags();
    currentExamProfile();
    log('правила экзамена задаёт ядро файлом профиля (--exam-profile); '
      + 'до его прихода открывается локальный мок-тест');
    if (CLI.examProfilePath && !CLI.spawnSidecar) {
      log(`ВНИМАНИЕ: --exam-profile ${CLI.examProfilePath} подан оболочке, но `
        + 'сайдкар запускается не ею (нет --spawn-sidecar). Передать файл нечему — '
        + 'подайте этот флаг тому процессу, который запускает ядро, иначе правила '
        + 'экзамена не будут заданы НИЧЕМ.');
    }

    registerIpc();
    wireSidecar();

    if (CLI.spawnSidecar) spawnSidecar();
    link.start();

    createWindow();

    // Начальное состояние — экран согласия. Блокировок здесь нет и быть не
    // может: студент ещё даже не дал согласие. Раньше на этом месте стоял
    // lockdown.engage(), и машина залочивалась на старте приложения.
    setExamState('consent', 'startup');

    // Наблюдатели (опрос мониторов) работают всегда: это доказательная база,
    // а не блокировка. С --no-lockdown журнал MULTIPLE_DISPLAYS наполняется.
    lockdown.startObservers();

    log(`режим запуска: kiosk ${CLI.noKiosk ? 'выключен (--no-kiosk)' : 'включён'}, `
      + `блокировки ${CLI.noLockdown ? 'выключены навсегда (--no-lockdown)' : 'включатся на время экзамена'}, `
      + `захват окна ${WINDOW_LOCKS_ALLOWED ? 'разрешён на время экзамена' : 'запрещён флагами'}`);
    // Заметное предупреждение, если запуск ослаблен. Печатается ПОСЛЕ строки
    // режима и до первой сессии: человек у машины должен увидеть его прежде,
    // чем начнёт экзамен, а не вычитать потом из отчёта.
    warnWeakenedLaunch();
    auditHeldShortcuts(`старт, состояние ${state.examState}`);

    // Мониторы: стартовая проверка + подписка на изменения в рантайме.
    handleDisplays({ count: screen.getAllDisplays().length }, 'startup');
    screen.on('display-added', () => handleDisplays({ count: screen.getAllDisplays().length }, 'display-added'));
    screen.on('display-removed', () => handleDisplays({ count: screen.getAllDisplays().length }, 'display-removed'));
    screen.on('display-metrics-changed', () => broadcastShellStatus());

    state.statusTimer = setInterval(broadcastShellStatus, 1000);
    if (state.statusTimer.unref) state.statusTimer.unref();
  });
}

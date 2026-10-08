'use strict';
/**
 * РАЗВЕДКА ИСТОЧНИКОВ LMS — отдельная точка входа, не экзамен.
 *
 * ЗАЧЕМ ЭТО СУЩЕСТВУЕТ. Профиль экзамена задаёт белый список источников, и при
 * внедрении в вузе выясняется, что списка не знает никто: Moodle тянет свой
 * домен, но рядом оказываются CDN, шрифты, видеосервер, счётчик и иногда
 * поставщик единого входа. Список, написанный руками, означает сорванный
 * экзамен на незагрузившемся скрипте. Список, написанный с запасом («разрешим
 * edu.kz»), открывает половину интернета. Этот инструмент открывает LMS,
 * СМОТРИТ, что она реально запрашивает, и предлагает черновик.
 *
 * ЧЕМ ОН НЕ ЯВЛЯЕТСЯ, и это главное:
 *
 *  - Он НЕ экзамен. Режим kiosk не включается, блокировки не ставятся,
 *    сочетания клавиш не перехватываются, окно обычное. За машиной сидит
 *    человек, который настраивает систему, и мешать ему нечем.
 *  - Фильтр здесь НИЧЕГО НЕ БЛОКИРУЕТ. Единственное его действие — запись.
 *    Этого достаточно: на выходе нужен список, а не защита.
 *  - Он НЕ ПРИМЕНЯЕТ собранный профиль. Файл называется черновиком, печатается
 *    на экран и сохраняется рядом; применяет его человек, вручную, своим
 *    `--exam-profile`. Инструмент, который сам себе выписывает белый список,
 *    был бы ровно той дырой, от которой профиль защищает: правила экзамена
 *    обязаны быть решением проктора, а не выводом программы.
 *
 * ОТКУДА БЕРУТСЯ ПРАВИЛА. Разбор записей, опознание поисковиков, класс «зона
 * целиком» — из shell/state.js, то есть из того самого слоя, который работает
 * на экзамене. Советы человеку — из tools/discover/classify.js. Ответ на
 * вопрос «а ядро это примет?» спрашивается у САМОГО ядра отдельным процессом
 * (tools/discover/core_check.py), а не угадывается второй копией таблиц.
 *
 * Запуск:
 *   npx electron tools/discover/main.js --url https://lms.vuz.edu.kz/login/index.php
 *   scripts/discover-origins.command https://lms.vuz.edu.kz/login/index.php
 */

const path = require('path');
const fs = require('fs');
const { spawnSync } = require('child_process');
const { app, BrowserWindow, BrowserView, Menu, ipcMain, session, shell, net } = require('electron');

const ROOT = path.join(__dirname, '..', '..');
const {
  foldObservations, buildDraft, displayUrl, whyLines, hostOf,
  DRAFT_SENTINEL, INCOMPLETE_WARNING,
} = require(path.join(__dirname, 'classify.js'));

// ---------------------------------------------------------------- аргументы
function argValue(name, fallback) {
  const argv = process.argv.slice(1);
  for (let i = 0; i < argv.length; i += 1) {
    const token = String(argv[i]);
    if (token === `--${name}`) return argv[i + 1] === undefined ? fallback : String(argv[i + 1]);
    if (token.startsWith(`--${name}=`)) return token.slice(name.length + 3);
  }
  return fallback;
}
function argFlag(name) {
  return process.argv.slice(1).some((t) => String(t) === `--${name}`);
}
function argInt(name, fallback) {
  const n = Number(argValue(name, ''));
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : fallback;
}

const CLI = {
  url: String(argValue('url', '') || '').trim(),
  out: String(argValue('out', path.join(ROOT, 'exam-profile.draft.json'))),
  institution: String(argValue('institution', '')),
  examId: String(argValue('exam-id', '')),
  // Автоматический прогон: открыть, подождать, сохранить, выйти. Нужен для
  // проверки на живой LMS без человека за машиной — и с жёстким потолком по
  // времени, чтобы процесс не остался висеть.
  seconds: argInt('seconds', 0),
  markAfter: argInt('mark-after', 0),
  hidden: argFlag('hidden'),
  help: argFlag('help') || argFlag('h'),
};

const HELP = `
Разведка источников LMS — собрать черновик белого списка по фактическим запросам.

  --url ADDR          адрес страницы входа LMS (обязателен)
  --out PATH          куда сохранить черновик (по умолчанию exam-profile.draft.json)
  --institution NAME  название вуза для черновика
  --exam-id CODE      код экзамена для черновика
  --seconds N         автоматический прогон: сохранить и выйти через N секунд
  --mark-after N      автоматически отметить «вход завершён» через N секунд
  --hidden            не показывать окно (только для автоматического прогона)

Это инструмент НАСТРОЙКИ, а не экзамен: kiosk и блокировки не включаются,
фильтр ничего не блокирует, собранный профиль НЕ применяется.
`;

// --------------------------------------------------------------- наблюдение
/** Все зафиксированные запросы. Ни одного решения по ним не принимается. */
const records = [];
/** Фаза: `auth` до отметки «вход завершён», `exam` после. */
let phase = 'auth';
let phaseMarkedAt = 0;
/** Текущий адрес по каждому webContents — чтобы знать, КТО запросил ресурс. */
const pageByWc = new Map();
let win = null;
let view = null;
/** Когда начался прогон — чтобы в файле стояла длительность, а не «неизвестно». */
const startedAt = Date.now();

/*
 * ЧЕГО РАЗВЕДКА НЕ ВИДЕЛА. Три отдельных набора, и все три едут в файл.
 *
 * Ревью нашло на LMS заказчика случай, который и заставил это написать.
 * `curl` по странице входа показывает ссылки на aitu.oes.kz и moodle.com —
 * живой прогон не увидел ни одной, потому что `onBeforeRequest` фиксирует
 * только ЗАПРОШЕННОЕ, а по `<a href>` никто не щёлкал. Хуже того:
 * ps1-dev.oes.kz/inject.php — собственный скрипт прокторинга вуза, и он
 * СПЯЩИЙ. На странице входа не делает ничего, просыпается по `#isExam` или
 * по postMessage, и тогда уходит на aitu.oes.kz и подключает
 * ajax.googleapis.com. Профиль, собранный «по странице входа», заблокировал
 * бы собственный прокторинг заказчика.
 *
 * Поэтому мы вытаскиваем из страницы то, что она ПРЕДЛАГАЕТ (`offeredHosts`),
 * и то, что ЛЕЖИТ В ТЕКСТЕ её скриптов (`scriptHosts`). Ни то, ни другое НЕ
 * попадает в белый список: это список «куда студент пойдёт, а фильтр его не
 * пустит», и читать его обязан человек.
 */
const offeredHosts = new Set();
const scriptHosts = new Set();
const pagesVisited = new Set();

/*
 * ПОЛНЫЕ адреса скриптов — ТОЛЬКО В ПАМЯТИ ЭТОГО ПРОЦЕССА.
 *
 * В `records` (а значит и в файл, и на экран) адрес уезжает без строки
 * запроса: там живут `sesskey` Moodle и одноразовые коды поставщика входа.
 * Это правильно и остаётся как есть. Но для чтения ТЕЛА скрипта строка
 * запроса обязательна, и вот живое доказательство с LMS заказчика:
 *
 *   GET https://ps1-dev.oes.kz/inject.php                        -> 4791 байт,
 *       ни одного адреса внутри
 *   GET https://ps1-dev.oes.kz/inject.php?site2=lms.astanait.edu.kz -> 19338 байт,
 *       внутри aitu.oes.kz и ajax.googleapis.com
 *
 * То есть по обрезанному адресу спящий скрипт прокторинга выдаёт заглушку, и
 * оба источника, нужные настоящему экзамену, остаются невидимыми. Поэтому
 * полный адрес держится здесь, в оперативной памяти, НИКУДА не записывается и
 * НИГДЕ не печатается: в вывод идут только найденные имена хостов.
 */
const scriptFullUrls = new Set();
const SCRIPT_URLS_KEPT = 300;
/** Адреса скриптов, которые уже сканировали, — чтобы не тянуть дважды. */
const scriptsScanned = new Set();
let scriptScanNote = '';
/** Сколько тел скриптов читать за прогон. Потолок, чтобы не ползать вечно. */
const SCRIPT_SCAN_LIMIT = 40;
const SCRIPT_SCAN_BYTES = 400 * 1024;
/*
 * Сколько запросов было записано на момент последнего сохранения. Не «сохраняли
 * или нет»: человек часто жмёт Cmd+S после входа, потом идёт дальше по курсу и
 * закрывает окно — и вторая половина картины пропадала бы, потому что «уже
 * сохранено». Второй проход по LMS — это ещё один живой вход под чьей-то
 * учётной записью, и терять собранное из-за флага нельзя.
 */
let savedAtCount = -1;

const OBSERVE_PARTITION = 'discover-observe';

function log(line) {
  process.stdout.write(`${line}\n`);
}

/**
 * Запись одного запроса.
 *
 * Полный адрес с запросом НЕ сохраняется НИКОГДА: в строке запроса Moodle
 * носит `sesskey`, а поставщики входа — одноразовые коды. Инструмент печатает
 * свой вывод на экран и в файл, и живой токен сеанса в этом файле превратил бы
 * средство настройки в утечку. Для белого списка нужен origin, и он остаётся.
 */
function remember(details) {
  const wcId = details && details.webContentsId;
  const page = String((details && details.referrer) || '')
    || String((wcId !== undefined && pageByWc.get(wcId)) || '');
  const rawUrl = String((details && details.url) || '');
  const rawType = String((details && details.resourceType) || 'other');
  // Полный адрес скрипта — в память, не в запись. Зачем именно полный,
  // объяснено у объявления scriptFullUrls.
  if (rawType === 'script' && /^https?:/i.test(rawUrl)
      && scriptFullUrls.size < SCRIPT_URLS_KEPT) {
    scriptFullUrls.add(rawUrl);
  }
  records.push({
    url: displayUrl(details && details.url),
    resourceType: String((details && details.resourceType) || 'other'),
    page: displayUrl(page),
    phase,
    at: Date.now(),
  });
}

function attachObserver(ses) {
  // ЕДИНСТВЕННОЕ отличие от фильтра экзамена: `cancel: false` стоит здесь
  // безусловно и ветки «заблокировать» в этой функции нет вовсе. Режим
  // наблюдения не должен уметь блокировать даже по ошибке.
  ses.webRequest.onBeforeRequest({ urls: ['*://*/*'] }, (details, callback) => {
    try { remember(details); } catch (err) { /* запись не должна ломать страницу */ }
    callback({ cancel: false });
  });
}

// ------------------------------------------------- чего разведка не видела
/**
 * Спросить у ОТКРЫТОЙ страницы, куда она предлагает пойти студенту.
 *
 * Читаются только адреса: `<a href>`, `action` у форм и `src` у встроенных
 * кадров. Ни текст страницы, ни значения полей, ни cookie не трогаются — для
 * белого списка нужны имена хостов, и больше ничего брать нельзя: человек,
 * возможно, уже вошёл под своей учётной записью.
 */
const COLLECT_LINKS_JS = `(function () {
  var out = [];
  function add(v) { if (v) { try { out.push(new URL(v, location.href).href); } catch (e) {} } }
  var i, nodes;
  nodes = document.querySelectorAll('a[href]');
  for (i = 0; i < nodes.length && i < 2000; i += 1) add(nodes[i].getAttribute('href'));
  nodes = document.querySelectorAll('form[action]');
  for (i = 0; i < nodes.length && i < 200; i += 1) add(nodes[i].getAttribute('action'));
  nodes = document.querySelectorAll('iframe[src],frame[src],embed[src],object[data]');
  for (i = 0; i < nodes.length && i < 200; i += 1) {
    add(nodes[i].getAttribute('src') || nodes[i].getAttribute('data'));
  }
  return out;
})()`;

function collectOffered(wc) {
  if (!wc || wc.isDestroyed()) return;
  wc.executeJavaScript(COLLECT_LINKS_JS, true).then((list) => {
    for (const raw of (Array.isArray(list) ? list : [])) {
      const host = hostOf(raw);
      if (host) offeredHosts.add(host);
    }
  }).catch(() => { /* страница могла уйти из-под нас — это не ошибка прогона */ });
}

/** Прочитать тело по адресу тем же наблюдаемым сеансом. Возвращает текст. */
function fetchText(url) {
  return new Promise((resolve) => {
    let request = null;
    try {
      request = net.request({ url, session: session.fromPartition(OBSERVE_PARTITION) });
    } catch (err) {
      resolve(''); return;
    }
    const chunks = [];
    let size = 0;
    let done = false;
    const finish = (text) => { if (!done) { done = true; resolve(text); } };
    const timer = setTimeout(() => {
      try { request.abort(); } catch (err) { /* уже закрыт */ }
      finish('');
    }, 6000);
    request.on('response', (response) => {
      response.on('data', (chunk) => {
        size += chunk.length;
        if (size <= SCRIPT_SCAN_BYTES) chunks.push(chunk);
      });
      response.on('end', () => { clearTimeout(timer); finish(Buffer.concat(chunks).toString('utf8')); });
      response.on('error', () => { clearTimeout(timer); finish(''); });
    });
    request.on('error', () => { clearTimeout(timer); finish(''); });
    try { request.end(); } catch (err) { clearTimeout(timer); finish(''); }
  });
}

/**
 * Прочитать тела скриптов страницы и вытащить из них ИМЕНА ХОСТОВ.
 *
 * Зачем вообще читать текст скрипта, если есть запросы: потому что спящий
 * скрипт запросов не делает. Вот живой случай с LMS заказчика — строки из
 * inject.php, который на странице входа молчит:
 *
 *   script.src = 'https://ajax.googleapis.com/ajax/libs/jquery/3.6.0/jquery.min.js';
 *   window.location.href = "https://aitu.oes.kz/internal_assignment?...";
 *
 * Оба хоста нужны НАСТОЯЩЕМУ экзамену и не видны разведке со страницы входа
 * ни одним запросом. Сканирование текста находит их за секунду.
 *
 * Что здесь важно НЕ делать: ни один найденный хост не попадает в белый
 * список. Текст скрипта — это упоминание, а не запрос; из `"https://" + host`
 * вытащится и адрес из комментария, и пример из документации. Поэтому находки
 * печатаются отдельным списком «названо в скриптах и ни разу не запрошено».
 */
/**
 * Отсев мусора из найденного в тексте скрипта.
 *
 * Минифицированный код полон склеек вида `"https://"+a+"/"+b`, и регулярное
 * выражение честно вытаскивает из них «хосты» `a` и `b`. Такие находки это не
 * адреса, а следы разбора, и держать их в списке «чего мы не видели» значило
 * бы прятать настоящие находки в шуме. Правило простое и структурное: в имени
 * обязана быть точка, последняя метка — буквы длиной не меньше двух.
 *
 * Чего здесь СОЗНАТЕЛЬНО НЕТ — списка «скучных» хостов. В текст скриптов
 * попадают w3.org, gnu.org и ссылки на документацию из лицензионных шапок, и
 * в выводе они будут. Это цена честности: стоит начать вычёркивать «заведомо
 * безобидные» имена, и первым же вычеркнутым окажется тот самый адрес, из-за
 * которого весь раздел и написан.
 */
function plausibleHost(host) {
  const h = String(host || '').toLowerCase();
  if (h.indexOf('.') === -1) return false;
  const labels = h.split('.');
  const tld = labels[labels.length - 1];
  if (!/^[a-z]{2,24}$/.test(tld)) return false;
  return labels.every((l) => /^[a-z0-9_-]{1,63}$/.test(l));
}

async function scanScripts() {
  const urls = [];
  for (const url of scriptFullUrls) {
    if (scriptsScanned.has(url)) continue;
    urls.push(url);
    if (urls.length >= SCRIPT_SCAN_LIMIT) break;
  }
  if (!urls.length) {
    scriptScanNote = scriptsScanned.size
      ? `прочитано тел скриптов: ${scriptsScanned.size}; новых с прошлого раза нет`
      : 'ни одного скрипта в прогоне не зафиксировано — читать было нечего';
    return;
  }
  let read = 0;
  for (const url of urls) {
    scriptsScanned.add(url);
    // eslint-disable-next-line no-await-in-loop — последовательно и нарочно:
    // инструмент настройки не должен устраивать залп по серверу вуза.
    const text = await fetchText(url);
    if (!text) continue;
    read += 1;
    const found = text.match(/https?:\/\/[^\s'"`<>()\\]{3,200}/gi) || [];
    for (const raw of found.slice(0, 2000)) {
      const host = hostOf(raw);
      if (host && plausibleHost(host)) scriptHosts.add(host);
    }
  }
  scriptScanNote = `прочитано тел скриптов: ${read} из ${urls.length} зафиксированных`
    + (urls.length >= SCRIPT_SCAN_LIMIT ? ` (потолок ${SCRIPT_SCAN_LIMIT} за прогон)` : '');
}

/** Сырые счётчики прогона для раздела «чего не видели». */
function coverageRaw() {
  return {
    seconds: (Date.now() - startedAt) / 1000,
    requests: records.length,
    pages: Array.from(pagesVisited),
    phaseMarked: Boolean(phaseMarkedAt),
    offeredHosts: Array.from(offeredHosts),
    scriptHosts: Array.from(scriptHosts),
    scriptsScanned: scriptsScanned.size,
    scriptScanNote,
  };
}

// ------------------------------------------------------------------- сверка
function pythonPath() {
  const fromEnv = String(process.env.PROCTOR_PYTHON || '').trim();
  if (fromEnv) return fromEnv;
  const venv = path.join(ROOT, '.venv', 'bin', 'python3');
  if (fs.existsSync(venv)) return venv;
  return 'python3';
}

/**
 * Спросить у ЯДРА, что оно думает о записях. Возвращает карту или null, если
 * спросить не удалось.
 *
 * Молчать про неудачу нельзя: «сверка с ядром не выполнена» и «ядро всё
 * приняло» — разные утверждения, и путать их в выводе означало бы обещать
 * проверку, которой не было.
 */
function askCore(origins) {
  if (!origins.length) return {};
  const script = path.join(__dirname, 'core_check.py');
  let res;
  try {
    res = spawnSync(pythonPath(), [script], {
      input: JSON.stringify(origins),
      encoding: 'utf8',
      timeout: 20000,
    });
  } catch (err) {
    return null;
  }
  if (!res || res.status !== 0 || !res.stdout) return null;
  let parsed = null;
  try { parsed = JSON.parse(res.stdout); } catch (err) { parsed = null; }
  if (!parsed || parsed.__error__) return null;
  return parsed;
}

// -------------------------------------------------------------------- вывод
function hr(ch) {
  return String(ch || '-').repeat(78);
}

/**
 * Печатный разбор. Порядок разделов здесь — не оформление, а содержание.
 *
 * ПЕРВЫМИ строками идут две вещи: «собрано только то, что вы прошли руками» и
 * «применять нельзя, пока не прочитал человек». Ревью нашло, почему это
 * обязано быть сверху, а не в конце: уставший админ ничего не копирует, он
 * просто подставляет файл. Предупреждение, живущее в пункте 4 из пяти, до
 * него не доходит.
 *
 * ВТОРЫМ разделом — чего разведка НЕ ВИДЕЛА. Это ответ на вопрос «насколько
 * список полон», и он важнее самого списка: неполный список, выданный за
 * полный, срывает экзамен и ставит ложный инцидент в журнал улик.
 */
function printReport(folded, built, core, outPath, backup) {
  const lines = [];
  const push = (s) => lines.push(s === undefined ? '' : s);
  const wrap = (text, indent) => {
    const pad = indent === undefined ? '    ' : indent;
    const words = String(text || '').split(/\s+/).filter(Boolean);
    let cur = pad;
    for (const word of words) {
      if ((`${cur} ${word}`).length > 78) { push(cur); cur = pad; }
      cur += (cur === pad ? '' : ' ') + word;
    }
    if (cur.trim()) push(cur);
  };
  const cov = built.draft.discovery.coverage;
  const merge = built.draft.discovery.merge || {};
  const prevRun = built.draft.discovery.previous_run || {};

  // --- 1. ПЕРВАЯ СТРОКА ВЫВОДА: неполнота и требование человека -----------
  push('');
  push(hr('!'));
  push('  ЭТО ЧЕРНОВИК. ПРИМЕНЯТЬ ЕГО В ТАКОМ ВИДЕ НЕЛЬЗЯ.');
  push(hr('!'));
  for (const line of INCOMPLETE_WARNING) push(`  ${line}`);
  push(hr('!'));
  push('');

  push(hr('='));
  push('РАЗВЕДКА ИСТОЧНИКОВ LMS — ЧТО СОБРАНО');
  push(hr('='));
  push(`адрес LMS:          ${CLI.url}`);
  push(`хост LMS:           ${folded.examHost || '—'}`);
  push(`прогон длился:      ${cov.seconds_observed} с`);
  push(`запросов записано:  ${records.length}`);
  push(`источников найдено: ${folded.sources.length}`);
  push(`страниц открыто:    ${cov.pages_visited_count}`);
  push(`типы ресурсов:      ${cov.resource_types_seen.join(', ') || '—'}`);
  push(`отметка «вход завершён»: ${phaseMarkedAt
    ? `поставлена, после неё записано ${records.filter((r) => r.phase === 'exam').length} запросов`
    : 'НЕ ставилась — все источники отнесены к фазе входа'}`);
  push('');

  // --- 2. ЧЕГО РАЗВЕДКА НЕ ВИДЕЛА -----------------------------------------
  push(hr('='));
  push('ЧЕГО РАЗВЕДКА НЕ ВИДЕЛА — ЧИТАТЬ ДО СПИСКА, А НЕ ПОСЛЕ');
  push(hr('='));
  if (!cov.student_path_not_walked.length) {
    push('  следы всех основных типов ресурсов в прогоне есть');
  }
  for (const item of cov.student_path_not_walked) {
    push('');
    wrap(`* ${item}`, '  ');
  }
  if (cov.offered_but_not_visited.length) {
    push('');
    wrap('СТРАНИЦА САМА ПРЕДЛАГАЕТ СТУДЕНТУ эти адреса (ссылки, формы, кадры), '
      + 'а разведка по ним не ходила — значит их нет и в списке, а студент '
      + 'по ним пойдёт:', '  ');
    for (const item of cov.offered_but_not_visited) {
      push(`    ${item.host}   [${item.class}] ${item.title}`);
    }
  }
  if (cov.mentioned_in_scripts_never_requested.length) {
    push('');
    wrap('НАЙДЕНО В ТЕКСТЕ СКРИПТОВ СТРАНИЦЫ и ни разу не запрошено. Так '
      + 'выглядит СПЯЩИЙ скрипт: на странице входа он молчит, а на старте '
      + 'экзамена уходит на свой адрес — и упирается в белый список:', '  ');
    for (const item of cov.mentioned_in_scripts_never_requested) {
      push(`    ${item.host}   [${item.class}] ${item.title}`);
    }
    push('');
    wrap(cov.mentioned_in_scripts_note, '    ');
  }
  push('');
  push(`  ${cov.scripts_scan_note || 'тела скриптов не читались'}`);
  if (prevRun.had_previous) {
    push('');
    if (prevRun.poorer) {
      push(hr('!'));
      wrap(`ВНИМАНИЕ: ${prevRun.verdict}`, '  ');
      for (const line of (prevRun.lines || [])) push(`    ${line}`);
      push(hr('!'));
    } else {
      push(`  сравнение с прошлым прогоном: ${prevRun.verdict}`);
    }
  }
  push('');

  // --- 3. ЧТО ПОПАЛО В СПИСОК ---------------------------------------------
  push(hr('='));
  push(`ПОПАЛИ В ЧЕРНОВИК (${built.draft.allowed_origins.length})`);
  push(hr('='));
  wrap('Правило инструмента: НЕ ОПОЗНАЛ — НЕ ВКЛЮЧИЛ. Здесь только адрес '
    + 'экзамена, соседи по домену вуза и CDN, на который нельзя положить свой '
    + 'файл. Всё прочее — ниже, в кандидатах.', '  ');
  push('');
  if (!built.draft.allowed_origins.length) push('  — ни одного —');
  for (const origin of built.draft.allowed_origins) {
    const s = built.included.find((x) => x.key === origin);
    push('');
    push(`  ${origin}${s && s.insecure ? '   [http, без шифрования]' : ''}`);
    if (!s) {
      push('    из прошлого прогона (в этом прогоне не запрашивался)');
      continue;
    }
    push(`    ${s.verdict.title}`);
    if (s.verdict.why) push(`    ${s.verdict.why}`);
    push(`    запросов: ${s.requests}; типы: ${s.resourceTypes.join(', ')}`);
    push(`    фаза: ${s.phases.map((p) => (p === 'auth' ? 'вход' : 'экзамен')).join(' и ')}`);
    for (const w of whyLines(s)) push(`    ${w}`);
    if (s.verdict.advice) wrap(s.verdict.advice);
  }
  push('');

  // --- 4. КАНДИДАТЫ: запрошено, но НЕ включено ----------------------------
  push(hr('='));
  push(`КАНДИДАТЫ: СТРАНИЦА ЭТО ЗАПРАШИВАЛА, А МЫ НЕ ВКЛЮЧИЛИ (${built.excluded.length})`);
  push(hr('='));
  wrap('Это НЕ отказ, а решение, которое принимает человек. Если без записи у '
    + 'студента ломается страница — впишите строку руками, прочитав, что '
    + 'именно через неё открывается. Строка для вставки готова у каждого '
    + 'кандидата.', '  ');
  push('');
  if (!built.excluded.length) push('  — ни одного —');
  for (const s of built.excluded) {
    push('');
    push(`  ${s.key}${s.insecure ? '   [http, без шифрования]' : ''}`);
    push(`    ${s.verdict.title}`);
    if (s.verdict.why) push(`    ${s.verdict.why}`);
    push(`    запросов: ${s.requests}; типы: ${s.resourceTypes.join(', ')}`);
    for (const w of whyLines(s)) push(`    ${w}`);
    if (s.verdict.advice) wrap(s.verdict.advice);
    const paste = (built.draft.discovery.candidates
      .find((c) => c.origin === s.key) || {}).paste_line;
    if (paste) push(`    строка для вставки в allowed_origins: ${paste.trim()}`);
  }
  push('');

  // --- 5. Сверка с ядром ---------------------------------------------------
  // Отдельным разделом, потому что это другой вопрос: не «что советует
  // инструмент», а «что из этого вообще подействует». И тут обязательна
  // оговорка: «ядро примет» значит «запись подействует», а не «источник
  // безопасен». Ядро знает три класса отказа, всё прочее для него `ok`.
  push(hr('='));
  push('СВЕРКА С КЛАССИФИКАЦИЕЙ ЯДРА (sidecar/config.py)');
  push(hr('='));
  if (core === null) {
    push('  НЕ ВЫПОЛНЕНА: ядро не удалось спросить (нет python или .venv).');
    push('  Это не значит «ядро всё приняло». Перед экзаменом прогоните');
    push('  профиль через сайдкар и посмотрите dropped_origins.');
  } else {
    const rows = Object.keys(core).sort();
    if (!rows.length) push('  нечего сверять: черновик пуст');
    for (const key of rows) {
      const info = core[key] || {};
      const ok = info.class === 'ok' || info.class === 'local';
      push(`  ${ok ? 'примет ' : 'ОТКЛОНИТ'}  ${key}  [класс ${info.class}]`);
      if (!ok && info.reason) push(`             ${info.reason}`);
    }
  }
  push('');
  wrap('ГРАНИЦА ЭТОЙ СВЕРКИ: «примет» означает «запись подействует», а НЕ '
    + '«через неё нельзя списать». Ядро знает три класса отказа — поисковик, '
    + 'ИИ-ассистент, зона целиком; любой другой хост для него «ok», включая '
    + 'файлообменник и чат с живым оператором. Сверка защищает от мёртвой '
    + 'записи, а не от канала связи.', '  ');
  push('');

  // --- 6. Накопление -------------------------------------------------------
  if (merge.had_previous) {
    push(hr('='));
    push('НАКОПЛЕНИЕ: ПРОШЛЫЙ ЧЕРНОВИК НЕ ЗАТЁРТ, А ДОПОЛНЕН');
    push(hr('='));
    push(`  добавлено этим прогоном: ${merge.added_now.length
      ? merge.added_now.join(', ') : '— ничего нового —'}`);
    push(`  сохранено из прошлого:   ${merge.kept_from_previous.length
      ? merge.kept_from_previous.join(', ') : '—'}`);
    if (merge.removed_by_human_kept_out.length) {
      wrap(`ВЫЧЕРКНУТОЕ ВАМИ НЕ ВОЗВРАЩЕНО (и не вернётся в следующих `
        + `прогонах): ${merge.removed_by_human_kept_out.join(', ')}`, '  ');
    }
    if (merge.human_fields_kept.length) {
      push(`  ваши поля сохранены: ${merge.human_fields_kept.join(', ')}`);
    }
    if (backup) push(`  прошлый файл отложен копией: ${backup}`);
    push('');
  }

  // --- 7. Что дальше -------------------------------------------------------
  push(hr('='));
  push('ЧТО ДАЛЬШЕ — ЭТО ДЕЛАЕТ ЧЕЛОВЕК, А НЕ ИНСТРУМЕНТ');
  push(hr('='));
  push(`  1. Черновик сохранён: ${outPath}`);
  wrap('Он НЕ ПРИМЕНЁН и применён быть не может: в полях проктора стоят '
    + `заглушки, а в notes — метка ${DRAFT_SENTINEL}. Сайдкар находит и то, и `
    + 'другое и пишет в шапку отчёта «экзамен не готов».', '     ');
  push('  2. Прочитайте каждую строку «нужен потому что» и вычеркните то, без');
  push('     чего экзамен обойдётся. Вычеркнутое инструмент больше не вернёт.');
  push('  3. Пройдите раздел КАНДИДАТЫ. Если без источника ломается страница —');
  push('     впишите готовую строку, прочитав, что именно он открывает.');
  push('  4. Заполните institution, exam_id, issued_by, issued_at и уберите');
  push(`     метку ${DRAFT_SENTINEL} из notes: это утверждения проктора,`);
  push('     и инструмент не вправе их выдумывать.');
  push('  5. Пройдите путь студента ЕЩЁ РАЗ (курс, тест, загрузка файла) тем');
  push('     же --out: прогоны НАКАПЛИВАЮТСЯ, прошлый файл не затирается, а');
  push('     ваши правки сохраняются. Страница входа показывает далеко не всё.');
  push('  6. Применяйте файлом: --exam-profile <путь>. Хеш в отчёт попадёт');
  push('     от того файла, который видел проктор.');
  const review = built.draft.discovery.review_required;
  if (review.length) {
    push('');
    wrap(`ПРОВЕРИТЬ ГЛАЗАМИ обязательно ${review.length} записей: `
      + `${review.join(', ')}. Инструмент включил их по правилу «свой домен или `
      + 'CDN с неизменяемым содержимым», но что на них лежит у вашего вуза, '
      + 'знаете только вы.', '  ');
  }
  const sso = built.excluded.filter((s) => s.verdict.kind === 'sso');
  if (sso.length) {
    push('');
    push(hr('!'));
    push('  ЕДИНЫЙ ВХОД НАЙДЕН И В ЧЕРНОВИК НЕ ВКЛЮЧЁН СОЗНАТЕЛЬНО:');
    for (const s of sso) push(`    ${s.key} — ${s.verdict.provider || 'поставщик входа'}`);
    push('  Разрешив такой источник, вы открываете не «вход», а весь аккаунт');
    push('  студента: почту, чат и файлы. Правильный путь — вход в LMS ДО');
    push('  старта экзамена, тогда поставщик в белый список не попадает вовсе.');
    // Самый неприятный и самый частый случай: вход в LMS вообще не работает
    // без поставщика. Тогда «вычеркнуть и забыть» не вариант, и сказать это
    // надо здесь же, а не оставлять человеку додумывать.
    const ssoOnlyAuth = sso.every((s) => s.phases.length === 1 && s.phases[0] === 'auth');
    const ssoNavigated = sso.some((s) => s.classes.indexOf('navigation') !== -1);
    if (ssoNavigated) {
      push('');
      push('  И ОТДЕЛЬНО: вход через поставщика здесь НЕ ДОБРОВОЛЬНЫЙ — страница');
      push('  входа LMS сама уводит на него навигацией. Значит выбор тут');
      push('  не «включать или нет», а из двух:');
      push('    1) студент входит в LMS ДО старта экзамена, профиль поставщика');
      push('       не содержит вовсе — на экзамене живёт только хост LMS;');
      push('    2) поставщик в белом списке, и вуз вслух соглашается, что');
      push('       почта, чат и файлы студента открыты всё время экзамена.');
      push('  Третьего варианта нет, и решать это обязан человек.');
      if (ssoOnlyAuth) {
        push('  Разведка подтверждает: поставщик запрашивался ТОЛЬКО до отметки');
        push('  «вход завершён» — то есть вариант 1 технически проходит.');
      }
    }
    push(hr('!'));
  }
  push('');
  push('  Фильтр этого инструмента НИЧЕГО не блокировал: режим наблюдения.');
  push('  Профиль — улика о заявленных правилах, а не защита: он применяется');
  push('  процессом на машине студента.');
  push('');

  const text = lines.join('\n');
  log(text);
  return text;
}

// ------------------------------------------------------------------ сохранение
/**
 * Прочитать прошлый черновик, если он есть. Возвращает объект или null.
 *
 * Нужно для НАКОПЛЕНИЯ. Прежняя версия писала файл безусловно
 * (`fs.writeFileSync` без единой проверки), и второй прогон молча уничтожал
 * первый — вместе с вычеркнутыми руками строками, заполненным `issued_by` и
 * датой выдачи. При этом сам инструмент пунктом 4 своего вывода советовал
 * «пройдите путь студента ещё раз», то есть прямым текстом велел сделать то,
 * что затирало результат. А `window-all-closed` сохранял ещё и сам, без
 * спроса: достаточно было открыть окно и закрыть его.
 */
function readPrevious(outPath) {
  try {
    if (!fs.existsSync(outPath)) return null;
    const raw = JSON.parse(fs.readFileSync(outPath, 'utf8'));
    return (raw && typeof raw === 'object' && !Array.isArray(raw)) ? raw : null;
  } catch (err) {
    log(`прошлый черновик ${outPath} не разбирается (${String((err && err.message) || err)}):`);
    log('  он будет СОХРАНЁН РЯДОМ как резервная копия, а накопление начнётся заново');
    return null;
  }
}

/** Отложить прошлый файл в сторону перед записью. Возвращает путь копии. */
function backupPrevious(outPath) {
  try {
    if (!fs.existsSync(outPath)) return '';
    const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..+$/, '');
    const copy = `${outPath.replace(/\.json$/i, '')}.${stamp}.bak.json`;
    fs.copyFileSync(outPath, copy);
    return copy;
  } catch (err) {
    return '';
  }
}

async function saveDraft(reason) {
  // Тела скриптов читаются ПЕРЕД сборкой черновика: находки из спящих
  // скриптов обязаны попасть в раздел «чего мы не видели» этого же файла.
  try { await scanScripts(); } catch (err) {
    scriptScanNote = `скан тел скриптов НЕ выполнен: ${String((err && err.message) || err)}`;
  }
  const outPath = path.resolve(CLI.out);
  const previous = readPrevious(outPath);

  const folded = foldObservations(records, { examUrl: CLI.url });
  const built = buildDraft(folded, {
    examUrl: CLI.url,
    institution: CLI.institution,
    examId: CLI.examId,
    coverage: coverageRaw(),
    previous,
  });

  // Сверка с ядром. Всё, что ядро отклонит, из черновика УХОДИТ: инструмент не
  // имеет права предлагать запись, которая на экзамене молча не подействует.
  const core = askCore(built.draft.allowed_origins.slice());
  if (core && typeof core === 'object') {
    const kept = [];
    for (const origin of built.draft.allowed_origins) {
      const info = core[origin] || {};
      if (info.class === 'ok' || info.class === 'local') { kept.push(origin); continue; }
      const source = built.included.find((s) => s.key === origin);
      built.draft.discovery.candidates.push({
        origin,
        class: `ядро: ${info.class || 'неизвестно'}`,
        title: 'ВЫБРОШЕНО СВЕРКОЙ С ЯДРОМ: эта запись не подействовала бы',
        reason: String(info.reason || 'ядро относит запись к классу, который не '
          + 'попадает в действующий белый список'),
        why: source ? whyLines(source) : [],
      });
      if (source) {
        source.verdict = Object.assign({}, source.verdict, {
          include: false,
          title: `${source.verdict.title} — НО ЯДРО ЭТУ ЗАПИСЬ ОТКЛОНИТ`,
          advice: String(info.reason || ''),
        });
      }
    }
    built.draft.allowed_origins = kept;
    const keptSet = new Set(kept);
    const d = built.draft.discovery;
    d.auth_origins = d.auth_origins.filter((o) => keptSet.has(o));
    d.exam_origins = d.exam_origins.filter((o) => keptSet.has(o));
    d.review_required = d.review_required.filter((o) => keptSet.has(o));
    built.included = built.included.filter((s) => keptSet.has(s.key));
    built.excluded = folded.sources.filter((s) => !keptSet.has(s.key));
  }
  built.draft.discovery.core_cross_check = core === null
    ? 'НЕ ВЫПОЛНЕНА: ядро не удалось спросить. Это не значит «ядро всё приняло»'
    : 'выполнена вызовом classify_origin() из sidecar/config.py';

  // Прошлый файл откладывается в сторону ВСЕГДА, а не только когда «что-то
  // пошло не так»: слияние — тоже изменение, и человек должен иметь возможность
  // вернуться к тому, что он вычитывал полчаса назад.
  const backup = backupPrevious(outPath);
  built.draft.discovery.previous_file_backup = backup
    ? path.basename(backup)
    : (previous ? 'резервную копию сделать не удалось' : 'прошлого файла не было');

  let writeError = '';
  try {
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    fs.writeFileSync(outPath, `${JSON.stringify(built.draft, null, 2)}\n`, 'utf8');
  } catch (err) {
    writeError = String((err && err.message) || err);
  }

  const text = printReport(folded, built, core, writeError
    ? `НЕ СОХРАНЁН (${writeError})` : outPath, backup);
  if (!writeError) {
    const reportPath = outPath.replace(/\.json$/i, '') + '.report.txt';
    try { fs.writeFileSync(reportPath, `${text}\n`, 'utf8'); } catch (err) { /* не критично */ }
    log(`  Разбор целиком: ${reportPath}`);
  }
  log(`  (сохранение по причине: ${reason})`);
  savedAtCount = records.length;
  return built;
}

// --------------------------------------------------------------------- окно
// 168, а не 128: сверху добавлена строка «это черновик, применять нельзя».
// Высота полосы и высота страницы в strip.html обязаны совпадать, иначе
// предупреждение обрежется ровно на том месте, где оно нужно.
const STRIP_HEIGHT = 168;

function layout() {
  if (!win || !view) return;
  const [w, h] = win.getContentSize();
  view.setBounds({ x: 0, y: STRIP_HEIGHT, width: w, height: Math.max(0, h - STRIP_HEIGHT) });
}

function pushState() {
  if (!win || win.isDestroyed()) return;
  let current = CLI.url;
  try {
    if (view && view.webContents && !view.webContents.isDestroyed()) {
      current = view.webContents.getURL() || CLI.url;
    }
  } catch (err) { current = CLI.url; }
  const folded = foldObservations(records, { examUrl: CLI.url });
  win.webContents.send('discover:state', {
    phase,
    marked: Boolean(phaseMarkedAt),
    requests: records.length,
    sources: folded.sources.length,
    included: folded.sources.filter((s) => s.verdict.include).length,
    // Кандидаты — то, что страница запрашивала, а инструмент НЕ включил. На
    // полосе это такое же важное число, как «в черновик»: если у студента
    // поедет вёрстка, причина будет здесь.
    candidates: folded.sources.filter((s) => !s.verdict.include).length,
    pages: pagesVisited.size,
    sso: folded.sources.filter((s) => s.verdict.kind === 'sso').map((s) => s.key),
    out: path.resolve(CLI.out),
    url: current,
  });
}

function markPhase() {
  if (phaseMarkedAt) return;
  phase = 'exam';
  phaseMarkedAt = Date.now();
  log('');
  log('>>> ОТМЕТКА «ВХОД ЗАВЕРШЁН». Источники, запрошенные дальше, уйдут в exam_origins.');
  log('');
  pushState();
}

function buildMenu() {
  // Меню нужно только ради горячих клавиш: кнопки есть на полосе сверху.
  const template = [
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
    {
      label: 'Разведка',
      submenu: [
        {
          label: 'Вход завершён (дальше — фаза экзамена)',
          accelerator: 'CmdOrCtrl+Shift+L',
          click: () => markPhase(),
        },
        {
          label: 'Сохранить черновик профиля',
          accelerator: 'CmdOrCtrl+S',
          click: () => { saveDraft('горячая клавиша').catch(() => {}); },
        },
        { type: 'separator' },
        { role: 'reload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    { role: 'editMenu' },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function createWindow() {
  win = new BrowserWindow({
    width: 1360,
    height: 960,
    show: !CLI.hidden,
    title: 'Разведка источников LMS — инструмент настройки, не экзамен',
    // Обычное окно. Ни kiosk, ни fullscreen, ни always-on-top: за машиной
    // человек, и отнимать у него управление здесь незачем.
    webPreferences: {
      preload: path.join(__dirname, 'strip-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  win.loadFile(path.join(__dirname, 'strip.html'));

  const ses = session.fromPartition(OBSERVE_PARTITION);
  // Partition БЕЗ `persist:` — хранилище в памяти. Человек, возможно, войдёт в
  // LMS под своей учётной записью, и оставлять её cookie на диске рядом с
  // инструментом настройки незачем: после выхода сеанс исчезает вместе с
  // процессом.
  attachObserver(ses);

  view = new BrowserView({
    webPreferences: {
      partition: OBSERVE_PARTITION,
      contextIsolation: true,
      nodeIntegration: false,
      // Страница вуза — сторонний сайт. Своего preload ей не даётся, в
      // инструмент она не может ничего сказать.
      preload: undefined,
    },
  });
  win.setBrowserView(view);
  layout();
  win.on('resize', layout);

  const wc = view.webContents;
  wc.setWindowOpenHandler(({ url }) => {
    // Единый вход часто открывается отдельным окном. Для разведки это нужно
    // разрешить — иначе путь входа не проходится, — но окно получает тот же
    // наблюдаемый partition, то есть его запросы тоже попадают в картину.
    log(`окно-подсказка: страница просит открыть ${displayUrl(url)}`);
    return {
      action: 'allow',
      overrideBrowserWindowOptions: {
        width: 900,
        height: 760,
        webPreferences: { partition: OBSERVE_PARTITION, contextIsolation: true },
      },
    };
  });

  // Каждая открытая страница отмечается отдельно: «страниц открыто: 1» — это
  // и есть главный признак, что дальше входа разведка не ходила, и в файле он
  // обязан стоять числом, а не угадываться по списку источников.
  const noteVisit = () => {
    try {
      const url = wc.getURL();
      if (url) pagesVisited.add(displayUrl(url));
    } catch (err) { /* ушла из-под нас */ }
    pushState();
  };
  wc.on('did-navigate', noteVisit);
  wc.on('did-navigate-in-page', noteVisit);
  wc.on('did-finish-load', () => {
    noteVisit();
    // Куда страница ПРЕДЛАГАЕТ пойти студенту — читается только после
    // загрузки и только адресами: ни текста, ни полей, ни cookie.
    collectOffered(wc);
  });
  wc.on('did-fail-load', (_e, code, desc, url) => {
    log(`страница не загрузилась: ${desc} (${code}) — ${displayUrl(url)}`);
    pushState();
  });

  wc.loadURL(CLI.url).catch((err) => {
    log(`не удалось открыть ${CLI.url}: ${String((err && err.message) || err)}`);
  });
}

// Адрес каждого webContents — чтобы в строке «нужен потому что» стояла
// страница, а не пустое место.
app.on('web-contents-created', (_event, contents) => {
  const track = () => {
    try { pageByWc.set(contents.id, contents.getURL()); } catch (err) { /* закрылся */ }
  };
  contents.on('did-start-navigation', track);
  contents.on('did-navigate', track);
  contents.on('did-navigate-in-page', track);
  contents.on('destroyed', () => pageByWc.delete(contents.id));
});

ipcMain.handle('discover:mark', () => { markPhase(); return true; });
ipcMain.handle('discover:save', () => { saveDraft('кнопка в окне').catch(() => {}); return true; });
ipcMain.handle('discover:open-out', () => {
  // Показать файл в Finder, а не открыть его: черновик читают, а не запускают.
  shell.showItemInFolder(path.resolve(CLI.out));
  return true;
});
ipcMain.handle('discover:quit', () => { app.quit(); return true; });

// ------------------------------------------------------------------- запуск
if (CLI.help || !CLI.url) {
  log(HELP);
  if (!CLI.url) log('ОШИБКА: не задан --url. Нечего разведывать.');
  app.quit();
  process.exitCode = CLI.url ? 0 : 2;
} else {
  app.whenReady().then(() => {
    buildMenu();
    createWindow();
    // Предупреждение о неполноте — ПЕРВОЕ, что человек видит, ещё до того,
    // как страница загрузится. Оно же повторяется первым разделом разбора:
    // то, что сказано один раз в конце, прочитано не будет.
    log(hr('!'));
    log('ЭТО ЧЕРНОВИК. ПРИМЕНЯТЬ ЕГО В ТАКОМ ВИДЕ НЕЛЬЗЯ.');
    for (const line of INCOMPLETE_WARNING) log(line);
    log(hr('!'));
    log('РЕЖИМ НАБЛЮДЕНИЯ. Фильтр записывает запросы и НИЧЕГО не блокирует.');
    log(`Открываю ${CLI.url}`);
    log('Пройдите путь студента: страница входа -> курс -> тест.');
    log('«Вход завершён» — кнопка в окне или Cmd+Shift+L.');
    log('«Сохранить черновик» — кнопка в окне или Cmd+S.');
    log(hr('='));

    if (CLI.markAfter) {
      setTimeout(() => markPhase(), CLI.markAfter * 1000);
    }
    if (CLI.seconds) {
      log(`автоматический прогон: сохраню черновик и выйду через ${CLI.seconds} с`);
      setTimeout(() => {
        // Сохранение теперь асинхронное — читаются тела скриптов, — и выход
        // обязан его дождаться: иначе автоматический прогон выдавал бы файл
        // без раздела «чего разведка не видела».
        saveDraft(`автоматический прогон, ${CLI.seconds} с`)
          .catch((err) => log(`сохранение не удалось: ${String((err && err.message) || err)}`))
          .then(() => app.quit());
      }, CLI.seconds * 1000);
    }
    // Период обновления полосы: раз в секунду. Чаще незачем — человек читает
    // счётчик, а не следит за каждым запросом.
    setInterval(pushState, 1000);
  });

  app.on('window-all-closed', () => {
    // Закрыли окно, не сохранив, — сохраняем сами. Потерять собранную картину
    // из-за закрытого окна было бы обидно: второй проход по LMS это ещё один
    // живой вход с чьей-то учётной записью.
    //
    // ПОЧЕМУ ЭТО БОЛЬШЕ НЕ ОПАСНО. Раньше такое самосохранение затирало файл
    // целиком: достаточно было открыть инструмент и закрыть окно, чтобы
    // вычеркнутые руками строки, заполненные issued_by и issued_at исчезли.
    // Теперь сохранение СЛИВАЕТ с прошлым файлом (mergeWithPrevious), не
    // возвращает вычеркнутое и не трогает заполненные поля человека, а
    // прошлый файл откладывает копией. Защита от потери свежего перестала
    // быть потерей накопленного.
    if (records.length && records.length !== savedAtCount) {
      saveDraft('окно закрыто, с прошлого сохранения появились новые запросы')
        .catch((err) => log(`сохранение не удалось: ${String((err && err.message) || err)}`))
        .then(() => app.quit());
      return;
    }
    app.quit();
  });
}

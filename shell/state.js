'use strict';
/**
 * Модель состояний оболочки — отдельным модулем, без побочных эффектов.
 *
 * Зачем отдельно: от этой таблицы зависит, в какой момент у студента
 * отбирают управление машиной. Инцидент 07.10 случился именно потому, что
 * такой модели не было вовсе: lockdown.engage() стоял на старте приложения,
 * и блокировки включались до экрана согласия. Теперь правило одно и
 * проверяемое без запуска Electron: блокировки живут только в LOCKED_STATES.
 */

/** Полный список состояний в порядке прохождения. */
const SHELL_STATES = Object.freeze([
  'idle', 'consent', 'preflight', 'calibration', 'exam', 'paused', 'finished',
]);

/**
 * Состояния, в которых блокировки обязаны быть активны. Больше нигде.
 *
 * Почему paused тоже здесь: пауза — состояние ВНУТРИ экзамена (сработал порог
 * риска либо проктор остановил тест), вопросы и поле ответа остаются на экране,
 * тест не завершён. Снимать блокировки на паузе значит выдать студенту законное
 * окно «сходить посмотреть ответ» — и превратить нарушение в способ обойти
 * прокторинг, вызвав паузу намеренно. Машину освобождает только finished
 * (или аварийный выход прокторa).
 */
const LOCKED_STATES = Object.freeze(['exam', 'paused']);

/**
 * Допустимые переходы. Renderer — источник сигнала о состоянии, но НЕ источник
 * истины: окно теста может быть подменено или сломано, поэтому любой переход
 * проверяется в main-процессе. Прыжок consent -> exam отвергается, и
 * блокировки по нему не включаются.
 */
const STATE_TRANSITIONS = Object.freeze({
  idle: Object.freeze(['consent', 'finished']),
  consent: Object.freeze(['preflight', 'finished', 'idle']),
  preflight: Object.freeze(['calibration', 'consent', 'finished']),
  calibration: Object.freeze(['exam', 'preflight', 'finished']),
  exam: Object.freeze(['paused', 'finished']),
  paused: Object.freeze(['exam', 'finished']),
  finished: Object.freeze(['idle', 'consent']),
});

function isState(name) {
  return typeof name === 'string' && SHELL_STATES.indexOf(name) !== -1;
}

/** Нужны ли блокировки в этом состоянии. */
function isLockedState(name) {
  return LOCKED_STATES.indexOf(name) !== -1;
}

/**
 * Проверка перехода. Единственное место, где решается, законен ли он.
 * @returns {{ok:boolean, reason:string}}
 */
function canTransition(from, to) {
  if (!isState(to)) return { ok: false, reason: 'unknown_state' };
  if (!isState(from)) return { ok: false, reason: 'unknown_current_state' };
  if (from === to) return { ok: true, reason: 'unchanged' };
  const allowed = STATE_TRANSITIONS[from] || [];
  if (allowed.indexOf(to) === -1) return { ok: false, reason: 'illegal_transition' };
  return { ok: true, reason: 'ok' };
}

/** Куда вообще можно уйти из этого состояния — для журнала при отказе. */
function allowedFrom(from) {
  const allowed = STATE_TRANSITIONS[from];
  return allowed ? allowed.slice() : [];
}

/**
 * Что флаги командной строки разрешают блокировать. Отдельная функция, а не
 * пара выражений в main.js, чтобы смысл флагов проверялся тестом:
 *
 *  --no-lockdown — блокировок нет НИКОГДА: ни перехвата сочетаний, ни очистки
 *    системного буфера обмена, ни захвата экрана окном. Режим разработки и
 *    прогона интерфейса на чужой машине.
 *  --no-kiosk — окно обязано вести себя как обычное: его можно двигать,
 *    сворачивать, закрывать, и оно не висит поверх чужих приложений. Поэтому
 *    он снимает захват окна целиком. Раньше alwaysOnTop стоял жёстко, вне
 *    зависимости от флага, и «отладочный» запуск так же накрывал экран.
 *
 * @param {{noKiosk?:boolean, noLockdown?:boolean}} flags
 * @returns {{shortcuts:boolean, window:boolean}} что разрешено НА ВРЕМЯ экзамена
 */
function lockPermissions(flags) {
  const f = flags || {};
  const noLockdown = Boolean(f.noLockdown);
  const noKiosk = Boolean(f.noKiosk);
  return {
    shortcuts: !noLockdown,
    window: !noLockdown && !noKiosk,
  };
}

/**
 * Ослабляющие флаги запуска и что именно каждый из них снимает.
 *
 * Таблица лежит здесь, а не в main.js, по той же причине, что и модель
 * состояний: от неё зависит, что будет написано в подписанном отчёте об
 * условиях экзамена, и проверяться она обязана без запуска Electron.
 *
 * `--allow-multi-display` в списке намеренно. Он не снимает блокировок, но
 * снимает РЕАКЦИЮ на второй экран: событие MULTIPLE_DISPLAYS в журнале
 * остаётся, а блокирующий экран не показывается, и экзамен идёт дальше при
 * подключённом проекторе. Для отчёта это такое же изменение условий.
 */
const WEAKENING_FLAGS = Object.freeze([
  Object.freeze({
    key: 'noLockdown',
    flag: '--no-lockdown',
    takes: 'ни перехвата системных сочетаний, ни очистки буфера обмена, '
      + 'ни удержания окна',
  }),
  Object.freeze({
    key: 'noKiosk',
    flag: '--no-kiosk',
    takes: 'окно не держится поверх других, его можно двигать, сворачивать и закрывать',
  }),
  Object.freeze({
    key: 'allowMultiDisplay',
    flag: '--allow-multi-display',
    takes: 'второй монитор не останавливает экзамен',
  }),
]);

/**
 * Какие ослабляющие флаги заданы. Чистая функция над теми же флагами, что
 * читает lockPermissions().
 *
 * @param {{noKiosk?:boolean, noLockdown?:boolean, allowMultiDisplay?:boolean}} flags
 * @returns {Array<{key:string, flag:string, takes:string}>} в порядке таблицы
 */
function weakeningFlags(flags) {
  const f = flags || {};
  return WEAKENING_FLAGS.filter((spec) => Boolean(f[spec.key]));
}

/**
 * Пригоден ли ЗАПУСК для настоящего экзамена, по одним только флагам.
 *
 * Это не весь вердикт: фактическое состояние защиты (удержаны ли сочетания,
 * применён ли contentProtection) знает только main-процесс, и он добавляет
 * свои причины в SHELL_CONFIG. Здесь — та часть, которая следует из флагов и
 * потому проверяема тестом.
 *
 * @returns {{examReady:boolean, reasons:string[], flags:string[]}}
 */
function launchVerdict(flags) {
  const weak = weakeningFlags(flags);
  return {
    examReady: weak.length === 0,
    flags: weak.map((spec) => spec.flag),
    reasons: weak.map((spec) => `${spec.flag}: ${spec.takes}`),
  };
}

// ===========================================================================
// ПРОФИЛЬ ЭКЗАМЕНА: какой адрес открывать и какие источники разрешены
// ===========================================================================
/*
 * Зачем это здесь, а не в main.js. Правило «куда студенту можно» решает, что
 * попадёт в подписанную цепочку как попытка обхода, — то есть это часть
 * доказательной базы, а не деталь окна. Такое правило обязано проверяться без
 * запуска Electron, ровно как таблица состояний выше: модуль не импортирует
 * ничего, кроме глобального URL, и гоняется чистым node.
 *
 * ГЛАВНОЕ ОГРАНИЧЕНИЕ СМЫСЛА. Профиль — УЛИКА, а не защита. Фильтр живёт
 * внутри процесса, который запускает сам студент; его можно обойти, подменив
 * сборку. Поэтому нигде — ни в коде, ни в HUD, ни в отчёте — не утверждается,
 * что профиль нельзя обойти. Ценность в другом: каждая попытка выхода за
 * белый список попадает в журнал ПОЛНЫМ адресом, и обойти фильтр, не оставив
 * этой записи, нельзя, потому что запись не зависит от флага «проверять».
 *
 * Урок Safe Exam Browser, из которого выросла эта конструкция: подпись
 * конфигурации у SEB была, а сверка Config Key в Moodle включалась отдельной
 * галочкой и по умолчанию была выключена. Защиту сделали опциональной — и её
 * не стало. Отсюда правило: у нас нет ни одной проверки, которую можно забыть
 * включить. Хеш действовавшего профиля уходит в цепочку ВСЕГДА, и «профиля
 * нет» — это тоже запись, а не отсутствие записи.
 */

/** Схемы, по которым странице экзамена вообще разрешено ходить. */
const EXAM_ALLOWED_SCHEMES = Object.freeze(['https:', 'http:']);

/*
 * ПРЕСЕТОВ ВУЗА ЗДЕСЬ НЕТ, И ЭТО РЕШЕНИЕ, А НЕ ПРОПУСК.
 *
 * Раньше здесь лежал пресет `ksu: ['*.ksu.edu.kz']`, а в ядре под тем же
 * именем — `['moodle.ksu.edu.kz', 'platonus.ksu.edu.kz']`. Одно слово
 * `--exam-preset ksu` означало разное в двух процессах: оболочка открывала
 * весь домен вуза (форум, библиотеку, файлообменник, Moodle другого
 * факультета), ядро — два конкретных хоста. Ровно тот случай, который
 * разобран с заказчиком как недопустимый, и он возник не из-за ошибки в
 * списке, а из-за того, что список был в двух местах.
 *
 * Поэтому белый список у системы один владелец — ЯДРО. Заготовку профиля под
 * вуз печатает `--exam-profile-example ksu` (sidecar/config.py:
 * EXAM_PROFILE_PRESETS), проктор правит её руками и подаёт файлом
 * `--exam-profile`. В цепочку уходит хеш файла, который проктор видел, а не
 * хеш строки из кода.
 */

/**
 * Классы «фильтр по домену, который НЕ ГОДИТСЯ». Запись, равная одному из
 * этих суффиксов, отвергается с причиной `too_broad`.
 *
 *  - `.edu` с 2001 года выдаётся только вузам США, аккредитованным агентствами
 *    Минобразования США. Казахстанские вузы живут на .edu.kz. Фильтр по .edu
 *    пропустил бы Гарвард и заблокировал заказчика — это разобрано с
 *    заказчиком отдельно и в код попасть не должно даже случайно.
 *  - класс `.edu.kz` целиком тоже слишком широк: под ним форумы,
 *    файлообменники, библиотеки и Moodle ДРУГИХ вузов с теми же курсами.
 *
 * Список не претендует на полноту публичных суффиксов: он закрывает именно те
 * записи, которые человек напишет, пытаясь «разрешить вузы».
 */
const TOO_BROAD_SUFFIXES = Object.freeze([
  'kz', 'ru', 'com', 'net', 'org', 'info', 'biz', 'edu', 'gov', 'mil', 'int',
  'edu.kz', 'ac.kz', 'gov.kz', 'org.kz', 'com.kz', 'net.kz', 'mil.kz',
  'edu.ru', 'ac.ru', 'gov.ru', 'org.ru', 'com.ru', 'net.ru',
  'ac.uk', 'co.uk', 'org.uk', 'edu.au', 'edu.cn', 'edu.in', 'edu.tr',
  'edu.pl', 'edu.ua', 'edu.uz', 'edu.kg', 'edu.az', 'edu.ge',
]);

/*
 * ПОИСКОВИКИ И ИИ-АССИСТЕНТЫ: ЭТИ ДВА СПИСКА НИЧЕГО НЕ РЕШАЮТ.
 *
 * Они нужны ровно для одного — НАЗВАТЬ ПРИЧИНУ уже состоявшегося отказа:
 * в отчёте «поисковик» и «источник не в белом списке» — разный разговор с
 * проктором, хотя отказ в обоих случаях один и тот же «хоста нет в списке».
 *
 * Решение «пускать или нет» принимает ТОЛЬКО белый список, который присылает
 * ядро (`effective_origins`). Ядро само выбрасывает из объявленного списка
 * поисковики и ассистентов, когда поиск не разрешён (`_apply_origin_policy` в
 * sidecar/config.py), — то есть до оболочки такая запись просто не доходит.
 *
 * ПОЧЕМУ СВОЙ ЗАПРЕТ ОТСЮДА УБРАН. Раньше оболочка вторым проходом блокировала
 * хост из белого списка, если находила его в этих таблицах. Таблицы оболочки и
 * ядра не совпадали (21 хост знала только оболочка), и на таком хосте
 * получалось худшее из возможного: студента режут на источнике, который
 * проктор разрешил, а ядро, перепроверив адрес по своей копии правил, видит
 * «разрешён» и записывает НЕ инцидент, а `disputed` — метку «оболочку
 * поправили». Строгость, из-за которой честная оболочка выглядит подменённой,
 * хуже отсутствия строгости: ценность профиля в записи, а не в запрете.
 *
 * Отсюда правило: совпадать с таблицами ядра этим спискам НЕ требуется и
 * сверять их незачем — от них зависит только формулировка. Промах в ту сторону
 * («ядро знает хост, оболочка нет») даёт в журнале «источник не в белом
 * списке» вместо «поисковик», и это потеря слова, а не улики.
 */
const SEARCH_DOMAINS = Object.freeze([
  'bing.com', 'duckduckgo.com', 'duck.com', 'yahoo.com', 'baidu.com',
  'ya.ru', 'rambler.ru', 'sputnik.ru', 'nigma.ru', 'aport.ru',
  'ecosia.org', 'startpage.com', 'qwant.com', 'mojeek.com', 'marginalia.nu',
  'ask.com', 'aol.com', 'naver.com', 'seznam.cz', 'yep.com', 'presearch.com',
  'search.brave.com', 'lite.duckduckgo.com', 'searx.be', 'searxng.site',
]);

/** Поисковики с множеством ccTLD — одним выражением, а не списком доменов. */
const SEARCH_PATTERNS = Object.freeze([
  /^(?:[a-z0-9-]+\.)*google(?:usercontent)?\.[a-z]{2,}(?:\.[a-z]{2,})?$/,
  /^(?:[a-z0-9-]+\.)*yandex\.[a-z]{2,}(?:\.[a-z]{2,})?$/,
]);

/*
 * ИИ-помощники. Тоже только для НАЗВАНИЯ причины (см. комментарий выше).
 *
 * ЧЕСТНО ПРО `--allow-search`: он снимает запрет И на поисковики, И на
 * ИИ-ассистентов — один флаг на оба класса, и так же ведёт себя ядро
 * (`_apply_origin_policy`: записи классов `search` и `assistant` остаются в
 * действующем списке при `allow_search_effective`). Раньше в этом месте стояло
 * «запрещены ВСЕГДА, флагом не снимаются» — неправда, и на разборе с
 * заказчиком такая строка читалась бы как гарантия. Ровно тот класс ошибки,
 * на котором развалился SEB, только в документации, а не в коде.
 *
 * Разделять флаг на два оболочка не может в одиночку: ядро перепроверяет
 * адрес по своей копии правил, и запрет, которого у ядра нет, превратил бы
 * настоящую попытку в `disputed`. Если заказчику нужны два отдельных
 * выключателя — это правка ЯДРА (поле профиля `allow_assistants`), и она
 * заведена в memory/backlog.md, а не сделана тихо на одной стороне.
 */
const AI_DOMAINS = Object.freeze([
  'openai.com', 'chatgpt.com', 'oaistatic.com', 'oaiusercontent.com',
  'claude.ai', 'anthropic.com', 'perplexity.ai', 'pplx.ai',
  'you.com', 'phind.com', 'poe.com', 'deepseek.com', 'moonshot.cn',
  'mistral.ai', 'groq.com', 'character.ai', 'huggingface.co',
  'copilot.microsoft.com', 'gigachat.ru', 'sberdevices.ru',
  'doubao.com', 'qwen.ai', 'tongyi.aliyun.com', 'kimi.ai', 'zhipuai.cn',
  'blackbox.ai', 'codeium.com', 'tabnine.com', 'writesonic.com',
]);

/** Классы ресурсов, которые мы различаем в журнале. */
const EXAM_RESOURCE_CLASSES = Object.freeze(['navigation', 'iframe', 'subresource']);

/**
 * Тип ресурса Electron -> класс факта.
 *
 * Различать обязательно: `mainFrame` — студент осознанно открыл адрес, а
 * `image`/`font`/`script` — страница сама полезла за своим ресурсом. Это
 * разные по смыслу факты, и вес у них в движке правил обязан быть разный:
 * иначе Moodle, подтянувший шрифт со стороннего CDN, весил бы столько же,
 * сколько набранный руками адрес шпаргалки.
 */
function examResourceClass(resourceType) {
  const t = typeof resourceType === 'string' ? resourceType : '';
  if (t === 'mainFrame' || t === 'main_frame' || t === 'navigation') return 'navigation';
  if (t === 'subFrame' || t === 'sub_frame' || t === 'iframe') return 'iframe';
  return 'subresource';
}

/** Осознанное действие студента (навигация) или обращение самой страницы. */
function isDeliberateResourceClass(cls) {
  return cls === 'navigation';
}

/**
 * Прочитать поле оборонительно. Профиль приходит от другого процесса, и его
 * форма нам не подконтрольна: это может быть обычный объект, Map или вообще
 * не объект. Поэтому и `?.`, и `.get()`.
 */
function readField(src, names) {
  if (src === null || src === undefined) return undefined;
  const list = Array.isArray(names) ? names : [names];
  for (const name of list) {
    let value;
    if (typeof src.get === 'function') {
      try { value = src.get(name); } catch (err) { value = undefined; }
    }
    if (value === undefined || value === null) {
      try { value = src?.[name]; } catch (err) { value = undefined; }
    }
    if (value !== undefined && value !== null) return value;
  }
  return undefined;
}

/** Список из чего угодно: массив, Set, строка с запятыми, одно значение. */
function readList(src, names) {
  const value = readField(src, names);
  if (value === undefined) return [];
  if (Array.isArray(value)) return value.slice(0, 256);
  if (value && typeof value.forEach === 'function' && typeof value !== 'string') {
    const out = [];
    try { value.forEach((item) => out.push(item)); } catch (err) { /* не итерируемо */ }
    return out.slice(0, 256);
  }
  if (typeof value === 'string') {
    return value.split(/[\s,;]+/).filter(Boolean).slice(0, 256);
  }
  /*
   * ЧИСЛО — НЕ СПИСОК, и оборачивать его в список нельзя.
   *
   * Поймано живым запуском 08.10. Ядро шлёт `status` ~2 Гц, и в нём лежит не
   * сам профиль, а его СВОДКА, где поле `origins` — это КОЛИЧЕСТВО источников
   * (`"origins": 1`), а не их перечень. Прежняя ветка `return [value]`
   * превращала счётчик в запись белого списка: в действующих правилах
   * появлялся источник с именем «1», он попадал в канонический вид и в наш
   * хеш применённых правил, а студент читал на экране согласия
   * «Разрешённые источники: 1, lms.astanait.edu.kz» — правило, которого
   * проктор не писал.
   *
   * Отсюда же и второй след: сводка перебивала перечень из `hello`, см.
   * examProfileCarriesRules() и applyExamProfile() в shell/main.js.
   */
  if (typeof value === 'number' || typeof value === 'bigint'
      || typeof value === 'boolean') {
    return [];
  }
  return [value];
}

/**
 * Несёт ли сообщение ПЕРЕЧЕНЬ разрешённых источников, а не сводку по ним.
 *
 * Нужна, чтобы оболочка отличала запись правил (`hello`, control-запись) от
 * периодической сводки в `status`: сводка называет те же поля, но числами, и
 * принимать её за правила — значит каждые полсекунды заменять белый список
 * проктора пустым. Имена ключей перечислены ровно один раз, здесь, и
 * normalizeExamProfile() читает список по этому же перечню.
 */
const EXAM_ORIGIN_FIELDS = Object.freeze([
  'allowed_origins', 'allowedOrigins', 'origins', 'sources', 'allowed', 'whitelist',
]);

function examProfileCarriesRules(raw) {
  if (!raw || typeof raw !== 'object') return false;
  for (const name of EXAM_ORIGIN_FIELDS) {
    const value = readField(raw, [name]);
    if (value === undefined) continue;
    if (Array.isArray(value)) return true;
    if (typeof value === 'string') return true;
    if (typeof value.forEach === 'function') return true;
  }
  // Поле с подстановочными источниками ядро кладёт только в запись правил.
  const wild = readField(raw, ['wildcard_origins', 'wildcardOrigins']);
  return Array.isArray(wild);
}

function readBool(src, names) {
  const value = readField(src, names);
  if (value === undefined) return false;
  if (typeof value === 'string') {
    return ['1', 'true', 'yes', 'on', 'да'].indexOf(value.trim().toLowerCase()) !== -1;
  }
  return Boolean(value);
}

function readText(src, names, limit) {
  const value = readField(src, names);
  if (value === undefined) return '';
  return String(value).trim().slice(0, limit || 400);
}

/** Имя хоста к единому виду: нижний регистр, без завершающей точки и скобок. */
function normalizeHost(host) {
  let h = String(host === undefined || host === null ? '' : host).trim().toLowerCase();
  if (h.startsWith('[') && h.endsWith(']')) h = h.slice(1, -1);
  while (h.endsWith('.')) h = h.slice(0, -1);
  return h;
}

/**
 * Петля. Блокируется ВСЕГДА, даже если проктор вписал её в белый список.
 *
 * Причина не в удобстве: по loopback живёт наш же канал к ядру
 * (ws://127.0.0.1). Страница экзамена — сторонний сайт, и если в него
 * что-нибудь внедрено, доступ к нашему сокету означал бы попытку подсунуть
 * ядру наблюдения. Своему интерфейсу это не мешает: фильтр стоит только на
 * сессии страницы экзамена, а локальный renderer живёт в defaultSession и
 * фильтра не видит вовсе.
 */
function isLoopbackHost(host) {
  const h = normalizeHost(host);
  if (!h) return false;
  if (h === 'localhost' || h.endsWith('.localhost')) return true;
  if (h === '::1' || h === '0:0:0:0:0:0:0:1') return true;
  if (h === '0.0.0.0' || h === '0') return true;
  if (/^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(h)) return true;
  if (/^::ffff:127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(h)) return true;
  // IPv4-mapped в ШЕСТНАДЦАТЕРИЧНОЙ записи: `[::ffff:7f00:1]` — это тот же
  // 127.0.0.1, и браузер по нему ходит. Текстовая форма `::ffff:127.0.0.1`
  // выше закрывала только один из двух способов написать один адрес, то есть
  // запрет снимался сменой написания. Проверяем предпоследнюю группу: `7f00`
  // и далее — это 127.0.0.0/8 (0x7f = 127).
  if (h.indexOf(':') !== -1 && /^[0-9a-f:]+$/.test(h)) {
    const groups = h.split(':').filter((g) => g !== '');
    if (groups.length >= 2 && groups[groups.length - 2].replace(/^0+/, '').length <= 4) {
      const hi = parseInt(groups[groups.length - 2], 16);
      if (Number.isFinite(hi) && hi >= 0x7f00 && hi <= 0x7fff
          && h.indexOf('ffff') !== -1) {
        return true;
      }
    }
  }
  return false;
}

/** host совпадает с domain или является его поддоменом. */
function hostUnderDomain(host, domain) {
  const h = normalizeHost(host);
  const d = normalizeHost(domain);
  if (!h || !d) return false;
  return h === d || h.endsWith(`.${d}`);
}

function matchesAnyDomain(host, domains) {
  for (const domain of domains) {
    if (hostUnderDomain(host, domain)) return domain;
  }
  return '';
}

/** Поисковик ли это. Возвращает сработавшее правило — оно идёт в журнал. */
function searchSourceMatch(host) {
  const h = normalizeHost(host);
  if (!h) return '';
  const byDomain = matchesAnyDomain(h, SEARCH_DOMAINS);
  if (byDomain) return byDomain;
  for (const re of SEARCH_PATTERNS) {
    if (re.test(h)) return re.source;
  }
  return '';
}

/** ИИ-помощник ли это. */
function aiSourceMatch(host) {
  return matchesAnyDomain(normalizeHost(host), AI_DOMAINS);
}

function defaultPortFor(scheme) {
  if (scheme === 'https:') return '443';
  if (scheme === 'http:') return '80';
  return '';
}

/** Похоже ли на имя хоста (или на IP-литерал). */
function looksLikeHost(host) {
  const h = normalizeHost(host);
  if (!h || h.length > 253) return false;
  if (h.indexOf(':') !== -1) return /^[0-9a-f:]+$/.test(h);          // IPv6
  if (!/^[a-z0-9.-]+$/.test(h)) return false;
  if (h.startsWith('-') || h.startsWith('.') || h.indexOf('..') !== -1) return false;
  return true;
}

/**
 * Разобрать одну запись белого списка.
 *
 * Принимаются формы: `https://moodle.ksu.edu.kz`, `moodle.ksu.edu.kz`,
 * `moodle.ksu.edu.kz:8443`, `*.ksu.edu.kz`, `.ksu.edu.kz`.
 * Путь в записи игнорируется осознанно и с отметкой: фильтр работает по
 * origin, и запись с путём создала бы ложное ощущение, что разрешён ровно
 * этот раздел Moodle.
 *
 * @returns {{ok:true, entry:object} | {ok:false, raw:string, reason:string}}
 */
function parseOriginSpec(spec) {
  const raw = String(spec === undefined || spec === null ? '' : spec).trim();
  if (!raw) return { ok: false, raw, reason: 'empty' };
  if (raw === '*' || raw === '*.*' || raw === '*://*/*') {
    return { ok: false, raw, reason: 'wildcard_all' };
  }

  let rest = raw;
  let subdomains = false;
  let pathIgnored = false;

  if (rest.startsWith('*.')) { subdomains = true; rest = rest.slice(2); }
  else if (rest.startsWith('.')) { subdomains = true; rest = rest.slice(1); }

  let scheme = '';
  let host = '';
  let port = '';

  if (rest.indexOf('://') !== -1) {
    let parsed;
    try { parsed = new URL(rest); } catch (err) { return { ok: false, raw, reason: 'bad_url' }; }
    scheme = String(parsed.protocol || '').toLowerCase();
    host = normalizeHost(parsed.hostname);
    port = String(parsed.port || '');
    if ((parsed.pathname && parsed.pathname !== '/') || parsed.search || parsed.hash) {
      pathIgnored = true;
    }
  } else {
    // host[:port] — схема по умолчанию https. http допустим только явной записью.
    const m = /^(.+?)(?::(\d{1,5}))?$/.exec(rest);
    if (!m) return { ok: false, raw, reason: 'bad_host' };
    host = normalizeHost(m[1]);
    port = m[2] || '';
    scheme = 'https:';
  }

  if (EXAM_ALLOWED_SCHEMES.indexOf(scheme) === -1) {
    return { ok: false, raw, reason: 'scheme_not_allowed' };
  }
  if (!looksLikeHost(host)) return { ok: false, raw, reason: 'bad_host' };

  // Тот самый отказ, ради которого список TOO_BROAD_SUFFIXES и существует.
  if (TOO_BROAD_SUFFIXES.indexOf(host) !== -1) {
    return { ok: false, raw, reason: 'too_broad' };
  }
  // «*.kz» и «*.edu.kz» отличаются от «kz» только звёздочкой — смысл тот же.
  if (subdomains && host.split('.').length < 2) {
    return { ok: false, raw, reason: 'too_broad' };
  }

  /*
   * ВАЖНО: схема в сверку НЕ входит, а порт входит только когда он назван.
   *
   * Это не упрощение, а согласование с ядром. Ядро держит СВОЮ копию профиля
   * и перепроверяет каждый присланный адрес (`ExamProfile.host_allowed` ->
   * `origin_matches` в sidecar/config.py): запись без порта принимает любой
   * порт, запись с портом требует совпадения, схема не сверяется вовсе.
   *
   * Если оболочка будет строже ядра, её запись об инциденте ядро сочтёт
   * расхождением (`disputed`) и инцидент НЕ выпустит — то есть настоящая
   * попытка выхода потеряется. Строгость, из-за которой пропадает улика,
   * хуже, чем отсутствие строгости: ценность профиля ровно в записи.
   * Небезопасный http остаётся отмеченным (`insecure`) и попадает в
   * предупреждения, но запретом не становится.
   */
  const pattern = `${subdomains ? '*.' : ''}${host}${port ? `:${port}` : ''}`;
  const entry = {
    // Та же запись, что печатает ядро в `effective_origins`: отчёт и HUD
    // должны показывать источники одинаково.
    origin: pattern,
    pattern,
    scheme,
    host,
    port,                 // '' — любой порт, как и у ядра
    subdomains,
    insecure: scheme === 'http:',
    // Локальный стенд. Ядро такие записи принимает (класс `local` в
    // classify_origin) — это мок-тест или стенд вуза, а не LMS. Запретить их
    // здесь значило бы ломать локальные прогоны, которые ядро разрешило.
    // Канал к ядру при этом остаётся закрытым: его порт отсекается отдельно,
    // в normalizeExamProfile() и в examRequestVerdict().
    local: isLoopbackHost(host),
    pathIgnored,
    raw,
  };
  return { ok: true, entry };
}

/**
 * Сверка хоста с записью белого списка. Зеркало `origin_matches()` из
 * sidecar/config.py — расходиться им нельзя (см. комментарий выше).
 *
 * @param {string} host имя хоста из адреса
 * @param {string} port порт из адреса; '' для порта по умолчанию
 */
function originSpecMatches(host, port, entry) {
  if (!entry) return false;
  const h = normalizeHost(host);
  if (!h || !entry.host) return false;
  if (entry.port && entry.port !== String(port || '')) return false;
  return entry.subdomains ? hostUnderDomain(h, entry.host) : h === entry.host;
}

/** Текстовая причина отказа — для журнала и для человека у машины. */
const ORIGIN_REJECT_REASONS = Object.freeze({
  empty: 'пустая запись',
  wildcard_all: 'запись «разрешить всё» — белым списком не является',
  bad_url: 'не разбирается как адрес',
  bad_host: 'не похоже на имя хоста',
  scheme_not_allowed: 'схема не http и не https',
  loopback_never_allowed: 'петля (127.0.0.1/localhost) не разрешается никогда: по ней живёт канал к ядру',
  control_channel: 'это порт канала оболочки к ядру: страница экзамена туда не ходит ни при каких записях',
  inactive: 'профиль помечен ядром как недействующий (active=false)',
  too_broad: 'класс доменов, а не конкретный источник: под него попадают форумы, '
    + 'файлообменники, библиотеки и Moodle других вузов. '
    + 'Отдельно про .edu: он с 2001 года выдаётся только вузам США, '
    + 'а заказчик живёт на .edu.kz — такой фильтр пропустил бы Гарвард '
    + 'и заблокировал заказчика',
});

function originRejectText(reason) {
  return ORIGIN_REJECT_REASONS[reason] || 'запись не принята';
}

/**
 * Привести присланный профиль к канонической форме.
 *
 * Принимает что угодно: объект, Map, null. Любая непонятная запись не
 * выбрасывается молча, а попадает в `rejected` — проктор должен увидеть, что
 * половина его списка не применилась, а не узнать об этом из инцидентов.
 *
 * @param {*} raw профиль от ядра или собранный из флагов
 * @param {{source?:string}} [opts]
 */
function normalizeExamProfile(raw, opts) {
  const source = String((opts && opts.source) || '') || 'none';
  // Порт канала к ядру: запись белого списка с этим портом отвергается, и
  // обращение к нему со страницы экзамена блокируется в любом случае.
  const controlPort = String((opts && opts.controlPort) || '') || '';
  const empty = {
    applied: false,
    source: 'none',
    label: '',
    examUrl: '',
    examOrigin: '',
    preset: '',
    presetLabel: '',
    broad: false,
    allowSearch: false,
    allowSearchByFlag: false,
    allowSearchBy: '',
    origins: [],
    rejected: [],
    insecure: [],
    local: [],
    controlPort: '',
    coreHash: '',
    coreHashShort: '',
    coreSource: '',
    coreSigned: false,
    coreSignatureState: '',
    institution: '',
    examId: '',
    coreHeadline: '',
    warnings: [],
    canonical: '',
  };
  if (raw === null || raw === undefined || typeof raw === 'string' || typeof raw === 'number') {
    empty.controlPort = controlPort;
    return Object.freeze(Object.assign(empty, { canonical: canonicalExamProfile(empty) }));
  }

  const out = {
    applied: false,
    source,
    label: readText(raw, ['label', 'name', 'exam_name', 'title'], 200),
    examUrl: '',
    examOrigin: '',
    preset: '',
    presetLabel: '',
    broad: false,
    allowSearch: readBool(raw, ['allow_search', 'allowSearch', 'search_allowed']),
    // ЧЬЁ это решение. Ядро присылает признак отдельно: «так объявил проктор
    // до экзамена» и «так решил оператор на этой машине» — разные вещи, и на
    // разборе разница принципиальная. Выводить её из `source` оболочки нельзя:
    // источник у профиля теперь всегда один — ядро.
    allowSearchByFlag: readBool(raw, ['allow_search_by_flag', 'allowSearchByFlag']),
    allowSearchBy: '',
    origins: [],
    rejected: [],
    insecure: [],
    local: [],
    controlPort,
    // Хеш, посчитанный ЯДРОМ по файлу профиля. Он авторитетный: именно он
    // стоит в шапке отчёта и в цепочке. Наш собственный хеш (по тому, что
    // фактически применяется) считается отдельно в main.js и служит сверкой,
    // а не заменой: это разные вопросы — «какие правила объявлены» и «какие
    // применяются», и путать их нельзя.
    coreHash: readText(raw, ['profile_hash', 'hash', 'sha256'], 120),
    coreHashShort: readText(raw, ['profile_hash_short'], 40),
    coreSource: readText(raw, ['profile_source'], 60),
    coreSigned: readBool(raw, ['profile_signed']),
    coreSignatureState: readText(raw, ['signature_state'], 60),
    institution: readText(raw, ['institution'], 200),
    examId: readText(raw, ['exam_id'], 120),
    // Готовая строка для ШАПКИ отчёта от ядра. Если она есть — берём её, а не
    // сочиняем свою: расхождений между экраном и документом быть не должно.
    coreHeadline: readText(raw, ['header_line', 'headline'], 400),
    warnings: readList(raw, ['warnings']).map((w) => String(w).slice(0, 400)),
    canonical: '',
  };

  /*
   * `active` — решение ЯДРА, применять ли фильтр вообще (ExamProfile.wire()).
   * Явное `false` сильнее любых непустых списков: профиль мог не загрузиться,
   * и остатки полей не повод включать фильтр.
   */
  const activeRaw = readField(raw, ['active']);
  const activeSaysNo = activeRaw !== undefined && !readBool(raw, ['active']);
  if (out.allowSearch) {
    out.allowSearchBy = out.allowSearchByFlag ? '--allow-search' : 'профиль';
  }

  // --- адрес экзамена -------------------------------------------------------
  const urlRaw = readText(raw, ['exam_url', 'examUrl', 'url', 'lms_url'], 2000);
  if (urlRaw) {
    let parsed = null;
    try { parsed = new URL(urlRaw); } catch (err) { parsed = null; }
    const scheme = parsed ? String(parsed.protocol || '').toLowerCase() : '';
    if (!parsed || EXAM_ALLOWED_SCHEMES.indexOf(scheme) === -1
        || !looksLikeHost(parsed.hostname)) {
      out.rejected.push({ raw: urlRaw, field: 'exam_url', reason: 'bad_url' });
    } else if (isLoopbackHost(parsed.hostname)
               && controlPort && String(parsed.port || '') === controlPort) {
      // Адрес экзамена, указывающий на наш же канал к ядру, — не экзамен.
      out.rejected.push({ raw: urlRaw, field: 'exam_url', reason: 'control_channel' });
    } else {
      out.examUrl = parsed.toString();
      const port = String(parsed.port || '') || defaultPortFor(scheme);
      out.examOrigin = `${scheme}//${normalizeHost(parsed.hostname)}`
        + (port === defaultPortFor(scheme) ? '' : `:${port}`);
    }
  }

  // --- белый список ---------------------------------------------------------
  const specs = [];
  // Origin самого адреса экзамена разрешается сам собой: иначе страница
  // экзамена не загрузилась бы. Отмечается как `from: exam_url`.
  if (out.examUrl) specs.push({ value: out.examUrl, from: 'exam_url' });

  /*
   * Пресет оболочка в источники НЕ разворачивает: таблицы пресетов у неё нет
   * (см. комментарий на месте бывшего EXAM_PRESETS). Имя пресета, если оно
   * приехало в профиле, сохраняется как ПОДПИСЬ для отчёта и HUD — чтобы
   * проверяющий видел, какой заготовкой пользовался проктор, — но ни одного
   * разрешённого источника из него не берётся. Разворачивает пресет ядро,
   * печатая заготовку профиля (`--exam-profile-example`).
   */
  const presetName = readText(raw, ['preset', 'university', 'preset_name'], 60).toLowerCase();
  if (presetName) out.preset = presetName;

  // Перечень имён — в EXAM_ORIGIN_FIELDS, рядом с examProfileCarriesRules():
  // две разные функции не должны расходиться в том, что считается правилами.
  for (const value of readList(raw, EXAM_ORIGIN_FIELDS)) {
    specs.push({ value, from: 'profile' });
  }

  const seen = new Set();
  for (const spec of specs) {
    const verdict = parseOriginSpec(spec.value);
    if (!verdict.ok) {
      out.rejected.push({ raw: verdict.raw, field: spec.from, reason: verdict.reason });
      continue;
    }
    const entry = Object.assign({}, verdict.entry, { from: spec.from });
    // Канал к ядру не открывается НИКОГДА, даже записью в белом списке: по
    // нему оболочка говорит с ядром, и страница экзамена — сторонний сайт.
    if (entry.local && controlPort && entry.port === controlPort) {
      out.rejected.push({ raw: entry.raw, field: spec.from, reason: 'control_channel' });
      continue;
    }
    const key = entry.pattern;
    if (seen.has(key)) continue;
    seen.add(key);
    out.origins.push(entry);
    if (entry.insecure) out.insecure.push(entry.origin);
    if (entry.local) out.local.push(entry.origin);
    if (entry.subdomains) out.broad = true;
  }

  out.origins.sort((a, b) => (a.origin < b.origin ? -1 : (a.origin > b.origin ? 1 : 0)));
  // Профиль считается применённым, когда он задаёт хоть что-то проверяемое:
  // адрес экзамена либо непустой белый список. Профиль из одних отвергнутых
  // записей применённым НЕ считается — иначе проктор получил бы «профиль
  // действовал» там, где не разрешено ничего.
  out.applied = !activeSaysNo && (Boolean(out.examUrl) || out.origins.length > 0);
  if (activeSaysNo && (out.examUrl || out.origins.length)) {
    out.rejected.push({ raw: 'active=false', field: 'active', reason: 'inactive' });
  }
  if (!out.applied) out.source = out.rejected.length ? source : 'none';
  out.canonical = canonicalExamProfile(out);
  return Object.freeze(out);
}

/**
 * Каноническая форма профиля — строка, которую main.js хеширует для цепочки.
 *
 * В неё входит ТОЛЬКО то, что фактически применяется к запросам. Подпись,
 * название и источник профиля сюда не входят: от них поведение фильтра не
 * зависит, а хеш обязан отвечать ровно на один вопрос — «какие правила
 * действовали». Порядок ключей и порядок источников фиксированы, поэтому
 * один и тот же профиль всегда даёт один и тот же хеш.
 */
function canonicalExamProfile(profile) {
  const p = profile || {};
  const origins = (p.origins || [])
    .map((e) => `${e.origin}${e.subdomains ? '/*' : ''}`)
    .slice()
    .sort();
  return JSON.stringify({
    v: 1,
    applied: Boolean(p.applied),
    exam_url: String(p.examUrl || ''),
    origins,
    allow_search: Boolean(p.allowSearch),
    preset: String(p.preset || ''),
    // Запрет, который не снимается ничем: канал оболочки к ядру. В
    // каноническую форму входит намеренно — хеш должен покрывать и то, что
    // запрещено всегда, иначе сборка без этого запрета дала бы тот же хеш.
    control_channel_denied: true,
  });
}

/**
 * Решение по одному запросу страницы экзамена. Чистая функция — именно её
 * гоняет модульная проверка на чистом node.
 *
 * @param {object} profile результат normalizeExamProfile()
 * @param {string} url полный адрес запроса
 * @param {string} resourceType тип ресурса Electron (details.resourceType)
 * @returns {{allow:boolean, reason:string, cls:string, deliberate:boolean,
 *            origin:string, host:string, scheme:string, matched:string,
 *            note:string}}
 */
function examRequestVerdict(profile, url, resourceType) {
  const cls = examResourceClass(resourceType);
  const base = {
    allow: false,
    reason: 'not_in_whitelist',
    cls,
    deliberate: isDeliberateResourceClass(cls),
    origin: '',
    host: '',
    scheme: '',
    matched: '',
    note: '',
  };

  const raw = String(url === undefined || url === null ? '' : url);
  let parsed = null;
  try { parsed = new URL(raw); } catch (err) { parsed = null; }
  if (!parsed) {
    // about:blank — начальный пустой документ BrowserView, не попытка выхода.
    if (/^about:blank(?:#|$)/i.test(raw.trim())) {
      return Object.assign(base, { allow: true, reason: 'about_blank', scheme: 'about:' });
    }
    return Object.assign(base, { reason: 'bad_url', note: 'адрес не разбирается' });
  }

  const scheme = String(parsed.protocol || '').toLowerCase();
  const host = normalizeHost(parsed.hostname);
  const port = String(parsed.port || '') || defaultPortFor(scheme);
  const origin = scheme && host
    ? `${scheme}//${host}${port === defaultPortFor(scheme) ? '' : `:${port}`}`
    : scheme;
  Object.assign(base, { scheme, host, origin });

  if (/^about:blank(?:#|$)/i.test(raw.trim())) {
    return Object.assign(base, { allow: true, reason: 'about_blank' });
  }

  // Уход через data:, blob:, file:, about: — отдельная причина, а не «не в
  // списке»: это попытка покинуть разрешённый origin вообще без сети, и в
  // журнале она должна называться своим именем.
  if (EXAM_ALLOWED_SCHEMES.indexOf(scheme) === -1) {
    return Object.assign(base, {
      reason: 'scheme_denied',
      note: `схема ${scheme || '?'} странице экзамена запрещена`,
    });
  }

  const p = profile || {};

  /*
   * Петля. Канал оболочки к ядру (ws://127.0.0.1:<порт>) закрыт всегда и
   * никакой записью не открывается: страница экзамена — сторонний сайт, и
   * доступ к нашему сокету означал бы попытку подсунуть ядру наблюдения.
   *
   * Остальная петля закрыта по общему правилу — её нет в белом списке. Но
   * ЕСЛИ проктор назвал локальный стенд (ядро такие записи принимает, класс
   * `local`), он открывается: запрещать то, что ядро разрешило, нельзя —
   * ядро сочтёт нашу запись расхождением и потеряет инцидент.
   */
  if (isLoopbackHost(host)) {
    if (p.controlPort && port === String(p.controlPort)) {
      return Object.assign(base, {
        reason: 'loopback_denied',
        note: 'это порт канала оболочки к ядру — страница экзамена туда не ходит',
      });
    }
  }

  if (!p.applied) {
    return Object.assign(base, {
      reason: 'no_profile',
      note: 'правила экзамена проктором не задавались: разрешён только локальный интерфейс',
    });
  }

  /*
   * Поисковик это или ИИ-ассистент — нужно только для НАЗВАНИЯ причины, если
   * отказ всё равно состоится. Своего запрета у оболочки нет: решает белый
   * список от ядра. Подробно, почему именно так, — у SEARCH_DOMAINS выше.
   */
  const search = searchSourceMatch(host);
  const ai = search ? '' : aiSourceMatch(host);
  const answerSource = search || ai;

  // Белый список — по хосту и порту, схема не сверяется (зеркало ядра).
  let matchedEntry = null;
  for (const entry of (p.origins || [])) {
    if (originSpecMatches(host, parsed.port || '', entry)) {
      matchedEntry = entry;
      break;
    }
  }

  if (!matchedEntry) {
    if (answerSource) {
      // Называем причину точнее, чем «не в списке»: в отчёте это другой
      // разговор с проктором, чем случайная внешняя ссылка в задании.
      return Object.assign(base, {
        reason: search ? 'search_denied' : 'ai_denied',
        matched: answerSource,
        note: search
          ? 'поисковик показывает готовый ответ прямо в выдаче; и в белом списке его нет'
          : 'ИИ-ассистент отвечает на вопрос задания напрямую; и в белом списке его нет',
      });
    }
    return Object.assign(base, { reason: 'not_in_whitelist' });
  }

  /*
   * ХОСТ В БЕЛОМ СПИСКЕ — ПРОПУСКАЕМ, даже если он похож на поисковик.
   *
   * Второго прохода по своим таблицам здесь НЕТ намеренно. Если хост дошёл до
   * оболочки в белом списке, значит ядро его туда положило: либо поиск разрешён
   * правилами, либо это хост самого адреса экзамена, который ядро добавляет в
   * действующий список само (иначе первая же загрузка теста стала бы
   * инцидентом). Заблокировать его здесь означало бы резать студента на
   * разрешённом источнике и получить в цепочку `disputed` вместо честного
   * пропуска — то есть пометку «оболочку поправили» за работу по правилам.
   */
  return Object.assign(base, {
    allow: true,
    /*
     * Формулировка пропуска. `search_allowed` ставится ТОЛЬКО когда поиск и
     * правда разрешён правилами: иначе хост, который ядро положило в белый
     * список по своему разбору (класс `ok`) и который попал в наши таблицы
     * «похож на поисковик», получал бы в журнале подпись «поиск разрешён» —
     * утверждение о правилах экзамена, которого в правилах нет.
     */
    reason: (answerSource && p.allowSearch) ? 'search_allowed'
      : (matchedEntry.from === 'exam_url' ? 'exam_origin' : 'whitelisted'),
    matched: matchedEntry.pattern,
  });
}

/**
 * Откуда взялся отказ: из ПРОФИЛЯ или из запрета, не зависящего от профиля.
 *
 * От этого зависит, каким видом события факт уходит в ядро, и это не
 * формальность. Отказы по профилю ядро ПЕРЕПРОВЕРЯЕТ по своей копии правил
 * (`_cmd_off_profile`), и у адреса обязан быть сетевой хост — иначе
 * `url_host()` вернёт пустую строку, `host_allowed("")` ответит «разрешено»,
 * и наша запись превратится в расхождение вместо инцидента.
 *
 * Поэтому отказы делятся на три рода.
 *
 *  - `profile` — обычный выход за белый список. У адреса есть сетевой хост,
 *    ядро перепроверяет его по своей копии правил и решает само.
 *  - `absolute` — запрет, который НЕ СНИМАЕТСЯ никакой записью белого списка:
 *    адрес без сетевого хоста (`file:`, `data:`, `blob:`) и канал оболочки к
 *    ядру. Такой отказ ядро тоже записывает полноценным инцидентом, но
 *    убеждается в нём САМО: хоста либо нет вовсе, либо это петля на его
 *    собственный порт, и то и другое ядру видно без доверия к оболочке.
 *    Раньше этот род уходил видом SHORTCUT_BLOCKED — то есть самая вероятная
 *    настоящая шпаргалка (`file:///Users/stud/shpora.html`) лежала в журнале
 *    под именем «заблокированное сочетание клавиш», весила вчетверо меньше
 *    перехода на chatgpt.com и намеренно исключалась из подраздела попыток в
 *    отчёте. Полный адрес сохранялся, но под чужим именем и в чужом канале.
 *  - `hard` — адрес не разобрался вообще. Доказательной ценности ноль,
 *    сослаться не на что; остаётся техническая запись.
 *
 * @returns {'profile'|'absolute'|'hard'|'none'}
 */
function examDenyClass(reason) {
  if (reason === 'not_in_whitelist' || reason === 'search_denied' || reason === 'ai_denied') {
    return 'profile';
  }
  if (reason === 'scheme_denied' || reason === 'loopback_denied') {
    return 'absolute';
  }
  if (reason === 'bad_url') {
    return 'hard';
  }
  // no_profile: правил нет, нарушить их нельзя. Обвинение по несуществующим
  // правилам хуже молчания — ядро такие сообщения отбрасывает намеренно.
  return 'none';
}

/** Человеческие подписи причин отказа — для журнала и HUD. */
const EXAM_DENY_TEXTS = Object.freeze({
  no_profile: 'правила экзамена не заданы — разрешён только локальный интерфейс',
  bad_url: 'адрес не разбирается',
  scheme_denied: 'запрещённая схема адреса',
  loopback_denied: 'обращение к локальному каналу ядра',
  ai_denied: 'ИИ-помощник',
  search_denied: 'поисковик',
  not_in_whitelist: 'источник не в белом списке экзамена',
});

function examDenyText(reason) {
  return EXAM_DENY_TEXTS[reason] || 'источник не разрешён';
}

/**
 * Строка для ШАПКИ отчёта, рядом с вердиктом, — не для раздела ограничений
 * внизу. Проктор должен видеть, какие правила действовали, там же, где видит
 * вердикт, иначе профиль превращается в сноску.
 */
function examProfileHeadline(profile, hash) {
  const p = profile || {};
  // Строка от ядра авторитетная: она же печатается в отчёте.
  if (p.coreHeadline) return p.coreHeadline;
  if (!p.applied) return 'правила экзамена проктором не задавались';
  const short = String(p.coreHashShort || hash || '').trim();
  const head = short ? `действовал профиль ${short}` : 'действовал профиль экзамена';
  return head + (p.allowSearch ? ' (поисковики разрешены)' : '');
}

/**
 * Дедупликация фактов по ключу с окном в несколько секунд.
 *
 * Зачем: одна страница Moodle с десятком заблокированных картинок залила бы
 * журнал десятком одинаковых инцидентов. При этом терять количество нельзя —
 * `suppressed` возвращает, сколько повторов было подавлено с прошлой записи,
 * и это число уходит в журнал вместе с фактом.
 *
 * Состояние внутри замыкания, ввода-вывода нет: гоняется чистым node.
 */
function requestThrottle(windowMs, capacity) {
  const window = Math.max(0, Number(windowMs) || 0);
  const cap = Math.max(16, Number(capacity) || 512);
  const seen = new Map();
  return function admit(key, nowMs) {
    const now = typeof nowMs === 'number' ? nowMs : Date.now();
    if (seen.size >= cap) {
      for (const [k, v] of seen) {
        if (now - v.at > window) seen.delete(k);
      }
      // Даже если ничего не устарело, расти без предела нельзя: выбрасываем
      // самую старую запись. Худшее следствие — лишняя запись в журнале,
      // а не потерянный факт.
      if (seen.size >= cap) {
        const oldest = seen.keys().next();
        if (!oldest.done) seen.delete(oldest.value);
      }
    }
    const prev = seen.get(String(key));
    if (prev && now - prev.at <= window) {
      prev.suppressed += 1;
      return { ok: false, suppressed: prev.suppressed };
    }
    const suppressed = prev ? prev.suppressed : 0;
    seen.set(String(key), { at: now, suppressed: 0 });
    return { ok: true, suppressed };
  };
}

// ---------------------------------------------------------------------------
// Место под наш интерфейс рядом со страницей экзамена
// ---------------------------------------------------------------------------

/*
 * Зачем это здесь, а не в main.js: от этих чисел зависит, видит ли студент
 * рамку наблюдения и может ли он до неё дотянуться мышью, — а проверять такое
 * надо без запуска Electron.
 *
 * BrowserView со страницей LMS — НАТИВНЫЙ слой поверх нашей веб-страницы.
 * Он не участвует в раскладке CSS: что попало под его прямоугольник, то
 * закрыто целиком и не принимает ни клика, ни прокрутки. Значит прямоугольник
 * обязан совпадать с тем местом, которое наша вёрстка оставила свободным, а
 * раскладка у неё РАЗНАЯ: шире 1100px HUD стоит правой колонкой, уже — ложится
 * нижней полосой во всю ширину, и шапка вырастает со 72px до ~125px
 * (shell/renderer/styles.css, @media max-width: 1100px).
 *
 * Поймано живым запуском 08.10 при окне 1000x800: отступ был прибит
 * константой {top:72,right:392}, HUD уехал вниз — и страница экзамена накрыла
 * его целиком. Риск-индикатор, лента инцидентов и кнопка паузы оказались под
 * чужой страницей: видно их не было, нажать было нельзя. Справа при этом
 * пустовала мёртвая полоса в 392px, где HUD уже не жил.
 *
 * Поэтому источник истины один — CSS: renderer измеряет СВОЙ прямоугольник и
 * присылает его, а здесь мы проверяем присланное. Константа осталась
 * запасным вариантом на случай, когда renderer ещё не доложил или упал.
 */

/** Запасной отступ: широкая раскладка, HUD правой колонкой. */
const EXAM_VIEW_INSET_FALLBACK = Object.freeze({
  top: 72, right: 392, bottom: 0, left: 0,
});

/** Минимум, который обязан остаться самой странице экзамена. */
const EXAM_VIEW_MIN_SIZE = Object.freeze({ width: 320, height: 240 });

/**
 * Пол, ниже которого отступ не принимается ни от кого.
 *
 * Отчёт о раскладке приходит из renderer, то есть из процесса, который на
 * машине студента можно подменить. Доверять ему ширину нашей собственной
 * рамки нельзя: отчёт «свободно всё окно» убрал бы HUD под страницу экзамена
 * и спрятал бы от студента сам факт наблюдения. Поэтому во время экзамена
 * шапка и одна из полос HUD (правая ИЛИ нижняя) резервируются всегда.
 */
const EXAM_VIEW_MIN_INSET = Object.freeze({ top: 48, hud: 160 });

function finiteSide(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return null;
  return Math.round(n);
}

/**
 * Проверить отступ, присланный renderer, против размера окна.
 *
 * @param {object} raw отчёт renderer: {top,right,bottom,left}
 * @param {{width:number,height:number}} size размер содержимого окна
 * @returns {{ok:boolean, inset:object, reason:string}}
 *   ok=false означает «берите запасной отступ», причина — для журнала.
 */
function normalizeExamViewInset(raw, size) {
  const width = Math.max(0, Math.round(Number((size && size.width) || 0)));
  const height = Math.max(0, Math.round(Number((size && size.height) || 0)));
  if (!raw || typeof raw !== 'object') {
    return { ok: false, inset: EXAM_VIEW_INSET_FALLBACK, reason: 'not_an_object' };
  }
  const top = finiteSide(raw.top);
  const right = finiteSide(raw.right);
  const bottom = finiteSide(raw.bottom);
  const left = finiteSide(raw.left);
  if (top === null || right === null || bottom === null || left === null) {
    return { ok: false, inset: EXAM_VIEW_INSET_FALLBACK, reason: 'bad_numbers' };
  }
  if (top < EXAM_VIEW_MIN_INSET.top) {
    return { ok: false, inset: EXAM_VIEW_INSET_FALLBACK, reason: 'topbar_not_reserved' };
  }
  if (Math.max(right, bottom) < EXAM_VIEW_MIN_INSET.hud) {
    return { ok: false, inset: EXAM_VIEW_INSET_FALLBACK, reason: 'hud_not_reserved' };
  }
  const inset = Object.freeze({ top, right, bottom, left });
  // Окно может быть меньше, чем сумма отступов: это не повод отдавать
  // странице экзамена отрицательный прямоугольник.
  if (width - left - right < EXAM_VIEW_MIN_SIZE.width
      || height - top - bottom < EXAM_VIEW_MIN_SIZE.height) {
    return { ok: false, inset, reason: 'window_too_small' };
  }
  return { ok: true, inset, reason: 'ok' };
}

/** Прямоугольник страницы экзамена по отступу и размеру окна. */
function examViewRect(inset, size) {
  const width = Math.max(0, Math.round(Number((size && size.width) || 0)));
  const height = Math.max(0, Math.round(Number((size && size.height) || 0)));
  const i = inset || EXAM_VIEW_INSET_FALLBACK;
  return {
    x: Math.max(0, i.left),
    y: Math.max(0, i.top),
    width: Math.max(0, width - i.left - i.right),
    height: Math.max(0, height - i.top - i.bottom),
  };
}

// ---------------------------------------------------------------------------
// Снимок окна экзамена к инциденту (docs/CONTRACT.md, «Снимок окна экзамена»)
// ---------------------------------------------------------------------------
/*
 * Снимается ТОЛЬКО представление экзамена: страница LMS, если тест идёт в
 * ней, иначе содержимое нашего окна. Рабочий стол, другие окна и
 * desktopCapturer — никогда. Здесь — правила, проверяемые без Electron:
 * когда снимать, как не снимать одно и то же трижды и как прочитать размер
 * готового JPEG. Сам capturePage() — в shell/main.js.
 */

/**
 * Пределы снимка. Ширина, качество и окно повтора — из контракта; 3 МБ —
 * предел приёма у ядра (SCREEN_EVIDENCE_MAX_BYTES в sidecar/protocol.py):
 * больше слать незачем, ядро всё равно отвергнет. 200 символов — столько ядро
 * оставляет от текста ошибки. 4 с — сколько ждём capturePage(): обычно он
 * отвечает за десятки миллисекунд, а ядро примет снимок и через 60 с, так что
 * 4 с — с большим запасом, но не настолько, чтобы зависший снимок держал
 * очередь событий.
 */
const SCREEN_EVIDENCE = Object.freeze({
  maxWidth: 1280,
  jpegQuality: 70,
  reuseMs: 1500,
  captureTimeoutMs: 4000,
  maxBytes: 3 * 1024 * 1024,
  errorMaxChars: 200,
});

/** Как назвать состояние в причине «не снято» — она ляжет в отчёт по-русски. */
const SCREEN_STATE_LABELS = Object.freeze({
  idle: 'ожидание',
  consent: 'экран согласия',
  preflight: 'предполётная проверка',
  calibration: 'калибровка',
  finished: 'экзамен завершён',
});

/**
 * Можно ли снимать окно экзамена прямо сейчас.
 *
 * Правило то же, что у блокировок: только exam и paused. На согласии,
 * проверке и калибровке вопроса и ответа на экране нет, а снимать там
 * значило бы снимать студента до экзамена. Ядро просит снимок у каждого
 * неслужебного события сессии — в том числе на калибровке, — поэтому отказ
 * отвечается причиной, и отчёт пишет «экзамен не на экране», а не «снимка нет».
 *
 * @param {{sessionStarted?:boolean, examState?:string, quitting?:boolean}} s
 * @returns {{capture:boolean, error:string}} error пуст и capture=false —
 *   сессии нет: не снимать и не отвечать вовсе.
 */
function screenCaptureVerdict(s) {
  const st = s || {};
  if (st.quitting || !st.sessionStarted) return { capture: false, error: '' };
  if (!isLockedState(st.examState)) {
    const label = SCREEN_STATE_LABELS[st.examState] || String(st.examState || '—');
    return { capture: false, error: `экзамен не на экране (${label})` };
  }
  return { capture: true, error: '' };
}

/** Начинаются ли байты с сигнатуры JPEG FF D8 FF — ровно то, что проверяет ядро. */
function isJpeg(buf) {
  return Boolean(buf) && buf.length >= 3
    && buf[0] === 0xFF && buf[1] === 0xD8 && buf[2] === 0xFF;
}

/**
 * Размер JPEG в пикселях по маркеру SOF — то, что реально лежит в файле.
 *
 * Зачем не getSize() картинки: на HiDPI-экране NativeImage считает в DIP, и
 * в журнал ушли бы ширина и высота вдвое меньше настоящих. Возвращает null,
 * если маркер не найден.
 */
function jpegSize(buf) {
  if (!isJpeg(buf)) return null;
  let i = 2;
  while (i + 3 < buf.length) {
    if (buf[i] !== 0xFF) return null;               // сегменты идут подряд
    const marker = buf[i + 1];
    if (marker === 0xFF) { i += 1; continue; }      // байты-заполнители
    if (marker === 0x01 || (marker >= 0xD0 && marker <= 0xD8)) { i += 2; continue; }
    if (marker === 0xD9 || marker === 0xDA) return null;   // конец или данные до SOF
    const len = (buf[i + 2] << 8) | buf[i + 3];
    if (len < 2) return null;
    // SOF0..SOF15, кроме DHT (C4), JPG (C8) и DAC (CC)
    if (marker >= 0xC0 && marker <= 0xCF && marker !== 0xC4 && marker !== 0xC8
        && marker !== 0xCC) {
      if (i + 8 >= buf.length) return null;
      const height = (buf[i + 5] << 8) | buf[i + 6];
      const width = (buf[i + 7] << 8) | buf[i + 8];
      return width > 0 && height > 0 ? { width, height } : null;
    }
    i += 2 + len;
  }
  return null;
}

/** Текст причины для отчёта: «capturePage не ответил за 4 с» (дробные — с запятой). */
function captureTimeoutText(ms) {
  const sec = Math.round(ms / 100) / 10;
  return `capturePage не ответил за ${String(sec).replace('.', ',')} с`;
}

/**
 * Не больше одного снимка в полёте и повтор байтов в окне reuseMs.
 *
 * Инциденты приходят пачками: один уход со страницы даёт WINDOW_BLUR, следом
 * связку и событие окружения. Снимать окно на каждое — три одинаковых кадра и
 * три параллельных capturePage(). Поэтому событие, пришедшее, пока снимок
 * делается, ждёт ЕГО результата, а событие в пределах reuseMs от удачного
 * снимка получает те же байты. Ответ ядру у каждого всё равно свой, со своим
 * event_id, — это делает вызывающий. Неудачный снимок не повторяется по
 * времени: следующее событие снимает заново.
 *
 * Таймаут timeoutMs: capturePage(), который не ответил, иначе держал бы
 * «снимок в полёте» вечно — все следующие события ждали бы его, и до конца
 * сессии ни одного снимка уже не было бы. По таймауту снимок отвечает
 * `{error:'capturePage не ответил за 4 с', timedOut:true}` всем, кто его ждал,
 * и сразу снимается с полёта: следующее событие начинает НОВЫЙ снимок. Если
 * зависший capturePage() всё-таки ответит позже, его результат отбрасывается:
 * ответ ядру уже ушёл, а кадр не того момента повторно раздавать нельзя.
 *
 * reset() — граница сессии: байты прошлой сессии новой не достаются, а снимок
 * прошлой, ещё летящий, дожидаемся (не дольше timeoutMs) и снимаем заново.
 *
 * @param {() => Promise<object>} capture снимок {error?:string, ...}
 * @param {{reuseMs?:number, timeoutMs?:number, now?:() => number,
 *          setTimeout?:Function, clearTimeout?:Function}} [opts]
 *   setTimeout/clearTimeout — для проверки без настоящих часов.
 */
function screenShotCoordinator(capture, opts) {
  const o = opts || {};
  const reuseMs = Number.isFinite(o.reuseMs) ? o.reuseMs : SCREEN_EVIDENCE.reuseMs;
  const timeoutMs = Number.isFinite(o.timeoutMs) && o.timeoutMs > 0
    ? o.timeoutMs : SCREEN_EVIDENCE.captureTimeoutMs;
  const now = typeof o.now === 'function' ? o.now : Date.now;
  const setTimer = typeof o.setTimeout === 'function' ? o.setTimeout : setTimeout;
  const clearTimer = typeof o.clearTimeout === 'function' ? o.clearTimeout : clearTimeout;
  // Сколько снимков, брошенных по таймауту, всё ещё висят в capturePage.
  // Пока висят `maxHung` (по умолчанию 2), новый не начинается: каждый зависший держит
  // кадр окна и потом ещё кодирует его в JPEG, а копить их без счёта нельзя.
  const maxHung = Number.isFinite(o.maxHung) && o.maxHung > 0 ? o.maxHung : 2;
  let inflight = null;   // {epoch, promise}
  let last = null;       // {shot, at}
  let epoch = 0;
  let hung = 0;

  function take() {
    if (last && now() - last.at <= reuseMs) return Promise.resolve(last.shot);
    if (inflight) {
      if (inflight.epoch === epoch) return inflight.promise;
      return inflight.promise.then(() => take());
    }
    if (hung >= maxHung) {
      return Promise.resolve({ error: 'capturePage не отвечает (предыдущий снимок завис)', timedOut: true });
    }
    const mine = epoch;
    let abandoned = false;
    const shot = Promise.resolve()
      .then(() => capture())
      .then(
        (res) => (res && typeof res === 'object' ? res : { error: 'снимок не получен' }),
        (err) => ({ error: `снимок не получен: ${String((err && err.message) || err)}` }),
      );
    // брошенный по таймауту снимок, когда всё-таки завершится, освобождает место
    shot.then(() => { if (abandoned) hung -= 1; });
    let entry = null;
    let timer = null;
    const expired = new Promise((resolve) => {
      timer = setTimer(() => {
        // С полёта снимаем сразу, до разрешения промиса: событие, пришедшее
        // в этот же тик, уже начинает новый снимок, а не ждёт зависший.
        if (inflight === entry) inflight = null;
        abandoned = true;
        hung += 1;
        resolve({ error: captureTimeoutText(timeoutMs), timedOut: true });
      }, timeoutMs);
    });
    const run = Promise.race([shot, expired]).then((res) => {
      clearTimer(timer);
      if (mine === epoch && !res.error) last = { shot: res, at: now() };
      return res;
    });
    entry = { epoch: mine, promise: run };
    inflight = entry;
    run.then(() => {
      if (inflight === entry) inflight = null;
    });
    return run;
  }

  function reset() {
    epoch += 1;
    last = null;
  }

  return { take, reset, busy: () => Boolean(inflight), hung: () => hung };
}

module.exports = {
  SHELL_STATES,
  WEAKENING_FLAGS,
  weakeningFlags,
  launchVerdict,
  LOCKED_STATES,
  STATE_TRANSITIONS,
  isState,
  isLockedState,
  canTransition,
  allowedFrom,
  lockPermissions,

  // --- профиль экзамена и фильтр источников ---
  EXAM_ALLOWED_SCHEMES,
  EXAM_RESOURCE_CLASSES,
  TOO_BROAD_SUFFIXES,
  SEARCH_DOMAINS,
  AI_DOMAINS,
  examResourceClass,
  isDeliberateResourceClass,
  isLoopbackHost,
  hostUnderDomain,
  searchSourceMatch,
  aiSourceMatch,
  parseOriginSpec,
  originSpecMatches,
  originRejectText,
  examDenyClass,
  EXAM_ORIGIN_FIELDS,
  examProfileCarriesRules,
  normalizeExamProfile,
  canonicalExamProfile,

  // --- место под наш интерфейс рядом со страницей экзамена ---
  EXAM_VIEW_INSET_FALLBACK,
  EXAM_VIEW_MIN_SIZE,
  EXAM_VIEW_MIN_INSET,
  normalizeExamViewInset,
  examViewRect,
  examRequestVerdict,
  examDenyText,
  examProfileHeadline,
  requestThrottle,

  // --- снимок окна экзамена к инциденту ---
  SCREEN_EVIDENCE,
  screenCaptureVerdict,
  isJpeg,
  jpegSize,
  screenShotCoordinator,
};

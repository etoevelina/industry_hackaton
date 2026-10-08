/* ===========================================================================
 * app.js — связка всего интерфейса: маршрутизация экранов, мост к сайдкару,
 * предполётные проверки, запуск сессии, экран отчёта.
 *
 * Доступ к бэкенду — только через window.proctor (shell/preload.js):
 *   onStatus(cb), onEvent(cb), onRisk(cb), onVerdict(cb), onCalibration(cb),
 *   sendTelemetry(msg), sendCommand(name), sessionStart(meta), capabilities()
 *
 * Если моста нет или ядро не отвечает, оболочка переходит в деградированный
 * режим: интерфейс полностью работоспособен, проверки помечены как
 * недоступные, об этом явно сказано на экране. Ничего не падает.
 *
 * ДИЗАЙН-СИСТЕМА NEON/PROCTOR (Р-13), что из неё держит этот файл:
 *   — поверхности: светлая среда чтения (.sheet) для согласия, проверок и
 *     отчёта, тёмная среда наблюдения (.monitor) для калибровки;
 *   — статус никогда не передаётся одним цветом: у каждой предполётной
 *     проверки есть и форма (инлайновый SVG), и словесное состояние;
 *   — моноширинный шрифт — только идентификаторы и таймкоды;
 *   — цвета риска берутся из переменных tokens.css, в коде их нет.
 *
 * ПРАВИЛА ЭКЗАМЕНА. Профиль (какой адрес открыт, какие источники разрешил
 * проктор, разрешены ли поисковики) показывается студенту ТРИЖДЫ: на экране
 * согласия, в предполётной проверке и отметкой в шапке отчёта рядом с
 * вердиктом. Разбор профиля — в hud.js (window.Proctor.profile), здесь только
 * отрисовка. Профиля может не быть вовсе: тогда блок честно пишет, что правила
 * не задавались и открыт только локальный тест, — пустым он не остаётся.
 *
 * ТОН СООБЩЕНИЙ (раздел Alerts гайда): наблюдаемый факт → контекст → действие.
 * Обвинительных формулировок в текстах нет: система сообщает наблюдение,
 * решение принимает человек.
 * =========================================================================== */

(function () {
  'use strict';

  var ENV_GRACE_MS = 3500;    // сколько ждём проверок окружения после первого ответа ядра
  var LINK_TIMEOUT_MS = 6000; // нет ни одного сообщения за это время — считаем ядро недоступным
  var LINK_STALE_MS = 7000;   // ядро молчит дольше — связь деградировала

  var SCREENS = ['consent', 'preflight', 'calibration', 'exam', 'report'];

  /**
   * Экран интерфейса -> состояние оболочки (shell/main.js, SHELL_STATES).
   * Оболочка включает блокировки только на exam и paused, поэтому имя экрана
   * обязано совпадать с реальностью: наврём здесь — машина залочится не вовремя.
   */
  var SCREEN_STATE = {
    consent: 'consent',
    preflight: 'preflight',
    calibration: 'calibration',
    exam: 'exam',
    report: 'finished'
  };

  /**
   * Поверхность экрана по правилу гайда (Р-13): тёмное — наблюдение и
   * диагностика, светлое — чтение, письмо и анализ. Значения совпадают с
   * классами в index.html; здесь они нужны только как страховка, если разметка
   * пришла без класса поверхности. Существующий класс не снимается никогда.
   */
  var SURFACE = {
    consent: 'sheet',
    preflight: 'monitor',   // диагностика окружения — тёмная среда
    calibration: 'monitor',
    exam: 'sheet',          // светлый лист; тёмный HUD живёт отдельным aside
    report: 'sheet'
  };

  /** Предполётные проверки. */
  var CHECKS = [
    { id: 'camera',   title: 'Камера',                             hint: 'Доступна и даёт стабильный поток кадров' },
    { id: 'mic',      title: 'Микрофон',                           hint: 'Доступен для анализа речи; аудио не сохраняется' },
    { id: 'displays', title: 'Один монитор',                       hint: 'Второй экран должен быть физически отключён' },
    { id: 'remote',   title: 'Нет ПО удалённого доступа',          hint: 'TeamViewer, AnyDesk, RDP, VNC и аналоги' },
    { id: 'vcam',     title: 'Нет виртуальной камеры',             hint: 'OBS Virtual Camera, ManyCam и подобные' },
    { id: 'vm',       title: 'Не виртуальная машина',              hint: 'Экзамен выполняется на физическом устройстве' },
    { id: 'procs',    title: 'Нет записи экрана и запрещённых программ', hint: 'Захват экрана, эмуляторы, автокликеры' }
  ];

  /** Какой инцидент какую проверку заваливает. */
  var EVENT_TO_CHECK = {
    MULTIPLE_DISPLAYS: 'displays',
    REMOTE_ACCESS_SOFTWARE: 'remote',
    VIRTUAL_CAMERA: 'vcam',
    VIRTUAL_MACHINE: 'vm',
    SCREEN_RECORDING: 'procs',
    BLACKLISTED_PROCESS: 'procs',
    SENSOR_LOST: 'camera'
  };

  var STATE_TEXT = {
    pending: 'проверяется…',
    ok: 'в порядке',
    warn: 'недоступно',
    fail: 'не пройдено'
  };

  /**
   * Экран согласия: полный перечень «что пишется / что НЕ пишется».
   * mode: 'yes' — попадает на диск, 'ram' — только в оперативной памяти,
   * 'no' — не собирается вообще. Режим дублируется формой и подписью.
   */
  var DISCLOSURE = [
    {
      signal: 'Кадры с камеры',
      mode: 'ram',
      detail: 'Разбираются покадрово в оперативной памяти: есть ли лицо, сколько лиц, ' +
              'поворот головы, направление взгляда, посторонние предметы. Сами кадры не ' +
              'сохраняются — на диск попадают только числа посекундной сводки.'
    },
    {
      // RawObservationLog в сайдкаре включён по умолчанию (log_raw) и пишет
      // посекундные окна наблюдений в журнал сессии независимо от инцидентов.
      signal: 'Посекундная сводка наблюдений',
      mode: 'yes',
      detail: 'Раз в секунду в журнал сессии пишутся числа: есть ли лицо и сколько лиц, ' +
              'сходство с эталоном лица, поворот головы и направление взгляда, моргание, ' +
              'найденные предметы; при удалённой сдаче — была ли речь, громкость и сходство ' +
              'с голосом владельца. Без кадров и звука.'
    },
    {
      signal: 'Кадр и клип инцидента',
      mode: 'yes',
      detail: 'Сохраняются только в момент зафиксированного инцидента: обрезанный кадр и ' +
              'короткий клип вокруг события, в каталоге сессии на этом компьютере.'
    },
    {
      // Эталон — вектор признаков в памяти сайдкара (detectors/identity.py):
      // ни снимок, ни вектор на диск не пишутся. Обещание «остаётся в каталоге
      // сессии» расходилось и с кодом, и с экраном калибровки.
      signal: 'Эталон лица с калибровки',
      mode: 'ram',
      detail: 'Нужен, чтобы подтвердить: за компьютером всё время один и тот же человек. ' +
              'Держится в памяти программы до конца сессии: ни снимок, ни вектор признаков ' +
              'на диск не пишутся.'
    },
    {
      signal: 'Итоги калибровки',
      mode: 'yes',
      detail: 'Числа: ваша нулевая точка взгляда, личные пороги, карта экрана и оценка ' +
              'качества эталона лица. Лежат в каталоге сессии, чтобы экзаменатор видел, ' +
              'с какими настройками шло наблюдение.'
    },
    {
      signal: 'Интервалы между нажатиями клавиш',
      mode: 'yes',
      detail: 'Миллисекунды между нажатиями и класс клавиши: символ, навигация, служебная. ' +
              'Ритм набора нужен, чтобы отличить набор от вставки.'
    },
    {
      signal: 'Символы нажатых клавиш',
      mode: 'no',
      detail: 'Какая именно клавиша нажата, система не знает и не передаёт. ' +
              'Текст, набранный в других окнах, физически недоступен.'
    },
    {
      signal: 'Текст вашего ответа',
      mode: 'no',
      detail: 'Из поля ответа наружу уходит только длина в символах. ' +
              'Сам ответ остаётся в окне теста.'
    },
    {
      signal: 'Звук, речь, расшифровка',
      mode: 'no',
      detail: 'Аудиозаписи нет: звук не сохраняется. В режиме аудитории аудиоканал ' +
              'выключен целиком: в классе он давал бы ложные срабатывания на соседей. ' +
              'При удалённой сдаче речь с микрофона разбирается в памяти, профиль голоса ' +
              'там же до конца сессии; в посекундную сводку попадают только числа.'
    },
    {
      signal: 'Содержимое буфера обмена',
      mode: 'no',
      detail: 'Текст не сохраняется и никуда не передаётся. Во время теста буфер ' +
              'очищается раз в 1,5 с; если в нём что-то было, фиксируются только факт ' +
              'и длина в символах.'
    },
    {
      signal: 'Экран и другие окна',
      mode: 'no',
      detail: 'Снимков экрана нет, список открытых файлов и окон не собирается.'
    },
    {
      signal: 'Проверки окружения',
      mode: 'yes',
      detail: 'Число мониторов, виртуальная камера, ПО удалённого доступа, признаки ' +
              'виртуальной машины, запись экрана, подключённые аудиоустройства, мессенджеры, ' +
              'ИИ-клиенты, средства автоматизации. Список запущенных программ просматривается ' +
              'в памяти; у программы, найденной любой из этих проверок, фиксируются имя, путь, ' +
              'номер процесса, пользователь ОС и время запуска.'
    },
    {
      signal: 'События окна теста',
      mode: 'yes',
      detail: 'Потеря фокуса, выход из полноэкранного режима, заблокированные горячие ' +
              'клавиши, факт вставки из буфера.'
    },
    {
      // Правила экзамена пишут адрес каждой попытки выйти за список, и об этом
      // нужно сказать до согласия, а не только в журнале.
      signal: 'Попытки открыть адрес вне правил экзамена',
      mode: 'yes',
      detail: 'Если проктор задал правила: полный адрес попытки и время попадают в журнал ' +
              'сессии. Сама страница не открывается и не сохраняется.'
    },
    {
      signal: 'Сетевые обращения',
      mode: 'no',
      detail: 'Сама система ничего не отправляет: доказательства и отчёт остаются на этом ' +
              'компьютере, облака и внешней аналитики нет. Если правила экзамена открывают ' +
              'страницу LMS, с ней работает только окно теста.'
    }
  ];

  var MODE_TEXT = {
    yes: 'пишется, локально',
    ram: 'только в памяти',
    no: 'не пишется'
  };

  /* =========================================================================
   * ПРАВИЛА ЭКЗАМЕНА НА ЭКРАНЕ.
   *
   * Зачем этот блок есть. Человек не может соблюдать правила, которых не знает.
   * Это прямое следствие позиционирования («Integrity Score вместо надзора»,
   * студент всегда видит, что фиксируется) и обычной этики: если список
   * разрешённых источников существует, но показан только проктору, то первая
   * же заблокированная ссылка выглядит как поломка программы, а запись о
   * попытке — как ловушка. Поэтому правила показаны ДВАЖДЫ до начала теста
   * (на согласии и в предполётной проверке) и остаются видимыми в HUD.
   *
   * Чего здесь нет и не будет. Утверждения, что профиль нельзя обойти.
   * Профиль — улика, а не защита: Safe Exam Browser проиграл не технически,
   * а организационно (подпись .seb была, а сверять Config Key в Moodle надо
   * было включать отдельно, и вузы не включали). Поэтому формулировки говорят
   * ровно то, что система действительно делает: фиксирует попытку с полным
   * адресом и кладёт хеш правил в подписанную цепочку.
   *
   * Значения по умолчанию, если профиль пуст: открыт только локальный тест,
   * поисковые системы запрещены. Пустой профиль — штатный режим, не ошибка.
   * ========================================================================= */

  var RULES_TITLE = 'Правила этого экзамена';

  /** Подписи строк блока правил. Порядок — от «куда смотреть» к «чем подписано». */
  var RULE_TERMS = {
    url: 'Адрес теста',
    sources: 'Разрешённые источники',
    search: 'Поисковые системы',
    rejected: 'Не принятые записи',
    hash: 'Профиль экзамена'
  };

  function el(id) { return document.getElementById(id); }

  /** Высота коробки по её содержимому, даже если сетка растянула её выше. */
  function contentHeight(box) {
    var last = box.lastElementChild;
    if (!last) return box.offsetHeight;
    var cs = window.getComputedStyle(box);
    return Math.ceil(last.getBoundingClientRect().bottom - box.getBoundingClientRect().top +
      (parseFloat(cs.paddingBottom) || 0) + (parseFloat(cs.borderBottomWidth) || 0));
  }
  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  function fmtDuration(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    return pad2(Math.floor(s / 60)) + ':' + pad2(s % 60);
  }

  /** Локальное экранирование: не зависит от наличия window.Proctor.text. */
  /**
   * Счётчик повторов из сообщения risk. Контракт (docs/CONTRACT.md) обещает
   * breakdown как [{kind,contribution,count}], но проверялся типом только
   * contribution. Нечисловой count уезжал в разметку отчёта как есть: при
   * рассинхроне версий или кривом JSON это парные теги внутри .bar__head
   * (грид полос съезжает), а в безобидном случае подпись «×undefined».
   * CSP (script-src 'self', без 'unsafe-inline') исполнение исключает,
   * но вёрстку отчёта на демо это ломает.
   */
  function normCount(v) {
    var n = typeof v === 'number' ? v : parseInt(v, 10);
    if (!isFinite(n) || n < 1) return 1;
    return Math.round(n);
  }

  function esc(s) {
    return String(s === undefined || s === null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function setText(id, value) {
    var node = el(id);
    if (node) node.textContent = value;
  }

  /** Телеметрия, таймкоды и идентификаторы — моноширинным (правило 5). */
  function markMono(ids) {
    for (var i = 0; i < ids.length; i++) {
      var node = el(ids[i]);
      if (node) node.classList.add('mono');
    }
  }

  /**
   * Инлайновый SVG: цвет — currentColor, штрих задан атрибутом, поэтому значок
   * виден и до того, как лист стилей опишет его класс. Правила из styles.css
   * перебивают презентационные атрибуты, так что оформление остаётся за CSS.
   */
  function svgOpen(size) {
    return '<svg viewBox="0 0 ' + size + ' ' + size + '" aria-hidden="true" focusable="false" ' +
           'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
           'stroke-linejoin="round">';
  }

  /** Маркер состояния предполётной проверки: форма дублирует цвет. */
  function markSvg(state) {
    var o = svgOpen(28) + '<circle cx="14" cy="14" r="11"></circle>';
    if (state === 'pending') return o + '</svg>';
    if (state === 'ok') return o + '<path d="M8.5 14.4 L12.4 18 L19.5 10.4"></path></svg>';
    if (state === 'warn') return o + '<path d="M14 8.6 L14 15.4 M14 19.1 L14 19.2"></path></svg>';
    return o + '<path d="M9.6 9.6 L18.4 18.4 M18.4 9.6 L9.6 18.4"></path></svg>';
  }

  /**
   * Маркер строки блока правил. Форма — второй носитель смысла, цвет третий:
   * галочка — правило задано, восклицание — правило ослаблено, пустое кольцо
   * с чертой — правило не задавалось. Цвет приходит из --tone, который задаёт
   * styles.css по data-state и по поверхности (на светлом фоне кислотный как
   * цвет текста запрещён, см. tokens.css).
   */
  function ruleSvg(state) {
    var o = svgOpen(16);
    if (state === 'ok') return o + '<path d="M3.2 8.6 L6.3 11.6 L12.8 4.6"></path></svg>';
    if (state === 'warn') {
      return o + '<path d="M8 1.6 L14.6 13.4 L1.4 13.4 Z"></path>' +
                 '<path d="M8 6 L8 9.4 M8 11.3 L8 11.4"></path></svg>';
    }
    return o + '<circle cx="8" cy="8" r="5.4"></circle><path d="M4.6 11.4 L11.4 4.6"></path></svg>';
  }

  /** Маркер строки таблицы согласия. */
  function modeSvg(mode) {
    var o = svgOpen(16);
    if (mode === 'yes') return o + '<path d="M3.2 8.6 L6.3 11.6 L12.8 4.6"></path></svg>';
    if (mode === 'ram') return o + '<circle cx="8" cy="8" r="4.4"></circle><path d="M8 5.6 L8 8.4"></path></svg>';
    return o + '<path d="M4.4 4.4 L11.6 11.6 M11.6 4.4 L4.4 11.6"></path></svg>';
  }

  /**
   * Запасные правила вёрстки для узлов, которые создаёт этот файл.
   * Селекторы завёрнуты в :where() — нулевая специфичность, любое правило из
   * styles.css их перебивает. Нужны, чтобы таблица согласия и строка причины
   * блокировки были читаемы даже до того, как лист стилей получит новые классы.
   * Значения — переменные tokens.css; второй аргумент var() задан только для
   * размеров (страховка, если tokens.css ещё не подключён). Цвета без запасных
   * значений: хардкод палитры запрещён.
   */
  var FALLBACK_CSS = [
    ':where(.disclose){display:block;margin:var(--space-8,32px) 0;}',
    ':where(.disclose__head){display:flex;flex-wrap:wrap;gap:var(--space-2,8px);align-items:baseline;justify-content:space-between;margin-bottom:var(--space-4,16px);}',
    ':where(.disclose__lede){max-width:72ch;font-size:var(--fs-small,14px);}',
    ':where(.table-wrap){overflow-x:auto;border:1px solid var(--border-hair);border-radius:var(--radius-md,12px);}',
    ':where(.disclose__table){width:100%;border-collapse:collapse;text-align:left;font-size:var(--fs-small,14px);line-height:var(--lh-body,1.55);}',
    ':where(.disclose__table th),:where(.disclose__table td){padding:var(--space-3,12px) var(--space-4,16px);border-bottom:1px solid var(--border-hair);vertical-align:top;}',
    ':where(.disclose__table thead th){font-family:var(--font-mono,monospace);font-size:var(--fs-small,14px);text-transform:uppercase;letter-spacing:var(--tracking-label,0.12em);white-space:nowrap;}',
    ':where(.disclose__table tbody th){font-weight:600;}',
    ':where(.disclose__table tr:last-child th),:where(.disclose__table tr:last-child td){border-bottom:0;}',
    ':where(.disclose__mode){display:inline-flex;gap:var(--space-2,8px);align-items:flex-start;}',
    ':where(.disclose__mode) svg{flex:none;width:16px;height:16px;margin-top:0.2em;}',
    ':where(.disclose__detail){max-width:62ch;}',
    ':where(.preflight-block){flex:1 1 32ch;max-width:64ch;font-size:var(--fs-small,14px);line-height:var(--lh-body,1.55);}',
    ':where(.chk__state){display:flex;flex-direction:column;gap:2px;align-items:flex-end;text-align:right;}',
    ':where(.chk__word),:where(.chk__note){font-size:var(--fs-small,14px);}',
    ':where(.chk__mark) svg{display:block;width:24px;height:24px;}',
    // блок «Правила этого экзамена»: согласие, предполётная проверка, шапка отчёта
    ':where(.rules){display:flex;flex-direction:column;gap:var(--space-4,16px);' +
      'padding:var(--space-6,24px);border:1px solid var(--border-hair);' +
      'border-radius:var(--radius-md,12px);}',
    ':where(.rules__head){display:flex;flex-wrap:wrap;gap:var(--space-2,8px);' +
      'align-items:baseline;justify-content:space-between;}',
    ':where(.rules__grid){display:flex;flex-direction:column;gap:var(--space-3,12px);margin:0;}',
    ':where(.rules__row){display:grid;grid-template-columns:minmax(0,22ch) minmax(0,1fr);' +
      'gap:var(--space-2,8px) var(--space-4,16px);align-items:start;}',
    ':where(.rules__term){display:flex;gap:var(--space-2,8px);align-items:flex-start;' +
      'margin:0;font-size:var(--fs-small,14px);font-weight:600;}',
    ':where(.rules__term) svg{flex:none;width:16px;height:16px;margin-top:0.2em;}',
    ':where(.rules__value){margin:0;min-width:0;font-size:var(--fs-small,14px);' +
      'line-height:var(--lh-body,1.55);overflow-wrap:anywhere;}',
    ':where(.rules__srclist){list-style:none;margin:0;padding:0;display:flex;' +
      'flex-direction:column;gap:var(--space-1,4px);}',
    ':where(.rules__note){margin:0;max-width:72ch;font-size:var(--fs-small,14px);' +
      'line-height:var(--lh-body,1.55);}',
    ':where(.rules__stamp){font-size:var(--fs-small,14px);overflow-wrap:anywhere;}',
    ':where(.report__rules){margin:0;font-size:var(--fs-small,14px);' +
      'line-height:var(--lh-body,1.55);overflow-wrap:anywhere;}'
  ].join('\n');

  function installFallbackStyles() {
    if (document.getElementById('ds-fallback-app')) return;
    try {
      var style = document.createElement('style');
      style.id = 'ds-fallback-app';
      style.textContent = FALLBACK_CSS;
      document.head.appendChild(style);
    } catch (e) { /* без запасных правил экраны всё равно работают */ }
  }

  // ------------------------------------------------------------------ мост

  /**
   * Bridge — тонкая обёртка над window.proctor.
   * Все вызовы в try/catch: отсутствие моста не должно ломать интерфейс.
   */
  function Bridge() {
    this.api = (typeof window !== 'undefined' && window.proctor) ? window.proctor : null;
    this.available = !!this.api;
    this.lastMsgAt = 0;
    this.sawAny = false;
  }

  Bridge.prototype._mark = function () {
    this.lastMsgAt = Date.now();
    this.sawAny = true;
  };

  /** Первый существующий метод из списка имён — preload может звать их по-разному. */
  Bridge.prototype._pick = function (names) {
    if (!this.api) return null;
    for (var i = 0; i < names.length; i++) {
      if (typeof this.api[names[i]] === 'function') return names[i];
    }
    return null;
  };

  /** Подписка с нормализацией конверта: payload может прийти как есть или вложенным. */
  Bridge.prototype.subscribe = function (method, key, cb) {
    return this._subscribe(method, key, cb, true);
  };

  /** То же, но без отметки «сайдкар жив» — для каналов самой оболочки. */
  Bridge.prototype.subscribeRaw = function (method, key, cb) {
    return this._subscribe(method, key, cb, false);
  };

  Bridge.prototype._subscribe = function (method, key, cb, mark) {
    if (!this.api || typeof this.api[method] !== 'function') return false;
    var self = this;
    try {
      this.api[method](function (arg) {
        if (mark) self._mark();
        var payload = arg;
        if (key && arg && typeof arg === 'object' && arg[key] && typeof arg[key] === 'object') payload = arg[key];
        try { cb(payload, arg); } catch (e) { /* ошибка обработчика не рвёт поток */ }
      });
      return true;
    } catch (e) { return false; }
  };

  /**
   * Карта возможностей сайдкара {vision,gaze,identity,audio,env}.
   * capabilities() у оболочки может вернуть другое (lockdown/matrix/shell) —
   * тогда это не карта каналов, и настоящая придёт в hello.
   */
  function pickSidecarCaps(raw) {
    if (!raw || typeof raw !== 'object') return null;
    var src = (raw.capabilities && typeof raw.capabilities === 'object') ? raw.capabilities : raw;
    var keys = ['vision', 'gaze', 'identity', 'audio', 'env'];
    for (var i = 0; i < keys.length; i++) {
      if (Object.prototype.hasOwnProperty.call(src, keys[i])) return src;
    }
    return null;
  }

  Bridge.prototype.capabilities = function () {
    if (!this.api || typeof this.api.capabilities !== 'function') return Promise.resolve(null);
    try {
      return Promise.resolve(this.api.capabilities()).catch(function () { return null; });
    } catch (e) { return Promise.resolve(null); }
  };

  Bridge.prototype.sessionStart = function (meta) {
    var name = this._pick(['sessionStart', 'startSession']);
    if (!name) return Promise.resolve(null);
    try {
      return Promise.resolve(this.api[name](meta)).catch(function () { return null; });
    } catch (e) { return Promise.resolve(null); }
  };

  Bridge.prototype.sendTelemetry = function (msg) {
    if (!this.api || typeof this.api.sendTelemetry !== 'function') return false;
    try { this.api.sendTelemetry(msg); return true; } catch (e) { return false; }
  };

  /** Сколько места наша вёрстка оставила свободным под страницу экзамена. */
  Bridge.prototype.reportExamViewInset = function (inset) {
    if (!this.api || typeof this.api.reportExamViewInset !== 'function') return false;
    try { this.api.reportExamViewInset(inset); return true; } catch (e) { return false; }
  };

  Bridge.prototype.sendCommand = function (name) {
    var method = this._pick(['sendCommand', 'command']);
    if (method) {
      try { this.api[method](name); return true; } catch (e) { return false; }
    }
    // точечные методы на случай, если общего нет
    var direct = { snapshot: 'snapshot', reset_risk: 'resetRisk', export_report: 'exportReport' };
    var fn = direct[name];
    if (fn && this.api && typeof this.api[fn] === 'function') {
      try { this.api[fn](); return true; } catch (e) { return false; }
    }
    return false;
  };

  /**
   * Отправка calibrate. В перечисленном API preload отдельного метода для
   * калибровки нет, поэтому пробуем известные варианты имён и общий send.
   * Если ничего нет — калибровка идёт локально по таймерам, интерфейс не рвётся.
   */
  Bridge.prototype.sendCalibrate = function (stage, point) {
    if (!this.api) return false;
    var named = ['sendCalibrate', 'calibrate', 'sendCalibration'];
    for (var i = 0; i < named.length; i++) {
      if (typeof this.api[named[i]] === 'function') {
        try { this.api[named[i]](stage, point || null); return true; } catch (e) { return false; }
      }
    }
    var msg = { v: 1, type: 'calibrate', ts: Date.now() / 1000, stage: stage, point: point || null };
    var generic = ['send', 'sendMessage', 'post'];
    for (var j = 0; j < generic.length; j++) {
      if (typeof this.api[generic[j]] === 'function') {
        try { this.api[generic[j]](msg); return true; } catch (e) { return false; }
      }
    }
    return false;
  };

  /**
   * Сообщить оболочке, где находится студент. Оболочка включает блокировки
   * только на exam и paused, поэтому экран обязан быть назван честно.
   * Ответа не ждём: решение принимает main-процесс, он же вернёт реальное
   * состояние через onProtection.
   */
  Bridge.prototype.setExamState = function (next) {
    if (!this.api || typeof this.api.setExamState !== 'function') return false;
    try {
      Promise.resolve(this.api.setExamState(next)).then(function (res) {
        if (res && res.ok === false) {
          // Оболочка отвергла переход — это не ошибка интерфейса, но знать полезно.
          // eslint-disable-next-line no-console
          console.warn('[proctor] оболочка отвергла состояние', next, res.reason);
        }
      }).catch(function () { /* канала нет — интерфейс не рвётся */ });
      return true;
    } catch (e) { return false; }
  };

  Bridge.prototype.sessionEnd = function (reason) {
    if (!this.api) return false;
    if (typeof this.api.sessionEnd === 'function') {
      try { this.api.sessionEnd({ reason: reason }); return true; } catch (e) { return false; }
    }
    if (typeof this.api.endSession === 'function') {
      // вариант оболочки: принимает строку-причину
      try { this.api.endSession(String(reason || 'student')); return true; } catch (e) { return false; }
    }
    var msg = { v: 1, type: 'session_end', ts: Date.now() / 1000, reason: reason };
    var generic = ['send', 'sendMessage', 'post'];
    for (var i = 0; i < generic.length; i++) {
      if (typeof this.api[generic[i]] === 'function') {
        try { this.api[generic[i]](msg); return true; } catch (e) { return false; }
      }
    }
    return false;
  };

  // ------------------------------------------------------------------ приложение

  function App() {
    this.bridge = new Bridge();
    this.hud = window.Proctor.hud;
    this.exam = window.Proctor.exam;
    this.calibration = window.Proctor.calibration;
    this.telemetry = window.Proctor.telemetry;
    this.text = window.Proctor.text || null;

    this.screen = null;
    this.caps = null;
    this.capsLoaded = false;
    this.checks = {};
    this.envGraceStartedAt = 0;
    this.demoMode = false;
    this.sessionId = null;
    this.meta = null;
    this.examResult = null;
    this.sessionStartedAt = 0;
    this.locked = false;
  }

  /** Экранирование: своё, если hud.js не отдал общий помощник. */
  App.prototype._esc = function (s) {
    if (this.text && typeof this.text.escapeHtml === 'function') return this.text.escapeHtml(s);
    return esc(s);
  };

  /** Человекочитаемый текст инцидента. */
  App.prototype._eventText = function (ev) {
    if (this.text && typeof this.text.eventText === 'function') return this.text.eventText(ev);
    if (ev && typeof ev.message === 'string' && ev.message.trim()) return ev.message.trim();
    return (ev && ev.kind) || 'Событие';
  };

  /** Пороги риска — из protocol.py через hud.js; запас совпадает с protocol.py. */
  App.prototype._thresholds = function () {
    if (this.text && this.text.thresholds) return this.text.thresholds;
    return { warn: 30, pause: 60, lock: 90 };
  };

  /**
   * Цвет уровня риска. Ведущий источник — hud.js:colorFor (styles.css держит
   * под него контрактные алиасы --ok/--warn/--high/--crit). Запас — переменные
   * риска из tokens.css. Литералов цвета в коде нет ни в одной ветке.
   */
  App.prototype._riskColor = function (score) {
    if (this.text && typeof this.text.colorFor === 'function') return this.text.colorFor(score);
    var th = this._thresholds();
    if (score >= th.lock) return 'var(--risk-lock, var(--crit))';
    if (score >= th.pause) return 'var(--risk-pause, var(--high))';
    if (score >= th.warn) return 'var(--risk-warn, var(--warn))';
    return 'var(--risk-ok, var(--ok))';
  };

  App.prototype._levelOf = function (score) {
    if (this.text && typeof this.text.levelOf === 'function') return this.text.levelOf(score);
    var th = this._thresholds();
    // Та же подпись, что в hud.js: автоматика до блокировки не доходит, её
    // потолок — приостановка, а решение принимает человек.
    if (score >= th.lock) return { cls: 'lvl-lock', text: 'решение проктора' };
    if (score >= th.pause) return { cls: 'lvl-pause', text: 'высокий' };
    if (score >= th.warn) return { cls: 'lvl-warn', text: 'внимание' };
    return { cls: 'lvl-ok', text: 'норма' };
  };

  App.prototype.boot = function () {
    var self = this;

    installFallbackStyles();

    this.telemetry.init(function (msg) { self.bridge.sendTelemetry(msg); });

    this.hud.init({
      // Пауза остаётся под блокировками (см. комментарий к LOCKED_STATES в
      // shell/main.js): иначе пауза стала бы легальным окном «сходить
      // посмотреть ответ». Оболочке сообщаем честно, что это именно пауза.
      onPause: function () {
        self.bridge.setExamState('paused');
        self.exam.pause();
      },
      onResume: function () {
        self.bridge.setExamState('exam');
        self.exam.resume();
      },
      onLock: function () { self.onLocked(); },
      onReport: function () { self.hud.hideLock(); self.showReport(); }
    });

    this.exam.init({
      telemetry: this.telemetry,
      onFinish: function (res) { self.onExamFinished(res); }
    });

    this.calibration.init({
      sendCalibrate: function (stage, point) { return self.bridge.sendCalibrate(stage, point); },
      onDone: function () { /* кнопка «К экзамену» включается внутри модуля */ }
    });

    this._wireConsent();
    this._wirePreflight();
    this._wireCalibration();
    this._wireReport();

    this._renderDisclosure();
    // Правила рисуем сразу, не дожидаясь профиля: пустой профиль — такой же
    // валидный ответ («правила не задавались»), и экран согласия обязан
    // показать его с первого кадра, а не пустым местом.
    this._renderRules();
    this._watchProfile();
    this._initChecks();
    this._subscribe();
    this._loadCapabilities();
    this._startLinkWatchdog();

    // идентификаторы и таймкоды на экране отчёта — моноширинным
    markMono(['report-session', 'report-duration']);

    this._watchLayout();

    this.calibration.reset();
    this.show('consent');
  };

  /**
   * Следить за раскладкой и докладывать её оболочке.
   *
   * Изменение размера окна — отдельный путь, на котором страница экзамена
   * может накрыть HUD: окно у студента обычное (его можно потянуть за край),
   * а перейдя границу 1100px вёрстка перекладывается целиком. Поэтому
   * подписываемся и на resize окна, и на изменение самих коробок через
   * ResizeObserver: шапка растёт в две строки не от размера окна, а от того,
   * что в неё перестало влезать.
   */
  App.prototype._watchLayout = function () {
    var self = this;
    var tick = null;
    function schedule() {
      if (tick) return;
      tick = setTimeout(function () { tick = null; self._reportExamViewInset(); }, 50);
    }
    try { window.addEventListener('resize', schedule); } catch (e) { /* нет окна */ }
    if (typeof window.ResizeObserver === 'function') {
      try {
        var ro = new window.ResizeObserver(schedule);
        var bar = document.querySelector('.topbar');
        var hud = el('hud');
        if (bar) ro.observe(bar);
        if (hud) ro.observe(hud);
        this._layoutObserver = ro;
      } catch (e) { /* наблюдатель не обязателен: resize уже подписан */ }
    }
  };

  // --- экраны ---

  App.prototype.show = function (name) {
    if (SCREENS.indexOf(name) === -1) return;
    this.screen = name;
    // Оболочка узнаёт о смене экрана до отрисовки: блокировки должны стоять
    // к моменту, когда студент увидит первый вопрос, и сняться к отчёту.
    if (SCREEN_STATE[name]) this.bridge.setExamState(SCREEN_STATE[name]);
    for (var i = 0; i < SCREENS.length; i++) {
      var key = SCREENS[i];
      var node = el('screen-' + key);
      if (!node) continue;
      node.hidden = key !== name;
      // поверхность задана в разметке; дописываем только если её там нет
      var surface = SURFACE[key] || '';
      if (surface && !node.classList.contains('sheet') && !node.classList.contains('monitor')) {
        node.classList.add(surface);
      }
    }

    var active = SCREENS.indexOf(name);
    var steps = document.querySelectorAll('#stepper .step');
    for (var j = 0; j < steps.length; j++) {
      var done = j < active;
      var isActive = j === active;
      steps[j].classList.toggle('is-active', isActive);
      steps[j].classList.toggle('is-done', done);
      steps[j].setAttribute('data-state', isActive ? 'active' : (done ? 'done' : 'next'));
      // текущий шаг помечен не только цветом
      if (isActive) steps[j].setAttribute('aria-current', 'step');
      else steps[j].removeAttribute('aria-current');
    }

    // HUD виден на калибровке и экзамене: студент сразу видит, что фиксируется
    var withHud = (name === 'calibration' || name === 'exam');
    if (withHud) this.hud.show(); else this.hud.hide();
    var root = el('app');
    if (root) {
      root.classList.toggle('has-hud', withHud);
      root.setAttribute('data-screen', name);
    }
    // Раскладка изменилась — пересчитываем место под страницу экзамена.
    // Второй вызов на следующем кадре: классы уже проставлены, но браузер
    // ещё не пересчитал геометрию, и измерять сейчас рано.
    this._reportExamViewInset();
    var self2 = this;
    if (typeof window.requestAnimationFrame === 'function') {
      window.requestAnimationFrame(function () { self2._reportExamViewInset(); });
    }
  };

  /**
   * Измерить и доложить оболочке место под страницу экзамена.
   *
   * ЗАЧЕМ ЭТО ВООБЩЕ ЕСТЬ. Страница LMS живёт в BrowserView — нативном слое
   * ПОВЕРХ нашей веб-страницы. Он не участвует в раскладке CSS: что попало
   * под его прямоугольник, то закрыто целиком и не принимает ни клика, ни
   * ввода, ни прокрутки. Прежде этот прямоугольник был прибит константой в
   * main.js ({top:72,right:392}) — то есть повторял ОДНУ из наших раскладок.
   * На живом запуске 08.10 при окне 1000x800 сработал медиа-запрос
   * max-width:1100px: HUD лёг нижней полосой во всю ширину, шапка выросла до
   * ~125px — и страница экзамена накрыла и вторую строку шапки, и HUD
   * целиком, вместе с риск-индикатором, лентой инцидентов и кнопкой паузы.
   * Справа при этом осталась мёртвая полоса 392px, где HUD уже не жил.
   *
   * Поэтому место считает тот, кто знает раскладку, — вёрстка. Меряем по
   * факту (getBoundingClientRect), а не по медиа-запросу: правило здесь одно
   * и переживёт любой новый breakpoint.
   */
  App.prototype._reportExamViewInset = function () {
    if (!this.bridge) return null;
    var vw = window.innerWidth || 0;
    var vh = window.innerHeight || 0;
    if (!vw || !vh) return null;
    // Зазор между нашим интерфейсом и чужой страницей: без него край LMS
    // прилипает к HUD и читается как его часть.
    var GAP = 16;

    var top = 0;
    var bar = document.querySelector('.topbar');
    if (bar) {
      var b = bar.getBoundingClientRect();
      if (b.height > 0) top = Math.ceil(b.bottom);
    }

    var right = 0;
    var bottom = 0;
    var hud = el('hud');
    var shown = false;
    if (hud && !hud.hidden) {
      var h = hud.getBoundingClientRect();
      var cs = window.getComputedStyle ? window.getComputedStyle(hud) : null;
      shown = h.width > 0 && h.height > 0
        && (!cs || (cs.visibility !== 'hidden' && cs.display !== 'none'));
      if (shown) {
        /*
         * Правая колонка или нижняя полоса — решаем по тому, какую сторону
         * HUD занял НА САМОМ ДЕЛЕ, а не по ширине окна. Полоса во всю ширину
         * (шире 70% окна) резервирует низ, узкая панель — право. Одна
         * формула на оба медиа-запроса и на любой следующий.
         */
        if (h.width >= vw * 0.7) bottom = Math.ceil(vh - h.top) + GAP;
        else right = Math.ceil(vw - h.left) + GAP;
      }
    }

    // HUD не показан — докладывать нечего: вне калибровки и экзамена
    // представление всё равно не прикреплено, а отчёт без места под HUD
    // оболочка справедливо отвергнет и напишет об этом в журнал.
    if (!shown) return null;

    var inset = { top: top, right: right, bottom: bottom, left: 0 };
    this.bridge.reportExamViewInset(inset);
    return inset;
  };

  // --- подписки на поток от сайдкара ---

  App.prototype._subscribe = function () {
    var self = this;
    this.bridge.subscribe('onStatus', 'status', function (st) {
      self.onStatus(st);
    });
    this.bridge.subscribe('onEvent', 'event', function (ev) {
      self.onEvent(ev);
    });
    this.bridge.subscribe('onRisk', 'risk', function (r) {
      self.hud.applyRisk(r);
    });
    this.bridge.subscribe('onVerdict', 'verdict', function (v) {
      self.onVerdict(v);
    });
    this.bridge.subscribe('onCalibration', 'calibration', function (c) {
      self.calibration.applyMessage(c);
    });
    // hello — источник истины по каналам сайдкара (см. docs/CONTRACT.md)
    this.bridge.subscribe('onHello', null, function (h) {
      var caps = pickSidecarCaps(h);
      if (caps) self._applyCaps(caps);
      self._feedProfile(h);
    });
    this.bridge.subscribe('onError', null, function (err) {
      if (!err || typeof err !== 'object') return;
      var msg = err.message || err.code || 'Ошибка ядра прокторинга';
      self.hud.toast('Сбой наблюдения', String(msg));
      if (err.fatal) self._setLink('degraded', 'ядро сообщило об ошибке');
    });
    // состояние самой оболочки: число мониторов и живость CV-канала.
    // Этот канал не считается признаком живого сайдкара — он про оболочку.
    this.bridge.subscribeRaw('onShellStatus', null, function (st) {
      self._applyShellStatus(st);
    });
    // Защищённый режим: включился или снялся. Канал оболочки, не сайдкара.
    this.bridge.subscribeRaw('onProtection', null, function (p) {
      self.hud.setProtection(p);
    });
  };

  /** Применить карту каналов сайдкара к HUD и проверкам. */
  App.prototype._applyCaps = function (caps) {
    if (!caps) return;
    this.caps = caps;
    this.hud.setCapabilities(caps);
    // предпросмотр камеры в оболочке допустим только если сайдкар её не держит
    this.calibration.setPreviewAllowed(caps.vision === false);
    if (!this.envGraceStartedAt) this.envGraceStartedAt = Date.now();

    if (caps.vision === false) this._setCheck('camera', 'fail', 'камера или модули зрения недоступны');
    if (caps.audio === false) this._setCheck('mic', 'warn', 'аудиоканал отключён');
    if (caps.env === false) {
      var ids = ['displays', 'remote', 'vcam', 'vm', 'procs'];
      for (var i = 0; i < ids.length; i++) this._setCheck(ids[i], 'warn', 'проверки ОС недоступны');
    }
    this._noteDegraded();
    this._resolvePending();
  };

  /** Состояние оболочки: мониторы и наличие связи с сайдкаром. */
  App.prototype._applyShellStatus = function (st) {
    if (!st || typeof st !== 'object') return;
    // Профиль экзамена задаёт оболочка, и его состояние может приехать здесь.
    this._feedProfile(st);
    // Состояние защищённого режима приходит и отдельным событием, и здесь:
    // статус оболочки — страховка на случай, если renderer загрузился позже
    // первого push и пропустил его.
    if (st.protection && typeof st.protection === 'object') {
      this.hud.setProtection(st.protection);
    }
    if (typeof st.displayCount === 'number') {
      // живое состояние оболочки — ведущее: отключили второй экран, пункт зеленеет сам
      if (st.displayCount > 1) {
        this._forceCheck('displays', 'fail', 'подключено мониторов: ' + st.displayCount);
      } else {
        this._forceCheck('displays', 'ok', 'один монитор');
      }
    }
    var connected = (st.sidecar && st.sidecar.connected === true) || st.cvAvailable === true;
    if (connected) {
      this._setLink('up', 'ядро прокторинга на связи');
    } else if (st.cvAvailable === false) {
      // оболочка прямо говорит, что сайдкара нет — не ждём таймаут
      this._setLink('down', st.cvMessage || 'ядро недоступно');
      this._enterDemoMode();
    }
  };

  App.prototype.onStatus = function (st) {
    if (!st || typeof st !== 'object') return;
    this._setLink('up', 'ядро прокторинга на связи');
    this.hud.applyStatus(st);

    if (!this.envGraceStartedAt) this.envGraceStartedAt = Date.now();

    if (typeof st.fps === 'number' && st.fps > 0) {
      this._setCheck('camera', 'ok', 'поток ' + st.fps.toFixed(1) + ' к/с');
    }
    if (st.audio_ok === true) this._setCheck('mic', 'ok', 'сигнал есть');
    this._resolvePending();
  };

  App.prototype.onEvent = function (ev) {
    if (!ev || typeof ev !== 'object' || !ev.kind) return;
    this._setLink('up', 'ядро прокторинга на связи');
    this.hud.applyEvent(ev);

    var chk = EVENT_TO_CHECK[ev.kind];
    if (chk) {
      var msg = this._eventText(ev);
      this._setCheck(chk, 'fail', msg.length > 60 ? msg.slice(0, 59) + '…' : msg);
    }
  };

  App.prototype.onVerdict = function (v) {
    this.hud.applyVerdict(v);
  };

  App.prototype.onLocked = function () {
    this.locked = true;
    if (this.exam.running) this.exam.finish('lock');
    // Тест окончен блокировкой — машину отпускаем: экзамена больше нет.
    this.bridge.setExamState('finished');
    this.bridge.sessionEnd('lock');
  };

  // --- возможности ядра ---

  App.prototype._loadCapabilities = function () {
    var self = this;
    this.bridge.capabilities().then(function (raw) {
      self.capsLoaded = true;
      self._feedProfile(raw);
      var caps = pickSidecarCaps(raw);
      if (caps) {
        // карта каналов пришла прямо из capabilities() — сайдкар отвечал
        self._setLink('up', 'ядро прокторинга на связи');
        self._applyCaps(caps);
      }
      // оболочка может отдать своё состояние: мониторы, живость CV-канала
      if (raw && typeof raw === 'object') {
        if (raw.shell && typeof raw.shell === 'object') self._applyShellStatus(raw.shell);
        else if (typeof raw.displayCount === 'number' || typeof raw.cvAvailable === 'boolean') {
          self._applyShellStatus(raw);
        }
      }
      self._resolvePending();
    });
  };

  /** Если часть каналов выключена, об этом честно пишем на экране проверки. */
  App.prototype._noteDegraded = function () {
    if (!this.caps) return;
    var off = [];
    if (this.caps.vision === false) off.push('компьютерное зрение');
    if (this.caps.gaze === false) off.push('анализ взгляда');
    if (this.caps.identity === false) off.push('проверка личности');
    if (this.caps.audio === false) off.push('анализ звука');
    if (this.caps.env === false) off.push('проверки окружения');
    if (!off.length) return;
    this._setNote('Ядро работает в урезанном режиме: недоступно — ' + off.join(', ') +
      '. Остальные каналы наблюдения продолжают работать.');
  };

  // --- связь ---

  App.prototype._setLink = function (state, text) {
    if (state === 'up' && this.demoMode) this._exitDemoMode();
    var wrap = document.querySelector('.link-state');
    var t = el('link-text');
    if (wrap) {
      wrap.classList.toggle('is-up', state === 'up');
      wrap.classList.toggle('is-down', state === 'down');
      wrap.classList.toggle('is-degraded', state === 'degraded');
      // состояние связи читается и без цвета точки
      wrap.setAttribute('data-state', state);
    }
    if (t && text) t.textContent = text;
  };

  App.prototype._startLinkWatchdog = function () {
    var self = this;
    var bootAt = Date.now();
    setInterval(function () {
      var now = Date.now();
      if (!self.bridge.sawAny) {
        if (now - bootAt > LINK_TIMEOUT_MS) {
          self._setLink('down', 'ядро недоступно');
          self._enterDemoMode();
        }
        return;
      }
      if (now - self.bridge.lastMsgAt > LINK_STALE_MS) {
        self._setLink('degraded', 'ядро не отвечает');
      }
    }, 2000);
  };

  /**
   * Деградированный режим: сайдкара нет. Интерфейс остаётся рабочим,
   * непройденные проверки помечаются как недоступные (жёлтые, не красные),
   * об этом явно написано — красный пункт по-прежнему блокирует старт.
   */
  App.prototype._enterDemoMode = function () {
    if (this.demoMode) return;
    this.demoMode = true;
    for (var i = 0; i < CHECKS.length; i++) {
      var id = CHECKS[i].id;
      if (this.checks[id].state === 'pending') {
        this._setCheck(id, 'warn', 'ядро не отвечает');
      }
    }
    this._noteIsDemo = true;
    this._setNote('Ядро прокторинга (Python-сайдкар) не отвечает на 127.0.0.1:8787. ' +
      'Проверки окружения выполнить нельзя, наблюдение не ведётся. ' +
      'Интерфейс работает в демонстрационном режиме — реальная сессия так не запускается.');
    this._renderChecks();
  };

  /** Ядро ответило после простоя — возвращаем проверки в ожидание и перепроверяем. */
  App.prototype._exitDemoMode = function () {
    if (!this.demoMode) return;
    this.demoMode = false;
    for (var i = 0; i < CHECKS.length; i++) {
      var c = this.checks[CHECKS[i].id];
      if (c && c.state === 'warn' && c.note === 'ядро не отвечает') {
        c.state = 'pending';
        c.note = '';
      }
    }
    if (this._noteIsDemo) { this._noteIsDemo = false; this._setNote(''); }
    this.envGraceStartedAt = Date.now();
    this._renderChecks();
    this._resolvePending();
  };

  // --- экран согласия ---

  App.prototype._wireConsent = function () {
    var self = this;
    var cb = el('consent-check');
    var btn = el('btn-consent');
    this._watchConsentFit();
    if (cb && btn) {
      cb.addEventListener('change', function () { btn.disabled = !cb.checked; });
      btn.addEventListener('click', function () {
        if (!cb.checked) return;
        self.meta = self._readMeta();
        self.show('preflight');
        if (!self.envGraceStartedAt) self.envGraceStartedAt = Date.now();
      });
    }
  };

  /**
   * Экран согласия должен помещаться без прокрутки. Сколь угодно высокой может
   * быть только колонка правил (профиль с десятком источников и отклонённых
   * записей) — ей и ставится потолок: ровно столько, сколько остаётся после
   * всего остального. «Сохраняется» и «Не записывается» не сжимаются никогда:
   * прятать под прокрутку то, что о человеке записывают, нельзя, поэтому
   * потолок не ниже их собственной высоты (не хватит и так — пусть лучше
   * прокрутится страница). Пока раскрыт полный перечень, потолок не трогаем:
   * иначе колонка выросла бы и строка «Полный перечень данных» уехала бы
   * из-под курсора. Точный замер вместо вычетов из 100vh — те ломались от
   * каждой новой строки текста.
   */
  App.prototype._fitConsent = function () {
    var screen = el('screen-consent');
    var rules = screen && screen.querySelector('.consent-sum > .rules');
    var more = screen && screen.querySelector('.consent-more');
    if (!screen || !rules || screen.hidden || (more && more.open)) return;
    rules.style.maxHeight = '';
    // узкий экран: колонки стопкой, страница прокручивается штатно — правила не режем
    var sum = rules.parentNode;
    if (window.getComputedStyle(sum).gridTemplateColumns.split(' ').length < 2) return;
    var floor = 0;
    var cols = screen.querySelectorAll('.consent-sum__col');
    for (var i = 0; i < cols.length; i++) floor = Math.max(floor, contentHeight(cols[i]));
    // второй проход добирает пиксель, потерянный на округлении дробных высот
    for (var pass = 0; pass < 2; pass++) {
      var over = screen.scrollHeight - screen.clientHeight;
      if (over <= 0) return;
      var cap = Math.max(Math.floor(rules.getBoundingClientRect().height) - over, floor);
      if (rules.style.maxHeight === cap + 'px') return;
      rules.style.maxHeight = cap + 'px';
    }
  };

  App.prototype._watchConsentFit = function () {
    var self = this;
    var pending = false;
    function schedule() {
      if (pending) return;
      pending = true;
      window.requestAnimationFrame(function () { pending = false; self._fitConsent(); });
    }
    this._scheduleConsentFit = schedule;
    try { window.addEventListener('resize', schedule); } catch (e) { /* нет окна */ }
    var more = document.querySelector('#screen-consent .consent-more');
    if (more) more.addEventListener('toggle', schedule);
    try { document.fonts.ready.then(schedule); } catch (e) { /* шрифты уже на месте */ }
    schedule();
  };

  App.prototype._readMeta = function () {
    var name = el('in-name'), student = el('in-student'), exam = el('in-exam');
    return {
      student_name: (name && name.value.trim()) || 'Без имени',
      student_id: (student && student.value.trim()) || 'ST-UNKNOWN',
      exam_id: (exam && exam.value.trim()) || 'EX-DEMO'
    };
  };

  /**
   * Контейнер для таблицы согласия. Если в разметке есть #consent-disclosure,
   * используем его; иначе создаём и ставим перед карточкой участника, чтобы
   * студент прочитал перечень до того, как вводить данные.
   */
  App.prototype._disclosureHost = function () {
    var host = el('consent-disclosure');
    if (host) return host;
    var screen = el('screen-consent');
    if (!screen) return null;
    var inner = screen.querySelector('.screen__inner') || screen;
    try {
      host = document.createElement('section');
      host.id = 'consent-disclosure';
      host.className = 'disclose';
      host.setAttribute('aria-labelledby', 'disclose-title');
      var form = inner.querySelector('.card--form');
      var bar = inner.querySelector('.consent-bar');
      var before = form || bar || null;
      if (before) inner.insertBefore(host, before);
      else inner.appendChild(host);
      return host;
    } catch (e) { return null; }
  };

  /**
   * Таблица «что пишется / что НЕ пишется». Светлая среда чтения: смысл
   * строки несут подпись и форма значка, цвет только усиливает.
   */
  App.prototype._renderDisclosure = function () {
    var host = this._disclosureHost();
    if (!host) return;
    var rows = '';
    for (var i = 0; i < DISCLOSURE.length; i++) {
      var r = DISCLOSURE[i];
      rows += '<tr data-mode="' + r.mode + '">' +
                '<th scope="row">' + this._esc(r.signal) + '</th>' +
                '<td><span class="disclose__mode" data-mode="' + r.mode + '">' +
                  modeSvg(r.mode) + '<span>' + this._esc(MODE_TEXT[r.mode]) + '</span>' +
                '</span></td>' +
                '<td class="disclose__detail">' + this._esc(r.detail) + '</td>' +
              '</tr>';
    }
    host.innerHTML =
      '<div class="disclose__head">' +
        '<h2 class="card__title" id="disclose-title">Что пишется и что не пишется</h2>' +
        '<p class="disclose__lede muted">Перечень полный: других данных система не собирает. ' +
          'Всё вычисление идёт на этом компьютере, сеть не используется.</p>' +
      '</div>' +
      '<div class="table-wrap">' +
        // .data-table из гайда описана для тёмной поверхности; экран согласия
        // светлый, поэтому у таблицы согласия свой класс
        '<table class="disclose__table">' +
          '<caption class="visually-hidden">Перечень фиксируемых и не фиксируемых данных: ' +
            'сигнал, попадает ли он на диск, что именно сохраняется</caption>' +
          '<thead><tr>' +
            '<th scope="col">Сигнал</th>' +
            '<th scope="col">Пишется на диск</th>' +
            '<th scope="col">Что именно</th>' +
          '</tr></thead>' +
          '<tbody>' + rows + '</tbody>' +
        '</table>' +
      '</div>';
  };

  // --- правила экзамена: согласие, предполётная проверка, шапка отчёта ---

  /** Профиль может дойти позже первого кадра — тогда экраны перерисуются. */
  App.prototype._watchProfile = function () {
    var self = this;
    var mod = window.Proctor?.profile;
    if (!mod || typeof mod.onChange !== 'function') return;
    try { mod.onChange(function () { self._renderRules(); }); } catch (e) { /* не критично */ }
  };

  /**
   * Отдать модулю правил конверт, в котором профиль МОЖЕТ лежать: shell-status,
   * capabilities, hello. hud.js спрашивает window.proctor напрямую, но какой
   * именно канал оболочка выбрала для профиля, на момент написания неизвестно,
   * поэтому все приходящие конверты проверяются тоже. Конверт без профиля
   * ничего не затирает — это проверяет setExamProfile.
   */
  App.prototype._feedProfile = function (raw) {
    var mod = window.Proctor?.profile;
    if (!mod || typeof mod.apply !== 'function') return;
    // перерисовку делает подписка в _watchProfile, второй раз звать не нужно
    try { mod.apply(raw); } catch (e) { /* чужой формат не ломает экран */ }
  };

  /**
   * Текущие правила. Разбор живёт в hud.js (он подключается раньше), здесь
   * только безопасное чтение: модуля может не быть вовсе, и тогда интерфейс
   * обязан вести себя как при пустом профиле, а не падать.
   */
  App.prototype._profile = function () {
    var mod = window.Proctor?.profile;
    if (mod && typeof mod.current === 'function') {
      try {
        var p = mod.current();
        if (p && typeof p === 'object') return p;
      } catch (e) { /* ниже вернём пустые правила */ }
    }
    return {
      present: false, hash: '', hashShort: '', url: '', host: '', local: true,
      sources: [], allowSearch: false, searchKnown: false, preset: '', title: '', source: ''
    };
  };

  /**
   * Строки блока правил. Каждая — своё состояние: ok (правило задано),
   * warn (правило ослаблено), off (правило не задавалось). Смысл несут
   * подпись и значение словами, форма значка дублирует, цвет только усиливает.
   */
  App.prototype._rulesRows = function (p) {
    var rows = [];
    var online = p.present && p.url && !p.local;

    rows.push(online
      ? { key: 'url', state: 'ok', html: '<span class="mono">' + this._esc(p.url) + '</span>' }
      : { key: 'url', state: 'off',
          html: 'локальный тест на этом компьютере, обращений в сеть нет' });

    if (p.sources.length) {
      var items = '';
      for (var i = 0; i < p.sources.length; i++) {
        var s = p.sources[i];
        // «и поддомены» словом: запись приходит как `host/*`, и звёздочка
        // в списке правил читается хуже, чем сказанное по-русски условие.
        items += '<li><span class="mono">' + this._esc(s.host) + '</span>' +
                 (s.wildcard ? ' и его поддомены' : '') +
                 (s.note ? ' — ' + this._esc(s.note) : '') + '</li>';
      }
      rows.push({ key: 'sources', state: 'ok',
                  html: '<ul class="rules__srclist">' + items + '</ul>' });
    } else if (online) {
      rows.push({ key: 'sources', state: 'ok',
                  html: 'только адрес теста выше: других источников проктор не открывал' });
    } else {
      rows.push({ key: 'sources', state: 'off',
                  html: 'не заданы: доступен только локальный тест' });
    }

    // Поисковики запрещены по умолчанию и остаются запрещёнными, пока правила
    // прямо не говорят иначе. Разрешение — ослабление, поэтому state=warn.
    // ЧЕМ разрешены — важно: флаг запуска и сам профиль это разные основания,
    // и оболочка их различает (allowSearchBy в shell/state.js).
    if (p.allowSearch) {
      var by = p.allowSearchBy === '--allow-search'
        ? 'разрешены флагом запуска --allow-search'
        : 'разрешены правилами этого экзамена';
      rows.push({ key: 'search', state: 'warn', html: this._esc(by) });
    } else {
      rows.push({ key: 'search', state: 'ok',
                  html: p.present ? 'запрещены' : 'запрещены: правила не задавались' });
    }

    /*
     * Записи, которые проктор написал, а оболочка не приняла. Показываем их
     * студенту сознательно: не принятая запись означает, что источник, который
     * проктор считал открытым, в действительности закрыт. Молчать об этом —
     * значит отправить человека на экзамен с неверным представлением о
     * правилах и зафиксировать ему попытку перехода туда, куда его отправили.
     */
    if (p.rejected.length) {
      var bad = '';
      for (var r = 0; r < p.rejected.length; r++) {
        bad += '<li><span class="mono">' + this._esc(p.rejected[r].raw) + '</span>' +
               (p.rejected[r].text ? ' — ' + this._esc(p.rejected[r].text) : '') + '</li>';
      }
      rows.push({ key: 'rejected', state: 'warn',
                  html: '<ul class="rules__srclist">' + bad + '</ul>' });
    }

    if (!p.present) {
      rows.push({ key: 'hash', state: 'off', html: 'проктором не задавался' });
    } else if (p.hash) {
      rows.push({ key: 'hash', state: 'ok',
                  html: '<span class="mono">' + this._esc(p.hash) + '</span>' });
    } else {
      rows.push({ key: 'hash', state: 'warn',
                  html: 'хеш оболочкой не передан; правила выше действуют' });
    }
    return rows;
  };

  /**
   * Отметка о профиле — короткая строка рядом с заголовком блока и рядом с
   * вердиктом в шапке отчёта. Ровно две возможные формулировки, третьей нет:
   * «действует профиль <хеш>» либо «правила экзамена проктором не задавались».
   * Пустое место вместо отметки означало бы, что про правила просто забыли,
   * а это именно та неоднозначность, из-за которой провалился Config Key.
   */
  App.prototype._rulesStamp = function (p) {
    if (!p.present) return 'правила экзамена проктором не задавались';
    if (!p.hash) return 'действует профиль, хеш оболочкой не передан';
    return 'действует профиль <span class="mono">' + this._esc(p.hash) + '</span>';
  };

  /** Пояснение под блоком правил. Формула та же: факт, контекст, действие. */
  App.prototype._rulesNote = function (p) {
    if (!p.present) {
      return 'Правила этого экзамена проктором не задавались, поэтому открыт только ' +
             'локальный тест на этом компьютере. В подписанную цепочку сессии уйдёт ' +
             'именно эта отметка — «правила не задавались», — а не пустое место: по ' +
             'отчёту видно, что список не забыли показать, его не было.';
    }
    var note = 'Список задаёт проктор под конкретный экзамен. Профиль — это правила и ' +
               'доказательство, а не непреодолимый барьер: мы не утверждаем, что обойти ' +
               'его невозможно. Ценность в другом — попытка открыть адрес вне списка ' +
               'попадает в журнал вместе с полным адресом, а хеш действовавших правил ' +
               'уходит в подписанную цепочку сессии. Отдельного переключателя, которым ' +
               'эту запись выключают, в системе нет.';
    if (p.allowSearch) {
      note += ' Поисковые системы для этого экзамена разрешены правилами. Их выдача ' +
              'часто показывает готовый ответ прямо на странице, поэтому разрешение ' +
              'отмечено и здесь, и в шапке отчёта, и в подписанной цепочке.';
    }
    if (p.rejected.length) {
      note += ' Часть записей списка не принята — они перечислены выше вместе с ' +
              'причиной. По этим адресам доступа нет, и переход на них будет ' +
              'зафиксирован так же, как на любой другой адрес вне списка.';
    }
    return note;
  };

  /**
   * Контейнер блока правил. Берём узел из разметки, а если его там нет —
   * создаём и ставим перед указанным ориентиром: правила должны стоять ДО
   * того, что студент делает дальше (читает перечень данных, запускает тест).
   */
  App.prototype._rulesHost = function (id, screenId, beforeSel) {
    var host = el(id);
    if (host) return host;
    var screen = el(screenId);
    if (!screen) return null;
    var inner = screen.querySelector('.screen__inner') || screen;
    try {
      host = document.createElement('section');
      host.id = id;
      host.className = 'rules';
      var before = beforeSel ? inner.querySelector(beforeSel) : null;
      if (before) inner.insertBefore(host, before);
      else inner.appendChild(host);
      return host;
    } catch (e) { return null; }
  };

  App.prototype._renderRulesBlock = function (host, p, suffix) {
    if (!host) return;
    var titleId = 'rules-title-' + suffix;
    var rows = this._rulesRows(p);
    var fullSources = '';
    if (suffix === 'consent' && p.sources.length > 2) {
      // На согласии длинный список источников (разведка LMS даёт и 5–10
      // доменов) выталкивал бы кнопку за экран. Видны первые два и счёт
      // остальных; полный список — в «Подробнее о правилах» ниже и целиком
      // на экране проверки окружения.
      for (var k = 0; k < rows.length; k++) {
        if (rows[k].key !== 'sources') continue;
        fullSources = rows[k].html;
        var shown = '';
        for (var j = 0; j < 2; j++) {
          shown += (j ? ', ' : '') + '<span class="mono">' + this._esc(p.sources[j].host) + '</span>' +
                   (p.sources[j].wildcard ? ' и поддомены' : '');
        }
        rows[k] = { key: 'sources', state: rows[k].state,
                    html: shown + ' и ещё ' + (p.sources.length - 2) + ' — полный список ниже' };
      }
    }
    var body = '';
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      body += '<div class="rules__row" data-state="' + r.state + '" data-rule="' + r.key + '">' +
                '<dt class="rules__term">' +
                  '<span class="rules__mark" aria-hidden="true">' + ruleSvg(r.state) + '</span>' +
                  '<span>' + this._esc(RULE_TERMS[r.key]) + '</span>' +
                '</dt>' +
                '<dd class="rules__value">' + r.html + '</dd>' +
              '</div>';
    }
    host.className = 'rules';
    host.setAttribute('aria-labelledby', titleId);
    host.setAttribute('data-profile', p.present ? 'yes' : 'no');
    var note = '<p class="rules__note muted">' + this._esc(this._rulesNote(p)) + '</p>';
    var key = '';
    if (suffix === 'consent') {
      // На согласии блок — колонка рядом с перечнем данных, экран обязан
      // помещаться без прокрутки. Пояснение свёрнуто, но то, на что человек
      // соглашается, остаётся на виду: попытка выйти за список пишется в
      // журнал с полным адресом. Полностью пояснение повторяется на экране
      // проверки окружения.
      // Строка о последствии стоит сразу под заголовком, выше правил: даже
      // если длинный профиль не уместится в колонку, её видно всегда.
      key = p.present
        ? '<p class="rules__key">Адрес любой попытки выйти за список пишется в журнал.</p>'
        : '';
      var list = fullSources
        ? '<p class="rules__note"><b>Разрешённые источники полностью:</b></p>' + fullSources
        : '';
      note = '<details class="rules__more"><summary>Подробнее о правилах</summary>' +
          list + note + '</details>';
    }
    host.innerHTML =
      '<div class="rules__head">' +
        '<h2 class="card__title" id="' + titleId + '">' + this._esc(RULES_TITLE) + '</h2>' +
        '<span class="rules__stamp">' + this._rulesStamp(p) + '</span>' +
      '</div>' + key +
      '<dl class="rules__grid">' + body + '</dl>' + note;
  };

  /**
   * Отметка о правилах в ШАПКЕ отчёта, рядом с вердиктом, — а не в разделе
   * ограничений внизу. Читающий отчёт видит оценку и действовавшие правила
   * одним взглядом: оценка без правил, при которых она получена, ничего не
   * значит, а внизу страницы её никто не ищет.
   */
  App.prototype._renderReportRules = function (p) {
    var node = el('report-rules');
    if (!node) {
      var lede = el('report-lede');
      if (!lede || !lede.parentNode) return;
      try {
        node = document.createElement('p');
        node.id = 'report-rules';
        node.className = 'report__rules';
        lede.parentNode.insertBefore(node, lede.nextSibling);
      } catch (e) { return; }
    }
    var state = p.present ? (p.allowSearch ? 'warn' : 'ok') : 'off';
    var html;
    if (!p.present) {
      // Одна фраза, а не отметка плюс пояснение: в шапке отчёта повтор одного
      // и того же утверждения двумя разными формулировками только мешает.
      html = this._esc('Правила экзамена проктором не задавались: был открыт только ' +
                       'локальный тест на этом компьютере.');
    } else {
      var head = p.hash
        ? 'Действовал профиль экзамена <span class="mono">' + this._esc(p.hash) + '</span>. '
        : this._esc('Действовал профиль экзамена, хеш оболочкой не передан. ');
      html = head + this._esc('Поисковые системы ' +
        (p.allowSearch ? 'были разрешены правилами экзамена.' : 'были запрещены.'));
    }
    node.setAttribute('data-state', state);
    node.innerHTML =
      '<span class="rules__mark" aria-hidden="true">' + ruleSvg(state) + '</span> ' +
      '<span>' + html + '</span>';
  };

  /** Перерисовать все три места, где показаны правила. Идемпотентно. */
  App.prototype._renderRules = function () {
    var p = this._profile();
    this._renderRulesBlock(
      this._rulesHost('consent-rules', 'screen-consent', '.disclose'), p, 'consent');
    this._renderRulesBlock(
      this._rulesHost('preflight-rules', 'screen-preflight', '.checks'), p, 'preflight');
    this._renderReportRules(p);
    if (this._scheduleConsentFit) this._scheduleConsentFit();
  };

  // --- экран проверок ---

  App.prototype._initChecks = function () {
    for (var i = 0; i < CHECKS.length; i++) {
      this.checks[CHECKS[i].id] = { state: 'pending', note: '' };
    }
    this._renderChecks();
  };

  App.prototype._setCheck = function (id, state, note) {
    var c = this.checks[id];
    if (!c) return;
    // провал не перетирается более мягким статусом в рамках одной проверки
    if (c.state === 'fail' && state !== 'fail' && !this._recheckAt) return;
    c.state = state;
    c.note = note || '';
    this._renderChecks();
  };

  /** Безусловная установка состояния — для живых данных оболочки, а не инцидентов. */
  App.prototype._forceCheck = function (id, state, note) {
    var c = this.checks[id];
    if (!c) return;
    if (c.state === state && c.note === note) return;
    c.state = state;
    c.note = note || '';
    this._renderChecks();
  };

  /** Проверки, которые остались в ожидании дольше окна ожидания, считаем пройденными:
   *  сайдкар сообщает только о найденных проблемах. */
  App.prototype._resolvePending = function () {
    if (!this.envGraceStartedAt) return;
    if (Date.now() - this.envGraceStartedAt < ENV_GRACE_MS) {
      var self = this;
      if (!this._graceTimer) {
        this._graceTimer = setTimeout(function () {
          self._graceTimer = null;
          self._resolvePending();
        }, ENV_GRACE_MS);
      }
      return;
    }
    if (!this.bridge.sawAny) return;

    var envIds = ['displays', 'remote', 'vcam', 'vm', 'procs'];
    for (var i = 0; i < envIds.length; i++) {
      if (this.checks[envIds[i]].state === 'pending') {
        this._setCheck(envIds[i], 'ok', 'отклонений не найдено');
      }
    }
    if (this.checks.mic.state === 'pending') {
      this._setCheck('mic', this.caps && this.caps.audio === false ? 'warn' : 'ok',
        this.caps && this.caps.audio === false ? 'аудиоканал отключён' : 'микрофон доступен');
    }
    if (this.checks.camera.state === 'pending' && this.caps && this.caps.vision !== false) {
      this._setCheck('camera', 'ok', 'камера доступна');
    }
    this._renderChecks();
  };

  /**
   * Список проверок. Состояние несут три носителя одновременно: форма значка,
   * слово статуса и пояснение. Цвет — четвёртый, вспомогательный.
   */
  App.prototype._renderChecks = function () {
    var list = el('checks');
    if (!list) return;

    var html = '';
    for (var i = 0; i < CHECKS.length; i++) {
      var def = CHECKS[i];
      var st = this.checks[def.id] || { state: 'pending', note: '' };
      var word = STATE_TEXT[st.state] || st.state;
      var note = st.note ? this._esc(st.note) : '';
      html += '<li class="chk is-' + st.state + '" data-state="' + st.state + '" data-check="' + def.id + '">' +
                '<span class="chk__mark">' + markSvg(st.state) + '</span>' +
                '<span class="chk__text">' +
                  '<span class="chk__title">' + this._esc(def.title) + '</span>' +
                  '<span class="chk__hint">' + this._esc(def.hint) + '</span>' +
                '</span>' +
                '<span class="chk__state">' +
                  '<span class="chk__word">' + this._esc(word) + '</span>' +
                  (note ? '<span class="chk__note muted">' + note + '</span>' : '') +
                '</span>' +
              '</li>';
    }
    list.innerHTML = html;
    list.setAttribute('role', 'list');

    this._updateStartGate();
  };

  /**
   * Кнопка старта и строка рядом с ней. Пока есть красный пункт, старт
   * заблокирован, а текст называет мешающий пункт и действие.
   * Формула гайда: наблюдаемый факт → контекст → понятное действие.
   */
  App.prototype._updateStartGate = function () {
    var failed = [], pending = [], unavailable = [];
    for (var i = 0; i < CHECKS.length; i++) {
      var def = CHECKS[i];
      var st = this.checks[def.id] || { state: 'pending', note: '' };
      if (st.state === 'fail') failed.push({ def: def, note: st.note });
      else if (st.state === 'pending') pending.push({ def: def, note: st.note });
      else if (st.state === 'warn') unavailable.push({ def: def, note: st.note });
    }

    var blocked = failed.length > 0 || pending.length > 0;
    var btn = el('btn-start');
    var line = this._blockLine();
    var state = 'ready';
    var text = '';

    if (failed.length === 1) {
      // факт → контекст → действие (формула гайда, раздел Alerts)
      state = 'blocked';
      text = 'Старт заблокирован пунктом «' + failed[0].def.title + '»' +
             (failed[0].note ? ' — ' + failed[0].note : '') + '. ' +
             failed[0].def.hint + '. Устраните и нажмите «Повторить проверки».';
    } else if (failed.length > 1) {
      state = 'blocked';
      var names = [];
      for (var j = 0; j < failed.length; j++) {
        names.push('«' + failed[j].def.title + '»' + (failed[j].note ? ' — ' + failed[j].note : ''));
      }
      text = 'Старт заблокирован, не пройдено пунктов: ' + failed.length + '. ' +
             names.join('; ') + '. Устраните и нажмите «Повторить проверки».';
    } else if (pending.length) {
      state = 'pending';
      var waiting = [];
      for (var k = 0; k < pending.length && k < 4; k++) waiting.push('«' + pending[k].def.title + '»');
      text = 'Проверки ещё идут: ' + waiting.join(', ') +
             (pending.length > 4 ? ' и ещё ' + (pending.length - 4) : '') +
             '. Кнопка включится, когда у всех пунктов появится статус.';
    } else if (unavailable.length) {
      state = 'ready-degraded';
      var off = [];
      for (var m = 0; m < unavailable.length; m++) {
        off.push('«' + unavailable[m].def.title + '»' + (unavailable[m].note ? ' — ' + unavailable[m].note : ''));
      }
      text = 'Можно начинать. Часть проверок выполнить нельзя: ' + off.join('; ') +
             '. По этим каналам наблюдение не ведётся, и это будет отмечено в отчёте.';
    } else {
      state = 'ready';
      text = 'Все пункты пройдены. Можно начинать тест.';
    }

    if (line) {
      line.textContent = text;
      line.setAttribute('data-state', state);
      line.classList.toggle('is-blocked', state === 'blocked');
      line.classList.toggle('is-pending', state === 'pending');
      line.classList.toggle('is-ready', state === 'ready' || state === 'ready-degraded');
    }
    if (btn) {
      btn.disabled = blocked;
      btn.setAttribute('aria-describedby', 'preflight-block');
      btn.setAttribute('title', blocked ? text : 'Начать тест');
    }
  };

  /** Строка причины блокировки — рядом с кнопкой старта. Создаётся при нужде. */
  App.prototype._blockLine = function () {
    var node = el('preflight-block');
    if (node) return node;
    var btn = el('btn-start');
    if (!btn || !btn.parentNode) return null;
    try {
      node = document.createElement('p');
      node.id = 'preflight-block';
      node.className = 'preflight-block';
      node.setAttribute('aria-live', 'polite');
      btn.parentNode.insertBefore(node, btn);
      return node;
    } catch (e) { return null; }
  };

  App.prototype._setNote = function (text) {
    var n = el('preflight-note');
    if (!n) return;
    n.textContent = text;
    n.hidden = !text;
  };

  App.prototype._wirePreflight = function () {
    var self = this;
    var recheck = el('btn-recheck');
    var start = el('btn-start');

    if (recheck) {
      recheck.addEventListener('click', function () {
        self._recheckAt = Date.now();
        self._initChecks();
        self.envGraceStartedAt = Date.now();
        self._setNote('');
        self.demoMode = false;
        self._loadCapabilities();
        setTimeout(function () { self._recheckAt = 0; }, 400);
        self._resolvePending();
      });
    }

    if (start) {
      start.addEventListener('click', function () {
        if (start.disabled) return;
        self._beginSession();
      });
    }
  };

  App.prototype._beginSession = function () {
    var self = this;
    if (!this.meta) this.meta = this._readMeta();
    this.sessionStartedAt = Date.now();
    this.sessionId = localSessionId();
    this.hud.setSessionId(this.sessionId);

    this.bridge.sessionStart(this.meta).then(function (res) {
      var id = null;
      if (typeof res === 'string') id = res;
      else if (res && typeof res === 'object') id = res.session_id || res.id || res.session || null;
      if (id) {
        self.sessionId = String(id);
        self.hud.setSessionId(self.sessionId);
      }
    });

    this.calibration.reset();
    this.show('calibration');
  };

  function localSessionId() {
    var d = new Date();
    return 'S-' + d.getFullYear() + pad2(d.getMonth() + 1) + pad2(d.getDate()) + '-' +
           pad2(d.getHours()) + pad2(d.getMinutes()) + pad2(d.getSeconds());
  }

  // --- калибровка -> экзамен ---

  App.prototype._wireCalibration = function () {
    var self = this;
    var next = el('btn-calib-next');
    if (next) {
      next.addEventListener('click', function () {
        self.show('exam');
        self.exam.start(self.meta ? self.meta.exam_id : 'Экзамен');
      });
    }
  };

  // --- отчёт ---

  App.prototype.onExamFinished = function (res) {
    this.examResult = res;
    this.telemetry.detachAll();
    if (res && res.reason !== 'lock') this.bridge.sessionEnd(res.reason || 'student');
    if (this.locked) return;   // при блокировке отчёт откроется с экрана блокировки
    this.showReport();
  };

  App.prototype.showReport = function () {
    this.show('report');
    this._renderReport();
  };

  App.prototype._renderReport = function () {
    var sum = this.hud.summary();
    var tele = this.telemetry.sessionSummary();

    // Отметка о правилах — в шапке, рядом с вердиктом, и обновляется здесь же:
    // профиль мог дойти уже во время теста.
    this._renderReportRules(this._profile());

    var score = Math.max(0, Math.min(100, sum.score || 0));
    var lv = this._levelOf(score);
    var ring = el('report-ring');
    var wrap = el('report-ring-wrap');
    var ringLen = this._decorateReportRing(ring);
    if (ring) {
      ring.style.strokeDashoffset = String((ringLen * (1 - score / 100)).toFixed(2));
      ring.style.stroke = this._riskColor(score);
    }
    if (wrap) {
      wrap.className = 'ring ring--lg ' + lv.cls;
      // уровень читается словом, а не только цветом кольца
      wrap.setAttribute('data-level', lv.text);
    }
    setText('report-score', String(Math.round(score)));
    setText('report-level', lv.text);
    setText('report-incidents', String(sum.total));
    setText('report-high', String(sum.high));
    setText('report-session', this.sessionId || '—');

    var dur = this.examResult ? this.examResult.elapsed_ms : (Date.now() - this.sessionStartedAt);
    setText('report-duration', fmtDuration(dur));

    var answered = this.examResult ? this.examResult.answered : 0;
    var total = this.examResult ? this.examResult.total : this.exam.questionCount();
    var reasonText = {
      student: 'Тест завершён участником.',
      timeout: 'Время теста истекло.',
      lock: 'Сессия закрыта системой: накопленный риск превысил порог блокировки.'
    };
    var lede = (this.examResult && reasonText[this.examResult.reason]) || 'Сессия завершена.';
    lede += ' Отвечено ' + answered + ' из ' + total + ' вопросов. ' +
            'Телеметрия набора: ' + tele.keystrokes + ' интервалов, средний ' +
            (tele.mean_ms ? tele.mean_ms + ' мс' : '—') +
            (tele.pastes ? ', вставок: ' + tele.pastes : '') + '. ' +
            'Итоговое решение принимает экзаменатор.';
    setText('report-lede', lede);

    this._renderBreakdown(sum.breakdown, sum.events);
    this._renderLog(sum.events);
  };

  /**
   * Кольцо итогового риска: длина пунктира считается от фактического радиуса из
   * разметки, а на кольцо один раз наносятся засечки порогов 30 / 60 / 90.
   * Засечки — нецветовая подсказка: видно, какой порог дуга прошла, даже если
   * цвет не читается. Возвращает длину окружности для dashoffset.
   */
  App.prototype._decorateReportRing = function (ring) {
    var fallbackLen = (this.text && this.text.ringLen) ? this.text.ringLen : 2 * Math.PI * 42;
    if (!ring || typeof ring.getAttribute !== 'function') return fallbackLen;

    var r = parseFloat(ring.getAttribute('r')) || 42;
    var len = 2 * Math.PI * r;
    ring.style.strokeDasharray = String(len.toFixed(2));

    var svg = ring.ownerSVGElement || ring.parentNode;
    if (!svg || typeof svg.getAttribute !== 'function' || el('report-ring-ticks')) return len;

    // Дуга окружности начинается на 3 часах, а растёт с 12: поворот уже делает
    // правило `.ring svg { rotate(-90deg) }`. Если его нет — компенсируем сами,
    // ровно так же, как это делает hud.js, иначе засечки уедут относительно дуги.
    var rotated = false;
    try {
      var cs = (typeof window.getComputedStyle === 'function') ? window.getComputedStyle(svg) : null;
      if (cs && cs.transform && cs.transform !== 'none') rotated = true;
    } catch (e) { /* нет getComputedStyle — считаем, что поворота нет */ }

    try {
      var NS = 'http://www.w3.org/2000/svg';
      var g = document.createElementNS(NS, 'g');
      g.setAttribute('id', 'report-ring-ticks');
      g.setAttribute('aria-hidden', 'true');
      if (!rotated) {
        g.style.transform = 'rotate(-90deg)';
        g.style.transformOrigin = '50% 50%';
      }
      var th = this._thresholds();
      var marks = [th.warn, th.pause, th.lock];
      for (var i = 0; i < marks.length; i++) {
        var a = (marks[i] / 100) * 2 * Math.PI;
        var line = document.createElementNS(NS, 'line');
        line.setAttribute('x1', (50 + Math.cos(a) * (r - 6)).toFixed(2));
        line.setAttribute('y1', (50 + Math.sin(a) * (r - 6)).toFixed(2));
        line.setAttribute('x2', (50 + Math.cos(a) * (r + 6)).toFixed(2));
        line.setAttribute('y2', (50 + Math.sin(a) * (r + 6)).toFixed(2));
        line.setAttribute('class', 'ring__tick');
        line.setAttribute('stroke-width', '1.4');
        // цвет засечки задаёт styles.css (.ring__tick / .sheet .ring__tick);
        // currentColor — только чтобы линия не исчезла без этих правил
        line.setAttribute('stroke', 'currentColor');
        g.appendChild(line);
      }
      svg.insertBefore(g, ring);
    } catch (e) { /* без засечек кольцо остаётся читаемым */ }
    return len;
  };

  App.prototype._renderBreakdown = function (breakdown, events) {
    var box = el('report-breakdown');
    if (!box) return;
    var items = [];

    if (breakdown && breakdown.length) {
      for (var i = 0; i < breakdown.length; i++) {
        var b = breakdown[i];
        if (!b || typeof b.contribution !== 'number' || b.contribution <= 0) continue;
        items.push({ kind: b.kind, value: b.contribution, count: normCount(b.count) });
      }
    } else if (events && events.length) {
      // разложение не пришло — считаем по числу инцидентов каждого типа
      var agg = {};
      for (var j = 0; j < events.length; j++) {
        var k = events[j].kind;
        agg[k] = (agg[k] || 0) + 1;
      }
      var keys = Object.keys(agg);
      for (var m = 0; m < keys.length; m++) {
        items.push({ kind: keys[m], value: agg[keys[m]], count: normCount(agg[keys[m]]) });
      }
    }

    if (!items.length) {
      box.innerHTML = '<li class="bars__empty">Вклад не зафиксирован: отклонений нет.</li>';
      return;
    }
    items.sort(function (a, b) { return b.value - a.value; });
    var max = items[0].value || 1;
    var names = (this.text && this.text.event) || {};
    var shorts = (this.text && this.text.short) || {};
    var html = '';
    for (var n = 0; n < items.length && n < 8; n++) {
      var it = items[n];
      var name = names[it.kind] || shorts[it.kind] || it.kind;
      html += '<li data-kind="' + this._esc(it.kind) + '">' +
                '<div class="bar__head">' +
                  // it.count уже прошёл normCount(): это единственная интерполяция
                  // в модуле без _esc, и безопасна она ровно потому, что гарантированно число
                  '<span class="bar__name">' + this._esc(name) + ' ×' + it.count + '</span>' +
                  '<span class="bar__val mono">' + Math.round(it.value) + '</span>' +
                '</div>' +
                '<div class="bar__track"><div class="bar__fill" style="width:' +
                  ((it.value / max) * 100).toFixed(1) + '%;background:' +
                  this._riskColor(Math.min(100, it.value)) + '"></div></div>' +
              '</li>';
    }
    box.innerHTML = html;
  };

  App.prototype._renderLog = function (events) {
    var box = el('report-log');
    if (!box) return;
    if (!events || !events.length) {
      box.innerHTML = '<li class="log__empty">Инцидентов не зафиксировано.</li>';
      return;
    }
    var hhmmss = (this.text && this.text.hhmmss) ? this.text.hhmmss : function () { return '--:--:--'; };
    var html = '';
    for (var i = events.length - 1; i >= 0; i--) {
      var ev = events[i];
      var sev = this._esc(ev.severity || 'medium');
      html += '<li data-sev="' + sev + '">' +
                '<span class="log__time mono">' + this._esc(hhmmss(ev.ts)) + '</span>' +
                '<span class="log__msg">' +
                  '<span class="log__sev sev-' + sev + '" aria-hidden="true"></span>' +
                  this._esc(this._eventText(ev)) +
                '</span>' +
              '</li>';
    }
    box.innerHTML = html;
  };

  App.prototype._wireReport = function () {
    var self = this;
    var btn = el('btn-report');
    if (!btn) return;
    btn.addEventListener('click', function () {
      var ok = self.bridge.sendCommand('export_report');
      var hint = el('report-hint');
      if (hint) {
        hint.textContent = ok
          ? 'Запрошена сборка отчёта: HTML-файл появится в каталоге сессии ' + (self.sessionId || '') + '.'
          : 'Ядро недоступно — отчёт собрать нельзя. Файлы сессии остаются в каталоге evidence.';
      }
    });
  };

  // ------------------------------------------------------------------ старт

  function start() {
    var app = new App();
    window.Proctor.app = app;
    app.boot();
  }

  window.Proctor = window.Proctor || {};
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();

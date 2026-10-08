/* ===========================================================================
 * calibration.js — калибровка перед экзаменом по паттерну сканирования из
 * гайда: тёмное поле наблюдения, овал, перекрестье, сканлайн, прогресс, статус.
 *
 * Стадии (docs/CONTRACT.md, сообщение calibrate):
 *   identity    — эталон лица (рамка + отсчёт)
 *   gaze_center — нулевая точка взгляда (7 с в центр)
 *   gaze_grid   — карта экрана, 9 точек по 1.5 с
 *   voice       — эталон голоса (3 с речи)
 *
 * СПИСОК СТАДИЙ НЕ ЗАХАРДКОЖЕН. Он собирается из hello.capabilities сайдкара:
 * недоступный канал -> стадия помечается пропущенной и НЕ выполняется. Важный
 * случай — решение Р-10: в режиме `classroom` анализ звука выключен намеренно,
 * поэтому стадия голоса там пропускается. Пропущенная стадия остаётся видимой
 * в списке с причиной: честная деградация должна читаться, а не прятаться.
 *
 * Прогресс ведётся локально по таймеру, но сообщения calibration от сайдкара
 * (progress/done/result) имеют приоритет — экран честен и с реальным сайдкаром,
 * и в деградированном режиме без CV-модулей.
 *
 * Исключение — карта экрана. Точки обходит ОБОЛОЧКА, и ответ сайдкара по
 * отдельной точке этап не заканчивает. Точка держится, пока сайдкар не
 * сообщит, что набрал её кадры (point_done), но не меньше POINT_MIN_MS и не
 * больше POINT_MAX_MS: на медленной камере точке нужно больше времени, а
 * фиксированные 1.5 с оставляли бы карту без кадров. Если сайдкар по точке
 * молчит (нет моста, мок, деградация) — GRID_MS. После последней точки уходит
 * calibrate {stage:'gaze_grid', point:null} — «обход закончен, строй карту», —
 * и этап ждёт итог (done) не дольше GRID_RESULT_WAIT_MS.
 *
 * Плохой итог этапа (grade 'poor', ok:false, карта не построена) не
 * проглатывается: калибровка встаёт на паузу и предлагает «Повторить этап»
 * или «Продолжить» с честным объяснением, чем это обернётся.
 *
 * Текстовый статус обязателен. Анимация (сканлайн, пульс точки, волна) только
 * поддерживает процесс: при prefers-reduced-motion она гаснет, а объяснение
 * «что происходит» и «что делать» остаётся на месте.
 *
 * Все значения цвета и размера — из shell/renderer/tokens.css.
 * =========================================================================== */

(function () {
  'use strict';

  var GRID_MS = 1500;
  var POINT_MIN_MS = 900;
  var POINT_MAX_MS = 3000;
  /** Сколько ждать карту экрана после обхода: расчёт — один кадр CV-потока. */
  var GRID_RESULT_WAIT_MS = 4000;

  /**
   * Описание стадий. cap — ключ из hello.capabilities.
   *
   * what — что в этот момент делает система (прозрачность: студент имеет право
   *        знать, что именно считается и что сохраняется).
   * todo — что сделать человеку.
   */
  var STAGE_DEFS = [
    {
      key: 'identity',
      name: 'Эталон лица',
      meta: 'снимок, 3 с',
      ms: 3000,
      cap: 'identity',
      // Формулировка точная: эталон — вектор признаков в памяти процесса
      // сайдкара (detectors/identity.py). Ни снимок, ни вектор на диск не
      // пишутся, обещать студенту хранение снимка нельзя.
      what: 'Система считает вектор признаков лица и держит его в памяти программы: ни снимок, ни вектор на диск не пишутся.',
      todo: 'Расположите лицо в овале и смотрите прямо в камеру.'
    },
    {
      key: 'gaze_center',
      name: 'Центр взгляда',
      meta: '7 с в центр экрана',
      ms: 7000,
      cap: 'gaze',
      what: 'Система измеряет вашу нулевую точку взгляда и личный разброс движений — дальше она сравнивает вас с вами, а не со «средним человеком».',
      todo: 'Смотрите в центр перекрестья, голову держите прямо.'
    },
    {
      key: 'gaze_grid',
      name: 'Карта экрана',
      meta: '9 точек по 1–3 с',
      ms: 9 * GRID_MS,
      cap: 'gaze',
      what: 'Девять точек задают карту экрана. По ней система отличает взгляд в край собственного экрана от взгляда мимо экрана.',
      todo: 'Следите глазами за точкой, голову не поворачивайте.'
    },
    {
      key: 'voice',
      name: 'Эталон голоса',
      meta: '3 с речи',
      ms: 3000,
      cap: 'audio',
      what: 'Система считает числовые признаки голоса. Запись не сохраняется: на диск уходят только числа.',
      todo: 'Прочитайте фразу вслух спокойным обычным голосом.'
    }
  ];

  /** Путь обхода девяти точек: сверху слева «змейкой» вниз. */
  var GRID = [
    [0.06, 0.08], [0.50, 0.08], [0.94, 0.08],
    [0.94, 0.50], [0.50, 0.50], [0.06, 0.50],
    [0.06, 0.92], [0.50, 0.92], [0.94, 0.92]
  ];

  /** Причина пропуска стадии — словами, без «недоступно» без объяснения. */
  var SKIP_REASON = {
    identity: 'Канал проверки личности не отвечает — этап пропущен, экзамен продолжится без эталона лица.',
    gaze_center: 'Анализ взгляда недоступен — этап пропущен, оценка взгляда работать не будет.',
    gaze_grid: 'Анализ взгляда недоступен — карта экрана не строится.',
    voice: 'Микрофон недоступен — этап пропущен.'
  };

  /** Этапы, которые без камеры измерить нечем, каким бы ни был их свой флаг. */
  var NEEDS_CAMERA = { identity: true, gaze_center: true, gaze_grid: true };

  var NO_CAMERA_REASON =
    'Камера недоступна — этап пропущен: без изображения измерять нечего.';

  /** Р-10: в аудитории анализ звука выключен намеренно, это не поломка. */
  var VOICE_SKIP_CLASSROOM =
    'Режим аудитории: анализ звука выключен по умолчанию, эталон голоса не нужен.';

  /**
   * Запасные правила для узлов, которые создаёт ЭТОТ файл: бейдж состояния
   * этапа, строка телеметрии, строка «что делать», процент прогресса и вид
   * пропущенного этапа в списке.
   *
   * :where() = нулевая специфичность: styles.css перебивает их любым правилом.
   * Значения из tokens.css; запасной аргумент var() только для размеров.
   * Рабочие подписи не мельче 14px (--fs-small) — правило 6 дизайн-системы.
   */
  var FALLBACK_CSS = [
    ':where(.calib__badge){align-self:flex-start;display:inline-flex;gap:var(--space-2,8px);' +
      'align-items:center;padding:4px var(--space-2,8px);' +
      'border:1px solid var(--border-hair-dark);border-radius:99px;' +
      'font-family:var(--font-mono,monospace);font-size:var(--fs-label,13px);}',
    ':where(.calib__tele){display:block;margin-top:var(--space-2,8px);' +
      'font-size:var(--fs-label,13px);color:var(--text-muted-on-dark);}',
    ':where(.calib__todo){margin:var(--space-2,8px) 0 0;font-size:var(--fs-small,14px);' +
      'line-height:var(--lh-body,1.55);min-height:1.55em;}',
    ':where(.calib__pct){font-size:var(--fs-small,14px);font-variant-numeric:tabular-nums;}',
    ':where(.cstep__text){display:flex;flex-direction:column;gap:2px;min-width:0;}',
    // Пропущенный этап остаётся читаемым: он должен быть виден, а не спрятан.
    ':where(.cstep.is-skipped){opacity:0.6;}',
    ':where(.cstep.is-skipped) :where(.cstep__idx){font-family:var(--font-mono,monospace);}'
  ].join('\n');

  function installFallbackStyles() {
    if (document.getElementById('ds-fallback-calib')) return;
    try {
      var style = document.createElement('style');
      style.id = 'ds-fallback-calib';
      style.textContent = FALLBACK_CSS;
      document.head.appendChild(style);
    } catch (e) { /* без запасных правил экран калибровки всё равно читается */ }
  }

  function el(id) { return document.getElementById(id); }

  function mk(tag, cls, attrs) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (attrs) {
      for (var k in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, k)) node.setAttribute(k, attrs[k]);
      }
    }
    return node;
  }

  function esc(s) {
    return String(s === undefined || s === null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  /** Причина от сайдкара -> предложение для человека. */
  function sentence(s) {
    s = String(s);
    return s.charAt(0).toUpperCase() + s.slice(1) + '.';
  }

  // =========================================================================

  function Calibration() {
    this.dom = {};
    this.plan = [];            // [{def, skip, reason}] — все стадии, включая пропущенные
    this.stageIndex = -1;      // индекс в plan
    this.stageProgress = 0;
    this.remoteProgress = {};
    this.results = {};
    this.running = false;
    this.done = false;

    this.caps = null;          // null = карта каналов ещё не известна
    this.mode = null;          // 'classroom' | 'remote' | null

    this._raf = null;
    this._t0 = 0;
    this._gridIdx = -1;
    this._lastSec = -1;
    this._gridFinalizeAt = 0;  // когда ушёл сигнал «строй карту» (0 — ещё нет)
    this._gridDone = false;    // итог карты экрана пришёл
    this._pointT0 = 0;         // когда показана текущая точка сетки
    this._pointState = '';     // '' — сайдкар молчит | 'collecting' | 'done'
    this._paused = false;      // этап закончился плохо, ждём решения человека
    this._startLabel = '';

    this.sendCalibrate = null;
    this.onDone = null;
    this.onStage = null;

    this._stream = null;
    this._allowPreview = false;
  }

  /* ------------------------------------------------------------------ init */

  Calibration.prototype.init = function (opts) {
    opts = opts || {};
    var self = this;
    this.sendCalibrate = opts.sendCalibrate || null;
    this.onDone = opts.onDone || null;
    this.onStage = opts.onStage || null;

    installFallbackStyles();

    this.dom = {
      steps: el('calib-steps'),
      head: document.querySelector('.calib__stagehead'),
      title: el('calib-title'),
      hint: el('calib-hint'),
      viewport: el('calib-viewport'),
      cross: document.querySelector('.scan-visual__cross'),
      face: el('calib-face'),
      video: el('calib-video'),
      count: el('calib-count'),
      dotfield: el('calib-dotfield'),
      dot: el('calib-dot'),
      voice: el('calib-voice'),
      progress: el('calib-progress'),
      counter: el('calib-counter'),
      footer: document.querySelector('.calib__footer'),
      btnStart: el('btn-calib-start'),
      btnRetry: el('btn-calib-retry'),
      btnNext: el('btn-calib-next')
    };

    this._mountStatus();
    this._resolvePlan();
    this._renderSteps();
    if (this.dom.btnRetry) this.dom.btnRetry.disabled = true;

    if (this.dom.btnStart) {
      this._startLabel = this.dom.btnStart.textContent;
      // На паузе после плохого этапа та же кнопка значит «Продолжить».
      this.dom.btnStart.addEventListener('click', function () {
        if (self._paused) self._resume();
        else self.start();
      });
    }
    if (this.dom.btnRetry) {
      this.dom.btnRetry.addEventListener('click', function () { self.retryStage(); });
    }

    // hello — источник истины по стадиям. Подписываемся сами: app.js передаёт
    // модулю только разрешение на предпросмотр, а нам нужны каналы и режим.
    this._listenHello();
    this.reset();
  };

  /**
   * Досоздать узлы статуса, которыми управляет модуль:
   *   #calib-badge — состояние этапа словом;
   *   #calib-tele  — моноширинная телеметрия (идентификатор стадии, отсчёт);
   *   #calib-todo  — что делать человеку;
   *   #calib-pct   — общий прогресс числом.
   * Разметку index.html пишет другой агент — ничего не перетираем, только
   * дополняем, если узла нет.
   */
  Calibration.prototype._mountStatus = function () {
    var head = this.dom.head || this.dom.viewport;

    if (head && !el('calib-badge')) {
      var badge = mk('span', 'badge calib__badge', { id: 'calib-badge' });
      badge.style.setProperty('--tone', 'var(--gray-500)');
      badge.textContent = 'ожидание';
      head.insertBefore(badge, head.firstChild);
    }
    if (head && !el('calib-tele')) {
      var tele = mk('span', 'calib__tele mono', { id: 'calib-tele', 'aria-hidden': 'true' });
      tele.textContent = '—';
      head.appendChild(tele);
    }
    if (!el('calib-todo')) {
      var todo = mk('p', 'calib__todo', { id: 'calib-todo', role: 'status' });
      if (this.dom.hint && this.dom.hint.parentNode) {
        this.dom.hint.parentNode.insertBefore(todo, this.dom.hint.nextSibling);
      } else if (head) {
        head.appendChild(todo);
      }
    }
    if (this.dom.counter && this.dom.counter.parentNode && !el('calib-pct')) {
      var pct = mk('b', 'calib__pct mono', { id: 'calib-pct' });
      pct.textContent = '0%';
      this.dom.counter.parentNode.insertBefore(pct, this.dom.counter.nextSibling);
    }

    this.dom.badge = el('calib-badge');
    this.dom.tele = el('calib-tele');
    this.dom.todo = el('calib-todo');
    this.dom.pct = el('calib-pct');

    // Подпись этапа меняется — скринридер должен её услышать один раз на этап,
    // а не на каждый кадр отсчёта. Поэтому live-регион только на #calib-todo.
    if (this.dom.hint) this.dom.hint.setAttribute('aria-live', 'off');
  };

  Calibration.prototype._listenHello = function () {
    var self = this;
    var api = null;
    try { api = window.proctor || null; } catch (e) { api = null; }
    if (!api) return;
    try {
      if (typeof api.onHello === 'function') {
        api.onHello(function (h) { self.applyHello(h); });
      }
      if (typeof api.capabilities === 'function') {
        Promise.resolve(api.capabilities()).then(function (raw) { self.applyHello(raw); },
          function () { /* моста нет — остаёмся на локальном плане */ });
      }
    } catch (e) { /* деградация без моста допустима */ }
  };

  /** hello целиком: каналы + режим развёртывания (Р-10). */
  Calibration.prototype.applyHello = function (h) {
    if (!h || typeof h !== 'object') return;
    var mode = h.exam_mode || h.mode || (h.config && h.config.exam_mode) || null;
    if (mode === 'classroom' || mode === 'remote') this.mode = mode;
    var caps = (h.capabilities && typeof h.capabilities === 'object') ? h.capabilities : null;
    if (caps) this.setCapabilities(caps);
    else { this._resolvePlan(); this._renderSteps(); this._renderProgress(); }
  };

  /** Карта каналов из hello (или из app.js, если он её передаст). */
  Calibration.prototype.setCapabilities = function (caps) {
    if (!caps || typeof caps !== 'object') return;
    var src = (caps.capabilities && typeof caps.capabilities === 'object') ? caps.capabilities : caps;
    this.caps = {
      vision: src.vision !== false,
      gaze: src.gaze !== false,
      identity: src.identity !== false,
      audio: src.audio !== false,
      env: src.env !== false
    };
    var mode = src.exam_mode || caps.exam_mode || caps.mode || null;
    if (mode === 'classroom' || mode === 'remote') this.mode = mode;

    // На ходу план не переписываем: студент не должен увидеть, как у него
    // меняется список этапов посреди калибровки.
    if (!this.running) {
      this._resolvePlan();
      this._renderSteps();
      this._renderProgress();
      this._renderCounter();
    }
  };

  /**
   * Предпросмотр камеры в renderer разрешён только если сайдкар камеру НЕ
   * использует (vision недоступен) — иначе два процесса делили бы одно
   * устройство, и проверка окружения начала бы врать.
   */
  Calibration.prototype.setPreviewAllowed = function (allowed) {
    this._allowPreview = !!allowed;
  };

  /* ------------------------------------------------------------ план стадий */

  Calibration.prototype._resolvePlan = function () {
    var caps = this.caps;
    var plan = [];
    for (var i = 0; i < STAGE_DEFS.length; i++) {
      var def = STAGE_DEFS[i];
      var skip = false;
      var reason = '';

      // Голос: сначала режим развёртывания, потом доступность микрофона.
      if (def.key === 'voice' && this.mode === 'classroom') {
        skip = true;
        reason = VOICE_SKIP_CLASSROOM;
      } else if (caps && def.cap && caps[def.cap] === false) {
        skip = true;
        reason = SKIP_REASON[def.key] || 'Канал недоступен — этап пропущен.';
      } else if (caps && caps.vision === false && NEEDS_CAMERA[def.key]) {
        /*
         * Нет камеры — нечем мерять ни лицо, ни взгляд, какими бы ни были
         * отдельные флаги `identity` и `gaze`. Канал зрения — общий вход для
         * обоих: держать этап «активным» значит вести студента через
         * измерение, которого не происходит. На живом проходе 08.10 так и
         * было: ядро без камеры, а «Эталон лица» и «Карта экрана» в плане.
         */
        skip = true;
        reason = NO_CAMERA_REASON;
      }
      plan.push({ def: def, skip: skip, reason: reason });
    }
    this.plan = plan;
  };

  Calibration.prototype._activeCount = function () {
    var n = 0;
    for (var i = 0; i < this.plan.length; i++) if (!this.plan[i].skip) n++;
    return n;
  };

  /** Номер текущей активной стадии (1-based) для подписи «этап N из M». */
  Calibration.prototype._activeOrdinal = function () {
    var n = 0;
    for (var i = 0; i <= this.stageIndex && i < this.plan.length; i++) {
      if (!this.plan[i].skip) n++;
    }
    return n;
  };

  Calibration.prototype._renderSteps = function () {
    if (!this.dom.steps) return;
    var html = '';
    var ord = 0;
    for (var i = 0; i < this.plan.length; i++) {
      var item = this.plan[i];
      var cls = 'cstep';
      var mark;

      if (item.skip) {
        cls += ' is-skipped';
        mark = '—';
      } else {
        ord++;
        mark = String(ord);
        if (i < this.stageIndex || (this.done && i <= this.stageIndex)) cls += ' is-done';
        else if (i === this.stageIndex) cls += ' is-active';
      }

      html += '<li class="' + cls + '">' +
                '<span class="cstep__idx mono">' + mark + '</span>' +
                '<span class="cstep__text">' +
                  '<span class="cstep__name">' + esc(item.def.name) + '</span>' +
                  '<span class="cstep__meta">' +
                    esc(item.skip ? 'пропущен' : item.def.meta) +
                  '</span>' +
                '</span>' +
              '</li>';
    }
    this.dom.steps.innerHTML = html;
  };

  Calibration.prototype._renderCounter = function () {
    if (!this.dom.counter) return;
    var total = this._activeCount();
    if (!total) { this.dom.counter.textContent = 'нет доступных этапов'; return; }
    var ord = this.running ? this._activeOrdinal() : (this.done ? total : 0);
    this.dom.counter.textContent = 'этап ' + ord + ' из ' + total;
  };

  /* -------------------------------------------------------------- отправка */

  /** false — сообщение заведомо не ушло (нет моста): ответа ждать не надо. */
  Calibration.prototype._send = function (stage, point) {
    if (!this.sendCalibrate) return false;
    try {
      return this.sendCalibrate(stage, point || null) !== false;
    } catch (e) { return false; /* канал недоступен — продолжаем локально */ }
  };

  /* ----------------------------------------------------------------- старт */

  Calibration.prototype.start = function () {
    if (this.running) return;
    this._resolvePlan();

    this.running = true;
    this.done = false;
    this.results = {};
    this.remoteProgress = {};
    if (this.dom.btnStart) this.dom.btnStart.hidden = true;
    if (this.dom.btnNext) this.dom.btnNext.hidden = true;
    if (this.dom.btnRetry) this.dom.btnRetry.disabled = false;
    this.stageIndex = -1;

    if (!this._activeCount()) { this._complete(); return; }
    this._nextStage();
  };

  Calibration.prototype.retryStage = function () {
    if (!this.running || this.stageIndex < 0) return;
    this._setPaused(false);
    this.stageIndex--;
    this._nextStage();
  };

  /** Пауза после плохого этапа: кнопка «Начать» становится «Продолжить». */
  Calibration.prototype._setPaused = function (on) {
    this._paused = !!on;
    if (!this.dom.btnStart) return;
    this.dom.btnStart.textContent = on ? 'Продолжить' : this._startLabel;
    this.dom.btnStart.hidden = !on;
  };

  /** Человек решил идти дальше с плохим этапом: это видно в итоговой сводке. */
  Calibration.prototype._resume = function () {
    if (!this.running || !this._paused) return;
    this._setPaused(false);
    this._nextStage();
  };

  /** Перейти к следующей НЕ пропущенной стадии. */
  Calibration.prototype._nextStage = function () {
    this.stageIndex++;
    while (this.stageIndex < this.plan.length && this.plan[this.stageIndex].skip) {
      this.stageIndex++;
    }
    if (this.stageIndex >= this.plan.length) { this._complete(); return; }

    var st = this.plan[this.stageIndex].def;
    this.stageProgress = 0;
    this._t0 = Date.now();
    this._gridIdx = -1;
    this._lastSec = -1;
    this._gridFinalizeAt = 0;
    this._gridDone = false;
    this._pointT0 = 0;
    this._pointState = '';
    // Повтор этапа начинается с нуля: прогресс и итог прошлой попытки
    // иначе закончили бы новую попытку на первом же кадре.
    delete this.remoteProgress[st.key];
    delete this.results[st.key];

    if (this.dom.title) this.dom.title.textContent = st.name;
    if (this.dom.hint) this.dom.hint.textContent = st.what;
    if (this.dom.todo) this.dom.todo.textContent = st.todo;
    this._setBadge('идёт измерение', 'var(--signal)');
    this._renderCounter();
    this._renderSteps();
    this._showFor(st.key);
    if (this.onStage) { try { this.onStage(st.key, this.stageIndex); } catch (e) {} }

    // gaze_grid отправляет calibrate на каждую точку, остальные — один раз
    if (st.key === 'gaze_center') this._send(st.key, [0.5, 0.5]);
    else if (st.key !== 'gaze_grid') this._send(st.key, null);

    this._loop();
  };

  Calibration.prototype._setBadge = function (text, tone) {
    if (!this.dom.badge) return;
    this.dom.badge.textContent = text;
    this.dom.badge.style.setProperty('--tone', tone || 'var(--gray-500)');
  };

  Calibration.prototype._showFor = function (key) {
    var d = this.dom;
    var dotStage = (key === 'gaze_center' || key === 'gaze_grid');

    if (d.face) d.face.hidden = key !== 'identity';
    if (d.dotfield) d.dotfield.hidden = !dotStage;
    if (d.voice) d.voice.hidden = key !== 'voice';
    // Перекрестье — часть паттерна сканирования взгляда, на остальных этапах
    // оно только мешает читать рамку лица.
    if (d.cross) d.cross.hidden = !dotStage;

    if (key === 'identity') this._startPreview();
    else this._stopPreview();

    if (key === 'gaze_center' && d.dot) {
      d.dot.style.left = '50%';
      d.dot.style.top = '50%';
    }
  };

  /* -------------------------------------------------------------- превью */

  Calibration.prototype._startPreview = function () {
    var d = this.dom;
    if (!this._allowPreview || !d.video || !navigator.mediaDevices ||
        !navigator.mediaDevices.getUserMedia) {
      if (d.video) d.video.hidden = true;
      return;
    }
    var self = this;
    navigator.mediaDevices.getUserMedia({ video: { width: 480, height: 640 }, audio: false })
      .then(function (stream) {
        // Пока шёл запрос, этап мог закончиться: тогда поток сразу гасим,
        // иначе камера останется занятой до конца сессии.
        var item = self.plan[self.stageIndex];
        if (!self.running || !item || item.def.key !== 'identity') {
          try {
            var tr = stream.getTracks();
            for (var i = 0; i < tr.length; i++) tr[i].stop();
          } catch (e) {}
          return;
        }
        self._stream = stream;
        d.video.srcObject = stream;
        d.video.hidden = false;
      })
      .catch(function () { d.video.hidden = true; });
  };

  Calibration.prototype._stopPreview = function () {
    if (this.dom.video) this.dom.video.hidden = true;
    if (this._stream) {
      try {
        var tracks = this._stream.getTracks();
        for (var i = 0; i < tracks.length; i++) tracks[i].stop();
      } catch (e) {}
      this._stream = null;
      if (this.dom.video) this.dom.video.srcObject = null;
    }
  };

  /* ------------------------------------------------------------------ цикл */

  Calibration.prototype._loop = function () {
    var self = this;
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }

    function frame() {
      if (!self.running) return;
      var item = self.plan[self.stageIndex];
      if (!item) return;
      var st = item.def;

      var elapsed = Date.now() - self._t0;
      var local = Math.min(1, elapsed / st.ms);
      var remote = self.remoteProgress[st.key];
      if (st.key === 'gaze_grid') {
        // Обход ведёт оболочка: прогресс сайдкара по точкам не может
        // закончить этап раньше, чем показаны все точки.
        self._gridStep(st);
        self.stageProgress = self._gridIdx >= GRID.length ? 1 : Math.min(0.99,
          (self._gridIdx + Math.min(1, (Date.now() - self._pointT0) / GRID_MS)) / GRID.length);
      } else {
        self.stageProgress = (typeof remote === 'number' && remote > local)
          ? Math.min(1, remote) : local;
      }

      self._renderStage(st, elapsed);
      self._renderProgress();

      if (self.stageProgress >= 1 && (st.key !== 'gaze_grid' || self._gridSettled())) {
        self._stageDone(st);
        return;
      }
      self._raf = requestAnimationFrame(frame);
    }
    this._raf = requestAnimationFrame(frame);
  };

  /**
   * Отрисовка кадра стадии.
   *
   * Текст обновляется не чаще раза в секунду: при 60 к/с запись в DOM каждый
   * кадр ничего не добавляет глазу и зря дёргает раскладку.
   */
  Calibration.prototype._renderStage = function (st, elapsed) {
    var d = this.dom;
    var leftSec = Math.max(0, Math.ceil((st.ms - elapsed) / 1000));
    var secTick = (leftSec !== this._lastSec);
    if (secTick) this._lastSec = leftSec;

    if (st.key === 'identity') {
      if (d.count && secTick) d.count.textContent = leftSec > 0 ? String(leftSec) : '';
      if (d.todo && secTick) {
        d.todo.textContent = st.todo + ' Осталось ' + leftSec + ' с.';
      }
      if (d.tele && secTick) d.tele.textContent = 'IDENTITY · ' + pad2(leftSec) + 's';
    }

    if (st.key === 'gaze_center') {
      if (d.todo && secTick) d.todo.textContent = st.todo + ' Осталось ' + leftSec + ' с.';
      if (d.tele && secTick) d.tele.textContent = 'GAZE_CENTER · ' + pad2(leftSec) + 's';
    }

    // gaze_grid рисует _gridStep(): точка меняется по событию, а не по часам

    if (st.key === 'voice') {
      if (d.todo && secTick) {
        d.todo.textContent = st.todo + ' Идёт измерение, осталось ' + leftSec + ' с.';
      }
      if (d.tele && secTick) d.tele.textContent = 'VOICE · ' + pad2(leftSec) + 's';
    }
  };

  Calibration.prototype._renderProgress = function () {
    var total = this._activeCount();
    var frac;
    if (!total) frac = 1;
    else {
      var doneBefore = Math.max(0, this._activeOrdinal() - 1);
      frac = (doneBefore + this.stageProgress) / total;
    }
    frac = Math.max(0, Math.min(1, frac));

    if (this.dom.progress) this.dom.progress.style.width = (frac * 100).toFixed(1) + '%';
    if (this.dom.pct) {
      var shown = Math.round(frac * 100);
      if (this.dom.pct.textContent !== shown + '%') this.dom.pct.textContent = shown + '%';
    }
  };

  /**
   * Шаг обхода сетки: перейти к следующей точке, когда текущая своё отстояла.
   * После последней точки _gridIdx === GRID.length — обход закончен.
   */
  Calibration.prototype._gridStep = function (st) {
    if (this._gridIdx >= GRID.length) return;
    var now = Date.now();
    if (this._gridIdx >= 0) {
      var t = now - this._pointT0;
      var ps = this._pointState;
      var next = t >= POINT_MAX_MS ||
        (ps === 'done' && t >= POINT_MIN_MS) ||
        (ps === '' && t >= GRID_MS);
      if (!next) return;
    }
    this._gridIdx++;
    if (this._gridIdx >= GRID.length) return;
    this._pointT0 = now;
    this._pointState = '';

    var d = this.dom;
    var p = GRID[this._gridIdx];
    if (d.dot) {
      // Позиция точки — данные, а не оформление: координаты приходят из
      // карты обхода, поэтому задаются атрибутом style (см. CSP в index.html).
      d.dot.style.left = (p[0] * 100).toFixed(2) + '%';
      d.dot.style.top = (p[1] * 100).toFixed(2) + '%';
    }
    this._send('gaze_grid', [p[0], p[1]]);
    if (d.todo) {
      d.todo.textContent = 'Точка ' + (this._gridIdx + 1) + ' из ' + GRID.length + '. ' + st.todo;
    }
    if (d.tele) {
      d.tele.textContent = 'GAZE_GRID · ' + pad2(this._gridIdx + 1) + '/' + pad2(GRID.length);
    }
  };

  /** Ответ сайдкара относится к точке, которая сейчас на экране? */
  Calibration.prototype._isCurrentPoint = function (pt) {
    var cur = GRID[this._gridIdx];
    return !!(cur && Array.isArray(pt) && pt.length === 2 &&
      Math.abs(pt[0] - cur[0]) < 1e-6 && Math.abs(pt[1] - cur[1]) < 1e-6);
  };

  /**
   * Обход сетки закончен: попросить карту и ждать её итога.
   * true — можно закрывать этап (итог пришёл, ждать нечего или ждать дольше
   * нельзя; во втором случае итог честно помечается как «ответа нет»).
   */
  Calibration.prototype._gridSettled = function () {
    if (!this._gridFinalizeAt) {
      this._gridFinalizeAt = Date.now();
      if (!this._send('gaze_grid', null)) return true;
      this._setBadge('расчёт карты', 'var(--signal)');
      if (this.dom.todo) {
        this.dom.todo.textContent = 'Строим карту экрана по ' + GRID.length + ' точкам…';
      }
      if (this.dom.tele) this.dom.tele.textContent = 'GAZE_GRID · MAP';
      return false;
    }
    if (this._gridDone) return true;
    if (Date.now() - this._gridFinalizeAt < GRID_RESULT_WAIT_MS) return false;
    this.results.gaze_grid = { ok: true, screen_map_applied: false, timeout: true };
    return true;
  };

  /**
   * Что пошло не так на этапе — словами для человека; '' — всё в порядке.
   * Деградация (канала нет, мок) — не провал: о ней уже сказано в плане.
   */
  Calibration.prototype._problemOf = function (key, res) {
    if (!res || typeof res !== 'object' || res.degraded) return '';
    var q = (res.quality && typeof res.quality === 'object') ? res.quality : res;
    if (key === 'gaze_grid') {
      // Этап сетки судим по карте: короткий центр портит общую оценку, но
      // не делает хорошую карту плохой (map_grade считает сайдкар по LOO).
      if (res.screen_map_applied !== false) {
        return res.ok === false ? sentence(res.reason || 'карта экрана не построена') : '';
      }
      if (res.timeout) return 'Модуль наблюдения не ответил вовремя — карта экрана не построена.';
      if (q.map_grade === 'poor' && typeof q.loo_rmse === 'number' && q.loo_rmse > 0) {
        return 'Карта экрана получилась неточной: ошибка около ' +
          Math.round(q.loo_rmse * 100) + '% ширины экрана.';
      }
      if (res.ok === false && res.reason) return sentence(res.reason);
      return 'Карта экрана не построилась: точкам не хватило кадров с вашим взглядом.';
    }
    if (res.ok === false) return sentence(res.reason || 'измерение не удалось');
    if (q.grade === 'poor') {
      return key === 'gaze_center'
        ? 'Взгляд не удержался в центре — нулевая точка получилась неустойчивой.'
        : 'Измерение получилось неустойчивым.';
    }
    return '';
  };

  Calibration.prototype._pauseOnProblem = function (st, problem) {
    var after = st.key === 'gaze_grid'
      ? 'взгляд будет оцениваться по персональным порогам, без карты экрана, и ложных отметок станет больше.'
      : 'наблюдение по этому каналу будет грубее.';
    this._setBadge('повторите этап', 'var(--risk-warn)');
    if (this.dom.todo) {
      this.dom.todo.textContent = problem + ' «Повторить этап» — переснять; «Продолжить» — идти дальше: ' + after;
    }
    if (this.dom.tele) this.dom.tele.textContent = st.key.toUpperCase() + ' · RETRY';
    if (this.dom.count) this.dom.count.textContent = '';
    if (this.dom.btnRetry) this.dom.btnRetry.disabled = false;
    this._setPaused(true);
  };

  Calibration.prototype._stageDone = function (st) {
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
    if (!this.results[st.key]) this.results[st.key] = { ok: true };

    var problem = this._problemOf(st.key, this.results[st.key]);
    if (problem) { this._pauseOnProblem(st, problem); return; }

    this._setBadge('этап пройден', 'var(--signal)');
    if (this.dom.todo) this.dom.todo.textContent = 'Этап пройден, переходим к следующему.';
    if (this.dom.count) this.dom.count.textContent = '';

    var self = this;
    setTimeout(function () { if (self.running) self._nextStage(); }, 420);
  };

  Calibration.prototype._complete = function () {
    var hadStages = this._activeCount() > 0;
    this.running = false;
    this.done = true;
    this._stopPreview();
    this._renderSteps();

    if (this.dom.progress) this.dom.progress.style.width = '100%';
    if (this.dom.pct) this.dom.pct.textContent = '100%';
    if (this.dom.face) this.dom.face.hidden = true;
    if (this.dom.dotfield) this.dom.dotfield.hidden = true;
    if (this.dom.voice) this.dom.voice.hidden = true;
    if (this.dom.cross) this.dom.cross.hidden = false;
    if (this.dom.btnRetry) this.dom.btnRetry.disabled = true;
    if (this.dom.btnNext) this.dom.btnNext.hidden = false;
    if (this.dom.tele) this.dom.tele.textContent = hadStages ? 'READY' : 'SKIPPED';

    if (hadStages) {
      this._setBadge('калибровка завершена', 'var(--signal)');
      if (this.dom.title) this.dom.title.textContent = 'Калибровка завершена';
      if (this.dom.hint) this.dom.hint.textContent = this._summaryText();
      if (this.dom.todo) {
        this.dom.todo.textContent = 'Можно переходить к экзамену. Панель наблюдения всё время ' +
          'показывает, что именно фиксируется.';
      }
    } else {
      // Ни один канал не отвечает. Это честно говорится вслух, а не маскируется.
      this._setBadge('калибровка недоступна', 'var(--gray-500)');
      if (this.dom.title) this.dom.title.textContent = 'Калибровка недоступна';
      if (this.dom.hint) {
        this.dom.hint.textContent = 'Ни один канал наблюдения не отвечает, персональную норму ' +
          'снять нечем. Экзамен можно продолжить: оболочка работает, но оценка взгляда и ' +
          'проверка личности будут отключены.';
      }
      if (this.dom.todo) this.dom.todo.textContent = 'Сообщите организатору и продолжайте.';
    }
    this._renderCounter();

    if (this.onDone) { try { this.onDone(this.results); } catch (e) {} }
  };

  /** Перечислить словами, что именно записано, и что пропущено и почему. */
  Calibration.prototype._summaryText = function () {
    var got = [];
    var weak = [];
    var skipped = [];
    for (var i = 0; i < this.plan.length; i++) {
      var item = this.plan[i];
      var name = item.def.name.toLowerCase();
      if (item.skip) skipped.push(name);
      else if (this._problemOf(item.def.key, this.results[item.def.key])) weak.push(name);
      else got.push(name);
    }
    var s = got.length ? 'Персональная норма записана: ' + got.join(', ') + '.' : '';
    if (weak.length) {
      s += (s ? ' ' : '') + 'Не удалось: ' + weak.join(', ') +
        ' — по этим каналам наблюдение будет грубее.';
    }
    if (skipped.length) {
      s += ' Пропущено: ' + skipped.join(', ') + ' — ' + this._firstSkipReason();
    }
    return s;
  };

  Calibration.prototype._firstSkipReason = function () {
    for (var i = 0; i < this.plan.length; i++) {
      if (this.plan[i].skip && this.plan[i].reason) {
        return this.plan[i].reason.charAt(0).toLowerCase() + this.plan[i].reason.slice(1);
      }
    }
    return 'канал недоступен.';
  };

  /* ------------------------------------------------- сообщения от сайдкара */

  /**
   * Входящее calibration: {stage, progress, done, result}.
   *
   * Оценку итога (плохое качество -> пауза с выбором) делает _stageDone():
   * здесь только складываем. Качество взгляда сайдкар кладёт в result.quality
   * (LOO-кросс-валидация карты), а не в корень result.
   */
  Calibration.prototype.applyMessage = function (msg) {
    if (!msg || typeof msg !== 'object' || !msg.stage) return;
    if (typeof msg.progress === 'number') this.remoteProgress[msg.stage] = msg.progress;
    if (msg.stage === 'gaze_grid') {
      var r = (msg.result && typeof msg.result === 'object') ? msg.result : null;
      // Итог карты — только ответ на сигнал конца обхода (docs/CONTRACT.md).
      if (msg.done && this._gridFinalizeAt) {
        this.results.gaze_grid = r || { ok: true };
        this._gridDone = true;
        return;
      }
      // Ход точки: сайдкар копит её кадры или уже набрал. Деградация и мок
      // кадров не копят — тогда точка стоит обычные GRID_MS.
      if (r && !msg.done && !r.degraded && !r.mock && this._isCurrentPoint(r.point) &&
          this._pointState !== 'done') {
        this._pointState = r.point_done ? 'done' : 'collecting';
      }
      return;
    }
    // Итог — только из done: промежуточный прогресс несёт оценку ПРОШЛОЙ
    // попытки, и по ней этап, закрытый по таймеру, встал бы на ложную паузу.
    // Итог стадии, с которой оболочка уже ушла (superseded), приходит позже
    // и попадает в сводку.
    if (msg.done) {
      this.remoteProgress[msg.stage] = 1;
      this.results[msg.stage] = (msg.result && typeof msg.result === 'object')
        ? msg.result : { ok: true };
    }
  };

  /* ----------------------------------------------------------------- сброс */

  Calibration.prototype.reset = function () {
    this._setPaused(false);
    this.running = false;
    this.done = false;
    this.stageIndex = -1;
    this.stageProgress = 0;
    this.results = {};
    this.remoteProgress = {};
    this._lastSec = -1;
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
    this._stopPreview();
    this._resolvePlan();

    if (this.dom.progress) this.dom.progress.style.width = '0%';
    if (this.dom.pct) this.dom.pct.textContent = '0%';
    if (this.dom.btnStart) this.dom.btnStart.hidden = false;
    if (this.dom.btnNext) this.dom.btnNext.hidden = true;
    if (this.dom.btnRetry) this.dom.btnRetry.disabled = true;
    if (this.dom.face) this.dom.face.hidden = true;
    if (this.dom.dotfield) this.dom.dotfield.hidden = true;
    if (this.dom.voice) this.dom.voice.hidden = true;
    if (this.dom.cross) this.dom.cross.hidden = false;
    if (this.dom.count) this.dom.count.textContent = '';
    if (this.dom.tele) this.dom.tele.textContent = '—';

    var total = this._activeCount();
    this._setBadge(total ? 'ожидание' : 'нет доступных этапов', 'var(--gray-500)');
    if (this.dom.title) this.dom.title.textContent = 'Подготовка';
    if (this.dom.hint) {
      this.dom.hint.textContent = total
        ? 'Калибровка задаёт вашу персональную норму: эталон лица, нулевую точку взгляда и ' +
          'карту экрана. Без неё система сравнивала бы вас со «средним человеком» и ошибалась чаще.'
        : 'Каналы наблюдения не отвечают — калибровать нечего.';
    }
    if (this.dom.todo) {
      this.dom.todo.textContent = total
        ? 'Нажмите «Начать калибровку», когда будете готовы. Этапов: ' + total + '.'
        : 'Можно переходить к экзамену, наблюдение будет работать в урезанном виде.';
    }
    this._renderCounter();
    this._renderSteps();
  };

  /** Текущий план стадий (для отладки и экрана отчёта). */
  Calibration.prototype.stages = function () { return this.plan.slice(); };

  window.Proctor = window.Proctor || {};
  window.Proctor.calibration = new Calibration();
})();

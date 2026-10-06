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
 * Текстовый статус обязателен. Анимация (сканлайн, пульс точки, волна) только
 * поддерживает процесс: при prefers-reduced-motion она гаснет, а объяснение
 * «что происходит» и «что делать» остаётся на месте.
 *
 * Все значения цвета и размера — из shell/renderer/tokens.css.
 * =========================================================================== */

(function () {
  'use strict';

  var GRID_MS = 1500;

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
      meta: '9 точек по 1.5 с',
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
    this._gridSent = -1;
    this._lastSec = -1;

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
      this.dom.btnStart.addEventListener('click', function () { self.start(); });
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

  Calibration.prototype._send = function (stage, point) {
    if (!this.sendCalibrate) return;
    try {
      this.sendCalibrate(stage, point || null);
    } catch (e) { /* канал недоступен — продолжаем локально */ }
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
    this.stageIndex--;
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
    this._gridSent = -1;
    this._lastSec = -1;

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
      self.stageProgress = (typeof remote === 'number' && remote > local)
        ? Math.min(1, remote) : local;

      self._renderStage(st, elapsed);
      self._renderProgress();

      if (self.stageProgress >= 1) { self._stageDone(st); return; }
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

    if (st.key === 'gaze_grid') {
      var idx = Math.min(GRID.length - 1, Math.floor(elapsed / GRID_MS));
      if (idx !== this._gridIdx) {
        this._gridIdx = idx;
        var p = GRID[idx];
        if (d.dot) {
          // Позиция точки — данные, а не оформление: координаты приходят из
          // карты обхода, поэтому задаются атрибутом style (см. CSP в index.html).
          d.dot.style.left = (p[0] * 100).toFixed(2) + '%';
          d.dot.style.top = (p[1] * 100).toFixed(2) + '%';
        }
        if (idx !== this._gridSent) {
          this._gridSent = idx;
          this._send('gaze_grid', [p[0], p[1]]);
        }
        if (d.todo) {
          d.todo.textContent = 'Точка ' + (idx + 1) + ' из ' + GRID.length +
            '. ' + st.todo;
        }
        if (d.tele) {
          d.tele.textContent = 'GAZE_GRID · ' + pad2(idx + 1) + '/' + pad2(GRID.length);
        }
      }
    }

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

  Calibration.prototype._stageDone = function (st) {
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
    if (!this.results[st.key]) this.results[st.key] = { ok: true };

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
    var skipped = [];
    for (var i = 0; i < this.plan.length; i++) {
      var item = this.plan[i];
      if (item.skip) skipped.push(item.def.name.toLowerCase());
      else got.push(item.def.name.toLowerCase());
    }
    var s = 'Персональная норма записана: ' + got.join(', ') + '.';
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

  /** Входящее calibration: {stage, progress, done, result}. */
  Calibration.prototype.applyMessage = function (msg) {
    if (!msg || typeof msg !== 'object' || !msg.stage) return;
    if (typeof msg.progress === 'number') this.remoteProgress[msg.stage] = msg.progress;
    if (msg.result && typeof msg.result === 'object') this.results[msg.stage] = msg.result;
    if (msg.done) {
      this.remoteProgress[msg.stage] = 1;
      if (!this.results[msg.stage]) this.results[msg.stage] = { ok: true };
    }
    // Качество калибровки взгляда сайдкар считает честно (LOO-кросс-валидация);
    // если оно плохое, говорим об этом прямо и предлагаем повторить этап.
    var res = this.results[msg.stage];
    if (res && (res.grade === 'poor' || res.ok === false) && this.dom.todo) {
      this.dom.todo.textContent = 'Измерение получилось неустойчивым. ' +
        'Нажмите «Повторить этап»: это снизит число ложных отметок на экзамене.';
      this._setBadge('повторите этап', 'var(--risk-warn)');
    }
  };

  /* ----------------------------------------------------------------- сброс */

  Calibration.prototype.reset = function () {
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

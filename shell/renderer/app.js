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
 * =========================================================================== */

(function () {
  'use strict';

  var ENV_GRACE_MS = 3500;    // сколько ждём проверок окружения после первого ответа ядра
  var LINK_TIMEOUT_MS = 6000; // нет ни одного сообщения за это время — считаем ядро недоступным
  var LINK_STALE_MS = 7000;   // ядро молчит дольше — связь деградировала

  var SCREENS = ['consent', 'preflight', 'calibration', 'exam', 'report'];

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

  function el(id) { return document.getElementById(id); }
  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  function fmtDuration(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    return pad2(Math.floor(s / 60)) + ':' + pad2(s % 60);
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
    this.text = window.Proctor.text;

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

  App.prototype.boot = function () {
    var self = this;

    this.telemetry.init(function (msg) { self.bridge.sendTelemetry(msg); });

    this.hud.init({
      onPause: function () { self.exam.pause(); },
      onResume: function () { self.exam.resume(); },
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

    this._initChecks();
    this._subscribe();
    this._loadCapabilities();
    this._startLinkWatchdog();

    this.calibration.reset();
    this.show('consent');
  };

  // --- экраны ---

  App.prototype.show = function (name) {
    if (SCREENS.indexOf(name) === -1) return;
    this.screen = name;
    for (var i = 0; i < SCREENS.length; i++) {
      var node = el('screen-' + SCREENS[i]);
      if (node) node.hidden = SCREENS[i] !== name;
    }
    var active = SCREENS.indexOf(name);
    var steps = document.querySelectorAll('#stepper .step');
    for (var j = 0; j < steps.length; j++) {
      steps[j].classList.toggle('is-active', j === active);
      steps[j].classList.toggle('is-done', j < active);
    }
    // HUD виден на калибровке и экзамене: студент сразу видит, что фиксируется
    var withHud = (name === 'calibration' || name === 'exam');
    if (withHud) this.hud.show(); else this.hud.hide();
    var root = el('app');
    if (root) root.classList.toggle('has-hud', withHud);
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
      var msg = this.text.eventText(ev);
      this._setCheck(chk, 'fail', msg.length > 42 ? msg.slice(0, 41) + '…' : msg);
    }
  };

  App.prototype.onVerdict = function (v) {
    this.hud.applyVerdict(v);
  };

  App.prototype.onLocked = function () {
    this.locked = true;
    if (this.exam.running) this.exam.finish('lock');
    this.bridge.sessionEnd('lock');
  };

  // --- возможности ядра ---

  App.prototype._loadCapabilities = function () {
    var self = this;
    this.bridge.capabilities().then(function (raw) {
      self.capsLoaded = true;
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

  App.prototype._readMeta = function () {
    var name = el('in-name'), student = el('in-student'), exam = el('in-exam');
    return {
      student_name: (name && name.value.trim()) || 'Без имени',
      student_id: (student && student.value.trim()) || 'ST-UNKNOWN',
      exam_id: (exam && exam.value.trim()) || 'EX-DEMO'
    };
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
        this._setCheck(envIds[i], 'ok', 'нарушений не найдено');
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

  App.prototype._renderChecks = function () {
    var list = el('checks');
    if (!list) return;
    var html = '';
    for (var i = 0; i < CHECKS.length; i++) {
      var def = CHECKS[i];
      var st = this.checks[def.id] || { state: 'pending', note: '' };
      html += '<li class="chk is-' + st.state + '">' +
                '<span class="chk__mark">' + markSvg(st.state) + '</span>' +
                '<span class="chk__text">' +
                  '<span class="chk__title">' + def.title + '</span>' +
                  '<span class="chk__hint">' + def.hint + '</span>' +
                '</span>' +
                '<span class="chk__state">' + (st.note || STATE_TEXT[st.state]) + '</span>' +
              '</li>';
    }
    list.innerHTML = html;

    var blocked = false, pending = false;
    for (var j = 0; j < CHECKS.length; j++) {
      var s = this.checks[CHECKS[j].id].state;
      if (s === 'fail') blocked = true;
      if (s === 'pending') pending = true;
    }
    var btn = el('btn-start');
    if (btn) btn.disabled = blocked || pending;
  };

  function markSvg(state) {
    if (state === 'pending') {
      return '<svg viewBox="0 0 28 28"><circle cx="14" cy="14" r="11"></circle></svg>';
    }
    if (state === 'ok') {
      return '<svg viewBox="0 0 28 28"><circle cx="14" cy="14" r="11"></circle>' +
             '<path d="M8.5 14.4 L12.4 18 L19.5 10.4"></path></svg>';
    }
    if (state === 'warn') {
      return '<svg viewBox="0 0 28 28"><circle cx="14" cy="14" r="11"></circle>' +
             '<path d="M14 8.6 L14 15.4 M14 19.1 L14 19.2"></path></svg>';
    }
    return '<svg viewBox="0 0 28 28"><circle cx="14" cy="14" r="11"></circle>' +
           '<path d="M9.6 9.6 L18.4 18.4 M18.4 9.6 L9.6 18.4"></path></svg>';
  }

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
    var T = this.text;

    var score = Math.max(0, Math.min(100, sum.score || 0));
    var lv = T.levelOf(score);
    var ring = el('report-ring');
    var wrap = el('report-ring-wrap');
    if (ring) {
      ring.style.strokeDashoffset = String(T.ringLen * (1 - score / 100));
      ring.style.stroke = T.colorFor(score);
    }
    if (wrap) wrap.className = 'ring ring--lg ' + lv.cls;
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
      lock: 'Сессия закрыта системой: риск превысил порог блокировки.'
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

  App.prototype._renderBreakdown = function (breakdown, events) {
    var box = el('report-breakdown');
    if (!box) return;
    var T = this.text;
    var items = [];

    if (breakdown && breakdown.length) {
      for (var i = 0; i < breakdown.length; i++) {
        var b = breakdown[i];
        if (!b || typeof b.contribution !== 'number' || b.contribution <= 0) continue;
        items.push({ kind: b.kind, value: b.contribution, count: b.count || 1 });
      }
    } else if (events && events.length) {
      // разложение не пришло — считаем по числу инцидентов каждого типа
      var agg = {};
      for (var j = 0; j < events.length; j++) {
        var k = events[j].kind;
        agg[k] = (agg[k] || 0) + 1;
      }
      var keys = Object.keys(agg);
      for (var m = 0; m < keys.length; m++) items.push({ kind: keys[m], value: agg[keys[m]], count: agg[keys[m]] });
    }

    if (!items.length) {
      box.innerHTML = '<li class="bars__empty">Вклад не зафиксирован: нарушений нет.</li>';
      return;
    }
    items.sort(function (a, b) { return b.value - a.value; });
    var max = items[0].value || 1;
    var html = '';
    for (var n = 0; n < items.length && n < 8; n++) {
      var it = items[n];
      var name = T.event[it.kind] || T.short[it.kind] || it.kind;
      html += '<li>' +
                '<div class="bar__head">' +
                  '<span class="bar__name">' + T.escapeHtml(name) + ' ×' + it.count + '</span>' +
                  '<span class="bar__val">' + Math.round(it.value) + '</span>' +
                '</div>' +
                '<div class="bar__track"><div class="bar__fill" style="width:' +
                  ((it.value / max) * 100).toFixed(1) + '%;background:' + T.colorFor(Math.min(100, it.value)) + '"></div></div>' +
              '</li>';
    }
    box.innerHTML = html;
  };

  App.prototype._renderLog = function (events) {
    var box = el('report-log');
    if (!box) return;
    var T = this.text;
    if (!events || !events.length) {
      box.innerHTML = '<li class="log__empty">Инцидентов не зафиксировано.</li>';
      return;
    }
    var html = '';
    for (var i = events.length - 1; i >= 0; i--) {
      var ev = events[i];
      html += '<li>' +
                '<span class="log__time">' + T.hhmmss(ev.ts) + '</span>' +
                '<span class="log__msg">' +
                  '<span class="log__sev sev-' + T.escapeHtml(ev.severity || 'medium') + '"></span>' +
                  T.escapeHtml(T.eventText(ev)) +
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

  function setText(id, value) {
    var node = el(id);
    if (node) node.textContent = value;
  }

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

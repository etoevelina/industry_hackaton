/* ===========================================================================
 * hud.js — панель прокторинга поверх экзамена.
 *
 * Принцип «гуманного прокторинга»: студент в любой момент видит то же, что
 * видит система. Эскалация мягкая и предсказуемая:
 *   verdict warn  -> ненавязчивое уведомление внизу экрана, само исчезает;
 *   verdict pause -> полупрозрачный оверлей, таймер остановлен, условия возврата;
 *   verdict lock  -> экран блокировки с номером сессии.
 *
 * Пороги и веса берутся из sidecar/protocol.py (RISK_WARN=30, RISK_PAUSE=60,
 * RISK_LOCK=90) — здесь они продублированы как константы отображения.
 * =========================================================================== */

(function () {
  'use strict';

  var RISK_WARN = 30.0;
  var RISK_PAUSE = 60.0;
  var RISK_LOCK = 90.0;

  var RING_LEN = 263.9;  // 2*pi*42 — длина окружности кольца в разметке

  /** Русские названия инцидентов — на случай пустого message от сайдкара. */
  var EVENT_TEXT = {
    PHONE_IN_FRAME: 'Телефон в кадре',
    PHONE_RAISED: 'Телефон поднят к лицу',
    PHONE_AIMED_AT_SCREEN: 'Телефон направлен на экран',
    FORBIDDEN_OBJECT: 'Посторонний предмет в кадре',

    NO_FACE: 'Лицо не найдено в кадре',
    SECOND_FACE: 'В кадре второй человек',
    IDENTITY_MISMATCH: 'За компьютером другой человек',
    LIVENESS_FAIL: 'Подозрение на фото или запись вместо живого лица',

    GAZE_DOWN: 'Взгляд опущен вниз',
    GAZE_SIDE: 'Взгляд уведён в сторону',
    GAZE_OFF_SCREEN: 'Взгляд вне экрана',
    HEAD_TURNED: 'Голова отвёрнута от экрана',

    VOICE_OTHER: 'Слышен посторонний голос',
    SPEECH_WITHOUT_LIP_MOTION: 'Речь без движения губ — возможна гарнитура',

    VIRTUAL_CAMERA: 'Обнаружена виртуальная камера',
    REMOTE_ACCESS_SOFTWARE: 'Запущено ПО удалённого доступа',
    VIRTUAL_MACHINE: 'Признаки виртуальной машины',
    SCREEN_RECORDING: 'Идёт запись экрана',
    MULTIPLE_DISPLAYS: 'Подключён второй монитор',
    BLACKLISTED_PROCESS: 'Запрещённая программа запущена',

    WINDOW_BLUR: 'Окно экзамена потеряло фокус',
    FULLSCREEN_EXIT: 'Выход из полноэкранного режима',
    SHORTCUT_BLOCKED: 'Заблокирована горячая клавиша',
    CLIPBOARD_PASTE: 'Вставка из буфера обмена',
    DEVTOOLS_ATTEMPT: 'Попытка открыть инструменты разработчика',

    PASTE_BURST: 'Вставлен большой блок текста',
    TYPING_ANOMALY: 'Ритм набора не похож на ваш обычный',

    FUSION_GAZE_THEN_ANSWER: 'Взгляд в сторону, затем быстрый ответ',
    FUSION_BLUR_THEN_ANSWER: 'Переключение окна, затем быстрый ответ',
    FUSION_PHONE_THEN_ANSWER: 'Телефон в кадре, затем быстрый ответ',

    SESSION_STARTED: 'Сессия начата',
    SESSION_ENDED: 'Сессия завершена',
    CALIBRATION_DONE: 'Калибровка завершена',
    SENSOR_LOST: 'Потерян датчик: камера или микрофон'
  };

  /** Короткие подписи для списка вклада в риск. */
  var EVENT_SHORT = {
    PHONE_IN_FRAME: 'телефон',
    PHONE_RAISED: 'телефон поднят',
    PHONE_AIMED_AT_SCREEN: 'съёмка экрана',
    FORBIDDEN_OBJECT: 'предмет',
    NO_FACE: 'нет лица',
    SECOND_FACE: 'второе лицо',
    IDENTITY_MISMATCH: 'другой человек',
    LIVENESS_FAIL: 'не живое лицо',
    GAZE_DOWN: 'взгляд вниз',
    GAZE_SIDE: 'взгляд в сторону',
    GAZE_OFF_SCREEN: 'взгляд вне экрана',
    HEAD_TURNED: 'поворот головы',
    VOICE_OTHER: 'чужой голос',
    SPEECH_WITHOUT_LIP_MOTION: 'речь без губ',
    VIRTUAL_CAMERA: 'виртуальная камера',
    REMOTE_ACCESS_SOFTWARE: 'удалённый доступ',
    VIRTUAL_MACHINE: 'виртуальная машина',
    SCREEN_RECORDING: 'запись экрана',
    MULTIPLE_DISPLAYS: 'второй монитор',
    BLACKLISTED_PROCESS: 'запрещённая программа',
    WINDOW_BLUR: 'потеря фокуса',
    FULLSCREEN_EXIT: 'выход из полноэкрана',
    SHORTCUT_BLOCKED: 'хоткей',
    CLIPBOARD_PASTE: 'вставка',
    DEVTOOLS_ATTEMPT: 'devtools',
    PASTE_BURST: 'вставка блока',
    TYPING_ANOMALY: 'ритм набора',
    FUSION_GAZE_THEN_ANSWER: 'взгляд -> ответ',
    FUSION_BLUR_THEN_ANSWER: 'окно -> ответ',
    FUSION_PHONE_THEN_ANSWER: 'телефон -> ответ',
    SENSOR_LOST: 'потерян датчик'
  };

  /** Какому индикатору принадлежит инцидент. */
  var KIND_CHANNEL = {
    PHONE_IN_FRAME: 'phone', PHONE_RAISED: 'phone', PHONE_AIMED_AT_SCREEN: 'phone',
    FORBIDDEN_OBJECT: 'phone',
    NO_FACE: 'face', SECOND_FACE: 'face', LIVENESS_FAIL: 'face',
    IDENTITY_MISMATCH: 'identity',
    GAZE_DOWN: 'gaze', GAZE_SIDE: 'gaze', GAZE_OFF_SCREEN: 'gaze', HEAD_TURNED: 'gaze',
    VOICE_OTHER: 'audio', SPEECH_WITHOUT_LIP_MOTION: 'audio',
    VIRTUAL_CAMERA: 'env', REMOTE_ACCESS_SOFTWARE: 'env', VIRTUAL_MACHINE: 'env',
    SCREEN_RECORDING: 'env', MULTIPLE_DISPLAYS: 'env', BLACKLISTED_PROCESS: 'env',
    WINDOW_BLUR: 'env', FULLSCREEN_EXIT: 'env', SHORTCUT_BLOCKED: 'env',
    CLIPBOARD_PASTE: 'env', DEVTOOLS_ATTEMPT: 'env',
    SENSOR_LOST: 'env'
  };

  /** Инциденты окружения не «рассасываются» сами — держим индикатор красным. */
  var STICKY_KINDS = {
    VIRTUAL_CAMERA: 1, REMOTE_ACCESS_SOFTWARE: 1, VIRTUAL_MACHINE: 1,
    SCREEN_RECORDING: 1, MULTIPLE_DISPLAYS: 1, BLACKLISTED_PROCESS: 1
  };

  /** Служебные события не показываются как инциденты. */
  var SERVICE_KINDS = {
    SESSION_STARTED: 1, SESSION_ENDED: 1, CALIBRATION_DONE: 1
  };

  var CHANNELS = [
    { id: 'face',     name: 'Лицо',       cap: 'vision' },
    { id: 'gaze',     name: 'Взгляд',     cap: 'gaze' },
    { id: 'phone',    name: 'Телефон',    cap: 'vision' },
    { id: 'identity', name: 'Личность',   cap: 'identity' },
    { id: 'audio',    name: 'Звук',       cap: 'audio' },
    { id: 'env',      name: 'Окружение',  cap: 'env' }
  ];

  var ZONE_TEXT = {
    center: 'в центре', screen: 'на экране', up: 'вверх', down: 'вниз',
    left: 'влево', right: 'вправо', side: 'в сторону',
    off: 'вне экрана', off_screen: 'вне экрана', unknown: 'нет данных'
  };

  var SEV_RANK = { info: 0, low: 1, medium: 2, high: 3, critical: 4 };

  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  function hhmmss(tsSec) {
    var d = (typeof tsSec === 'number' && isFinite(tsSec)) ? new Date(tsSec * 1000) : new Date();
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function eventText(ev) {
    if (ev && typeof ev.message === 'string' && ev.message.trim()) return ev.message.trim();
    return (ev && EVENT_TEXT[ev.kind]) || (ev && ev.kind) || 'Событие';
  }

  function levelOf(score) {
    if (score >= RISK_LOCK) return { cls: 'lvl-lock', text: 'блокировка' };
    if (score >= RISK_PAUSE) return { cls: 'lvl-pause', text: 'высокий' };
    if (score >= RISK_WARN) return { cls: 'lvl-warn', text: 'внимание' };
    return { cls: 'lvl-ok', text: 'норма' };
  }

  function colorFor(score) {
    if (score >= RISK_LOCK) return 'var(--crit)';
    if (score >= RISK_PAUSE) return 'var(--high)';
    if (score >= RISK_WARN) return 'var(--warn)';
    return 'var(--ok)';
  }

  function el(id) { return document.getElementById(id); }

  // ------------------------------------------------------------------ HUD

  function Hud() {
    this.dom = {};
    this.caps = { vision: true, gaze: true, identity: true, audio: true, env: true };
    this.score = 0;
    this.lastStatus = null;
    this.lastBreakdown = [];
    // как только пришло сообщение risk, поле risk из status перестаёт быть ведущим
    this._riskFromRiskMsg = false;
    this.events = [];          // все инциденты сессии (для отчёта)
    this.highlights = {};      // channel -> {state, text, until|sticky}
    this.verdict = 'none';
    this.sessionId = '—';
    this.onPause = null;       // колбэки наружу: остановить/продолжить таймер
    this.onResume = null;
    this.onLock = null;
    this._tick = null;
    this._toastTimer = null;
    this._lastToastAt = 0;
  }

  Hud.prototype.init = function (opts) {
    opts = opts || {};
    var self = this;
    this.dom = {
      root: el('hud'),
      body: el('hud-body'),
      toggle: el('hud-toggle'),
      ringWrap: el('hud-ring-wrap'),
      ring: el('hud-ring'),
      score: el('hud-score'),
      level: el('hud-level'),
      contrib: el('hud-contrib'),
      fps: el('hud-fps'),
      chans: el('hud-chans'),
      feed: el('hud-feed'),
      toasts: el('toasts'),
      pause: el('overlay-pause'),
      pauseReason: el('pause-reason'),
      pauseWait: el('pause-wait'),
      resumeBtn: el('btn-resume'),
      lock: el('overlay-lock'),
      lockReason: el('lock-reason'),
      lockSession: el('lock-session'),
      lockBtn: el('btn-lock-report')
    };
    this.onPause = opts.onPause || null;
    this.onResume = opts.onResume || null;
    this.onLock = opts.onLock || null;
    this.onReport = opts.onReport || null;

    if (this.dom.toggle) {
      this.dom.toggle.addEventListener('click', function () {
        self.dom.root.classList.toggle('is-collapsed');
      });
    }
    if (this.dom.resumeBtn) {
      this.dom.resumeBtn.addEventListener('click', function () { self.clearPause(true); });
    }
    if (this.dom.lockBtn) {
      this.dom.lockBtn.addEventListener('click', function () {
        if (self.onReport) self.onReport();
      });
    }

    this._renderChannels();
    this.setRisk(0, [], 'none');
    this._tick = setInterval(function () { self._renderChannelStates(); }, 1000);
  };

  Hud.prototype.setCapabilities = function (caps) {
    if (caps && typeof caps === 'object') {
      this.caps = {
        vision: caps.vision !== false,
        gaze: caps.gaze !== false,
        identity: caps.identity !== false,
        audio: caps.audio !== false,
        env: caps.env !== false
      };
    }
    this._renderChannelStates();
  };

  Hud.prototype.setSessionId = function (id) {
    this.sessionId = id || '—';
    if (this.dom.lockSession) this.dom.lockSession.textContent = this.sessionId;
  };

  Hud.prototype.show = function () { if (this.dom.root) this.dom.root.hidden = false; };
  Hud.prototype.hide = function () { if (this.dom.root) this.dom.root.hidden = true; };

  // --- каналы ---

  Hud.prototype._renderChannels = function () {
    if (!this.dom.chans) return;
    var html = '';
    for (var i = 0; i < CHANNELS.length; i++) {
      var c = CHANNELS[i];
      html += '<li class="chan s-off" id="chan-' + c.id + '">' +
              '<span class="chan__dot"></span>' +
              '<span class="chan__text">' +
                '<span class="chan__name">' + c.name + '</span>' +
                '<span class="chan__state" id="chanstate-' + c.id + '">нет данных</span>' +
              '</span>' +
            '</li>';
    }
    this.dom.chans.innerHTML = html;
  };

  /** Базовое состояние канала по последнему status. */
  Hud.prototype._baseState = function (id) {
    var s = this.lastStatus;
    var caps = this.caps;

    if (id === 'face') {
      if (!caps.vision) return ['off', 'канал недоступен'];
      if (!s) return ['off', 'нет данных'];
      if (s.face_count > 1) return ['bad', 'в кадре ' + s.face_count + ' лица'];
      if (!s.face_present) return ['bad', 'лицо не видно'];
      return ['ok', 'в кадре'];
    }
    if (id === 'gaze') {
      if (!caps.gaze) return ['off', 'канал недоступен'];
      if (!s || !s.gaze) return ['off', 'нет данных'];
      var z = s.gaze.zone || 'unknown';
      var t = ZONE_TEXT[z] || String(z);
      if (z === 'center' || z === 'screen') return ['ok', t];
      if (z === 'unknown') return ['off', t];
      if (z === 'off' || z === 'off_screen') return ['bad', t];
      return ['warn', t];
    }
    if (id === 'phone') {
      if (!caps.vision) return ['off', 'канал недоступен'];
      if (!s) return ['off', 'нет данных'];
      return s.phone ? ['bad', 'виден в кадре'] : ['ok', 'не обнаружен'];
    }
    if (id === 'identity') {
      if (!caps.identity) return ['off', 'канал недоступен'];
      if (!s) return ['off', 'нет данных'];
      return s.identity_ok ? ['ok', 'подтверждена'] : ['warn', 'не подтверждена'];
    }
    if (id === 'audio') {
      if (!caps.audio) return ['off', 'микрофон недоступен'];
      if (!s) return ['off', 'нет данных'];
      // audio_ok из status (см. docs/CONTRACT.md) означает «микрофон опрашивается
      // без ошибок», а НЕ «посторонних звуков нет». Подписи «тишина, ваш голос» /
      // «посторонний звук» подменяли одно другим: HUD утверждал про запись то, чего
      // сайдкар не измерял. Про чужой голос говорят инциденты VOICE_OTHER и
      // SPEECH_WITHOUT_LIP_MOTION — они подсвечивают канал через highlights.
      return s.audio_ok ? ['ok', 'микрофон слушает'] : ['warn', 'сигнала с микрофона нет'];
    }
    if (id === 'env') {
      if (!caps.env) return ['off', 'проверки недоступны'];
      return ['ok', 'проверки пройдены'];
    }
    return ['off', 'нет данных'];
  };

  Hud.prototype._renderChannelStates = function () {
    var now = Date.now();
    for (var i = 0; i < CHANNELS.length; i++) {
      var id = CHANNELS[i].id;
      var node = el('chan-' + id);
      var label = el('chanstate-' + id);
      if (!node || !label) continue;

      var hl = this.highlights[id];
      if (hl && !hl.sticky && hl.until <= now) { delete this.highlights[id]; hl = null; }

      var st, text;
      if (hl) { st = hl.state; text = hl.text; }
      else { var b = this._baseState(id); st = b[0]; text = b[1]; }

      node.className = 'chan s-' + st;
      label.textContent = text;
    }
  };

  Hud.prototype.applyStatus = function (st) {
    if (!st || typeof st !== 'object') return;
    this.lastStatus = st;
    if (this.dom.fps) {
      var fps = (typeof st.fps === 'number') ? st.fps.toFixed(1) : '—';
      var state = st.state ? ' · ' + st.state : '';
      this.dom.fps.textContent = 'поток: ' + fps + ' к/с' + state;
    }
    if (typeof st.risk === 'number' && !this._riskFromRiskMsg) this.setRisk(st.risk, null, null);
    this._renderChannelStates();
  };

  // --- риск ---

  Hud.prototype.setRisk = function (score, breakdown, action) {
    if (typeof score === 'number' && isFinite(score)) {
      this.score = Math.max(0, Math.min(100, score));
      var lv = levelOf(this.score);
      if (this.dom.ring) {
        this.dom.ring.style.strokeDashoffset = String(RING_LEN * (1 - this.score / 100));
        this.dom.ring.style.stroke = colorFor(this.score);
      }
      if (this.dom.score) this.dom.score.textContent = String(Math.round(this.score));
      if (this.dom.level) this.dom.level.textContent = lv.text;
      if (this.dom.ringWrap) this.dom.ringWrap.className = 'ring ' + lv.cls;
    }
    if (breakdown) this._renderContrib(breakdown);
    if (action) this.verdict = action;
  };

  Hud.prototype.applyRisk = function (msg) {
    if (!msg) return;
    this._riskFromRiskMsg = true;  // дальше ведущий источник — сообщения risk
    this.lastBreakdown = msg.breakdown || [];
    this.setRisk(typeof msg.score === 'number' ? msg.score : this.score, msg.breakdown, msg.action);
  };

  Hud.prototype._renderContrib = function (breakdown) {
    if (!this.dom.contrib) return;
    var items = (breakdown || []).slice().filter(function (b) {
      return b && typeof b.contribution === 'number' && b.contribution > 0;
    });
    items.sort(function (a, b) { return b.contribution - a.contribution; });
    items = items.slice(0, 3);
    if (!items.length) {
      this.dom.contrib.innerHTML = '<li class="hud__contribempty">нарушений нет</li>';
      return;
    }
    var html = '';
    for (var i = 0; i < items.length; i++) {
      var b = items[i];
      var name = EVENT_SHORT[b.kind] || EVENT_TEXT[b.kind] || b.kind || 'событие';
      var cnt = b.count ? ' ×' + b.count : '';
      html += '<li><span class="c__n">' + escapeHtml(name) + cnt + '</span>' +
              '<span class="c__v">+' + Math.round(b.contribution) + '</span></li>';
    }
    this.dom.contrib.innerHTML = html;
  };

  // --- события ---

  Hud.prototype.applyEvent = function (ev) {
    if (!ev || typeof ev !== 'object' || !ev.kind) return;
    if (SERVICE_KINDS[ev.kind]) return;

    this.events.push(ev);

    var chan = KIND_CHANNEL[ev.kind];
    if (chan) {
      var sev = ev.severity || 'medium';
      var state = (SEV_RANK[sev] || 0) >= 2 ? 'bad' : 'warn';
      this.highlights[chan] = {
        state: state,
        text: shortFor(ev),
        sticky: !!STICKY_KINDS[ev.kind],
        until: Date.now() + 6000
      };
      this._renderChannelStates();
    }
    this._pushFeed(ev);
  };

  function shortFor(ev) {
    var s = EVENT_SHORT[ev.kind];
    if (s) return s;
    var t = eventText(ev);
    return t.length > 26 ? t.slice(0, 25) + '…' : t;
  }

  Hud.prototype._pushFeed = function (ev) {
    if (!this.dom.feed) return;
    var li = document.createElement('li');
    li.className = 'feed__new';
    var sev = ev.severity || 'medium';
    li.innerHTML =
      '<span class="feed__sev sev-' + escapeHtml(sev) + '"></span>' +
      '<span class="feed__time">' + hhmmss(ev.ts) + '</span>' +
      '<span class="feed__msg">' + escapeHtml(eventText(ev)) + '</span>';

    var empty = this.dom.feed.querySelector('.feed__empty');
    if (empty) empty.remove();
    this.dom.feed.insertBefore(li, this.dom.feed.firstChild);
    while (this.dom.feed.children.length > 5) {
      this.dom.feed.removeChild(this.dom.feed.lastChild);
    }
  };

  // --- вердикты: warn / pause / lock ---

  Hud.prototype.applyVerdict = function (v) {
    if (!v) return;
    var action = v.action || 'none';
    var reason = v.reason || '';
    this.verdict = action;

    if (action === 'lock') { this.showLock(reason); return; }
    if (action === 'pause') { this.showPause(reason); return; }
    if (action === 'warn') {
      // пауза важнее мягкого предупреждения: не перебиваем оверлей тостом
      if (this.dom.pause && !this.dom.pause.hidden) { this._allowResume(true); return; }
      this.toast('Вернитесь к экрану', reason || 'Зафиксировано отклонение от нормы наблюдения');
      return;
    }
    // none — всё в норме: если стояла пауза, разрешаем продолжить
    if (this.dom.pause && !this.dom.pause.hidden) this._allowResume(true);
  };

  /** Мягкое уведомление. Не модалка, не блокирует ввод, исчезает само. */
  Hud.prototype.toast = function (title, sub) {
    if (!this.dom.toasts) return;
    var now = Date.now();
    if (now - this._lastToastAt < 2500) return;  // не спамим
    this._lastToastAt = now;

    var node = document.createElement('div');
    node.className = 'toast';
    node.innerHTML =
      '<span class="toast__mark"></span>' +
      '<span><b>' + escapeHtml(title) + '</b>' +
      (sub ? '<br><span class="toast__sub">' + escapeHtml(sub) + '</span>' : '') +
      '</span>';
    this.dom.toasts.appendChild(node);
    setTimeout(function () {
      node.classList.add('is-out');
      setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 300);
    }, 6000);
  };

  Hud.prototype.showPause = function (reason) {
    if (!this.dom.pause) return;
    if (this.dom.pauseReason) {
      this.dom.pauseReason.textContent = reason || 'Условия наблюдения нарушены.';
    }
    this._allowResume(false);
    this.dom.pause.hidden = false;
    if (this.onPause) { try { this.onPause(); } catch (e) {} }
  };

  Hud.prototype._allowResume = function (ok) {
    if (this.dom.resumeBtn) this.dom.resumeBtn.disabled = !ok;
    if (this.dom.pauseWait) {
      this.dom.pauseWait.textContent = ok
        ? 'Наблюдение в норме — можно продолжать.'
        : 'Ожидание нормализации…';
    }
  };

  /** Снять паузу. byUser=true — студент нажал «Продолжить». */
  Hud.prototype.clearPause = function (byUser) {
    if (!this.dom.pause || this.dom.pause.hidden) return;
    if (byUser && this.dom.resumeBtn && this.dom.resumeBtn.disabled) return;
    this.dom.pause.hidden = true;
    if (this.onResume) { try { this.onResume(); } catch (e) {} }
  };

  Hud.prototype.showLock = function (reason) {
    this.clearPause(false);
    if (!this.dom.lock) return;
    if (this.dom.lockReason) {
      this.dom.lockReason.textContent = reason || 'Накопленный риск превысил допустимый порог.';
    }
    if (this.dom.lockSession) this.dom.lockSession.textContent = this.sessionId;
    this.dom.lock.hidden = false;
    if (this.onLock) { try { this.onLock(); } catch (e) {} }
  };

  Hud.prototype.hideLock = function () { if (this.dom.lock) this.dom.lock.hidden = true; };

  /** Сводка для экрана отчёта. */
  Hud.prototype.summary = function () {
    var high = 0;
    for (var i = 0; i < this.events.length; i++) {
      if ((SEV_RANK[this.events[i].severity] || 0) >= 3) high++;
    }
    return {
      score: this.score,
      level: levelOf(this.score),
      total: this.events.length,
      high: high,
      events: this.events.slice(),
      breakdown: this.lastBreakdown || []
    };
  };

  function escapeHtml(s) {
    return String(s === undefined || s === null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  window.Proctor = window.Proctor || {};
  window.Proctor.hud = new Hud();
  window.Proctor.text = {
    event: EVENT_TEXT,
    short: EVENT_SHORT,
    zone: ZONE_TEXT,
    serviceKinds: SERVICE_KINDS,
    eventText: eventText,
    levelOf: levelOf,
    colorFor: colorFor,
    hhmmss: hhmmss,
    escapeHtml: escapeHtml,
    sevRank: SEV_RANK,
    thresholds: { warn: RISK_WARN, pause: RISK_PAUSE, lock: RISK_LOCK },
    ringLen: RING_LEN
  };
})();

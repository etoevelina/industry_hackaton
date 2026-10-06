/* ===========================================================================
 * calibration.js — четыре этапа калибровки перед экзаменом.
 *
 *   identity    — снимок эталона лица (рамка + отсчёт 3 секунды)
 *   gaze_center — 7 секунд взгляда в центр экрана (нулевая точка взгляда)
 *   gaze_grid   — 9 точек: углы, середины сторон, центр, по 1.5 секунды
 *   voice       — 3 секунды речи (эталон голоса владельца)
 *
 * На каждый этап уходит сообщение calibrate по docs/CONTRACT.md:
 *   {stage:"identity"|"gaze_center"|"gaze_grid"|"voice", point:[x,y]|null}
 * Для gaze_grid point — нормированные координаты точки в карте экрана (0..1).
 *
 * Прогресс ведётся локально по таймеру, но если сайдкар присылает сообщения
 * calibration с полями progress/done, они имеют приоритет: так экран остаётся
 * честным и в режиме без CV-модулей, и с реальным сайдкаром.
 * =========================================================================== */

(function () {
  'use strict';

  var STAGES = [
    { key: 'identity',    name: 'Эталон лица',    meta: 'снимок, 3 с',        ms: 3000 },
    { key: 'gaze_center', name: 'Центр взгляда',  meta: '7 с в центр экрана', ms: 7000 },
    { key: 'gaze_grid',   name: 'Карта экрана',   meta: '9 точек по 1.5 с',   ms: 9 * 1500 },
    { key: 'voice',       name: 'Эталон голоса',  meta: '3 с речи',           ms: 3000 }
  ];

  var GRID_MS = 1500;

  // Путь обхода девяти точек: сверху слева «змейкой» вниз.
  var GRID = [
    [0.06, 0.08], [0.50, 0.08], [0.94, 0.08],
    [0.94, 0.50], [0.50, 0.50], [0.06, 0.50],
    [0.06, 0.92], [0.50, 0.92], [0.94, 0.92]
  ];

  var TITLES = {
    identity: 'Эталон лица',
    gaze_center: 'Центр взгляда',
    gaze_grid: 'Карта экрана',
    voice: 'Эталон голоса'
  };

  var HINTS = {
    identity: 'Расположите лицо в рамке, смотрите в камеру. Снимок делается автоматически и хранится только на этом компьютере.',
    gaze_center: 'Смотрите на синюю точку в центре, не двигая головой. Так система узнаёт вашу нулевую точку взгляда.',
    gaze_grid: 'Следите глазами за точкой, голову держите прямо. Девять позиций задают границы экрана в системе координат взгляда.',
    voice: 'Прочитайте фразу вслух обычным голосом. Записываются только числовые признаки — аудио не сохраняется.'
  };

  function el(id) { return document.getElementById(id); }

  function Calibration() {
    this.dom = {};
    this.stageIndex = -1;
    this.stageProgress = 0;
    this.remoteProgress = {};
    this.results = {};
    this.running = false;
    this.done = false;
    this._raf = null;
    this._t0 = 0;
    this._gridIdx = -1;
    this._gridSent = -1;
    this.sendCalibrate = null;
    this.onDone = null;
    this.onStage = null;
    this._stream = null;
    this._allowPreview = false;
  }

  Calibration.prototype.init = function (opts) {
    opts = opts || {};
    var self = this;
    this.sendCalibrate = opts.sendCalibrate || null;
    this.onDone = opts.onDone || null;
    this.onStage = opts.onStage || null;

    this.dom = {
      steps: el('calib-steps'),
      title: el('calib-title'),
      hint: el('calib-hint'),
      viewport: el('calib-viewport'),
      face: el('calib-face'),
      video: el('calib-video'),
      count: el('calib-count'),
      dotfield: el('calib-dotfield'),
      dot: el('calib-dot'),
      voice: el('calib-voice'),
      progress: el('calib-progress'),
      counter: el('calib-counter'),
      btnStart: el('btn-calib-start'),
      btnRetry: el('btn-calib-retry'),
      btnNext: el('btn-calib-next')
    };

    this._renderSteps();
    if (this.dom.btnRetry) this.dom.btnRetry.disabled = true;

    if (this.dom.btnStart) {
      this.dom.btnStart.addEventListener('click', function () { self.start(); });
    }
    if (this.dom.btnRetry) {
      this.dom.btnRetry.addEventListener('click', function () { self.retryStage(); });
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

  Calibration.prototype._renderSteps = function () {
    if (!this.dom.steps) return;
    var html = '';
    for (var i = 0; i < STAGES.length; i++) {
      var s = STAGES[i];
      var cls = 'cstep';
      if (i < this.stageIndex || (this.done && i <= this.stageIndex)) cls += ' is-done';
      else if (i === this.stageIndex) cls += ' is-active';
      html += '<li class="' + cls + '">' +
                '<span class="cstep__idx">' + (i + 1) + '</span>' +
                '<span>' +
                  '<span class="cstep__name">' + s.name + '</span><br>' +
                  '<span class="cstep__meta">' + s.meta + '</span>' +
                '</span>' +
              '</li>';
    }
    this.dom.steps.innerHTML = html;
  };

  Calibration.prototype._send = function (stage, point) {
    if (!this.sendCalibrate) return;
    try { this.sendCalibrate(stage, point || null); } catch (e) { /* канал недоступен — идём локально */ }
  };

  Calibration.prototype.start = function () {
    if (this.running) return;
    this.running = true;
    this.done = false;
    this.results = {};
    this.remoteProgress = {};
    if (this.dom.btnStart) this.dom.btnStart.hidden = true;
    if (this.dom.btnNext) this.dom.btnNext.hidden = true;
    if (this.dom.btnRetry) this.dom.btnRetry.disabled = false;
    this.stageIndex = -1;
    this._nextStage();
  };

  Calibration.prototype.retryStage = function () {
    if (!this.running || this.stageIndex < 0) return;
    this.stageIndex--;
    this._nextStage();
  };

  Calibration.prototype._nextStage = function () {
    this.stageIndex++;
    if (this.stageIndex >= STAGES.length) { this._complete(); return; }

    var st = STAGES[this.stageIndex];
    this.stageProgress = 0;
    this._t0 = Date.now();
    this._gridIdx = -1;
    this._gridSent = -1;

    if (this.dom.title) this.dom.title.textContent = TITLES[st.key] || st.name;
    if (this.dom.hint) this.dom.hint.textContent = HINTS[st.key] || '';
    if (this.dom.counter) {
      this.dom.counter.textContent = 'этап ' + (this.stageIndex + 1) + ' из ' + STAGES.length;
    }
    this._renderSteps();
    this._showFor(st.key);
    if (this.onStage) { try { this.onStage(st.key, this.stageIndex); } catch (e) {} }

    // gaze_grid отправляет calibrate на каждую точку, остальные этапы — один раз
    if (st.key === 'gaze_center') this._send(st.key, [0.5, 0.5]);
    else if (st.key !== 'gaze_grid') this._send(st.key, null);

    this._loop();
  };

  Calibration.prototype._showFor = function (key) {
    var d = this.dom;
    if (d.face) d.face.hidden = key !== 'identity';
    if (d.dotfield) d.dotfield.hidden = !(key === 'gaze_center' || key === 'gaze_grid');
    if (d.voice) d.voice.hidden = key !== 'voice';

    if (key === 'identity') {
      this._startPreview();
    } else {
      this._stopPreview();
    }
    if (key === 'gaze_center' && d.dot) {
      d.dot.style.left = '50%';
      d.dot.style.top = '50%';
    }
  };

  Calibration.prototype._startPreview = function () {
    var d = this.dom;
    if (!this._allowPreview || !d.video || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      if (d.video) d.video.hidden = true;
      return;
    }
    var self = this;
    navigator.mediaDevices.getUserMedia({ video: { width: 480, height: 640 }, audio: false })
      .then(function (stream) {
        self._stream = stream;
        d.video.srcObject = stream;
        d.video.hidden = false;
      })
      .catch(function () { d.video.hidden = true; });
  };

  Calibration.prototype._stopPreview = function () {
    if (this.dom.video) { this.dom.video.hidden = true; }
    if (this._stream) {
      try {
        var tracks = this._stream.getTracks();
        for (var i = 0; i < tracks.length; i++) tracks[i].stop();
      } catch (e) {}
      this._stream = null;
      if (this.dom.video) this.dom.video.srcObject = null;
    }
  };

  Calibration.prototype._loop = function () {
    var self = this;
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }

    function frame() {
      if (!self.running) return;
      var st = STAGES[self.stageIndex];
      if (!st) return;

      var elapsed = Date.now() - self._t0;
      var local = Math.min(1, elapsed / st.ms);
      var remote = self.remoteProgress[st.key];
      self.stageProgress = (typeof remote === 'number' && remote > local) ? Math.min(1, remote) : local;

      self._renderStage(st, elapsed);
      self._renderProgress();

      if (self.stageProgress >= 1) { self._stageDone(st); return; }
      self._raf = requestAnimationFrame(frame);
    }
    this._raf = requestAnimationFrame(frame);
  };

  Calibration.prototype._renderStage = function (st, elapsed) {
    var d = this.dom;
    if (st.key === 'identity' && d.count) {
      var left = Math.max(0, Math.ceil((st.ms - elapsed) / 1000));
      d.count.textContent = left > 0 ? String(left) : '';
    }
    if (st.key === 'gaze_grid' && d.dot) {
      var idx = Math.min(GRID.length - 1, Math.floor(elapsed / GRID_MS));
      if (idx !== this._gridIdx) {
        this._gridIdx = idx;
        var p = GRID[idx];
        d.dot.style.left = (p[0] * 100).toFixed(2) + '%';
        d.dot.style.top = (p[1] * 100).toFixed(2) + '%';
        if (idx !== this._gridSent) {
          this._gridSent = idx;
          this._send('gaze_grid', [p[0], p[1]]);
        }
        if (d.hint) {
          d.hint.textContent = 'Точка ' + (idx + 1) + ' из ' + GRID.length +
            '. Следите глазами, голову держите прямо.';
        }
      }
    }
    if (st.key === 'gaze_center' && d.hint) {
      var sec = Math.max(0, Math.ceil((st.ms - elapsed) / 1000));
      d.hint.textContent = 'Смотрите в центр точки. Осталось ' + sec + ' с.';
    }
    if (st.key === 'voice' && d.hint) {
      var vs = Math.max(0, Math.ceil((st.ms - elapsed) / 1000));
      d.hint.textContent = 'Говорите, идёт запись эталона. Осталось ' + vs + ' с.';
    }
  };

  Calibration.prototype._renderProgress = function () {
    if (!this.dom.progress) return;
    var total = (this.stageIndex + this.stageProgress) / STAGES.length;
    this.dom.progress.style.width = (Math.max(0, Math.min(1, total)) * 100).toFixed(1) + '%';
  };

  Calibration.prototype._stageDone = function (st) {
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
    if (!this.results[st.key]) this.results[st.key] = { ok: true };
    var self = this;
    if (this.dom.hint) this.dom.hint.textContent = 'Этап пройден.';
    setTimeout(function () { if (self.running) self._nextStage(); }, 420);
  };

  Calibration.prototype._complete = function () {
    this.running = false;
    this.done = true;
    this._stopPreview();
    this._renderSteps();
    if (this.dom.progress) this.dom.progress.style.width = '100%';
    if (this.dom.title) this.dom.title.textContent = 'Калибровка завершена';
    if (this.dom.hint) {
      this.dom.hint.textContent = 'Персональная норма записана: эталон лица, нулевая точка взгляда, карта экрана, эталон голоса.';
    }
    if (this.dom.counter) this.dom.counter.textContent = 'все 4 этапа пройдены';
    if (this.dom.face) this.dom.face.hidden = true;
    if (this.dom.dotfield) this.dom.dotfield.hidden = true;
    if (this.dom.voice) this.dom.voice.hidden = true;
    if (this.dom.btnRetry) this.dom.btnRetry.disabled = true;
    if (this.dom.btnNext) this.dom.btnNext.hidden = false;
    if (this.onDone) { try { this.onDone(this.results); } catch (e) {} }
  };

  /** Входящее сообщение calibration от сайдкара: {stage, progress, done, result}. */
  Calibration.prototype.applyMessage = function (msg) {
    if (!msg || typeof msg !== 'object' || !msg.stage) return;
    if (typeof msg.progress === 'number') this.remoteProgress[msg.stage] = msg.progress;
    if (msg.result && typeof msg.result === 'object') {
      this.results[msg.stage] = msg.result;
    }
    if (msg.done) {
      this.remoteProgress[msg.stage] = 1;
      if (!this.results[msg.stage]) this.results[msg.stage] = { ok: true };
    }
  };

  /** Полный сброс (возврат на экран калибровки заново). */
  Calibration.prototype.reset = function () {
    this.running = false;
    this.done = false;
    this.stageIndex = -1;
    this.stageProgress = 0;
    this.results = {};
    this.remoteProgress = {};
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
    this._stopPreview();
    if (this.dom.progress) this.dom.progress.style.width = '0%';
    if (this.dom.btnStart) this.dom.btnStart.hidden = false;
    if (this.dom.btnNext) this.dom.btnNext.hidden = true;
    if (this.dom.btnRetry) this.dom.btnRetry.disabled = true;
    if (this.dom.title) this.dom.title.textContent = 'Подготовка';
    if (this.dom.hint) this.dom.hint.textContent = 'Нажмите «Начать калибровку», когда будете готовы.';
    if (this.dom.counter) this.dom.counter.textContent = 'этап 0 из ' + STAGES.length;
    this._renderSteps();
  };

  Calibration.prototype.stages = function () { return STAGES; };

  window.Proctor = window.Proctor || {};
  window.Proctor.calibration = new Calibration();
})();

'use strict';
/**
 * WS-клиент оболочки к Python-сайдкару (ws://127.0.0.1:8787).
 *
 * Формат строго по docs/CONTRACT.md: один JSON-объект на сообщение,
 * конверт {"v":1,"type":<MsgType>,"ts":<float секунды>, ...payload}.
 *
 * Свойства реализации:
 *  - реконнект с экспоненциальным бэкоффом 0.5 с -> 5 с (только loopback, наружу ничего);
 *  - очередь исходящих сообщений, пока сокет не открыт (кольцевой буфер: при переполнении
 *    вытесняются самые старые, счётчик потерь виден в info());
 *  - отсутствие пакета `ws` не ломает оболочку: available() === false, HUD честно
 *    показывает «CV-канал недоступен», тест продолжается;
 *  - контроль живости: сайдкар шлёт status ~2 Гц, поэтому тишина дольше 15 с трактуется
 *    как мёртвый сокет -> terminate -> реконнект.
 *
 * Модуль намеренно не импортирует electron: его можно гонять под чистым node.
 */

const EventEmitter = require('events');

const PROTOCOL_VERSION = 1;
const DEFAULT_WS_HOST = '127.0.0.1';
const DEFAULT_WS_PORT = 8787;

/** Зеркало MsgType из sidecar/protocol.py. Расходиться не должно. */
const MsgType = Object.freeze({
  // sidecar -> shell
  HELLO: 'hello',
  STATUS: 'status',
  EVENT: 'event',
  RISK: 'risk',
  CALIBRATION: 'calibration',
  VERDICT: 'verdict',
  ERROR: 'error',
  // shell -> sidecar
  SESSION_START: 'session_start',
  SESSION_END: 'session_end',
  CALIBRATE: 'calibrate',
  TELEMETRY: 'telemetry',
  SHELL_EVENT: 'shell_event',
  COMMAND: 'command',
});

/** Зеркало EventKind из sidecar/protocol.py. Новых строк тут не изобретаем. */
const EventKind = Object.freeze({
  PHONE_IN_FRAME: 'PHONE_IN_FRAME',
  PHONE_RAISED: 'PHONE_RAISED',
  PHONE_AIMED_AT_SCREEN: 'PHONE_AIMED_AT_SCREEN',
  FORBIDDEN_OBJECT: 'FORBIDDEN_OBJECT',

  NO_FACE: 'NO_FACE',
  SECOND_FACE: 'SECOND_FACE',
  IDENTITY_MISMATCH: 'IDENTITY_MISMATCH',
  LIVENESS_FAIL: 'LIVENESS_FAIL',

  GAZE_DOWN: 'GAZE_DOWN',
  GAZE_SIDE: 'GAZE_SIDE',
  GAZE_OFF_SCREEN: 'GAZE_OFF_SCREEN',
  HEAD_TURNED: 'HEAD_TURNED',

  VOICE_OTHER: 'VOICE_OTHER',
  SPEECH_WITHOUT_LIP_MOTION: 'SPEECH_WITHOUT_LIP_MOTION',

  VIRTUAL_CAMERA: 'VIRTUAL_CAMERA',
  REMOTE_ACCESS_SOFTWARE: 'REMOTE_ACCESS_SOFTWARE',
  VIRTUAL_MACHINE: 'VIRTUAL_MACHINE',
  SCREEN_RECORDING: 'SCREEN_RECORDING',
  MULTIPLE_DISPLAYS: 'MULTIPLE_DISPLAYS',
  BLACKLISTED_PROCESS: 'BLACKLISTED_PROCESS',

  WINDOW_BLUR: 'WINDOW_BLUR',
  FULLSCREEN_EXIT: 'FULLSCREEN_EXIT',
  SHORTCUT_BLOCKED: 'SHORTCUT_BLOCKED',
  CLIPBOARD_PASTE: 'CLIPBOARD_PASTE',
  DEVTOOLS_ATTEMPT: 'DEVTOOLS_ATTEMPT',

  PASTE_BURST: 'PASTE_BURST',
  TYPING_ANOMALY: 'TYPING_ANOMALY',

  FUSION_GAZE_THEN_ANSWER: 'FUSION_GAZE_THEN_ANSWER',
  FUSION_BLUR_THEN_ANSWER: 'FUSION_BLUR_THEN_ANSWER',
  FUSION_PHONE_THEN_ANSWER: 'FUSION_PHONE_THEN_ANSWER',

  SESSION_STARTED: 'SESSION_STARTED',
  SESSION_ENDED: 'SESSION_ENDED',
  CALIBRATION_DONE: 'CALIBRATION_DONE',
  SENSOR_LOST: 'SENSOR_LOST',
});

const EVENT_KIND_VALUES = Object.freeze(new Set(Object.values(EventKind)));

/** Зеркала вспомогательных enum-ов протокола (нужны renderer-у для подписей). */
const Severity = Object.freeze({
  INFO: 'info', LOW: 'low', MEDIUM: 'medium', HIGH: 'high', CRITICAL: 'critical',
});
const VerdictAction = Object.freeze({
  NONE: 'none', WARN: 'warn', PAUSE: 'pause', LOCK: 'lock',
});

/** Допустимые значения из docs/CONTRACT.md — валидируем на выходе из оболочки. */
const TELEMETRY_KINDS = Object.freeze(['keystroke', 'paste', 'answer_submit', 'question_shown']);
const CALIBRATION_STAGES = Object.freeze(['gaze_center', 'gaze_grid', 'identity', 'voice']);
const COMMAND_NAMES = Object.freeze(['snapshot', 'reset_risk', 'export_report']);

/** Секунды с эпохи как float — тот же формат, что time.time() в Python. */
function nowSec() {
  return Date.now() / 1000;
}

/** Ленивый импорт ws: нет пакета — работаем без CV-канала, а не падаем. */
function loadWsModule() {
  try {
    return require('ws');
  } catch (err) {
    return null;
  }
}

class SidecarLink extends EventEmitter {
  /**
   * @param {{host?:string, port?:number, queueLimit?:number, logger?:Function,
   *          backoffMinMs?:number, backoffMaxMs?:number, idleTimeoutMs?:number}} [opts]
   */
  constructor(opts = {}) {
    super();
    this.setMaxListeners(50);

    this.host = opts.host || DEFAULT_WS_HOST;
    this.port = opts.port || DEFAULT_WS_PORT;
    this.url = `ws://${this.host}:${this.port}`;

    this.queueLimit = opts.queueLimit || 1000;
    this.backoffMinMs = opts.backoffMinMs || 500;
    this.backoffMaxMs = opts.backoffMaxMs || 5000;
    this.idleTimeoutMs = opts.idleTimeoutMs || 15000;
    this._log = typeof opts.logger === 'function' ? opts.logger : () => {};

    this._WS = loadWsModule();
    this._ws = null;
    this._queue = [];
    this._dropped = 0;
    this._attempts = 0;
    this._reconnectTimer = null;
    this._livenessTimer = null;
    this._stopped = true;
    this._lastRxSec = 0;
    this._everConnected = false;

    /** Последний hello от сайдкара: capabilities + version. */
    this.capabilities = null;
    this.sidecarVersion = null;
  }

  // ------------------------------------------------------------------ состояние

  /** Есть ли вообще транспорт (установлен ли пакет ws). */
  available() {
    return this._WS !== null;
  }

  get connected() {
    return Boolean(this._ws && this._WS && this._ws.readyState === this._WS.OPEN);
  }

  /** Снимок состояния канала для HUD. */
  info() {
    return {
      available: this.available(),
      connected: this.connected,
      everConnected: this._everConnected,
      url: this.url,
      queued: this._queue.length,
      dropped: this._dropped,
      attempts: this._attempts,
      lastRxAgo: this._lastRxSec ? Math.max(0, nowSec() - this._lastRxSec) : null,
      capabilities: this.capabilities,
      version: this.sidecarVersion,
    };
  }

  // ------------------------------------------------------------- жизненный цикл

  start() {
    if (!this._stopped) return;
    this._stopped = false;
    if (!this.available()) {
      this._log('ws не установлен: оболочка работает без CV-канала (npm i)');
      this.emit('unavailable', { reason: 'ws_module_missing' });
      return;
    }
    this._connect();
    this._livenessTimer = setInterval(() => this._checkLiveness(), 5000);
    if (this._livenessTimer.unref) this._livenessTimer.unref();
  }

  stop() {
    this._stopped = true;
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this._livenessTimer) {
      clearInterval(this._livenessTimer);
      this._livenessTimer = null;
    }
    const sock = this._ws;
    this._ws = null;
    if (sock) {
      // Снимаем свои слушатели, но ОБЯЗАТЕЛЬНО оставляем глушитель 'error':
      // close() на недоустановленном соединении ws отдаёт ошибку, и без
      // слушателя EventEmitter уронит процесс.
      try {
        sock.removeAllListeners();
        sock.on('error', () => {});
      } catch (err) { /* нечего снимать */ }
      try { sock.close(); } catch (err) { /* уже мёртв */ }
      try { sock.terminate(); } catch (err) { /* уже мёртв */ }
    }
  }

  _connect() {
    if (this._stopped || this._ws || !this._WS) return;

    let sock;
    try {
      sock = new this._WS(this.url, { handshakeTimeout: 2500, perMessageDeflate: false });
    } catch (err) {
      this._log(`не удалось создать сокет: ${err && err.message}`);
      this._scheduleReconnect();
      return;
    }
    this._ws = sock;

    sock.on('open', () => {
      if (this._ws !== sock) return;
      this._attempts = 0;
      this._everConnected = true;
      this._lastRxSec = nowSec();
      this._log(`сайдкар подключён: ${this.url}`);
      this.emit('open', this.info());
      this._flush();
    });

    sock.on('message', (data) => {
      if (this._ws !== sock) return;
      this._lastRxSec = nowSec();
      this._handleRaw(data);
    });

    sock.on('error', (err) => {
      // ws после 'error' всегда даёт 'close' — реконнект планируем там.
      this.emit('socket-error', { message: err && err.message ? err.message : String(err) });
    });

    sock.on('close', (code) => {
      if (this._ws !== sock) return;
      this._ws = null;
      this._log(`сокет сайдкара закрыт (code=${code})`);
      this.emit('close', { code });
      this._scheduleReconnect();
    });
  }

  _scheduleReconnect() {
    if (this._stopped || this._reconnectTimer) return;
    const delay = Math.min(this.backoffMaxMs, this.backoffMinMs * Math.pow(2, this._attempts));
    this._attempts += 1;
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this._connect();
    }, delay);
    if (this._reconnectTimer.unref) this._reconnectTimer.unref();
    this.emit('reconnect-scheduled', { delayMs: delay, attempt: this._attempts });
  }

  /** Сайдкар шлёт status ~2 Гц: длинная тишина = мёртвый сокет. */
  _checkLiveness() {
    if (!this.connected) return;
    if (nowSec() - this._lastRxSec > this.idleTimeoutMs / 1000) {
      this._log('сайдкар молчит — переподключаемся');
      const sock = this._ws;
      try { sock.terminate(); } catch (err) { /* close придёт сам */ }
    }
  }

  // ----------------------------------------------------------------- приём

  _handleRaw(data) {
    let msg;
    try {
      msg = JSON.parse(typeof data === 'string' ? data : data.toString('utf8'));
    } catch (err) {
      this.emit('msg:error', { code: 'bad_json', message: 'Сайдкар прислал не-JSON', fatal: false });
      return;
    }
    if (!msg || typeof msg !== 'object' || typeof msg.type !== 'string') return;

    if (msg.type === MsgType.HELLO) {
      this.capabilities = msg.capabilities || null;
      this.sidecarVersion = msg.version || null;
    }

    // Единый поток + типизированный канал на каждый MsgType.
    // Имя 'error' для EventEmitter зарезервировано, поэтому префиксуем 'msg:'.
    this.emit('message', msg);
    this.emit(`msg:${msg.type}`, msg);
  }

  /** Подписка на весь поток сайдкара. Возвращает функцию отписки. */
  onMessage(cb) {
    this.on('message', cb);
    return () => this.off('message', cb);
  }

  /** Подписка на один MsgType. Возвращает функцию отписки. */
  onType(msgType, cb) {
    const channel = `msg:${msgType}`;
    this.on(channel, cb);
    return () => this.off(channel, cb);
  }

  // ---------------------------------------------------------------- отправка

  /** Низкоуровневая отправка готового конверта. */
  send(envelopeObj) {
    if (this.connected) {
      try {
        this._ws.send(JSON.stringify(envelopeObj));
        return true;
      } catch (err) {
        this._log(`send упал, уводим в очередь: ${err && err.message}`);
      }
    }
    this._enqueue(envelopeObj);
    return false;
  }

  _enqueue(envelopeObj) {
    if (this._queue.length >= this.queueLimit) {
      this._queue.shift();
      this._dropped += 1;
    }
    this._queue.push(envelopeObj);
  }

  _flush() {
    while (this._queue.length && this.connected) {
      const msg = this._queue[0];
      try {
        this._ws.send(JSON.stringify(msg));
        this._queue.shift();
      } catch (err) {
        break;
      }
    }
  }

  /** Собрать конверт протокола. */
  static envelope(msgType, payload = {}) {
    return Object.assign({ v: PROTOCOL_VERSION, type: msgType, ts: nowSec() }, payload);
  }

  _emitEnvelope(msgType, payload) {
    return this.send(SidecarLink.envelope(msgType, payload));
  }

  // ------------------------------------------------------- типизированные хелперы

  /**
   * shell_event: оболочка шлёт уже готовый EventKind.
   * Неизвестный kind не отправляем — контракт запрещает новые строковые литералы.
   */
  sendShellEvent(kind, detail = {}) {
    if (!EVENT_KIND_VALUES.has(kind)) {
      this._log(`отброшен неизвестный EventKind: ${kind}`);
      return false;
    }
    return this._emitEnvelope(MsgType.SHELL_EVENT, { kind, detail: detail || {} });
  }

  /** session_start: {student_id, exam_id, student_name}. */
  sendSessionStart(meta = {}) {
    return this._emitEnvelope(MsgType.SESSION_START, {
      student_id: String(meta.student_id || meta.studentId || 'unknown'),
      exam_id: String(meta.exam_id || meta.examId || 'unknown'),
      student_name: String(meta.student_name || meta.studentName || ''),
    });
  }

  sendSessionEnd(reason = 'shell_exit') {
    return this._emitEnvelope(MsgType.SESSION_END, { reason: String(reason) });
  }

  /** calibrate: stage из белого списка, point — [x,y] либо null. */
  sendCalibrate(stage, point = null) {
    if (!CALIBRATION_STAGES.includes(stage)) {
      this._log(`отброшена неизвестная стадия калибровки: ${stage}`);
      return false;
    }
    let pt = null;
    if (Array.isArray(point) && point.length === 2) {
      pt = [Number(point[0]) || 0, Number(point[1]) || 0];
    }
    return this._emitEnvelope(MsgType.CALIBRATE, { stage, point: pt });
  }

  /**
   * telemetry: поток из renderer. Сами символы клавиш не передаются (приватность),
   * поля — ровно по таблице «Телеметрия ввода» из docs/CONTRACT.md.
   */
  sendTelemetry(msg = {}) {
    const kind = msg.kind;
    if (!TELEMETRY_KINDS.includes(kind)) {
      this._log(`отброшена телеметрия неизвестного вида: ${kind}`);
      return false;
    }
    const payload = { kind, question_id: String(msg.question_id || ''), ts: Number(msg.ts) || nowSec() };

    if (kind === 'keystroke') {
      payload.interval_ms = Math.max(0, Math.round(Number(msg.interval_ms) || 0));
      payload.key_class = ['char', 'nav', 'ctrl'].includes(msg.key_class) ? msg.key_class : 'char';
    } else if (kind === 'paste') {
      payload.length = Math.max(0, Math.round(Number(msg.length) || 0));
      payload.source = 'clipboard';
    } else if (kind === 'answer_submit') {
      payload.length = Math.max(0, Math.round(Number(msg.length) || 0));
      payload.time_to_answer_ms = Math.max(0, Math.round(Number(msg.time_to_answer_ms) || 0));
      const st = msg.typing_stats || {};
      payload.typing_stats = {
        mean_ms: Number(st.mean_ms) || 0,
        std_ms: Number(st.std_ms) || 0,
        chars: Math.max(0, Math.round(Number(st.chars) || 0)),
      };
    } else if (kind === 'question_shown') {
      payload.difficulty = Math.round(Number(msg.difficulty) || 0);
    }
    return this._emitEnvelope(MsgType.TELEMETRY, payload);
  }

  /** command: snapshot | reset_risk | export_report. */
  sendCommand(name) {
    if (!COMMAND_NAMES.includes(name)) {
      this._log(`отброшена неизвестная команда: ${name}`);
      return false;
    }
    return this._emitEnvelope(MsgType.COMMAND, { name });
  }
}

module.exports = {
  SidecarLink,
  MsgType,
  EventKind,
  Severity,
  VerdictAction,
  TELEMETRY_KINDS,
  CALIBRATION_STAGES,
  COMMAND_NAMES,
  PROTOCOL_VERSION,
  DEFAULT_WS_HOST,
  DEFAULT_WS_PORT,
  nowSec,
};

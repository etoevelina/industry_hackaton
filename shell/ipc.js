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
const fs = require('fs');
const os = require('os');
const path = require('path');

const PROTOCOL_VERSION = 1;
const DEFAULT_WS_HOST = '127.0.0.1';
const DEFAULT_WS_PORT = 8787;

/**
 * Потолок размера SHELL_CONFIG. Ядро рвёт соединение на сообщении больше
 * ws_max_message (1 МиБ); берём с запасом, чтобы запись о режиме запуска
 * доходила при любом числе мониторов и акселераторов.
 */
const SHELL_CONFIG_MAX_CHARS = 64 * 1024;

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
  /*
   * Фактический режим запуска оболочки. Не инцидент студента, а условия
   * наблюдения: какие защиты машины реально применены и какие сняты флагами.
   * Раньше это состояние жило только в HUD (protectionInfo()) и в подписанную
   * цепочку не попадало вообще — снятые блокировки были видны лишь по
   * отсутствию SHORTCUT_BLOCKED и WINDOW_BLUR, что неотличимо от честной
   * сессии без нажатий.
   */
  SHELL_CONFIG: 'SHELL_CONFIG',

  /*
   * Выход за белый список источников экзамена. Два вида, а не один, потому
   * что вес у них в движке правил обязан быть разный:
   *
   *  NAVIGATION_OFF_PROFILE — студент осознанно открыл адрес (ввёл, перешёл
   *    по ссылке, подставил в адресную строку): это попытка покинуть экзамен.
   *    Ядро даёт ему вес 20 и НЕ глушит повторы: каждая попытка — улика;
   *  SUBRESOURCE_OFF_PROFILE — страница экзамена сама обратилась к
   *    неразрешённому источнику (шрифт со стороннего CDN, картинка, iframe,
   *    fetch): свойство чужой вёрстки, а не поведение студента. Вес 2, и
   *    повторы с одного источника ядро сворачивает.
   *
   * Имена и веса — из sidecar/protocol.py. Расходиться им нельзя: ядро
   * отвергает незнакомый вид, а вес решает, доведёт ли канал экзамен до
   * приостановки.
   *
   * Валить их в один вид значило бы приравнять Moodle, подтянувший шрифт с
   * googleapis, к набранному руками адресу шпаргалки.
   */
  NAVIGATION_OFF_PROFILE: 'NAVIGATION_OFF_PROFILE',
  SUBRESOURCE_OFF_PROFILE: 'SUBRESOURCE_OFF_PROFILE',

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

/**
 * Виды, которых ядро может ещё не знать, и вид-замена для каждого.
 *
 * Зачем это вообще нужно. Ядро отвергает незнакомый kind ошибкой
 * `unknown_kind` — и факт в подписанную цепочку НЕ попадает. Для фильтра
 * источников это недопустимо: вся его ценность в том, что попытка выхода
 * остаётся в журнале, и «оболочка новее ядра» не может быть причиной, по
 * которой улика исчезла. Молчаливая потеря выглядела бы ровно как честная
 * сессия без попыток — тот же дефект, что был у выключенных блокировок.
 *
 * Поэтому: отправляем точный вид; услышали `unknown_kind` — запоминаем, что
 * это ядро его не знает, ПЕРЕОТПРАВЛЯЕМ уже ушедшие факты видом-заменой и
 * дальше шлём замену сразу. Точное имя сохраняется в detail
 * (`blocked_kind`), так что смысл записи не теряется и при замене.
 */
const KIND_FALLBACKS = Object.freeze({
  [EventKind.NAVIGATION_OFF_PROFILE]: EventKind.SHORTCUT_BLOCKED,
  [EventKind.SUBRESOURCE_OFF_PROFILE]: EventKind.SHORTCUT_BLOCKED,
});

/** Сколько недавних «оптимистичных» фактов держим для переотправки. */
const FALLBACK_REPLAY_LIMIT = 64;
/** Сколько ждём ошибку `unknown_kind`, прежде чем считать факт принятым. */
const FALLBACK_REPLAY_TTL_MS = 15000;

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
/*
 * Полный словарь команд из sidecar/protocol.py. `proctor_lock` /
 * `proctor_release` — решение ЧЕЛОВЕКА над приостановленным экзаменом.
 *
 * Без них политика «блокировку подтверждает проктор» была недоставляема: при
 * `--auto-lock=false` (по умолчанию) приостановка по порогу 90 не снималась
 * никем — ни студентом штатно, ни проктором, потому что команда отсекалась
 * здесь же, в `sendCommand`. Единственным выходом оставался `session_end`.
 */
const COMMAND_NAMES = Object.freeze([
  'snapshot', 'reset_risk', 'export_report', 'proctor_lock', 'proctor_release',
]);

/** Команды, которые обязаны назвать, кто именно принял решение. */
const ACTOR_REQUIRED = Object.freeze(['proctor_lock', 'proctor_release']);

/** Имя заголовка и переменной окружения для токена канала (sidecar/main.py). */
const AUTH_HEADER = 'X-Proctor-Token';
const AUTH_TOKEN_ENV_VAR = 'PROCTOR_WS_TOKEN';

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

    /*
     * Токен канала. Аутентификация в сайдкаре включена ПО УМОЛЧАНИЮ, а
     * оболочка его не предъявляла вовсе — то есть при штатном запуске
     * получала 403 и не подключалась совсем. Токен ищется в том же порядке,
     * в каком его отдаёт сайдкар: явный параметр, переменная окружения,
     * файл в temp-каталоге (`qih-proctor-<порт>.token`, права 0600).
     */
    this.tokenFile = opts.tokenFile || '';
    this._tokenSource = '';
    this.token = String(opts.token || process.env[AUTH_TOKEN_ENV_VAR] || '').trim();
    if (!this.token) this.token = this._readTokenFile();

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

    /** Виды, про которые ядро уже сказало «не знаю такого». */
    this._unsupportedKinds = new Set();
    /** Недавно отправленные оптимистичные факты — на случай переотправки. */
    this._replay = [];
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

  /**
   * Прочитать токен из файла, который пишет сайдкар.
   *
   * Файл лежит в temp-каталоге ОС, а не в каталоге сессий и не в репозитории:
   * в первый уходят доказательства, которые забирает проктор, второй копируют
   * и публикуют. Ошибку чтения глотаем молча: нет токена — попробуем без
   * него, и если канал без аутентификации, всё сработает как раньше.
   */
  _readTokenFile() {
    const file = this.tokenFile
      || path.join(os.tmpdir(), `qih-proctor-${Number(this.port)}.token`);
    try {
      const body = JSON.parse(fs.readFileSync(file, 'utf8'));
      const token = String((body && body.token) || '').trim();
      if (token) this._tokenSource = file;
      return token;
    } catch (err) {
      return '';
    }
  }

  /** Перечитать токен (сайдкар перезапустился — токен новый). */
  refreshToken() {
    const next = String(process.env[AUTH_TOKEN_ENV_VAR] || '').trim() || this._readTokenFile();
    if (next && next !== this.token) {
      this.token = next;
      this._log('токен канала перечитан');
      return true;
    }
    return false;
  }

  _connect() {
    if (this._stopped || this._ws || !this._WS) return;

    // Перед каждой попыткой перечитываем токен: сайдкар мог перезапуститься,
    // и бесконечный реконнект со старым токеном давал бы 403 навсегда.
    if (this._attempts > 0) this.refreshToken();

    let sock;
    try {
      const options = { handshakeTimeout: 2500, perMessageDeflate: false };
      if (this.token) options.headers = { [AUTH_HEADER]: this.token };
      sock = new this._WS(this.url, options);
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
      const message = err && err.message ? err.message : String(err);
      // 403 на рукопожатии — это почти всегда токен, и молчать об этом нельзя:
      // без подсказки симптом выглядит как «сайдкар не запущен».
      if (/403/.test(message)) {
        this._log(this.token
          ? `сайдкар отклонил токен (403). Токен прочитан из ${this._tokenSource || 'переменной окружения'}; `
            + 'если сайдкар перезапускали, токен сменился — он лежит в его логе и в файле '
            + `qih-proctor-${Number(this.port)}.token`
          : 'сайдкар отклонил подключение (403): канал требует токен, а оболочка его не нашла. '
            + `Укажите ${AUTH_TOKEN_ENV_VAR} или запустите сайдкар с --no-auth`);
      }
      this.emit('socket-error', { message });
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

    if (msg.type === MsgType.ERROR && msg.code === 'unknown_kind') {
      this._degradeUnknownKind(msg);
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
    const payload = detail && typeof detail === 'object' ? detail : {};

    // Ядро уже сообщило, что этого вида не знает: шлём замену сразу, без
    // повторной попытки и без лишней ошибки в его журнале.
    const known = KIND_FALLBACKS[kind];
    if (known && this._unsupportedKinds.has(kind)) {
      return this._sendFallback(kind, known, payload);
    }

    if (known) this._rememberForReplay(kind, payload);
    return this._emitEnvelope(MsgType.SHELL_EVENT, { kind, detail: payload });
  }

  /** Отправка вида-замены с сохранением точного имени в detail. */
  _sendFallback(kind, fallbackKind, detail) {
    return this._emitEnvelope(MsgType.SHELL_EVENT, {
      kind: fallbackKind,
      detail: Object.assign({}, detail, {
        // combo/reason читает существующее правило ядра для SHORTCUT_BLOCKED,
        // поэтому заполняем их осмысленно, а не оставляем пустыми.
        combo: String(detail.combo || kind),
        blocked_kind: kind,
        kind_fallback: true,
        kind_fallback_note: `ядро не знает вид ${kind}; факт записан как ${fallbackKind}`,
      }),
    });
  }

  _rememberForReplay(kind, detail) {
    const now = Date.now();
    this._replay.push({ kind, detail, at: now });
    while (this._replay.length
      && (this._replay.length > FALLBACK_REPLAY_LIMIT
        || now - this._replay[0].at > FALLBACK_REPLAY_TTL_MS)) {
      this._replay.shift();
    }
  }

  /**
   * Ядро отвергло вид события. Понять, какой именно, и переотправить всё,
   * что уже ушло этим видом, заменой — иначе улики за эти секунды пропали бы.
   *
   * Сообщение ядра содержит имя вида (`Неизвестный вид события: X`), и это
   * основной признак. Если имени в тексте нет, а оптимистичный вид в полёте
   * ровно один — берём его: ошибка пришла по нашему же сообщению.
   */
  _degradeUnknownKind(msg) {
    const text = String((msg && msg.message) || '');
    const candidates = Object.keys(KIND_FALLBACKS)
      .filter((kind) => !this._unsupportedKinds.has(kind));
    if (!candidates.length) return;

    let hit = candidates.find((kind) => text.indexOf(kind) !== -1) || '';
    if (!hit) {
      const inFlight = [...new Set(this._replay.map((r) => r.kind))]
        .filter((kind) => candidates.indexOf(kind) !== -1);
      if (inFlight.length !== 1) return;     // не угадываем: молчим
      [hit] = inFlight;
    }

    this._unsupportedKinds.add(hit);
    const fallbackKind = KIND_FALLBACKS[hit];
    const pending = this._replay.filter((r) => r.kind === hit);
    this._replay = this._replay.filter((r) => r.kind !== hit);
    this._log(`ядро не знает вид ${hit}: далее шлём как ${fallbackKind}`
      + `, переотправляем уже ушедших фактов ${pending.length}`);
    for (const row of pending) this._sendFallback(hit, fallbackKind, row.detail);
    this.emit('kind-unsupported', { kind: hit, fallback: fallbackKind, replayed: pending.length });
  }

  /** Какие виды ядро не приняло — уходит в SHELL_CONFIG как честная оговорка. */
  unsupportedKinds() {
    return [...this._unsupportedKinds];
  }

  /**
   * shell_event SHELL_CONFIG: фактический режим запуска оболочки.
   *
   * Отдельный метод, а не прямой вызов sendShellEvent, из-за потолка размера.
   * В payload идут перечисления: удержанные акселераторы, мониторы, переменные
   * окружения. Сообщение, превысившее ws_max_message ядра (1 МиБ), ядро не
   * получит ВОВСЕ — и отсутствие записи в цепочке выглядело бы ровно как
   * оболочка, промолчавшая о своём режиме. Поэтому при переполнении режем
   * перечисления, а вердикт (weakened / weakened_by) уходит целиком: он и есть
   * содержание записи.
   */
  sendShellConfig(config) {
    if (!config || typeof config !== 'object') {
      this._log('SHELL_CONFIG не отправлен: конфигурация не объект');
      return false;
    }
    let payload = config;
    try {
      if (JSON.stringify(config).length > SHELL_CONFIG_MAX_CHARS) {
        payload = Object.assign({}, config, {
          displays: [],
          held_shortcuts: (config.held_shortcuts || []).slice(0, 32),
          truncated: true,
        });
      }
    } catch (err) {
      this._log(`SHELL_CONFIG не сериализуется: ${err && err.message}`);
      return false;
    }
    return this.sendShellEvent(EventKind.SHELL_CONFIG, payload);
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

  /**
   * command: snapshot | reset_risk | export_report | proctor_lock | proctor_release.
   *
   * `opts.actor` — кто принял решение, `opts.reason` — почему. Для решений
   * проктора `actor` обязателен: сайдкар отвечает ошибкой `actor_required`,
   * и это правильно — запись в журнале без имени человека не имеет смысла.
   * Проверяем и здесь, чтобы не отправлять заведомо отклоняемое сообщение.
   */
  sendCommand(name, opts = {}) {
    if (!COMMAND_NAMES.includes(name)) {
      this._log(`отброшена неизвестная команда: ${name}`);
      return false;
    }
    const payload = { name };
    const actor = String(opts.actor || '').trim();
    const reason = String(opts.reason || opts.note || '').trim();
    if (ACTOR_REQUIRED.includes(name) && !actor) {
      this._log(`команда ${name} отброшена: не указано, кто принял решение (actor)`);
      return false;
    }
    if (actor) payload.actor = actor.slice(0, 120);
    if (reason) payload.reason = reason.slice(0, 400);
    return this._emitEnvelope(MsgType.COMMAND, payload);
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
  ACTOR_REQUIRED,
  AUTH_HEADER,
  AUTH_TOKEN_ENV_VAR,
  KIND_FALLBACKS,
  PROTOCOL_VERSION,
  DEFAULT_WS_HOST,
  DEFAULT_WS_PORT,
  nowSec,
};

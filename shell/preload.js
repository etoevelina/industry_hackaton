'use strict';
/**
 * Мост между renderer (страница теста) и main-процессом оболочки.
 *
 * contextIsolation=true, nodeIntegration=false: renderer видит только
 * window.proctor с перечисленными ниже методами, никакого доступа к node.
 *
 * Приватность по контракту: телеметрия ввода чистится ЗДЕСЬ. Наружу уходят
 * только классы клавиш и интервалы, сами символы не покидают renderer —
 * даже если вызывающий код по невнимательности их передал.
 */

const { contextBridge, ipcRenderer } = require('electron');

/** Каналы main -> renderer. Подписка возможна только на них. */
const IN = Object.freeze({
  hello: 'proctor:hello',
  status: 'proctor:status',
  event: 'proctor:event',
  risk: 'proctor:risk',
  calibration: 'proctor:calibration',
  verdict: 'proctor:verdict',
  error: 'proctor:error',
  shellStatus: 'proctor:shell-status',
  blocking: 'proctor:blocking',
});

const TELEMETRY_KINDS = Object.freeze(['keystroke', 'paste', 'answer_submit', 'question_shown']);
const KEY_CLASSES = Object.freeze(['char', 'nav', 'ctrl']);
const CALIBRATION_STAGES = Object.freeze(['gaze_center', 'gaze_grid', 'identity', 'voice']);
const COMMAND_NAMES = Object.freeze(['snapshot', 'reset_risk', 'export_report']);

function nowSec() {
  return Date.now() / 1000;
}

function num(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

/**
 * Подписка на канал. Возвращает функцию отписки, чтобы renderer мог
 * снимать слушателей при смене экрана и не копить их.
 */
function subscribe(channel, cb) {
  if (typeof cb !== 'function') return () => {};
  const handler = (event, payload) => {
    try {
      cb(payload);
    } catch (err) {
      // ошибка в обработчике renderer не должна рвать канал
      // eslint-disable-next-line no-console
      console.error('[proctor] обработчик упал:', err);
    }
  };
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
}

/**
 * Приведение телеметрии к форме из docs/CONTRACT.md.
 * Всё лишнее отбрасывается: никаких символов, текстов ответов и содержимого буфера.
 */
function sanitizeTelemetry(raw) {
  const msg = raw || {};
  const kind = msg.kind;
  if (!TELEMETRY_KINDS.includes(kind)) return null;

  const out = {
    kind,
    question_id: String(msg.question_id || msg.questionId || ''),
    ts: num(msg.ts, nowSec()),
  };

  if (kind === 'keystroke') {
    out.interval_ms = Math.max(0, Math.round(num(msg.interval_ms || msg.intervalMs, 0)));
    const kc = msg.key_class || msg.keyClass;
    out.key_class = KEY_CLASSES.includes(kc) ? kc : 'char';
  } else if (kind === 'paste') {
    out.length = Math.max(0, Math.round(num(msg.length, 0)));
    out.source = 'clipboard';
  } else if (kind === 'answer_submit') {
    out.length = Math.max(0, Math.round(num(msg.length, 0)));
    out.time_to_answer_ms = Math.max(0, Math.round(num(msg.time_to_answer_ms || msg.timeToAnswerMs, 0)));
    const st = msg.typing_stats || msg.typingStats || {};
    out.typing_stats = {
      mean_ms: num(st.mean_ms || st.meanMs, 0),
      std_ms: num(st.std_ms || st.stdMs, 0),
      chars: Math.max(0, Math.round(num(st.chars, 0))),
    };
  } else if (kind === 'question_shown') {
    out.difficulty = Math.round(num(msg.difficulty, 0));
  }
  return out;
}

function sendTelemetry(raw) {
  const msg = sanitizeTelemetry(raw);
  if (!msg) return false;
  ipcRenderer.send('proctor:telemetry', msg);
  return true;
}

const api = {
  /** Версия протокола обмена (см. sidecar/protocol.py). */
  protocolVersion: 1,
  platform: process.platform,

  // --------------------------------------------------- подписки на сайдкар
  onHello: (cb) => subscribe(IN.hello, cb),
  onStatus: (cb) => subscribe(IN.status, cb),
  onEvent: (cb) => subscribe(IN.event, cb),
  onRisk: (cb) => subscribe(IN.risk, cb),
  onCalibration: (cb) => subscribe(IN.calibration, cb),
  onVerdict: (cb) => subscribe(IN.verdict, cb),
  onError: (cb) => subscribe(IN.error, cb),

  // ------------------------------------------------- подписки на оболочку
  /** Состояние оболочки раз в секунду: CV-канал, мониторы, блокировки. */
  onShellStatus: (cb) => subscribe(IN.shellStatus, cb),
  /** Блокирующий экран: {active, reason, title, text}. */
  onBlocking: (cb) => subscribe(IN.blocking, cb),

  /** Универсальная подписка по короткому имени канала. */
  on: (name, cb) => (IN[name] ? subscribe(IN[name], cb) : () => {}),

  // -------------------------------------------------------------- телеметрия
  sendTelemetry,

  /** Нажатие клавиши: передаём только класс и интервал, не символ. */
  keystroke: (questionId, intervalMs, keyClass) => sendTelemetry({
    kind: 'keystroke', question_id: questionId, interval_ms: intervalMs, key_class: keyClass,
  }),

  /** Вставка из буфера: только длина вставленного блока. */
  paste: (questionId, length) => sendTelemetry({
    kind: 'paste', question_id: questionId, length,
  }),

  /** Отправка ответа: длина, время и статистика ритма набора. */
  answerSubmit: (questionId, length, timeToAnswerMs, typingStats) => sendTelemetry({
    kind: 'answer_submit',
    question_id: questionId,
    length,
    time_to_answer_ms: timeToAnswerMs,
    typing_stats: typingStats,
  }),

  /** Показ вопроса: нужен fusion-движку как точка отсчёта. */
  questionShown: (questionId, difficulty) => sendTelemetry({
    kind: 'question_shown', question_id: questionId, difficulty,
  }),

  // ----------------------------------------------------------- команды сессии
  /** meta: {student_id, exam_id, student_name}. */
  startSession: (meta) => ipcRenderer.invoke('proctor:session-start', meta || {}),
  endSession: (reason) => ipcRenderer.invoke('proctor:session-end', String(reason || 'renderer_request')),

  /** stage: gaze_center | gaze_grid | identity | voice; point: [x,y] либо null. */
  calibrate: (stage, point) => {
    if (!CALIBRATION_STAGES.includes(stage)) return Promise.resolve(false);
    const pt = Array.isArray(point) && point.length === 2
      ? [num(point[0], 0), num(point[1], 0)]
      : null;
    return ipcRenderer.invoke('proctor:calibrate', stage, pt);
  },

  /** name: snapshot | reset_risk | export_report. */
  command: (name) => {
    if (!COMMAND_NAMES.includes(name)) return Promise.resolve(false);
    return ipcRenderer.invoke('proctor:command', name);
  },
  snapshot: () => ipcRenderer.invoke('proctor:command', 'snapshot'),
  resetRisk: () => ipcRenderer.invoke('proctor:command', 'reset_risk'),
  exportReport: () => ipcRenderer.invoke('proctor:command', 'export_report'),

  // -------------------------------------------------------- состояние оболочки
  /** Честная карта блокировок: {lockdown, matrix, shell}. */
  capabilities: () => ipcRenderer.invoke('proctor:capabilities'),
  /** Текущее состояние оболочки без ожидания следующего пуша. */
  shellStatus: () => ipcRenderer.invoke('proctor:shell-status'),

  /**
   * Защита содержимого от скриншотов и записи экрана.
   * Без аргумента — переключение (демонстрация эффекта «чёрное окно»).
   */
  toggleContentProtection: (value) => ipcRenderer.invoke(
    'toggle-content-protection',
    typeof value === 'boolean' ? value : undefined,
  ),

  /** Блокирующий экран поверх теста: {active, reason, title, text}. */
  setBlocking: (payload) => ipcRenderer.invoke('proctor:set-blocking', payload || { active: false }),

  /** Завершить тест и закрыть оболочку. */
  exit: (reason) => ipcRenderer.invoke('proctor:exit', String(reason || 'renderer_exit')),
};

contextBridge.exposeInMainWorld('proctor', api);

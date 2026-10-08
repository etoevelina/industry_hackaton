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
  protection: 'proctor:protection',
});

/**
 * Состояния оболочки. Берём из общего модуля, а не держим копию: разъехавшийся
 * список здесь означал бы, что renderer присылает состояние, которого
 * main-процесс не знает, и переход молча отвергается.
 */
const EXAM_STATES = require('./state').SHELL_STATES;

const TELEMETRY_KINDS = Object.freeze(['keystroke', 'paste', 'answer_submit', 'question_shown']);
const KEY_CLASSES = Object.freeze(['char', 'nav', 'ctrl']);
const CALIBRATION_STAGES = Object.freeze(['gaze_center', 'gaze_grid', 'identity', 'voice']);
/*
 * Полный словарь команд (sidecar/protocol.py). `proctor_lock` /
 * `proctor_release` — решение человека над приостановленным экзаменом; без
 * них приостановка по порогу блокировки была неснимаема никем, потому что
 * команда отсекалась здесь. `deliver_package` — повтор копирования пакета в
 * папку проктора с финального экрана; условия (сессия закрыта, пакет на
 * диске) проверяет main-процесс, ответ — {ok, reason?, message?}.
 */
const COMMAND_NAMES = Object.freeze([
  'snapshot', 'reset_risk', 'export_report', 'proctor_lock', 'proctor_release',
  'deliver_package',
]);
const ACTOR_REQUIRED = Object.freeze(['proctor_lock', 'proctor_release']);

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
 * Метка вопроса: номер в билете, а не текст.
 *
 * Шестнадцать символов из списка ниже и ни одного пробела. Банк вопросов —
 * внешние данные: id вида `q3/<первая строка ответа>` приходит не от злого
 * умысла, а от небрежного экспорта, и до этой проверки такая метка уезжала в
 * подписанный журнал и в пакет проктору. Обрезка по длине задачу не решает:
 * обрезанный ответ это всё ещё ответ, поэтому непохожее на идентификатор
 * заменяется устойчивым суррогатом (та же логика, что в `safe_label()`
 * сайдкара — сайдкар проверяет повторно и нам на слово не верит).
 */
const LABEL_MAX_LEN = 16;
const LABEL_OK = /^[0-9A-Za-z_.\-\u0400-\u04FF]+$/;

function safeLabel(value) {
  const rawLabel = String(value === undefined || value === null ? '' : value).trim();
  if (!rawLabel) return '';
  if (rawLabel.length <= LABEL_MAX_LEN && LABEL_OK.test(rawLabel)) return rawLabel;
  // Суррогат: одинаковый вход — одинаковый выход, поэтому дедупликация по
  // вопросу на стороне сайдкара продолжает работать, а содержание уходит.
  let h = 0x811c9dc5;
  for (let i = 0; i < rawLabel.length; i += 1) {
    h ^= rawLabel.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return 'qid-' + ('0000000' + h.toString(16)).slice(-8);
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
    question_id: safeLabel(msg.question_id || msg.questionId),
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

/**
 * Отчёт о раскладке: какой прямоугольник окна renderer оставил свободным под
 * страницу экзамена. Четыре неотрицательных числа и ничего больше —
 * проверяет присланное всё равно main-процесс (normalizeExamViewInset в
 * shell/state.js), но пропускать сюда произвольный объект незачем.
 */
function sendExamViewInset(raw) {
  const side = (value) => {
    const n = Number(value);
    return Number.isFinite(n) && n >= 0 ? Math.round(n) : 0;
  };
  if (!raw || typeof raw !== 'object') return false;
  ipcRenderer.send('proctor:exam-view-inset', {
    top: side(raw.top),
    right: side(raw.right),
    bottom: side(raw.bottom),
    left: side(raw.left),
  });
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
  /**
   * Защищённый режим: {active, examState, reason, accelerators, systemWide, …}.
   * Приходит при каждом изменении — HUD обязан показывать это студенту.
   */
  onProtection: (cb) => subscribe(IN.protection, cb),

  /** Универсальная подписка по короткому имени канала. */
  on: (name, cb) => (IN[name] ? subscribe(IN[name], cb) : () => {}),

  /**
   * Сообщить оболочке, сколько места наша вёрстка оставила свободным под
   * страницу экзамена: {top,right,bottom,left} в пикселях окна. Источник
   * истины по раскладке — CSS, а не константа в main-процессе.
   */
  reportExamViewInset: sendExamViewInset,

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

  /** name: snapshot | reset_risk | export_report | proctor_lock | proctor_release | deliver_package. */
  command: (name, opts) => {
    if (!COMMAND_NAMES.includes(name)) return Promise.resolve(false);
    const o = opts || {};
    const actor = String(o.actor || '').trim();
    const reason = String(o.reason || o.note || '').trim();
    // Решение без указания, кто его принял, бессмысленно: вся политика
    // затевалась ради того, чтобы в журнале стоял человек.
    if (ACTOR_REQUIRED.includes(name) && !actor) return Promise.resolve(false);
    return ipcRenderer.invoke('proctor:command', name, {
      actor: actor.slice(0, 120),
      reason: reason.slice(0, 400),
    });
  },
  snapshot: () => ipcRenderer.invoke('proctor:command', 'snapshot'),
  resetRisk: () => ipcRenderer.invoke('proctor:command', 'reset_risk'),
  exportReport: () => ipcRenderer.invoke('proctor:command', 'export_report'),
  /** Повторить копирование последнего пакета в папку проктора. */
  deliverPackage: () => ipcRenderer.invoke('proctor:command', 'deliver_package'),
  /**
   * Решение проктора над приостановленным экзаменом.
   * `actor` обязателен: сайдкар отвечает ошибкой actor_required без него.
   */
  proctorLock: (actor, reason) => ipcRenderer.invoke('proctor:command', 'proctor_lock',
    { actor: String(actor || '').trim().slice(0, 120),
      reason: String(reason || '').trim().slice(0, 400) }),
  proctorRelease: (actor, reason) => ipcRenderer.invoke('proctor:command', 'proctor_release',
    { actor: String(actor || '').trim().slice(0, 120),
      reason: String(reason || '').trim().slice(0, 400) }),

  // -------------------------------------------------------- состояние оболочки
  /** Честная карта блокировок: {lockdown, matrix, shell}. */
  capabilities: () => ipcRenderer.invoke('proctor:capabilities'),
  /** Текущее состояние оболочки без ожидания следующего пуша. */
  shellStatus: () => ipcRenderer.invoke('proctor:shell-status'),

  /** Текущее состояние защищённого режима. */
  protection: () => ipcRenderer.invoke('proctor:protection'),

  /**
   * Сообщить оболочке, где находится студент: idle | consent | preflight |
   * calibration | exam | paused | finished. Оболочка включает блокировки
   * только на exam и paused.
   *
   * Это СИГНАЛ, не команда: main-процесс проверяет и само имя, и допустимость
   * перехода, и вправе отказать. Ответ — {ok, state, reason?}.
   */
  setExamState: (next) => {
    if (!EXAM_STATES.includes(next)) {
      return Promise.resolve({ ok: false, state: null, reason: 'unknown_state' });
    }
    return ipcRenderer.invoke('proctor:exam-state', next);
  },

  /** Список состояний — чтобы renderer не держал свою копию. */
  examStates: EXAM_STATES,

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

  /**
   * Открыть отчёт сессии (<каталог сессии>/report.html) браузером по
   * умолчанию. Путь не передаётся: его знает main-процесс со слов ядра, и он
   * же откажет, пока сессия не закрыта и блокировки не сняты.
   * Ответ — {ok, reason?, message?, path?}.
   */
  openReport: () => ipcRenderer.invoke('proctor:open-report'),

  /** Завершить тест и закрыть оболочку. */
  exit: (reason) => ipcRenderer.invoke('proctor:exit', String(reason || 'renderer_exit')),
};

contextBridge.exposeInMainWorld('proctor', api);

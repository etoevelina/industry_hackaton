'use strict';
/**
 * Electron-оболочка «защищённого браузера» для локального прокторинга.
 *
 * Отвечает за:
 *  - kiosk-окно без рамки, поверх всех окон, с setContentProtection (окно выходит
 *    чёрным на скриншотах и в записи экрана — ключевой демо-эффект);
 *  - слой блокировки окружения (shell/lockdown.js) и журналирование каждой
 *    подавленной комбинации как SHELL_EVENT SHORTCUT_BLOCKED;
 *  - контроль мониторов: второй экран => MULTIPLE_DISPLAYS + блокирующий экран;
 *  - удержание фокуса и полного экрана: blur => WINDOW_BLUR + возврат фокуса,
 *    leave-full-screen => FULLSCREEN_EXIT + возврат в fullscreen;
 *  - WS-канал к сайдкару (shell/ipc.js). Сайдкар не поднялся — оболочка работает,
 *    HUD честно показывает «CV-канал недоступен».
 *
 * Весь текст для пользователя — по-русски. Наружу (в интернет) не ходим: только loopback.
 */

const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const {
  app, BrowserWindow, ipcMain, screen, globalShortcut, clipboard, session, Menu,
} = require('electron');

const { SidecarLink, EventKind, MsgType, DEFAULT_WS_HOST, DEFAULT_WS_PORT } = require('./ipc');
const { Lockdown, comboThrottle } = require('./lockdown');

const ROOT_DIR = path.resolve(__dirname, '..');
const RENDERER_INDEX = path.join(__dirname, 'renderer', 'index.html');

/** Аварийный выход для прокторa/жюри: иначе из kiosk-окна не выйти. */
const ADMIN_EXIT_ACCELERATOR = 'CommandOrControl+Alt+Shift+Q';

// ---------------------------------------------------------------------------
// Аргументы командной строки
// ---------------------------------------------------------------------------

function argFlag(name) {
  return process.argv.includes(`--${name}`);
}

function argValue(name, fallback) {
  const prefix = `--${name}=`;
  const found = process.argv.find((a) => a.startsWith(prefix));
  if (found) return found.slice(prefix.length);
  const idx = process.argv.indexOf(`--${name}`);
  if (idx !== -1 && process.argv[idx + 1] && !process.argv[idx + 1].startsWith('--')) {
    return process.argv[idx + 1];
  }
  return fallback;
}

const CLI = {
  spawnSidecar: argFlag('spawn-sidecar'),
  noKiosk: argFlag('no-kiosk'),              // отладочный режим: окно можно двигать
  studentId: argValue('student-id', 'demo-student'),
  examId: argValue('exam-id', 'demo-exam'),
  studentName: argValue('student-name', 'Демо-студент'),
  wsHost: argValue('ws-host', DEFAULT_WS_HOST),
  wsPort: Number(argValue('ws-port', String(DEFAULT_WS_PORT))) || DEFAULT_WS_PORT,
};

function log(...args) {
  // eslint-disable-next-line no-console
  console.log('[shell]', ...args);
}

// ---------------------------------------------------------------------------
// Состояние процесса
// ---------------------------------------------------------------------------

const state = {
  win: null,
  overlay: null,
  blockingReason: null,
  blockingTitle: '',
  blockingText: '',
  contentProtection: true,
  displayCount: 1,
  sessionStarted: false,
  sessionAuto: false,
  sessionMeta: null,
  quitting: false,
  sidecarChild: null,
  rendererReady: false,
  lastVerdict: null,
  lastRisk: null,
  statusTimer: null,
};

const link = new SidecarLink({ host: CLI.wsHost, port: CLI.wsPort, logger: (m) => log('ipc:', m) });

/** Троттлинг событий оболочки: одна комбинация — не чаще раза в 400 мс. */
const allowCombo = comboThrottle(400);
/** Производные события (paste/devtools) — реже, чтобы не дублировать сигнал. */
const allowDerived = comboThrottle(1500);
/** Потеря фокуса и выход из fullscreen — раз в секунду, иначе ping-pong. */
const allowWindowState = comboThrottle(1000);

const lockdown = new Lockdown({
  logger: (m) => log('lockdown:', m),
  onShortcutBlocked: (combo, meta) => {
    link.sendShellEvent(EventKind.SHORTCUT_BLOCKED, {
      combo,
      lock: meta.lock,
      label: meta.label,
      source: meta.source,
      platform: meta.platform,
    });
    log('подавлена комбинация', combo);
  },
  onClipboardCleared: (info) => {
    // Непустой буфер во время теста — след копирования. Сам текст наружу не уходит.
    if (!allowDerived('clipboard-clear')) return;
    link.sendShellEvent(EventKind.CLIPBOARD_PASTE, {
      reason: 'clipboard_nonempty_cleared',
      length: info.length,
      source: 'clipboard',
    });
  },
  onDisplays: (info) => handleDisplays(info, 'poll'),
});

// ---------------------------------------------------------------------------
// Передача в renderer
// ---------------------------------------------------------------------------

function sendToRenderer(channel, payload) {
  const win = state.win;
  if (!win || win.isDestroyed()) return;
  const wc = win.webContents;
  if (!wc || wc.isDestroyed()) return;
  try {
    wc.send(channel, payload);
  } catch (err) {
    log('send в renderer не удался:', err && err.message);
  }
}

function shellStatus() {
  const sidecar = link.info();
  return {
    platform: process.platform,
    sidecar,
    cvAvailable: sidecar.connected,
    // строка готова к выводу в HUD как есть
    cvMessage: sidecar.connected
      ? 'CV-канал активен'
      : (sidecar.available ? 'CV-канал недоступен' : 'CV-канал недоступен (не установлен ws)'),
    contentProtection: state.contentProtection,
    displayCount: state.displayCount,
    blocking: state.blockingReason
      ? { reason: state.blockingReason, title: state.blockingTitle, text: state.blockingText }
      : null,
    session: {
      started: state.sessionStarted,
      auto: state.sessionAuto,
      meta: state.sessionMeta,
    },
    lastVerdict: state.lastVerdict,
    lastRisk: state.lastRisk,
    lockdown: lockdown.capabilities(),
    adminExit: ADMIN_EXIT_ACCELERATOR,
  };
}

function broadcastShellStatus() {
  sendToRenderer('proctor:shell-status', shellStatus());
}

// ---------------------------------------------------------------------------
// Блокирующий экран (нативное оверлей-окно, не зависит от renderer)
// ---------------------------------------------------------------------------

/**
 * Строка в JS-литерал для инлайн-скрипта. '<' экранируем в <, иначе текст
 * вида '</script>' (а его может прислать renderer через setBlocking) разорвёт тег.
 */
function jsString(value) {
  return JSON.stringify(String(value)).replace(/</g, '\\u003c');
}

function blockingHtml(title, text) {
  const html = `<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Тест приостановлен</title><style>
html,body{margin:0;height:100%;background:#0b0f14;color:#e6edf3;
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
.wrap{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;
text-align:center;padding:48px;box-sizing:border-box;}
.mark{width:72px;height:72px;border-radius:50%;border:3px solid #f0883e;color:#f0883e;
display:flex;align-items:center;justify-content:center;font-size:40px;margin-bottom:28px;}
h1{font-size:28px;margin:0 0 14px;font-weight:600;}
p{margin:0;max-width:640px;color:#9fb0c0;font-size:17px;}
.hint{margin-top:32px;font-size:14px;color:#6b7c8c;}
</style></head><body><div class="wrap">
<div class="mark">!</div><h1></h1><p></p>
<div class="hint">Экран снимется автоматически, как только нарушение будет устранено.</div>
</div><script>
document.querySelector('h1').textContent = ${jsString(title)};
document.querySelector('p').textContent = ${jsString(text)};
</script></body></html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

function showBlocking(reason, title, text) {
  state.blockingReason = reason;
  state.blockingTitle = title;
  state.blockingText = text;
  sendToRenderer('proctor:blocking', { active: true, reason, title, text });

  if (state.overlay && !state.overlay.isDestroyed()) {
    state.overlay.loadURL(blockingHtml(title, text));
    state.overlay.show();
    state.overlay.focus();
    state.overlay.moveTop();
    broadcastShellStatus();
    return;
  }

  // Оверлей не делаем fullscreen: на macOS это создало бы отдельный Space и
  // увело окно из-под kiosk-окна. Вместо этого — точные границы экрана,
  // уровень screen-saver и видимость над чужим fullscreen.
  const primary = screen.getPrimaryDisplay();
  const overlay = new BrowserWindow({
    x: primary.bounds.x,
    y: primary.bounds.y,
    width: primary.bounds.width,
    height: primary.bounds.height,
    frame: false,
    show: false,
    movable: false,
    resizable: false,
    minimizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    backgroundColor: '#0b0f14',
    title: 'Тест приостановлен',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      devTools: false,
      sandbox: true,
    },
  });
  overlay.setAlwaysOnTop(true, 'screen-saver', 2);
  try {
    overlay.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  } catch (err) { /* не все платформы */ }
  try {
    overlay.setContentProtection(state.contentProtection);
  } catch (err) { /* платформа не поддерживает */ }
  overlay.loadURL(blockingHtml(title, text));
  overlay.once('ready-to-show', () => {
    if (!overlay.isDestroyed()) {
      overlay.show();
      overlay.focus();
      overlay.moveTop();
    }
  });
  overlay.webContents.on('before-input-event', (e) => e.preventDefault());
  state.overlay = overlay;
  broadcastShellStatus();
}

function hideBlocking(reason) {
  // снимаем только тот экран, который сами же и поставили
  if (reason && state.blockingReason && state.blockingReason !== reason) return;
  state.blockingReason = null;
  state.blockingTitle = '';
  state.blockingText = '';
  if (state.overlay && !state.overlay.isDestroyed()) {
    state.overlay.destroy();
  }
  state.overlay = null;
  sendToRenderer('proctor:blocking', { active: false, reason: reason || null });
  const win = state.win;
  if (win && !win.isDestroyed()) {
    win.show();
    win.focus();
  }
  broadcastShellStatus();
}

// ---------------------------------------------------------------------------
// Мониторы
// ---------------------------------------------------------------------------

function handleDisplays(info, source) {
  const count = info && typeof info.count === 'number' ? info.count : screen.getAllDisplays().length;
  const displays = (info && info.displays) || screen.getAllDisplays().map((d) => ({
    id: d.id, internal: Boolean(d.internal), bounds: d.bounds, scale: d.scaleFactor,
  }));
  state.displayCount = count;

  if (count > 1) {
    link.sendShellEvent(EventKind.MULTIPLE_DISPLAYS, { count, displays, source });
    showBlocking(
      'multiple_displays',
      'Обнаружено несколько экранов',
      `Подключено экранов: ${count}. Отключите внешние мониторы, проекторы и приставки захвата — тест продолжится автоматически.`,
    );
  } else {
    hideBlocking('multiple_displays');
  }
  broadcastShellStatus();
}

// ---------------------------------------------------------------------------
// Подавление комбинаций внутри окна (before-input-event)
// ---------------------------------------------------------------------------

/**
 * Разбор нажатия. Возвращает null, если комбинация разрешена, либо
 * {combo, reason, kinds:[EventKind]} для подавления.
 *
 * Логика: mod = Ctrl (Win/Linux) либо Cmd (macOS) — одна таблица на обе платформы.
 */
function classifyInput(input) {
  if (!input || input.type !== 'keyDown') return null;

  const rawKey = typeof input.key === 'string' ? input.key : '';
  const key = rawKey.toLowerCase();
  const code = typeof input.code === 'string' ? input.code : '';
  const ctrl = Boolean(input.control);
  const meta = Boolean(input.meta);
  const alt = Boolean(input.alt);
  const shift = Boolean(input.shift);
  const mod = ctrl || meta;
  const isDarwin = process.platform === 'darwin';

  const parts = [];
  if (ctrl) parts.push('Ctrl');
  if (meta) parts.push(isDarwin ? 'Cmd' : 'Super');
  if (alt) parts.push('Alt');
  if (shift) parts.push('Shift');
  let name;
  if (code === 'Space' || rawKey === ' ') name = 'Space';
  else if (code === 'Tab' || key === 'tab') name = 'Tab';
  else if (rawKey.length === 1) name = rawKey.toUpperCase();
  else if (rawKey) name = rawKey;
  else name = code || '?';
  const combo = parts.concat([name]).join('+');

  const isFn = (n) => key === n.toLowerCase() || code === n;

  // 1. DevTools
  if (isFn('F12') || (mod && shift && ['i', 'j', 'c'].includes(key))) {
    return { combo, reason: 'devtools', kinds: [EventKind.SHORTCUT_BLOCKED, EventKind.DEVTOOLS_ATTEMPT] };
  }
  // 2. Буфер обмена
  if (mod && !alt && !shift && ['c', 'v', 'x'].includes(key)) {
    const kinds = [EventKind.SHORTCUT_BLOCKED];
    if (key === 'v') kinds.push(EventKind.CLIPBOARD_PASTE);
    return { combo, reason: 'clipboard', kinds };
  }
  // 3. Печать / сохранение / выделить всё / перезагрузка
  if (mod && !alt && (key === 'r' || (!shift && ['p', 's', 'a'].includes(key)))) {
    return { combo, reason: 'page_action', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  // 4. Новое окно / вкладка / закрытие
  if (mod && !alt && !shift && ['n', 't', 'w'].includes(key)) {
    return { combo, reason: 'new_window', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  // 5. Выход из приложения
  if (alt && isFn('F4')) {
    return { combo, reason: 'app_quit', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  if (meta && key === 'q') {
    return { combo, reason: 'app_quit', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  // 6. Свернуть / скрыть / системный поиск (macOS)
  if (meta && ['m', 'h'].includes(key)) {
    return { combo, reason: 'minimize_hide', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  if (meta && name === 'Space') {
    return { combo, reason: 'os_launcher', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  // 7. Полный экран
  if (isFn('F11') || (meta && ctrl && key === 'f')) {
    return { combo, reason: 'fullscreen_toggle', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  // 8. Перебор вкладок
  if (mod && name === 'Tab') {
    return { combo, reason: 'tab_cycle', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  // 9. Снимок экрана на macOS (Cmd+Shift+3/4/5)
  if (meta && shift && ['3', '4', '5'].includes(key)) {
    return { combo, reason: 'screenshot_key', kinds: [EventKind.SHORTCUT_BLOCKED] };
  }
  return null;
}

function reportBlockedCombo(verdict, source) {
  const { combo, reason, kinds } = verdict;
  for (const kind of kinds) {
    const allow = kind === EventKind.SHORTCUT_BLOCKED ? allowCombo : allowDerived;
    if (!allow(`${kind}:${combo}`)) continue;
    link.sendShellEvent(kind, { combo, reason, source });
  }
}

// ---------------------------------------------------------------------------
// Главное окно
// ---------------------------------------------------------------------------

function fallbackRendererHtml() {
  // Используется, только если shell/renderer/index.html ещё не положен:
  // оболочка не должна показывать пустой экран на демо.
  const html = `<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Защищённый браузер</title><style>
html,body{margin:0;height:100%;background:#0b0f14;color:#e6edf3;
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
.wrap{padding:40px;max-width:900px;margin:0 auto;}
h1{font-size:22px;margin:0 0 6px;}
.sub{color:#7d8d9c;margin-bottom:28px;}
.card{background:#111823;border:1px solid #1e2a38;border-radius:12px;padding:18px 20px;margin-bottom:14px;}
.row{display:flex;justify-content:space-between;gap:16px;padding:4px 0;}
.k{color:#9fb0c0;}
.ok{color:#56d364;} .bad{color:#f85149;} .warn{color:#f0883e;}
button{background:#1f6feb;border:0;color:#fff;padding:9px 16px;border-radius:8px;
font-size:14px;cursor:pointer;}
</style></head><body><div class="wrap">
<h1>Защищённый браузер прокторинга</h1>
<div class="sub">Интерфейс теста ещё не установлен (shell/renderer/index.html). Это служебный экран оболочки.</div>
<div class="card">
<div class="row"><span class="k">CV-канал</span><span id="cv" class="bad">проверяем…</span></div>
<div class="row"><span class="k">Экранов</span><span id="disp">—</span></div>
<div class="row"><span class="k">Защита содержимого</span><span id="cp">—</span></div>
<div class="row"><span class="k">Блокировки окружения</span><span id="locks">—</span></div>
<div class="row"><span class="k">Risk-score</span><span id="risk">—</span></div>
</div>
<button id="toggle">Переключить защиту содержимого</button>
</div><script>
function paint(s){
  var cv=document.getElementById('cv');
  cv.textContent=s.cvMessage; cv.className=s.cvAvailable?'ok':'bad';
  document.getElementById('disp').textContent=s.displayCount;
  document.getElementById('cp').textContent=s.contentProtection?'включена':'выключена';
  var l=s.lockdown&&s.lockdown.summary;
  document.getElementById('locks').textContent=l?(l.supported+' полных / '+l.partial+' частичных / '+l.unsupported+' недоступных ('+s.lockdown.platformLabel+')'):'—';
}
if(window.proctor){
  window.proctor.onShellStatus(paint);
  window.proctor.shellStatus().then(paint);
  window.proctor.onRisk(function(m){document.getElementById('risk').textContent=Math.round(m.score)+' ('+m.level+')';});
  document.getElementById('toggle').addEventListener('click',function(){window.proctor.toggleContentProtection();});
}
</script></body></html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

function createWindow() {
  const primary = screen.getPrimaryDisplay();
  const win = new BrowserWindow({
    width: primary.workAreaSize.width,
    height: primary.workAreaSize.height,
    show: false,
    frame: false,
    kiosk: !CLI.noKiosk,
    fullscreen: !CLI.noKiosk,
    alwaysOnTop: true,
    movable: CLI.noKiosk,
    resizable: CLI.noKiosk,
    minimizable: false,
    maximizable: true,
    closable: false,
    skipTaskbar: false,
    autoHideMenuBar: true,
    backgroundColor: '#0b0f14',
    title: 'Защищённый браузер прокторинга',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),
      devTools: false,
      sandbox: false,
      spellcheck: false,
      webviewTag: false,
      backgroundThrottling: false,
    },
  });
  state.win = win;

  win.setAlwaysOnTop(true, 'screen-saver', 1);
  try {
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  } catch (err) { /* не все платформы */ }

  // Ключевой демо-момент: окно исключается из захвата экрана.
  applyContentProtection(true);

  win.once('ready-to-show', () => {
    win.show();
    win.focus();
    if (!CLI.noKiosk) {
      win.setKiosk(true);
      win.setFullScreen(true);
    }
  });

  // --- удержание фокуса -----------------------------------------------------
  win.on('blur', () => {
    if (state.quitting) return;
    // оверлей блокирующего экрана сам забирает фокус — это не нарушение
    if (state.blockingReason) return;
    if (allowWindowState('window-blur')) {
      link.sendShellEvent(EventKind.WINDOW_BLUR, {
        reason: 'focus_lost',
        platform: process.platform,
      });
      log('потеря фокуса — возвращаем окно');
    }
    // возврат фокуса немедленно
    if (!win.isDestroyed()) {
      win.show();
      win.focus();
      win.moveTop();
    }
  });

  // --- удержание fullscreen -------------------------------------------------
  win.on('leave-full-screen', () => {
    if (state.quitting || CLI.noKiosk) return;
    if (allowWindowState('fullscreen-exit')) {
      link.sendShellEvent(EventKind.FULLSCREEN_EXIT, { reason: 'left_fullscreen' });
    }
    if (!win.isDestroyed()) {
      win.setFullScreen(true);
      win.setKiosk(true);
      win.focus();
    }
  });

  win.on('minimize', () => {
    if (state.quitting || CLI.noKiosk) return;
    if (allowWindowState('window-minimize')) {
      link.sendShellEvent(EventKind.WINDOW_BLUR, { reason: 'minimized' });
    }
    win.restore();
    win.focus();
  });

  // Окно теста нельзя закрыть обычным способом — только админским выходом.
  win.on('close', (e) => {
    if (state.quitting) return;
    e.preventDefault();
    if (allowCombo('window-close-attempt')) {
      link.sendShellEvent(EventKind.SHORTCUT_BLOCKED, {
        combo: 'WINDOW_CLOSE',
        reason: 'window_close_request',
        source: 'window',
      });
    }
    win.show();
    win.focus();
  });

  hardenWebContents(win.webContents);

  win.webContents.on('did-finish-load', () => {
    state.rendererReady = true;
    broadcastShellStatus();
    if (state.blockingReason) {
      sendToRenderer('proctor:blocking', {
        active: true,
        reason: state.blockingReason,
        title: state.blockingTitle,
        text: state.blockingText,
      });
    }
  });

  if (fs.existsSync(RENDERER_INDEX)) {
    win.loadFile(RENDERER_INDEX);
  } else {
    log('shell/renderer/index.html не найден — служебный экран оболочки');
    win.loadURL(fallbackRendererHtml());
  }
  return win;
}

function applyContentProtection(on) {
  state.contentProtection = Boolean(on);
  for (const w of [state.win, state.overlay]) {
    if (!w || w.isDestroyed()) continue;
    try {
      w.setContentProtection(state.contentProtection);
    } catch (err) {
      log('setContentProtection не поддержан:', err && err.message);
    }
  }
  return state.contentProtection;
}

// ---------------------------------------------------------------------------
// Ужесточение webContents
// ---------------------------------------------------------------------------

/** Одно и то же webContents приходит и из createWindow, и из web-contents-created. */
const hardenedContents = new WeakSet();

function hardenWebContents(wc) {
  if (!wc || wc.isDestroyed()) return;
  if (hardenedContents.has(wc)) return;
  hardenedContents.add(wc);

  wc.on('context-menu', (e) => e.preventDefault());

  wc.setWindowOpenHandler(({ url }) => {
    if (allowCombo(`window-open:${url}`)) {
      link.sendShellEvent(EventKind.SHORTCUT_BLOCKED, {
        combo: 'WINDOW_OPEN',
        reason: 'window_open_denied',
        source: 'webContents',
        url,
      });
    }
    return { action: 'deny' };
  });

  wc.on('will-navigate', (e, url) => {
    const current = wc.getURL();
    if (url === current) return;
    e.preventDefault();
    if (allowCombo(`navigate:${url}`)) {
      link.sendShellEvent(EventKind.SHORTCUT_BLOCKED, {
        combo: 'NAVIGATION',
        reason: 'navigation_denied',
        source: 'webContents',
        url,
      });
    }
    log('навигация запрещена:', url);
  });

  wc.on('will-redirect', (e) => e.preventDefault());

  wc.on('will-attach-webview', (e) => e.preventDefault());

  wc.on('before-input-event', (e, input) => {
    const verdict = classifyInput(input);
    if (!verdict) return;
    e.preventDefault();
    reportBlockedCombo(verdict, 'before-input-event');
  });

  wc.on('devtools-opened', () => {
    try { wc.closeDevTools(); } catch (err) { /* уже закрыты */ }
    if (allowDerived('devtools-opened')) {
      link.sendShellEvent(EventKind.DEVTOOLS_ATTEMPT, { reason: 'devtools_opened', source: 'webContents' });
    }
  });

  wc.on('render-process-gone', (e, details) => {
    log('renderer упал:', details && details.reason);
  });
}

// ---------------------------------------------------------------------------
// Сайдкар дочерним процессом (только по флагу --spawn-sidecar)
// ---------------------------------------------------------------------------

function firstExisting(paths) {
  for (const p of paths) {
    try { if (fs.existsSync(p)) return p; } catch (err) { /* нет доступа */ }
  }
  return null;
}

function spawnSidecar() {
  const entry = firstExisting([
    path.join(ROOT_DIR, 'sidecar', 'main.py'),
    path.join(ROOT_DIR, 'sidecar', 'server.py'),
    path.join(ROOT_DIR, 'sidecar', 'app.py'),
    path.join(ROOT_DIR, 'sidecar', '__main__.py'),
  ]);
  if (!entry) {
    log('точка входа сайдкара не найдена — запустите его вручную');
    return;
  }
  const python = process.env.PROCTOR_PYTHON
    || firstExisting([
      path.join(ROOT_DIR, '.venv', 'bin', 'python3'),
      path.join(ROOT_DIR, '.venv', 'bin', 'python'),
      path.join(ROOT_DIR, '.venv', 'Scripts', 'python.exe'),
    ])
    || 'python3';

  log('запускаем сайдкар:', python, entry);
  let child;
  try {
    child = spawn(python, [entry], {
      cwd: ROOT_DIR,
      env: Object.assign({}, process.env, { PYTHONUNBUFFERED: '1' }),
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (err) {
    log('сайдкар не запустился:', err && err.message);
    return;
  }
  state.sidecarChild = child;
  child.stdout.on('data', (d) => log('sidecar:', String(d).trimEnd()));
  child.stderr.on('data', (d) => log('sidecar!', String(d).trimEnd()));
  child.on('exit', (code) => {
    log('сайдкар завершился, код', code);
    state.sidecarChild = null;
  });
  child.on('error', (err) => log('сайдкар: ошибка процесса', err && err.message));
}

function killSidecar() {
  const child = state.sidecarChild;
  if (!child) return;
  state.sidecarChild = null;
  try { child.kill('SIGTERM'); } catch (err) { /* уже мёртв */ }
}

// ---------------------------------------------------------------------------
// IPC из renderer
// ---------------------------------------------------------------------------

function registerIpc() {
  // Переключатель защиты содержимого — для показа эффекта на демо.
  ipcMain.handle('toggle-content-protection', (e, value) => {
    const next = typeof value === 'boolean' ? value : !state.contentProtection;
    applyContentProtection(next);
    broadcastShellStatus();
    log('защита содержимого:', state.contentProtection ? 'включена' : 'выключена');
    return state.contentProtection;
  });

  ipcMain.handle('proctor:capabilities', () => ({
    lockdown: lockdown.capabilities(),
    matrix: Lockdown.matrix(),
    shell: shellStatus(),
  }));

  ipcMain.handle('proctor:shell-status', () => shellStatus());

  ipcMain.handle('proctor:session-start', (e, meta) => {
    const payload = {
      student_id: (meta && (meta.student_id || meta.studentId)) || CLI.studentId,
      exam_id: (meta && (meta.exam_id || meta.examId)) || CLI.examId,
      student_name: (meta && (meta.student_name || meta.studentName)) || CLI.studentName,
    };
    state.sessionStarted = true;
    state.sessionAuto = false;
    state.sessionMeta = payload;
    link.sendSessionStart(payload);
    broadcastShellStatus();
    return payload;
  });

  ipcMain.handle('proctor:session-end', (e, reason) => {
    link.sendSessionEnd(typeof reason === 'string' ? reason : 'renderer_request');
    state.sessionStarted = false;
    broadcastShellStatus();
    return true;
  });

  ipcMain.handle('proctor:calibrate', (e, stage, point) => link.sendCalibrate(stage, point));

  ipcMain.handle('proctor:command', (e, name) => link.sendCommand(name));

  // Телеметрия — поток, ответа не ждём (ipcMain.on, не handle).
  ipcMain.on('proctor:telemetry', (e, msg) => {
    link.sendTelemetry(msg || {});
  });

  // Renderer может попросить свой блокирующий экран (например по verdict=pause).
  ipcMain.handle('proctor:set-blocking', (e, payload) => {
    const p = payload || {};
    if (p.active) {
      showBlocking(
        String(p.reason || 'renderer'),
        String(p.title || 'Тест приостановлен'),
        String(p.text || 'Вернитесь в рамку кадра и дождитесь разрешения.'),
      );
    } else {
      hideBlocking(p.reason ? String(p.reason) : null);
    }
    return state.blockingReason;
  });

  // Запрос выхода из оболочки (кнопка «завершить тест» в renderer).
  ipcMain.handle('proctor:exit', (e, reason) => {
    shutdown(typeof reason === 'string' ? reason : 'renderer_exit');
    return true;
  });
}

// ---------------------------------------------------------------------------
// Канал сайдкара -> renderer
// ---------------------------------------------------------------------------

function wireSidecar() {
  const channels = {
    [MsgType.HELLO]: 'proctor:hello',
    [MsgType.STATUS]: 'proctor:status',
    [MsgType.EVENT]: 'proctor:event',
    [MsgType.RISK]: 'proctor:risk',
    [MsgType.CALIBRATION]: 'proctor:calibration',
    [MsgType.VERDICT]: 'proctor:verdict',
    [MsgType.ERROR]: 'proctor:error',
  };

  link.onMessage((msg) => {
    const channel = channels[msg.type];
    if (channel) sendToRenderer(channel, msg);
    if (msg.type === MsgType.RISK) {
      state.lastRisk = { score: msg.score, level: msg.level, action: msg.action };
    }
    if (msg.type === MsgType.VERDICT) {
      state.lastVerdict = { action: msg.action, reason: msg.reason, score: msg.score };
    }
  });

  link.onType(MsgType.HELLO, () => {
    log('сайдкар сообщил возможности:', JSON.stringify(link.capabilities));
    broadcastShellStatus();
    // Если renderer не стартовал сессию сам — стартуем с параметров запуска,
    // иначе доказательная база не откроется и демо окажется пустым.
    setTimeout(() => {
      if (state.sessionStarted) return;
      const payload = {
        student_id: CLI.studentId,
        exam_id: CLI.examId,
        student_name: CLI.studentName,
      };
      state.sessionStarted = true;
      state.sessionAuto = true;
      state.sessionMeta = payload;
      link.sendSessionStart(payload);
      log('сессия открыта автоматически:', JSON.stringify(payload));
      broadcastShellStatus();
    }, 3000);
  });

  link.on('open', () => broadcastShellStatus());
  link.on('close', () => broadcastShellStatus());
  link.on('unavailable', () => broadcastShellStatus());
  link.on('socket-error', () => broadcastShellStatus());
}

// ---------------------------------------------------------------------------
// Завершение
// ---------------------------------------------------------------------------

function shutdown(reason) {
  if (state.quitting) return;
  state.quitting = true;
  log('завершение оболочки:', reason);

  try { link.sendSessionEnd(reason); } catch (err) { /* канала может не быть */ }

  if (state.statusTimer) {
    clearInterval(state.statusTimer);
    state.statusTimer = null;
  }
  lockdown.release();
  try { globalShortcut.unregisterAll(); } catch (err) { /* нечего снимать */ }
  try { clipboard.clear(); } catch (err) { /* нет буфера */ }
  killSidecar();

  // даём 250 мс на доставку session_end и выходим жёстко, минуя before-quit
  setTimeout(() => {
    link.stop();
    for (const w of BrowserWindow.getAllWindows()) {
      try { w.destroy(); } catch (err) { /* уже уничтожено */ }
    }
    app.exit(0);
  }, 250);
}

// ---------------------------------------------------------------------------
// Старт
// ---------------------------------------------------------------------------

if (!app.requestSingleInstanceLock()) {
  log('оболочка уже запущена — второй экземпляр закрывается');
  app.exit(0);
} else {
  app.on('second-instance', () => {
    const win = state.win;
    if (win && !win.isDestroyed()) {
      win.show();
      win.focus();
    }
  });

  // Снимаем системное меню: вместе с ним уходят штатные акселераторы
  // (Cmd+Q, Cmd+W, Cmd+M, Cmd+H, Ctrl+R и прочие).
  Menu.setApplicationMenu(null);

  app.on('window-all-closed', () => {
    if (!state.quitting) shutdown('window_all_closed');
  });

  app.on('before-quit', (e) => {
    if (state.quitting) return;
    e.preventDefault();
    shutdown('before_quit');
  });

  // Повторная регистрация хоткеев при возврате фокуса приложению.
  app.on('browser-window-focus', () => {
    if (state.quitting) return;
    lockdown.reregister();
  });

  app.on('browser-window-blur', () => {
    if (state.quitting || state.blockingReason) return;
    // Отдельно от win.on('blur'): ловим случай, когда фокус ушёл с любого окна
    // оболочки, включая оверлей. Троттлинг общий, дубля в журнале не будет.
    if (allowWindowState('window-blur')) {
      link.sendShellEvent(EventKind.WINDOW_BLUR, { reason: 'app_window_blur' });
    }
    const win = state.win;
    if (win && !win.isDestroyed() && !state.blockingReason) {
      win.show();
      win.focus();
    }
  });

  app.on('web-contents-created', (e, wc) => hardenWebContents(wc));

  app.whenReady().then(() => {
    // Разрешаем только камеру/микрофон (превью в renderer), всё прочее — запрет.
    try {
      const ses = session.defaultSession;
      ses.setPermissionRequestHandler((wc, permission, callback) => {
        callback(permission === 'media');
      });
      ses.setPermissionCheckHandler((wc, permission) => permission === 'media');
    } catch (err) {
      log('permission handler:', err && err.message);
    }

    registerIpc();
    wireSidecar();

    if (CLI.spawnSidecar) spawnSidecar();
    link.start();

    createWindow();

    // Блокировки окружения + честная карта возможностей
    const caps = lockdown.engage();
    log(`блокировки (${caps.platformLabel}): полных ${caps.summary.supported}, `
      + `частичных ${caps.summary.partial}, недоступных ${caps.summary.unsupported}`);

    // Админский выход: без него из kiosk-окна не выбраться.
    try {
      globalShortcut.register(ADMIN_EXIT_ACCELERATOR, () => shutdown('admin_exit'));
      log('аварийный выход прокторa:', ADMIN_EXIT_ACCELERATOR);
    } catch (err) {
      log('не удалось зарегистрировать аварийный выход:', err && err.message);
    }

    // Мониторы: стартовая проверка + подписка на изменения в рантайме.
    handleDisplays({ count: screen.getAllDisplays().length }, 'startup');
    screen.on('display-added', () => handleDisplays({ count: screen.getAllDisplays().length }, 'display-added'));
    screen.on('display-removed', () => handleDisplays({ count: screen.getAllDisplays().length }, 'display-removed'));
    screen.on('display-metrics-changed', () => broadcastShellStatus());

    state.statusTimer = setInterval(broadcastShellStatus, 1000);
    if (state.statusTimer.unref) state.statusTimer.unref();
  });
}

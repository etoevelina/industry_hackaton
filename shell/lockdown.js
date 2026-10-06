'use strict';
/**
 * Lockdown — слой блокировки рабочего окружения (п. 2.3 ТЗ).
 *
 * Что делает:
 *  1. регистрирует globalShortcut на системные комбинации (Alt+Tab, Super/Cmd+Space,
 *     CommandOrControl+Tab, CommandOrControl+Q/W/N/T, PrintScreen, Alt+F4, F11, F12,
 *     Command+M/H на macOS) — пойманная комбинация не доходит до ОС и порождает
 *     SHELL_EVENT SHORTCUT_BLOCKED;
 *  2. чистит буфер обмена раз в 1.5 с во время теста (clipboard.clear());
 *  3. раз в 2 с пересчитывает мониторы (страховка на случай, если события
 *     display-added/display-removed не придут — так бывает при переключении KVM);
 *  4. отдаёт честный capabilities(): по каждой блокировке — supported true/false/'partial'
 *     для ТЕКУЩЕЙ платформы плюс фактический результат регистрации.
 *
 * Про честность. Часть блокировок принципиально недостижима из user-space:
 *  - macOS: Cmd+Tab, Mission Control (Ctrl+Up / F3), Spotlight (Cmd+Space) и
 *    Force Quit (Cmd+Opt+Esc) держит WindowServer, приложение их не перехватывает;
 *  - Windows: Ctrl+Alt+Del и Ctrl+Shift+Esc обрабатывает winlogon/SAS, их нельзя
 *    перекрыть без драйвера; клавиша Win глушится только low-level hook'ом.
 * Компенсация в обоих случаях одна: окно alwaysOnTop('screen-saver') + принудительный
 * возврат фокуса + запись WINDOW_BLUR / FULLSCREEN_EXIT в доказательную базу.
 * Именно это capabilities() и показывает жюри как осознанное ограничение, а не дыру.
 *
 * Electron импортируется лениво (через try/catch), чтобы модуль можно было
 * синтаксически проверить и подключить под чистым node без electron.
 */

const PLATFORM = process.platform;

const PLATFORM_LABELS = Object.freeze({
  darwin: 'macOS',
  win32: 'Windows',
  linux: 'Linux',
});

/** Ленивый доступ к electron: в main-процессе есть, под чистым node — нет. */
function electronApi() {
  try {
    return require('electron');
  } catch (err) {
    return null;
  }
}

function platformKey() {
  if (PLATFORM === 'darwin' || PLATFORM === 'win32' || PLATFORM === 'linux') return PLATFORM;
  return 'linux';
}

function pick(map, plat) {
  if (!map) return undefined;
  return Object.prototype.hasOwnProperty.call(map, plat) ? map[plat] : map.default;
}

/**
 * Таблица блокировок. support: true (работает), 'partial' (перехватывается не всегда /
 * компенсируется другим механизмом), false (из user-space недостижимо).
 */
const LOCK_SPECS = Object.freeze([
  {
    key: 'window_switch',
    label: 'Переключение окон (Alt+Tab / Cmd+Tab)',
    kind: 'shortcut',
    accelerators: { darwin: ['Command+Tab'], win32: ['Alt+Tab'], linux: ['Alt+Tab'] },
    support: { darwin: false, win32: 'partial', linux: 'partial' },
    note: {
      darwin: 'Cmd+Tab зарезервирован WindowServer, из приложения не перехватывается. Компенсация: окно поверх всех + возврат фокуса + событие WINDOW_BLUR в отчёте.',
      win32: 'globalShortcut перехватывает Alt+Tab в большинстве сборок, но оболочка проводника может забрать комбинацию раньше. Факт ухода фокуса всё равно пишется как WINDOW_BLUR.',
      linux: 'Зависит от оконного менеджера: часть WM отдаёт Alt+Tab себе.',
    },
  },
  {
    key: 'os_launcher',
    label: 'Системный поиск / меню «Пуск» (Cmd+Space / Win)',
    kind: 'shortcut',
    accelerators: { darwin: ['Command+Space'], win32: ['Super'], linux: ['Super'] },
    support: { darwin: false, win32: false, linux: false },
    note: {
      darwin: 'Spotlight по Cmd+Space принадлежит системе; регистрация проходит не всегда и ОС имеет приоритет. Открытие Spotlight уводит фокус — это фиксируется как WINDOW_BLUR.',
      win32: 'Одиночная клавиша Win не является валидным акселератором Electron и глушится только low-level клавиатурным hook\'ом (вне user-space). Фиксируется через WINDOW_BLUR.',
      linux: 'Клавишу Super перехватывает WM.',
    },
  },
  {
    key: 'app_quit',
    label: 'Выход из приложения (Cmd+Q / Alt+F4)',
    kind: 'shortcut',
    accelerators: { darwin: ['Command+Q'], win32: ['Alt+F4', 'Control+Q'], linux: ['Alt+F4', 'Control+Q'] },
    support: { darwin: true, win32: true, linux: 'partial' },
    note: {
      darwin: 'Перехватывается и globalShortcut, и before-input-event в окне.',
      win32: 'Alt+F4 перехватывается; принудительное завершение через диспетчер задач остаётся возможным и видно по обрыву сессии.',
      linux: 'Зависит от WM.',
    },
  },
  {
    key: 'window_close',
    label: 'Закрытие окна (Cmd+W / Ctrl+W)',
    kind: 'shortcut',
    accelerators: { default: ['CommandOrControl+W'] },
    support: { darwin: true, win32: true, linux: true },
    note: { default: 'Окно без рамки, close-запрос перехватывается в main-процессе.' },
  },
  {
    key: 'new_window_tab',
    label: 'Новое окно / вкладка (Cmd+N, Cmd+T)',
    kind: 'shortcut',
    accelerators: { default: ['CommandOrControl+N', 'CommandOrControl+T'] },
    support: { darwin: true, win32: true, linux: true },
    note: { default: 'Дополнительно закрыт setWindowOpenHandler: deny — создать второе окно из renderer нельзя.' },
  },
  {
    key: 'tab_cycle',
    label: 'Перебор вкладок (Ctrl+Tab / Ctrl+Shift+Tab)',
    kind: 'shortcut',
    accelerators: { default: ['CommandOrControl+Tab', 'CommandOrControl+Shift+Tab'] },
    support: { darwin: 'partial', win32: true, linux: true },
    note: {
      darwin: 'На macOS Ctrl+Tab перехватывается, Cmd+Tab — нет (см. window_switch).',
      default: 'В оболочке одна вкладка, комбинация глушится на уровне окна.',
    },
  },
  {
    key: 'minimize_hide',
    label: 'Свернуть / скрыть приложение (Cmd+M, Cmd+H)',
    kind: 'shortcut',
    accelerators: { darwin: ['Command+M', 'Command+H'], win32: ['Super+D'], linux: [] },
    support: { darwin: true, win32: 'partial', linux: false },
    note: {
      darwin: 'Перехватывается; плюс kiosk-окно не сворачивается по minimize-запросу.',
      win32: 'Win+D перехватывается не всегда (комбинация с клавишей Win), уход фокуса пишется как WINDOW_BLUR.',
      linux: 'Отдано WM.',
    },
  },
  {
    key: 'screenshot_key',
    label: 'Клавиша снимка экрана (PrintScreen / Cmd+Shift+3/4/5)',
    kind: 'shortcut',
    accelerators: {
      darwin: ['Command+Shift+3', 'Command+Shift+4', 'Command+Shift+5'],
      win32: ['PrintScreen', 'Alt+PrintScreen', 'Super+PrintScreen'],
      linux: ['PrintScreen'],
    },
    support: { darwin: 'partial', win32: 'partial', linux: 'partial' },
    note: {
      darwin: 'Комбинации скриншота регистрируются, но системный сервис снимков может отработать раньше. Реальная защита — setContentProtection: содержимое окна выходит чёрным.',
      win32: 'PrintScreen перехватывается; сторонние скриншотеры со своими хоткеями — нет. Реальная защита — setContentProtection.',
      linux: 'Зависит от окружения.',
    },
  },
  {
    key: 'fullscreen_toggle',
    label: 'Выход из полного экрана (F11)',
    kind: 'shortcut',
    accelerators: { default: ['F11'] },
    support: { darwin: true, win32: true, linux: true },
    note: { default: 'Плюс обработчик leave-full-screen: окно немедленно возвращается в fullscreen, событие FULLSCREEN_EXIT уходит в отчёт.' },
  },
  {
    key: 'devtools',
    label: 'Инструменты разработчика (F12, Ctrl+Shift+I/J/C)',
    kind: 'shortcut',
    accelerators: { default: ['F12'] },
    support: { darwin: true, win32: true, linux: true },
    note: { default: 'webPreferences.devTools=false, before-input-event глушит F12 и Ctrl+Shift+I/J/C, попытка пишется как DEVTOOLS_ATTEMPT.' },
  },
  {
    key: 'clipboard_clear',
    label: 'Очистка буфера обмена (раз в 1.5 с)',
    kind: 'runtime',
    accelerators: { default: [] },
    support: { darwin: true, win32: true, linux: true },
    note: { default: 'clipboard.clear() по таймеру во время теста; непустой буфер перед очисткой фиксируется как CLIPBOARD_PASTE.' },
  },
  {
    key: 'content_protection',
    label: 'Защита содержимого от скриншотов и записи экрана',
    kind: 'runtime',
    accelerators: { default: [] },
    support: { darwin: true, win32: true, linux: false },
    note: {
      darwin: 'setContentProtection(true): окно исключается из захвата, на снимке и в записи — чёрный прямоугольник.',
      win32: 'WDA_EXCLUDEFROMCAPTURE, требуется Windows 10 2004+; на более старых сборках окно просто не исключается.',
      linux: 'Не поддерживается платформой.',
    },
  },
  {
    key: 'display_watch',
    label: 'Контроль числа мониторов',
    kind: 'runtime',
    accelerators: { default: [] },
    support: { darwin: true, win32: true, linux: true },
    note: { default: 'display-added/display-removed + опрос раз в 2 с; второй экран => MULTIPLE_DISPLAYS и блокирующий экран.' },
  },
  {
    key: 'mission_control',
    label: 'Mission Control / обзор задач (Ctrl+Up, F3, Win+Tab)',
    kind: 'shortcut',
    accelerators: { darwin: [], win32: ['Super+Tab'], linux: [] },
    support: { darwin: false, win32: false, linux: false },
    note: {
      darwin: 'Mission Control и Spaces обслуживает WindowServer, перехват из приложения невозможен. Фиксируется как WINDOW_BLUR.',
      win32: 'Win+Tab (Task View) не блокируется без low-level hook. Фиксируется как WINDOW_BLUR.',
      linux: 'Отдано WM.',
    },
  },
  {
    key: 'task_manager',
    label: 'Диспетчер задач / Force Quit (Ctrl+Shift+Esc, Cmd+Opt+Esc, Ctrl+Alt+Del)',
    kind: 'shortcut',
    accelerators: { default: [] },
    support: { darwin: false, win32: false, linux: false },
    note: {
      darwin: 'Cmd+Opt+Esc обрабатывает система, перехватить нельзя.',
      win32: 'Ctrl+Alt+Del обрабатывает winlogon через SAS, Ctrl+Shift+Esc — shell. Без драйвера или политики домена не блокируется.',
      linux: 'Недостижимо из user-space.',
    },
  },
]);

/**
 * Троттлинг по ключу: одна и та же комбинация не чаще, чем раз в windowMs.
 * Нужен, чтобы зажатый Ctrl+C не залил журнал событий.
 */
function comboThrottle(windowMs = 400) {
  const seen = new Map();
  return function allow(key) {
    const now = Date.now();
    const prev = seen.get(key);
    if (prev !== undefined && now - prev < windowMs) return false;
    seen.set(key, now);
    // чистим старьё, чтобы Map не рос бесконечно за долгую сессию
    if (seen.size > 256) {
      for (const [k, t] of seen) {
        if (now - t > windowMs * 10) seen.delete(k);
      }
    }
    return true;
  };
}

class Lockdown {
  /**
   * @param {{
   *   onShortcutBlocked?: (combo:string, meta:object)=>void,
   *   onClipboardCleared?: (info:{length:number})=>void,
   *   onDisplays?: (info:{count:number, displays:Array, changed:boolean, reassert:boolean})=>void,
   *   logger?: Function,
   *   clipboardIntervalMs?: number,
   *   displayIntervalMs?: number,
   *   throttleMs?: number
   * }} [opts]
   */
  constructor(opts = {}) {
    this.platform = platformKey();
    this.platformLabel = PLATFORM_LABELS[this.platform] || this.platform;

    this._onShortcutBlocked = opts.onShortcutBlocked || (() => {});
    this._onClipboardCleared = opts.onClipboardCleared || (() => {});
    this._onDisplays = opts.onDisplays || (() => {});
    this._log = typeof opts.logger === 'function' ? opts.logger : () => {};

    this.clipboardIntervalMs = opts.clipboardIntervalMs || 1500;
    this.displayIntervalMs = opts.displayIntervalMs || 2000;
    this._allow = comboThrottle(opts.throttleMs || 400);

    this.engaged = false;
    this._electronPresent = false;  // была ли реальная попытка регистрации хоткеев
    this._clipboardTimer = null;
    this._displayTimer = null;
    this._registered = new Map();   // accelerator -> boolean
    this._triggered = new Map();    // lock key -> счётчик срабатываний
    this.clipboardClears = 0;
    this.displayCount = 0;
    this._lastDisplayCount = -1;
    this._lastDisplayReassert = 0;
  }

  // ------------------------------------------------------------------- включение

  /** Идемпотентно: можно звать повторно на browser-window-focus. */
  engage() {
    const api = electronApi();
    if (!api) {
      this._log('electron недоступен — Lockdown работает в режиме «только отчёт о возможностях»');
      this.engaged = true;
      return this.capabilities();
    }
    this._electronPresent = true;
    this._registerShortcuts(api);
    this._startClipboardTimer(api);
    this._startDisplayTimer(api);
    this.engaged = true;
    return this.capabilities();
  }

  release() {
    const api = electronApi();
    if (api && api.globalShortcut) {
      try {
        api.globalShortcut.unregisterAll();
      } catch (err) {
        this._log(`unregisterAll: ${err && err.message}`);
      }
    }
    this._registered.clear();
    if (this._clipboardTimer) {
      clearInterval(this._clipboardTimer);
      this._clipboardTimer = null;
    }
    if (this._displayTimer) {
      clearInterval(this._displayTimer);
      this._displayTimer = null;
    }
    this.engaged = false;
  }

  /** Повторная регистрация хоткеев — после возврата фокуса приложению. */
  reregister() {
    const api = electronApi();
    if (!api || !api.globalShortcut) return;
    this._registerShortcuts(api);
  }

  // ------------------------------------------------------------------ хоткеи

  _acceleratorsFor(spec) {
    const list = pick(spec.accelerators, this.platform);
    return Array.isArray(list) ? list : [];
  }

  _registerShortcuts(api) {
    const gs = api.globalShortcut;
    if (!gs) return;
    for (const spec of LOCK_SPECS) {
      if (spec.kind !== 'shortcut') continue;
      for (const acc of this._acceleratorsFor(spec)) {
        if (this._registered.get(acc) === true) {
          // уже держим — проверим, что регистрация жива
          let alive = false;
          try { alive = gs.isRegistered(acc); } catch (err) { alive = false; }
          if (alive) continue;
        }
        let ok = false;
        try {
          ok = Boolean(gs.register(acc, () => this._onHotkey(spec, acc)));
        } catch (err) {
          // невалидный для платформы акселератор (например одиночный Super) — это нормально
          ok = false;
          this._log(`акселератор не принят системой: ${acc} (${err && err.message})`);
        }
        this._registered.set(acc, ok);
      }
    }
  }

  _onHotkey(spec, accelerator) {
    this._triggered.set(spec.key, (this._triggered.get(spec.key) || 0) + 1);
    if (!this._allow(`gs:${accelerator}`)) return;
    this._onShortcutBlocked(accelerator, {
      lock: spec.key,
      label: spec.label,
      source: 'globalShortcut',
      platform: this.platform,
    });
  }

  // --------------------------------------------------------------- буфер обмена

  _startClipboardTimer(api) {
    if (this._clipboardTimer || !api.clipboard) return;
    this._clipboardTimer = setInterval(() => {
      try {
        const text = api.clipboard.readText() || '';
        const hadContent = text.length > 0;
        api.clipboard.clear();
        if (hadContent) {
          this.clipboardClears += 1;
          // сам текст наружу не отдаём — только длину (приватность)
          this._onClipboardCleared({ length: text.length });
        }
      } catch (err) {
        this._log(`clipboard: ${err && err.message}`);
      }
    }, this.clipboardIntervalMs);
    if (this._clipboardTimer.unref) this._clipboardTimer.unref();
  }

  // -------------------------------------------------------------------- дисплеи

  _startDisplayTimer(api) {
    if (this._displayTimer || !api.screen) return;
    this.pollDisplays();
    this._displayTimer = setInterval(() => this.pollDisplays(), this.displayIntervalMs);
    if (this._displayTimer.unref) this._displayTimer.unref();
  }

  /**
   * Опрос мониторов. Колбэк зовётся при изменении числа экранов, а также
   * раз в 15 с, пока экранов больше одного (перенапоминание для event-engine).
   */
  pollDisplays() {
    const api = electronApi();
    if (!api || !api.screen) return null;
    let displays = [];
    try {
      displays = api.screen.getAllDisplays() || [];
    } catch (err) {
      this._log(`screen.getAllDisplays: ${err && err.message}`);
      return null;
    }
    this.displayCount = displays.length;
    const brief = displays.map((d) => ({
      id: d.id,
      internal: Boolean(d.internal),
      bounds: d.bounds,
      scale: d.scaleFactor,
    }));
    const changed = this.displayCount !== this._lastDisplayCount;
    const now = Date.now();
    const reassert = !changed && this.displayCount > 1 && now - this._lastDisplayReassert > 15000;
    if (changed || reassert) {
      this._lastDisplayCount = this.displayCount;
      this._lastDisplayReassert = now;
      this._onDisplays({ count: this.displayCount, displays: brief, changed, reassert });
    }
    return { count: this.displayCount, displays: brief };
  }

  // --------------------------------------------------------------- возможности

  /**
   * Честная карта возможностей для текущей платформы.
   * supported: true | false | 'partial' — declared-значение, понижённое до false,
   * если ни один акселератор блокировки реально не зарегистрировался.
   */
  capabilities() {
    const locks = {};
    let supported = 0;
    let partial = 0;
    let unsupported = 0;

    for (const spec of LOCK_SPECS) {
      const accs = this._acceleratorsFor(spec);
      const declared = pick(spec.support, this.platform);
      const note = pick(spec.note, this.platform) || '';
      let registered = null;   // null = регистрация не выполнялась (нет electron)
      if (spec.kind === 'shortcut' && this._electronPresent) {
        registered = accs.length > 0 && accs.some((a) => this._registered.get(a) === true);
      }
      let effective = declared === undefined ? false : declared;
      // Понижаем заявленное до false только если регистрация реально
      // выполнялась (есть electron) и ни один акселератор не взялся.
      if (spec.kind === 'shortcut' && this._electronPresent && this.engaged
          && accs.length > 0 && registered === false) {
        effective = false;
      }
      if (spec.kind === 'shortcut' && accs.length === 0) {
        effective = false;
      }
      locks[spec.key] = {
        label: spec.label,
        kind: spec.kind,
        accelerators: accs,
        declared: declared === undefined ? false : declared,
        registered,
        supported: effective,
        triggered: this._triggered.get(spec.key) || 0,
        note,
      };
      if (effective === true) supported += 1;
      else if (effective === 'partial') partial += 1;
      else unsupported += 1;
    }

    return {
      platform: this.platform,
      platformLabel: this.platformLabel,
      engaged: this.engaged,
      clipboardClears: this.clipboardClears,
      displayCount: this.displayCount,
      locks,
      summary: { supported, partial, unsupported, total: LOCK_SPECS.length },
    };
  }

  /**
   * Матрица «блокировка x платформа» для показа жюри: что умеем на macOS,
   * что на Windows. Не зависит от текущей платформы.
   */
  static matrix() {
    const rows = LOCK_SPECS.map((spec) => ({
      key: spec.key,
      label: spec.label,
      kind: spec.kind,
      darwin: { supported: pick(spec.support, 'darwin') || false, note: pick(spec.note, 'darwin') || '' },
      win32: { supported: pick(spec.support, 'win32') || false, note: pick(spec.note, 'win32') || '' },
    }));
    return { rows };
  }
}

module.exports = { Lockdown, LOCK_SPECS, comboThrottle, PLATFORM_LABELS };

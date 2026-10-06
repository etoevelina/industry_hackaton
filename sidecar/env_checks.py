"""
Проверки окружения: защита от remote-помощи и подмены видеопотока.

Зачем это нужно. Вся CV-часть смотрит в камеру и доверяет тому, что видит.
Два класса обхода CV не ловится принципиально:

  1. Подмена потока. Студент ставит OBS Virtual Camera и отдаёт вместо живого
     видео заранее записанный ролик, где он честно смотрит в экран.
  2. Remote-помощь. За машиной сидит кто-то ещё — через AnyDesk/TeamViewer/VNC
     или просто в соседнем окне мессенджера, куда улетает скриншот задания.

Оба случая видны не в кадре, а в состоянии операционной системы: список
устройств захвата, список процессов, число экранов, признаки гипервизора.
Этот модуль — второй, независимый от камеры источник доказательств.

Как устроено
------------
`run_all_checks(config)` вызывает шесть независимых проверок и возвращает
`list[EnvFinding]` (`kind` / `detail` / `severity`). Каждая проверка:

  * самостоятельна — её можно вызвать отдельно, она не зависит от остальных;
  * никогда не бросает исключений — внутренняя ошибка даёт пустой список;
  * все внешние команды запускаются с таймаутом и `capture_output`, результат
    кешируется на несколько секунд (`_TTL_*`), чтобы опрос раз в 20 с
    (`env_interval`) не превращался в постоянный запуск `system_profiler`.

Главный риск такого модуля — ложные срабатывания: на демо-дне сработавшая
проверка блокирует собственный показ. Поэтому:

  * совпадения ищутся регулярками с якорями, а не подстрокой: системные
    демоны macOS `remoted`, `remotepairingd`, `cameracaptured`,
    `continuitycaptureagent`, `screentimeagent` не должны попадать в находки
    по словам «remote», «capture», «screen»;
  * процессы из системных путей (`/System`, `/usr/libexec`, ...) отбрасываются,
    кроме явно разрешённых (`screensharingd`, `screencapture`, `ARDAgent`,
    `QuickTime Player` — они лежат в системных каталогах, но нас интересуют);
  * зеркалированный второй экран (проектор на демо) не считается вторым
    монитором — считаются только логически независимые экраны;
  * сила сигнала выражается через `detail["conf"]`: вклад события в risk-score
    равен `RISK_WEIGHTS[kind] * conf`, поэтому «OBS просто установлен» даёт
    куда меньше, чем «OBS Virtual Camera отдаёт кадры прямо сейчас».

Каждая находка несёт в `detail` конкретику: имя процесса, pid, путь, время
запуска, имя устройства, шаблон, по которому произошло совпадение. В отчёте
должно быть видно, ЧТО именно нашли, а не просто «обнаружено ПО».

Платформы: macOS (основная), Windows (весь код под `sys.platform == "win32"`),
Linux (dmi/sysfs). Отсутствие `psutil` выключает процессные проверки, но не
ломает остальные. Сетевых обращений наружу нет.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from protocol import EventKind, Severity  # noqa: E402

__all__ = [
    "EnvFinding",
    "run_all_checks",
    "check_virtual_camera",
    "check_remote_access",
    "check_virtual_machine",
    "check_screen_recording",
    "check_blacklisted_processes",
    "check_displays",
    "available",
    "describe_checks",
]

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

#: Таймауты внешних команд. Всё быстрое; system_profiler на arm64 ~0.3–0.8 с.
_T_FAST = 4.0
_T_PROFILER = 12.0

#: Время жизни кеша. Камеры и гипервизор меняются редко, экраны — часто
#: (воткнули монитор посреди экзамена, это надо увидеть на следующем цикле).
_TTL_CAMERAS = 30.0
_TTL_DISPLAYS = 8.0
_TTL_PROCESSES = 3.0
_TTL_STATIC = 300.0
_TTL_LISTENERS = 10.0


# ===========================================================================
# Результат проверки
# ===========================================================================
@dataclass
class EnvFinding:
    """Находка проверки окружения.

    Контракт: `{kind: EventKind, detail: dict, severity: Severity}`.
    `detail["conf"]` читает Event Engine — это множитель веса события.
    """
    kind: EventKind
    detail: dict[str, Any] = field(default_factory=dict)
    severity: Severity = Severity.MEDIUM

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["severity"] = self.severity.value
        return d

    def __str__(self) -> str:  # для логов и ручного прогона
        return (f"{self.kind.value} [{self.severity.value}] "
                f"conf={self.detail.get('conf')} {self.detail.get('summary', '')}")


# ===========================================================================
# Чтение конфига
# ===========================================================================
def _cfg(config: Any, section: str, key: str, default: Any) -> Any:
    """Прочитать параметр по схеме config.py: секция -> префикс -> плоский ключ.

    `config[section][key]` -> `config[f"{section}_{key}"]` -> `config[key]`.
    Любой мусор вместо конфига трактуется как «настроек нет».
    """
    if not isinstance(config, dict):
        return default
    node = config.get(section)
    if isinstance(node, dict) and key in node:
        return node[key]
    flat = f"{section}_{key}"
    if flat in config:
        return config[flat]
    if key in config:
        return config[key]
    return default


def _cfg_bool(config: Any, section: str, key: str, default: bool) -> bool:
    value = _cfg(config, section, key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "да")
    try:
        return bool(value)
    except Exception:
        return default


def _cfg_float(config: Any, section: str, key: str, default: float) -> float:
    try:
        return float(_cfg(config, section, key, default))
    except (TypeError, ValueError):
        return default


def _cfg_list(config: Any, section: str, key: str) -> list[str]:
    value = _cfg(config, section, key, None)
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


# ===========================================================================
# Кеш с TTL + запуск внешних команд
# ===========================================================================
class _TTLCache:
    """Потокобезопасный кеш «значение + время». Нужен, потому что проверки
    могут вызываться и из env-тикера, и вручную, и из нескольких потоков."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float) -> Any:
        with self._lock:
            item = self._data.get(key)
        if item is None:
            return None
        ts, value = item
        return value if (time.time() - ts) <= ttl else None

    def put(self, key: str, value: Any) -> Any:
        with self._lock:
            self._data[key] = (time.time(), value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def with_prefix(self, prefix: str) -> dict[str, Any]:
        with self._lock:
            return {k[len(prefix):]: v for k, v in self._data.items()
                    if k.startswith(prefix)}


_CACHE = _TTLCache()


def reset_cache() -> None:
    """Сбросить кеши. Нужно только в тестах и при смене окружения."""
    _CACHE.clear()


def _run(args: Sequence[str], timeout: float = _T_FAST) -> str | None:
    """Выполнить команду и вернуть stdout. Никогда не бросает и не блокирует.

    `LC_ALL=C` — чтобы парсинг не зависел от локали. stdin закрыт, иначе
    интерактивная утилита повиснет до таймаута.
    """
    if not args:
        return None
    exe = shutil.which(args[0])
    if exe is None:
        return None
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        proc = subprocess.run(
            [exe, *args[1:]],
            capture_output=True, text=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL, env=env, errors="replace",
        )
    except Exception:
        return None
    out = proc.stdout or ""
    if proc.returncode != 0 and not out.strip():
        return None
    return out


def _run_cached(key: str, args: Sequence[str], ttl: float,
                timeout: float = _T_FAST) -> str | None:
    cached = _CACHE.get(key, ttl)
    if cached is not None:
        return None if cached == "" else cached
    out = _run(args, timeout=timeout)
    _CACHE.put(key, out if out is not None else "")
    return out


def _system_profiler(data_type: str, ttl: float) -> Any:
    """`system_profiler -json <type>` -> разобранный JSON или None."""
    if not IS_MAC:
        return None
    key = f"sp:{data_type}"
    cached = _CACHE.get(key, ttl)
    if cached is not None:
        return None if cached == "__none__" else cached
    out = _run(["system_profiler", "-json", data_type], timeout=_T_PROFILER)
    data: Any = None
    if out:
        import json
        try:
            parsed = json.loads(out)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            data = parsed.get(data_type)
    _CACHE.put(key, data if data is not None else "__none__")
    return data


def _sysctl(name: str) -> str | None:
    out = _run_cached(f"sysctl:{name}", ["sysctl", "-n", name], _TTL_STATIC)
    return out.strip() if out else None


# ===========================================================================
# Снимок процессов
# ===========================================================================
@dataclass(frozen=True)
class ProcInfo:
    """Минимум о процессе, нужный проверкам (и достаточный для отчёта)."""
    pid: int
    name: str
    exe: str
    cmdline: str
    username: str
    create_time: float

    @property
    def name_l(self) -> str:
        return _norm_name(self.name)

    @property
    def exe_base_l(self) -> str:
        return _norm_name(os.path.basename(self.exe)) if self.exe else ""

    @property
    def exe_l(self) -> str:
        return self.exe.lower()

    @property
    def cmdline_l(self) -> str:
        return self.cmdline.lower()

    @property
    def app_bundle(self) -> str:
        """Имя .app-бандла из пути (macOS): `/Applications/OBS.app/...` -> `obs`."""
        m = re.search(r"/([^/]+)\.app/", self.exe)
        return _norm_name(m.group(1)) if m else ""

    def as_detail(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "name": self.name,
            "exe": self.exe or None,
            "user": self.username or None,
            "started": _fmt_ts(self.create_time),
            "uptime_sec": round(max(time.time() - self.create_time, 0.0), 1)
            if self.create_time else None,
        }


def _norm_name(value: str) -> str:
    """Нормализовать имя процесса: регистр, .exe/.app, служебные хвосты."""
    name = (value or "").strip().lower()
    for suffix in (".exe", ".app", ".bin"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


def _fmt_ts(ts: float) -> str | None:
    if not ts:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return None


def _psutil() -> Any:
    """Ленивый импорт psutil. Нет пакета -> None, процессные проверки молчат."""
    cached = _CACHE.get("psutil", _TTL_STATIC)
    if cached is not None:
        return None if cached == "__none__" else cached
    try:
        import psutil  # type: ignore
    except Exception:
        _CACHE.put("psutil", "__none__")
        return None
    _CACHE.put("psutil", psutil)
    return psutil


def _processes() -> list[ProcInfo]:
    """Снимок процессов. Кешируется на `_TTL_PROCESSES`, чтобы шесть проверок
    в одном цикле не сканировали таблицу процессов шесть раз."""
    cached = _CACHE.get("procs", _TTL_PROCESSES)
    if cached is not None:
        return list(cached)

    psutil = _psutil()
    result: list[ProcInfo] = []
    if psutil is None:
        return _CACHE.put("procs", result)

    attrs = ["pid", "name", "exe", "cmdline", "username", "create_time"]
    try:
        iterator = psutil.process_iter(attrs=attrs, ad_value=None)
    except Exception:
        return _CACHE.put("procs", result)

    self_pid = os.getpid()
    for proc in iterator:
        try:
            info = proc.info
        except Exception:
            continue
        pid = int(info.get("pid") or 0)
        if pid in (0, self_pid):
            continue
        cmd = info.get("cmdline") or []
        if not isinstance(cmd, (list, tuple)):
            cmd = [str(cmd)]
        result.append(ProcInfo(
            pid=pid,
            name=str(info.get("name") or ""),
            exe=str(info.get("exe") or ""),
            cmdline=" ".join(str(c) for c in cmd),
            username=str(info.get("username") or ""),
            create_time=float(info.get("create_time") or 0.0),
        ))
    return _CACHE.put("procs", result)


# ---------------------------------------------------------------------------
# Отсев системного шума
# ---------------------------------------------------------------------------
#: Пути, в которых лежат только штатные компоненты ОС. Процессы оттуда
#: не попадают в находки, если их имя не разрешено явно (`allow`).
_SYSTEM_PREFIXES_MAC = ("/system/", "/usr/libexec/", "/usr/sbin/", "/usr/bin/",
                        "/sbin/", "/bin/", "/library/apple/")
_SYSTEM_PREFIXES_WIN = ("c:\\windows\\",)
_SYSTEM_PREFIXES_LINUX = ("/usr/lib/", "/usr/libexec/", "/lib/", "/sbin/", "/usr/sbin/")

#: Демоны macOS, чьи имена пересекаются с нашими словарями по подстроке.
#: Держим отдельным списком — это страховка на случай, если путь не прочитался.
_SYSTEM_NAMES = frozenset({
    "remoted", "remotepairingd", "remotemanagementd", "mediaremoted",
    "mediaremoteagent", "cameracaptured", "continuitycaptureagent",
    "screentimeagent", "screentimewidgetextension", "screensaverengine",
    "screenreaderuiserver", "universalaccessd", "avconferenced",
    "wirelessradiomanagerd", "replayd", "controlcenter", "sharingd",
})


def _is_system_noise(proc: ProcInfo, allow: frozenset[str]) -> bool:
    """Процесс — штатный компонент ОС, а не то, что мы ищем?"""
    name = proc.name_l
    if name in allow or proc.app_bundle in allow:
        return False
    if name in _SYSTEM_NAMES:
        return True
    exe = proc.exe_l
    if not exe:
        return False
    if IS_WIN:
        return exe.startswith(_SYSTEM_PREFIXES_WIN)
    if IS_MAC:
        return exe.startswith(_SYSTEM_PREFIXES_MAC)
    if IS_LINUX:
        return exe.startswith(_SYSTEM_PREFIXES_LINUX)
    return False


# ===========================================================================
# Словари сигнатур
# ===========================================================================
#: Каталог: (человекочитаемое имя, регулярка по имени процесса/бандла).
#: Регулярки с якорями — см. предупреждение про `remoted` в докстринге модуля.
Catalog = tuple[tuple[str, str], ...]


def _compile(catalog: Catalog) -> tuple[tuple[str, re.Pattern[str]], ...]:
    out: list[tuple[str, re.Pattern[str]]] = []
    for label, pattern in catalog:
        try:
            out.append((label, re.compile(pattern, re.IGNORECASE)))
        except re.error:  # битая регулярка не должна ломать весь модуль
            continue
    return tuple(out)


#: ПО удалённого доступа. Именно оно даёт «второго пилота» за машиной.
REMOTE_ACCESS_CATALOG: Catalog = (
    ("AnyDesk", r"^anydesk"),
    ("TeamViewer", r"teamviewer|^tv_w32$|^tv_x64$"),
    ("RustDesk", r"^rustdesk"),
    ("Parsec", r"^parsec"),
    ("VNC", r"(?:real|tight|tiger|ultra|turbo|x11)vnc|^vnc(server|viewer|agent|connect)?$"
            r"|^vino-server$|^vncserver|^vncviewer"),
    ("Chrome Remote Desktop", r"chrome.?remote.?desktop|remoting_(me2me_)?host|chromoting"),
    ("Ammyy Admin", r"^ammyy|^aa_v\d"),
    ("Supremo", r"^supremo"),
    ("Splashtop", r"splashtop|^srserver$|^srmanager|^strwinclt"),
    ("ScreenConnect / ConnectWise Control", r"screenconnect|connectwise(control)?"),
    ("DWService", r"^dwservice|^dwagent"),
    ("Radmin", r"^radmin|^r_server$|^rserver3$"),
    ("LogMeIn", r"logmein|^lmi_?(guardian|rescue)?$"),
    ("GoToAssist / GoToMyPC", r"gotoassist|gotomypc|^g2m(comm|launcher)"),
    ("Zoho Assist", r"zohoassist|^za_?service|^zaservice"),
    ("Remote Utilities", r"^rutserv|^rfusclient"),
    ("NoMachine", r"nomachine|^nxserver|^nxnode|^nxd$"),
    ("AeroAdmin", r"^aeroadmin"),
    ("GetScreen", r"^getscreen"),
    ("HopToDesk", r"^hoptodesk"),
    ("Iperius Remote", r"^iperius"),
    ("LiteManager", r"litemanager|^romserver|^romfusclient"),
    ("Mikogo", r"^mikogo"),
    ("Jump Desktop", r"^jump ?desktop|^jumpdesktop"),
    # screensharingd / ARDAgent намеренно не здесь: их разбирает
    # `_mac_screen_sharing()` — он отличает активную сессию от включённой службы
    ("Apple Remote Desktop (админ-консоль)", r"^remote ?desktop$"),
    ("Microsoft Remote Desktop", r"^microsoft remote desktop$|^mstsc$|^msrdc$"),
    ("Ngrok / туннель на локальный порт", r"^ngrok$|^cloudflared$|^localtunnel$"),
)

#: ПО, которое *создаёт* виртуальную камеру. Отдельно от имён устройств:
#: процесс называется «OBS», а устройство — «OBS Virtual Camera», по шаблону
#: устройства процесс не найти.
VIRTUAL_CAMERA_HOST_CATALOG: Catalog = (
    ("OBS Studio (источник виртуальной камеры)", r"^obs(\d{0,2})?$|^obs[ _-]?studio$"),
    ("Streamlabs Desktop", r"streamlabs"),
    ("ManyCam", r"many[ _-]?cam"),
    ("Snap Camera", r"snap[ _-]?camera"),
    ("XSplit VCam", r"^xsplit"),
    ("DroidCam", r"droid[ _-]?cam"),
    ("EpocCam", r"epoc[ _-]?cam"),
    ("Iriun Webcam", r"^iriun"),
    ("Reincubate Camo", r"^camo$|^camo studio$|^reincubate"),
    ("SplitCam", r"split[ _-]?cam"),
    ("Webcamoid", r"^webcamoid$"),
    ("NVIDIA Broadcast", r"nvidia broadcast"),
    ("mmhmm", r"^mmhmm"),
    ("DeepFaceLive / Avatarify", r"deepface(live|lab)|^avatarify"),
)

#: Виртуальные камеры: имена устройств захвата и плагинов.
VIRTUAL_CAMERA_CATALOG: Catalog = (
    ("OBS Virtual Camera", r"obs[ _-]?virtual|obs[ _-]?cam|obs-mac-virtualcam|obs[ _-]?vcam"),
    ("ManyCam", r"many[ _-]?cam"),
    ("Snap Camera", r"snap[ _-]?camera"),
    ("XSplit VCam", r"xsplit"),
    ("DroidCam", r"droid[ _-]?cam"),
    ("EpocCam", r"epoc[ _-]?cam"),
    ("Iriun Webcam", r"iriun"),
    ("Reincubate Camo", r"reincubate|\bcamo\b"),
    ("SplitCam", r"split[ _-]?cam"),
    ("e2eSoft VCam", r"e2esoft|^vcam\b|\bvcam$"),
    ("Webcamoid", r"webcamoid|^akvcam"),
    ("v4l2loopback", r"v4l2[ _-]?loopback|dummy video device"),
    ("NDI Virtual Input", r"\bndi\b.*(cam|input|virtual)|newtek ndi"),
    ("NVIDIA Broadcast", r"nvidia broadcast"),
    ("Streamlabs Virtual Camera", r"streamlabs"),
    ("PRISM Live / Lens", r"prism (live|lens)"),
    ("mmhmm", r"^mmhmm"),
    ("Restream", r"restream"),
    ("Avatarify / DeepFaceLive", r"avatarify|deepface(live|lab)"),
    ("Виртуальная камера (общий признак)",
     r"virtual (web)?cam(era)?|fake (web)?cam(era)?|\bvirtualcam\b"),
)

#: ПО записи/трансляции экрана. Сильные сигналы — те, что работают только
#: во время записи (`CptHost` у Zoom, `screencapture` у macOS).
SCREEN_RECORDING_STRONG: Catalog = (
    ("OBS Studio", r"^obs(\d{0,2})?$|^obs[ _-]?studio$|^obs-browser"),
    ("Streamlabs Desktop", r"streamlabs"),
    ("ScreenFlow", r"^screenflow"),
    ("Camtasia", r"camtasia"),
    ("Bandicam", r"^bandicam$|^bdcam$"),
    ("Fraps", r"^fraps$"),
    ("Mirillis Action!", r"^mirillis|^action!?$"),
    ("Loom", r"^loom$|^loom desktop$"),
    ("ShareX", r"^sharex$"),
    ("Snagit", r"^snagit"),
    ("CleanShot X", r"^cleanshot"),
    ("Kap", r"^kap$"),
    ("Screenity / Screencastify (десктоп)", r"^screenity$|^screencastify$"),
    ("Zoom: демонстрация экрана активна", r"^cpthost$|^aomhost(64)?$"),
    ("Снимок экрана macOS выполняется", r"^screencapture$"),
    ("ScreenRec / ScreenPal", r"^screenrec$|^screenpal$|^screencast-o-matic$"),
    ("Movavi Screen Recorder", r"^movavi"),
    ("XRecorder / Apowersoft", r"^apowersoft|^apowerrec"),
    ("flameshot / SimpleScreenRecorder", r"^flameshot$|^simplescreenrecorder$|^kazam$"),
)

#: Слабые сигналы: приложение умеет писать экран, но факт записи не подтверждён.
SCREEN_RECORDING_WEAK: Catalog = (
    ("QuickTime Player", r"^quicktime ?player$"),
    ("Zoom", r"^zoom(\.us)?$|^zoom ?meetings?$"),
    ("Снимок экрана (macOS)", r"^screenshot$"),
    ("VLC", r"^vlc$"),
    ("ffmpeg", r"^ffmpeg$"),
)

#: Аргументы командной строки, которые превращают слабый сигнал в сильный:
#: это уже не «VLC запущен», а «VLC пишет экран».
_CAPTURE_CMDLINE_MARKERS: tuple[tuple[str, str], ...] = (
    ("avfoundation", "захват экрана через AVFoundation"),
    ("gdigrab", "захват экрана через gdigrab"),
    ("x11grab", "захват экрана через x11grab"),
    ("ddagrab", "захват экрана через Desktop Duplication"),
    ("screen-capture-recorder", "захват экрана через screen-capture-recorder"),
    ("screen://", "захват экрана (VLC screen://)"),
    ("--record-screen", "запись экрана по ключу командной строки"),
    ("desktop=", "захват рабочего стола"),
)

#: Мессенджеры: канал, по которому уходит задание и приходит подсказка.
MESSENGER_CATALOG: Catalog = (
    ("Telegram", r"^telegram"),
    ("Discord", r"^discord"),
    ("WhatsApp", r"^whatsapp"),
    ("Slack", r"^slack$|^slack helper"),
    ("Skype", r"^skype"),
    ("Viber", r"^viber"),
    ("Signal", r"^signal$|^signal desktop$"),
    ("Microsoft Teams", r"^(ms|microsoft )?teams$|^teams$|^msteams$"),
    ("WeChat", r"^wechat"),
    ("Element / Matrix", r"^element$|^element desktop$|^riot$"),
    ("Messages (iMessage)", r"^messages$|^imessage$"),
    ("Google Chat (десктоп)", r"^google chat$"),
    ("Max", r"^max$|^max messenger$"),
)

#: Локальные и десктопные ИИ-клиенты: подсказка без интернета и без браузера.
AI_CLIENT_CATALOG: Catalog = (
    ("Ollama", r"^ollama"),
    ("LM Studio", r"lm.?studio|^lms$"),
    ("GPT4All", r"gpt4all"),
    ("llama.cpp", r"^llama-?(server|cli|cpp|run|bench)$|(^|/)main\b.*--model"),
    ("Jan", r"^jan$|^jan\.ai$"),
    ("LocalAI", r"^local-?ai$"),
    ("AnythingLLM", r"anything-?llm"),
    ("Msty", r"^msty$"),
    ("Open WebUI", r"open-?webui"),
    ("KoboldCpp", r"^kobold"),
    ("text-generation-webui / oobabooga", r"oobabooga|text-generation-webui"),
    ("vLLM", r"^vllm$"),
    ("ChatGPT (десктоп)", r"^chatgpt$|^openai$"),
    ("Claude (десктоп/CLI)", r"^claude$|^claude desktop$"),
    ("GitHub Copilot (десктоп)", r"^copilot$|^github copilot$"),
    ("Perplexity (десктоп)", r"^perplexity$"),
    ("Gemini / Bard (десктоп)", r"^gemini$|^bard$"),
    ("Cursor", r"^cursor$|^cursor helper"),
    ("Windsurf", r"^windsurf$"),
)

#: Автоматизация ввода: макрос может вставить готовый ответ по хоткею.
AUTOMATION_CATALOG: Catalog = (
    ("Cheat Engine", r"cheat ?engine"),
    ("AutoHotkey", r"^autohotkey"),
    ("Keyboard Maestro", r"^keyboard ?maestro"),
    ("Hammerspoon", r"^hammerspoon$"),
    ("BetterTouchTool", r"^bettertouchtool$"),
    ("AutoIt", r"^autoit"),
    ("Auto Mouse Click / макро-кликеры", r"auto ?(mouse )?click|^ghost ?mouse$"),
)

#: Гостевые дополнения гипервизоров: прямое доказательство ВМ.
VM_GUEST_CATALOG: Catalog = (
    ("Parallels Tools", r"^prl_|^prlcc$|^parallels"),
    ("VirtualBox Guest Additions", r"^vbox(service|client|tray)$|^virtualbox"),
    ("VMware Tools", r"^vmtoolsd$|^vmware-?(tools-?daemon|user|resolution)|^vm3dservice$"),
    ("QEMU Guest Agent", r"^qemu-?ga$|^qemu-?guest-?agent$"),
    ("Hyper-V Integration Services", r"^hv_(kvp|vss|fcopy)_daemon$|^vmicsvc$"),
    ("Xen Guest Utilities", r"^xe-?(daemon|linux-distribution)$|^xenservice$"),
    ("UTM / Apple Virtualization", r"^utm$|^qemu-system-"),
)

#: Признаки гипервизора в строках оборудования (hw.model, DMI, BIOS).
_VM_HARDWARE_PATTERNS: Catalog = (
    ("VMware", r"vmware"),
    ("VirtualBox", r"virtualbox|vbox|innotek"),
    ("Parallels", r"parallels"),
    ("QEMU/KVM", r"qemu|kvm|bochs|seabios"),
    ("Microsoft Hyper-V", r"hyper-?v|microsoft corporation virtual|virtual machine"),
    ("Xen", r"\bxen\b"),
    ("UTM", r"\butm\b"),
    ("Apple Virtualization", r"apple virtual (machine|platform)|vz-"),
)

#: Порты, на которых слушает ПО удалённого доступа. Сигнал слабый сам по себе
#: (порт мог занять что угодно), поэтому идёт с низким conf и именем процесса.
#: В списке только характерные порты: 4000, 5000, 8080 и прочие «общие» номера
#: сюда не попадают — на машине разработчика они заняты чем угодно (на macOS
#: 5000 и 7000 слушает AirPlay Receiver), и это было бы ложным срабатыванием.
_REMOTE_PORTS: dict[int, str] = {
    5900: "VNC / Screen Sharing",
    5901: "VNC (дисплей :1)",
    5902: "VNC (дисплей :2)",
    3389: "RDP",
    5938: "TeamViewer",
    5939: "TeamViewer (резервный)",
    7070: "AnyDesk",
    6568: "AnyDesk (прямое соединение)",
    21115: "RustDesk",
    21116: "RustDesk",
    21118: "RustDesk",
    5279: "Radmin",
    4899: "Radmin",
}

#: Универсальные стримеры из `VIRTUAL_CAMERA_HOST_CATALOG`: запущенный OBS сам
#: по себе — запись экрана, а не подмена камеры (см. `check_virtual_camera`).
_VCAM_GENERIC_HOSTS = frozenset({
    "OBS Studio (источник виртуальной камеры)",
    "Streamlabs Desktop",
    "mmhmm",
    "NVIDIA Broadcast",
})

_RX_REMOTE = _compile(REMOTE_ACCESS_CATALOG)
_RX_VCAM = _compile(VIRTUAL_CAMERA_CATALOG)
_RX_VCAM_HOST = _compile(VIRTUAL_CAMERA_HOST_CATALOG)
_RX_REC_STRONG = _compile(SCREEN_RECORDING_STRONG)
_RX_REC_WEAK = _compile(SCREEN_RECORDING_WEAK)
_RX_MESSENGER = _compile(MESSENGER_CATALOG)
_RX_AI = _compile(AI_CLIENT_CATALOG)
_RX_AUTOMATION = _compile(AUTOMATION_CATALOG)
_RX_VM_GUEST = _compile(VM_GUEST_CATALOG)
_RX_VM_HW = _compile(_VM_HARDWARE_PATTERNS)

#: Имена, которым разрешено жить в системных путях (см. `_is_system_noise`).
_ALLOW_SYSTEM = frozenset({
    "screensharingd", "screensharing", "screensharingagent", "ardagent",
    "screencapture", "screenshot", "quicktime player", "messages",
    "remote desktop", "vncserver", "ffmpeg", "vlc",
})


# ===========================================================================
# Сопоставление процессов с каталогом
# ===========================================================================
@dataclass(frozen=True)
class _Hit:
    label: str
    pattern: str
    proc: ProcInfo
    matched_on: str

    def as_detail(self) -> dict[str, Any]:
        d = self.proc.as_detail()
        d["software"] = self.label
        d["matched_on"] = self.matched_on
        d["pattern"] = self.pattern
        return d


def _match_processes(catalog: tuple[tuple[str, re.Pattern[str]], ...],
                     procs: Iterable[ProcInfo] | None = None,
                     *, use_cmdline: bool = False,
                     allow: frozenset[str] = _ALLOW_SYSTEM) -> list[_Hit]:
    """Найти процессы, подходящие под каталог. Один процесс — одно совпадение."""
    hits: list[_Hit] = []
    for proc in (procs if procs is not None else _processes()):
        if _is_system_noise(proc, allow):
            continue
        fields: list[tuple[str, str]] = [("name", proc.name_l)]
        if proc.exe_base_l and proc.exe_base_l != proc.name_l:
            fields.append(("exe", proc.exe_base_l))
        if proc.app_bundle and proc.app_bundle != proc.name_l:
            fields.append(("bundle", proc.app_bundle))
        if use_cmdline and proc.cmdline_l:
            fields.append(("cmdline", proc.cmdline_l[:400]))
        for label, rx in catalog:
            found = next((where for where, value in fields if value and rx.search(value)), None)
            if found:
                hits.append(_Hit(label=label, pattern=rx.pattern, proc=proc, matched_on=found))
                break
    return hits


def _match_custom(words: Sequence[str], procs: Iterable[ProcInfo] | None = None) -> list[_Hit]:
    """Совпадения по пользовательскому чёрному списку из конфига (подстроки).

    Конфиг — воля проктора, поэтому здесь допустимо простое вхождение
    подстроки, но системный шум всё равно отсекается.
    """
    cleaned = [w.strip().lower() for w in words if len(w.strip()) >= 3]
    if not cleaned:
        return []
    hits: list[_Hit] = []
    for proc in (procs if procs is not None else _processes()):
        if _is_system_noise(proc, _ALLOW_SYSTEM):
            continue
        for word in cleaned:
            where = None
            if word in proc.name_l:
                where = "name"
            elif proc.exe_base_l and word in proc.exe_base_l:
                where = "exe"
            elif proc.app_bundle and word in proc.app_bundle:
                where = "bundle"
            if where:
                hits.append(_Hit(label=f"из конфига: {word}", pattern=word,
                                 proc=proc, matched_on=where))
                break
    return hits


def _group_hits(hits: Sequence[_Hit]) -> list[dict[str, Any]]:
    """Свернуть совпадения по приложению.

    Electron-приложения (Slack, Telegram, Claude, ChatGPT) держат десятки
    процессов-помощников внутри одного `.app`: сырой список из 43 строк в
    отчёте нечитаем и создаёт ложное впечатление, что найдено 43 нарушения.
    Группируем по имени ПО, представителем берём главный процесс приложения
    (имя совпадает с бандлом) или самый старый, остальные учитываем числом
    и перечислением pid.
    """
    groups: dict[str, list[_Hit]] = {}
    for hit in hits:
        groups.setdefault(hit.label, []).append(hit)

    rows: list[dict[str, Any]] = []
    for label, items in groups.items():
        main = next((h for h in items if h.proc.name_l and h.proc.name_l == h.proc.app_bundle),
                    None)
        if main is None:
            main = min(items, key=lambda h: h.proc.create_time or float("inf"))
        row = main.as_detail()
        row["instances"] = len(items)
        row["pids"] = sorted({h.proc.pid for h in items})[:32]
        names: list[str] = []
        for h in items:
            if h.proc.name and h.proc.name not in names:
                names.append(h.proc.name)
        row["process_names"] = names[:8]
        rows.append(row)
    rows.sort(key=lambda r: (-int(r.get("instances") or 1), str(r.get("software") or "")))
    return rows


def _summarize(hits: Sequence[_Hit]) -> str:
    """Короткая строка для отчёта: «Slack (pid 80149, процессов: 7), Telegram (pid 70731)»."""
    parts: list[str] = []
    for row in _group_hits(hits):
        tail = (f", процессов: {row['instances']}"
                if int(row.get("instances") or 1) > 1 else "")
        parts.append(f"{row.get('software')} (pid {row.get('pid')}{tail})")
    return ", ".join(parts[:10]) + (" и ещё…" if len(parts) > 10 else "")


def _started_after(hits: Sequence[_Hit], since: float) -> list[_Hit]:
    """Процессы, запущенные после начала сессии — самый сильный довод."""
    if since <= 0:
        return []
    return [h for h in hits if h.proc.create_time and h.proc.create_time >= since]


# ===========================================================================
# 1. Виртуальная камера
# ===========================================================================
def _capture_devices() -> list[dict[str, Any]]:
    """Список устройств захвата видео в системе (по возможности с именами)."""
    cached = _CACHE.get("cams", _TTL_CAMERAS)
    if cached is not None:
        return list(cached)

    devices: list[dict[str, Any]] = []
    try:
        if IS_MAC:
            devices.extend(_capture_devices_mac())
        elif IS_WIN:
            devices.extend(_capture_devices_win())
        elif IS_LINUX:
            devices.extend(_capture_devices_linux())
    except Exception:
        pass
    try:
        devices.extend(_capture_devices_opencv())
    except Exception:
        pass

    # дедуп по (имя, источник)
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for dev in devices:
        key = (str(dev.get("name", "")).lower(), str(dev.get("source", "")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(dev)
    return _CACHE.put("cams", unique)


def _capture_devices_mac() -> list[dict[str, Any]]:
    """macOS: `system_profiler SPCameraDataType`."""
    data = _system_profiler("SPCameraDataType", _TTL_CAMERAS)
    out: list[dict[str, Any]] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("_name") or "").strip()
        model = str(item.get("spcamera_model-id") or "").strip()
        if not name and not model:
            continue
        out.append({"name": name or model, "model": model or None,
                    "unique_id": item.get("spcamera_unique-id"),
                    "source": "system_profiler"})
    return out


def _capture_devices_linux() -> list[dict[str, Any]]:
    """Linux: имена из sysfs (/sys/class/video4linux/videoN/name)."""
    out: list[dict[str, Any]] = []
    base = Path("/sys/class/video4linux")
    if not base.is_dir():
        return out
    for node in sorted(base.glob("video*")):
        try:
            name = (node / "name").read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            continue
        if name:
            out.append({"name": name, "device": f"/dev/{node.name}", "source": "sysfs"})
    return out


def _capture_devices_win() -> list[dict[str, Any]]:
    """Windows: фильтры DirectShow из реестра (категория Video Capture Sources)."""
    out: list[dict[str, Any]] = []
    if not IS_WIN:
        return out
    try:
        import winreg  # type: ignore
    except Exception:
        return out
    # {860BB310-5D01-11d0-BD3B-00A0C911CE86} — CLSID_VideoInputDeviceCategory
    keys = (
        r"SOFTWARE\Classes\CLSID\{860BB310-5D01-11d0-BD3B-00A0C911CE86}\Instance",
        r"SOFTWARE\WOW6432Node\Classes\CLSID"
        r"\{860BB310-5D01-11d0-BD3B-00A0C911CE86}\Instance",
    )
    for sub in keys:
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub)
        except Exception:
            continue
        try:
            index = 0
            while True:
                try:
                    child = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, child) as node:
                        friendly, _ = winreg.QueryValueEx(node, "FriendlyName")
                except Exception:
                    continue
                if friendly:
                    out.append({"name": str(friendly), "clsid": child,
                                "source": "directshow"})
        finally:
            try:
                root.Close()
            except Exception:
                pass
    return out


def _capture_devices_opencv() -> list[dict[str, Any]]:
    """Кросс-платформенные имена устройств через OpenCV-перечислитель.

    `cv2_enumerate_cameras` — тонкая обёртка над теми же бэкендами, что
    использует OpenCV (AVFoundation / DirectShow+MSMF / V4L2), и отдаёт
    имена устройств по индексам, которые принимает `cv2.VideoCapture`.
    Пакет опционален: нет его — просто нет этого источника имён.
    """
    try:
        from cv2_enumerate_cameras import enumerate_cameras  # type: ignore
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    try:
        for cam in enumerate_cameras():
            name = str(getattr(cam, "name", "") or "").strip()
            if not name:
                continue
            out.append({"name": name, "index": getattr(cam, "index", None),
                        "backend": getattr(cam, "backend", None),
                        "source": "opencv"})
    except Exception:
        return out
    return out


def _virtual_camera_plugins() -> list[dict[str, Any]]:
    """Установленные плагины виртуальных камер (не обязательно активные)."""
    out: list[dict[str, Any]] = []
    if IS_MAC:
        # Классические DAL-плагины CoreMediaIO: сюда ставится OBS Virtual Camera.
        for root in ("/Library/CoreMediaIO/Plug-Ins/DAL",
                     "/Library/CoreMediaIO/Plug-Ins/FCP-DAL",
                     os.path.expanduser("~/Library/CoreMediaIO/Plug-Ins/DAL")):
            base = Path(root)
            if not base.is_dir():
                continue
            try:
                entries = sorted(base.iterdir())
            except Exception:
                continue
            for entry in entries:
                if entry.suffix.lower() in (".plugin", ".bundle"):
                    out.append({"name": entry.stem, "path": str(entry),
                                "source": "coremediaio_dal"})
        # Современные Camera Extensions (System Extensions).
        listing = _run_cached("sysext", ["systemextensionsctl", "list"], _TTL_CAMERAS)
        for line in (listing or "").splitlines():
            line = line.strip()
            if not line or line.lower().startswith(("---", "no ", "0 extension")):
                continue
            if "camera" in line.lower() or "cmio" in line.lower() or "dal" in line.lower():
                out.append({"name": line, "source": "system_extension"})
    elif IS_LINUX:
        if Path("/sys/module/v4l2loopback").exists():
            out.append({"name": "v4l2loopback", "path": "/sys/module/v4l2loopback",
                        "source": "kernel_module"})
    elif IS_WIN:
        out.extend(_virtual_camera_plugins_win())
    return out


def _virtual_camera_plugins_win() -> list[dict[str, Any]]:
    """Windows: признаки установленных виртуальных камер в реестре."""
    out: list[dict[str, Any]] = []
    if not IS_WIN:
        return out
    try:
        import winreg  # type: ignore
    except Exception:
        return out
    candidates = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\OBS Studio", "OBS Studio"),
        (winreg.HKEY_CLASSES_ROOT, r"CLSID\{A3FCE0F5-3493-419F-958A-ABA1250EC20B}",
         "OBS Virtual Camera"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\ManyCam", "ManyCam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Dev47Apps", "DroidCam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\e2eSoft\VCam", "e2eSoft VCam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\SplitmediaLabs", "XSplit VCam"),
    )
    for hive, path, label in candidates:
        try:
            with winreg.OpenKey(hive, path):
                out.append({"name": label, "path": path, "source": "registry"})
        except Exception:
            continue
    return out


def check_virtual_camera(config: Any = None) -> list[EnvFinding]:
    """Виртуальная камера: подмена видеопотока вместо живого человека.

    Три независимых признака, от сильного к слабому:

    1. Устройство захвата с характерным именем («OBS Virtual Camera»,
       «ManyCam Virtual Webcam», «DroidCam Source») — поток уже доступен
       приложениям, это почти наверняка то, чем нас и подменяют.
    2. Процесс соответствующего ПО запущен (OBS, ManyCam, DroidCam-клиент).
    3. Плагин/расширение установлено, но ни устройства, ни процесса нет —
       слабый признак «инструмент под рукой», идёт с низким conf.

    `conf` склеивается из признаков, поэтому «устройство + процесс» весит
    больше, чем каждый по отдельности. Непрерывность видеопотока (нет
    морганий, повтор кадров) проверяет IdentityVerifier — это другой канал,
    здесь только состояние ОС.
    """
    try:
        started = time.perf_counter()
        devices = _capture_devices()
        plugins = _virtual_camera_plugins()
        procs = _match_processes(_RX_VCAM_HOST, use_cmdline=False)

        device_hits: list[dict[str, Any]] = []
        for dev in devices:
            haystack = " ".join(str(dev.get(k) or "") for k in ("name", "model"))
            for label, rx in _RX_VCAM:
                if rx.search(haystack):
                    row = dict(dev)
                    row["software"] = label
                    row["pattern"] = rx.pattern
                    device_hits.append(row)
                    break

        plugin_hits: list[dict[str, Any]] = []
        for plug in plugins:
            name = str(plug.get("name") or "")
            for label, rx in _RX_VCAM:
                if rx.search(name):
                    row = dict(plug)
                    row["software"] = label
                    row["pattern"] = rx.pattern
                    plugin_hits.append(row)
                    break

        if not device_hits and not plugin_hits and not procs:
            return []

        # conf: устройство — почти доказательство, остальное — улики
        conf = 0.0
        reasons: list[str] = []
        if device_hits:
            conf = max(conf, 0.95)
            names = ", ".join(str(d.get("name")) for d in device_hits[:4])
            reasons.append(f"в системе зарегистрировано устройство захвата: {names}")
        # OBS и Streamlabs — универсальные стримеры: сам факт их запуска это
        # ещё запись экрана, а не подмена камеры. Они повышают уверенность
        # только вместе с устройством или установленным плагином; иначе их
        # забирает check_screen_recording. Узкоспециальные инструменты
        # (ManyCam, DroidCam, Camo) говорят о подмене сами по себе.
        dedicated = [h for h in procs if h.label not in _VCAM_GENERIC_HOSTS]
        if device_hits or plugin_hits:
            relevant = procs
        else:
            relevant = dedicated
            procs = dedicated

        if relevant:
            if device_hits:
                conf = max(conf, 0.95)
            elif plugin_hits:
                conf = max(conf, 0.8)
            else:
                conf = max(conf, 0.6)
            reasons.append(f"запущено ПО виртуальной камеры: {_summarize(relevant)}")

        if plugin_hits and not device_hits and not relevant:
            if not _cfg_bool(config, "env", "flag_installed_only", True):
                return []
            conf = max(conf, 0.3)
            names = ", ".join(str(p.get("name")) for p in plugin_hits[:4])
            reasons.append(f"установлен плагин виртуальной камеры (не активен): {names}")
        elif plugin_hits and not device_hits:
            names = ", ".join(str(p.get("name")) for p in plugin_hits[:4])
            reasons.append(f"установлен плагин виртуальной камеры: {names}")

        if not reasons:  # остались только «универсальные» стримеры — не наш случай
            return []

        if conf >= 0.9:
            severity = Severity.CRITICAL
        elif conf >= 0.6:
            severity = Severity.HIGH
        else:
            severity = Severity.LOW

        detail: dict[str, Any] = {
            "conf": round(conf, 2),
            "summary": "; ".join(reasons),
            "reason": "Видеопоток может быть подменён: обнаружены признаки виртуальной камеры",
            "devices_matched": device_hits,
            "plugins_matched": plugin_hits,
            "processes": _group_hits(procs),
            "all_capture_devices": [str(d.get("name")) for d in devices],
            "platform": sys.platform,
            "check": "check_virtual_camera",
            "check_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return [EnvFinding(EventKind.VIRTUAL_CAMERA, detail, severity)]
    except Exception as exc:  # проверка не имеет права ломать сайдкар
        return _self_error("check_virtual_camera", exc)


# ===========================================================================
# 2. ПО удалённого доступа
# ===========================================================================
#: «ИМЯ:PID» в хвосте строки netstat. Имя начинается не с цифры и не с двоеточия
#: (иначе шаблон цепляет IPv6-адрес `::1.51881`) и не содержит двойных пробелов.
_RX_NETSTAT_OWNER = re.compile(r"(?:^|\s)([^\s\d:][^\s:]*(?: [^\s:]+)*):(\d+)(?=\s|$)")


def _listening_ports() -> list[dict[str, Any]]:
    """Локальные TCP-порты в состоянии LISTEN (без root-прав).

    Сначала psutil (там сразу pid), на macOS без root он падает с AccessDenied —
    тогда `netstat -anv -p tcp`, который показывает все слушающие сокеты
    непривилегированному пользователю.
    """
    cached = _CACHE.get("listen", _TTL_LISTENERS)
    if cached is not None:
        return list(cached)

    rows: list[dict[str, Any]] = []
    psutil = _psutil()
    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="tcp"):
                if conn.status != getattr(psutil, "CONN_LISTEN", "LISTEN"):
                    continue
                if not conn.laddr:
                    continue
                rows.append({"port": int(conn.laddr.port), "pid": conn.pid,
                             "source": "psutil"})
        except Exception:
            rows = []

    if not rows:
        out = _run(["netstat", "-anv", "-p", "tcp"], timeout=_T_FAST)
        for line in (out or "").splitlines():
            if "LISTEN" not in line:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            local = parts[3]
            port_str = local.rsplit(".", 1)[-1] if "." in local else local.rsplit(":", 1)[-1]
            try:
                port = int(port_str)
            except ValueError:
                continue
            # владелец сокета в конце строки: «Python:82840», «OrbStack Helper:446».
            # Имя может содержать одиночные пробелы, но не серии пробелов —
            # иначе жадный шаблон съедает все колонки netstat до двоеточия.
            owners = _RX_NETSTAT_OWNER.findall(line)
            owner = owners[-1] if owners else None
            rows.append({
                "port": port,
                "process": owner[0].strip() if owner else None,
                "pid": int(owner[1]) if owner else None,
                "source": "netstat",
            })
    return _CACHE.put("listen", rows)


def _mac_screen_sharing() -> list[dict[str, Any]]:
    """macOS: признаки включённого/активного Screen Sharing и Apple Remote Desktop.

    `screensharingd` живёт только пока к машине реально подключён VNC-клиент —
    это признак активной сессии, а не просто включённой галочки в настройках.
    Наличие `com.apple.RemoteManagement.plist` означает включённый ARD.
    """
    if not IS_MAC:
        return []
    out: list[dict[str, Any]] = []
    for proc in _processes():
        if proc.name_l in ("screensharingd", "screensharingagent", "ardagent",
                           "aramdhelper", "remotedesktopagent"):
            out.append({"kind": "активная сессия удалённого экрана",
                        **proc.as_detail()})
    for path in ("/Library/Preferences/com.apple.RemoteManagement.plist",
                 "/Library/Preferences/com.apple.ScreenSharing.launchd"):
        if Path(path).exists():
            out.append({"kind": "служба удалённого управления включена", "path": path})
    return out


def _win_remote_session() -> list[dict[str, Any]]:
    """Windows: активная RDP-сессия и включённый терминальный доступ."""
    if not IS_WIN:
        return []
    out: list[dict[str, Any]] = []
    try:
        import ctypes  # локально: на macOS windll отсутствует
        # SM_REMOTESESSION == 0x1000: процесс исполняется в сессии удалённого стола
        if int(ctypes.windll.user32.GetSystemMetrics(0x1000)) != 0:  # type: ignore[attr-defined]
            out.append({"kind": "экзамен открыт внутри RDP-сессии",
                        "api": "GetSystemMetrics(SM_REMOTESESSION)"})
    except Exception:
        pass
    session = os.environ.get("SESSIONNAME", "")
    if session and not session.upper().startswith("CONSOLE"):
        out.append({"kind": "переменная SESSIONNAME указывает на удалённую сессию",
                    "value": session})
    try:
        import winreg  # type: ignore
        path = r"SYSTEM\CurrentControlSet\Control\Terminal Server"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            deny, _ = winreg.QueryValueEx(key, "fDenyTSConnections")
            if int(deny) == 0:
                out.append({"kind": "входящие RDP-подключения разрешены",
                            "registry": path})
    except Exception:
        pass
    return out


def _linux_remote_session() -> list[dict[str, Any]]:
    """Linux: вход по SSH или X11-сессия через сеть."""
    if not IS_LINUX:
        return []
    out: list[dict[str, Any]] = []
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"):
        out.append({"kind": "сессия открыта по SSH",
                    "value": os.environ.get("SSH_CONNECTION", "")})
    who = _run(["who"], timeout=_T_FAST)
    for line in (who or "").splitlines():
        m = re.search(r"\(([^)]+)\)\s*$", line)
        if m and not m.group(1).startswith(":"):
            out.append({"kind": "вход с удалённого адреса", "value": m.group(1)})
    return out


def check_remote_access(config: Any = None) -> list[EnvFinding]:
    """ПО удалённого доступа и активные remote-сессии.

    Три источника: процессы из `REMOTE_ACCESS_CATALOG`; платформенные признаки
    активной сессии (на macOS живой `screensharingd`, на Windows
    `GetSystemMetrics(SM_REMOTESESSION)`, на Linux SSH/`who`); слушающие порты
    характерных сервисов.

    Активная сессия — самый сильный довод (conf 0.95): за машиной прямо сейчас
    второй человек. Запущенный клиент — улика (0.85). Разрешённая служба без
    подключения — предупреждение (0.5), потому что галочка в настройках ещё
    никого не изобличает.
    """
    try:
        started = time.perf_counter()
        procs = _match_processes(_RX_REMOTE)
        platform_hits = _mac_screen_sharing() + _win_remote_session() + _linux_remote_session()

        port_hits: list[dict[str, Any]] = []
        for row in _listening_ports():
            label = _REMOTE_PORTS.get(int(row.get("port") or 0))
            if label:
                port_hits.append({**row, "service": label})

        if not procs and not platform_hits and not port_hits:
            return []

        active = [h for h in platform_hits if "активная" in str(h.get("kind", ""))
                  or "RDP-сессии" in str(h.get("kind", ""))
                  or "SSH" in str(h.get("kind", ""))
                  or "удалённого адреса" in str(h.get("kind", ""))]

        conf = 0.0
        reasons: list[str] = []
        if active:
            conf = max(conf, 0.95)
            reasons.append("обнаружена активная сессия удалённого доступа: "
                           + "; ".join(str(h.get("kind")) for h in active[:3]))
        if procs:
            conf = max(conf, 0.85)
            reasons.append(f"запущено ПО удалённого доступа: {_summarize(procs)}")
        if platform_hits and not active:
            conf = max(conf, 0.5)
            reasons.append("включена служба удалённого управления: "
                           + "; ".join(str(h.get("kind")) for h in platform_hits[:3]))
        if port_hits and conf < 0.5:
            conf = max(conf, 0.45)
            reasons.append("открыт порт сервиса удалённого доступа: "
                           + ", ".join(f"{p['port']} ({p['service']})" for p in port_hits[:4]))
        elif port_hits:
            reasons.append("открыты порты: "
                           + ", ".join(f"{p['port']} ({p['service']})" for p in port_hits[:4]))

        severity = (Severity.CRITICAL if conf >= 0.9
                    else Severity.HIGH if conf >= 0.7
                    else Severity.MEDIUM)

        detail: dict[str, Any] = {
            "conf": round(conf, 2),
            "summary": "; ".join(reasons),
            "reason": "Возможна подсказка через удалённый доступ к этому компьютеру",
            "processes": _group_hits(procs),
            "system": platform_hits,
            "ports": port_hits,
            "platform": sys.platform,
            "check": "check_remote_access",
            "check_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return [EnvFinding(EventKind.REMOTE_ACCESS_SOFTWARE, detail, severity)]
    except Exception as exc:
        return _self_error("check_remote_access", exc)


# ===========================================================================
# 3. Виртуальная машина
# ===========================================================================
def _vm_hardware_strings() -> list[tuple[str, str]]:
    """Строки оборудования, в которых видно гипервизор: (источник, значение)."""
    rows: list[tuple[str, str]] = []
    if IS_MAC:
        for name in ("hw.model", "machdep.cpu.brand_string", "hw.product",
                     "machdep.cpu.features"):
            value = _sysctl(name)
            if value:
                rows.append((f"sysctl {name}", value))
    elif IS_LINUX:
        for path in ("/sys/class/dmi/id/sys_vendor", "/sys/class/dmi/id/product_name",
                     "/sys/class/dmi/id/board_vendor", "/sys/class/dmi/id/bios_vendor",
                     "/sys/class/dmi/id/product_version"):
            try:
                value = Path(path).read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                continue
            if value:
                rows.append((path, value))
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
            if re.search(r"^flags\s*:.*\bhypervisor\b", cpuinfo, re.MULTILINE):
                rows.append(("/proc/cpuinfo", "флаг hypervisor присутствует"))
        except Exception:
            pass
    elif IS_WIN:
        rows.extend(_vm_hardware_strings_win())
    return rows


def _vm_hardware_strings_win() -> list[tuple[str, str]]:
    """Windows: BIOS и модель системы из реестра + сервисы гостевых дополнений."""
    rows: list[tuple[str, str]] = []
    if not IS_WIN:
        return rows
    try:
        import winreg  # type: ignore
    except Exception:
        return rows
    bios = r"HARDWARE\DESCRIPTION\System\BIOS"
    for value_name in ("SystemManufacturer", "SystemProductName", "BIOSVendor",
                       "BIOSVersion", "BaseBoardManufacturer"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, bios) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
        except Exception:
            continue
        text = " ".join(value) if isinstance(value, (list, tuple)) else str(value)
        if text.strip():
            rows.append((f"registry {bios}\\{value_name}", text.strip()))
    for service in ("VBoxGuest", "VBoxService", "vmci", "vmhgfs", "vmmouse",
                    "vmrawdsk", "prl_tg", "prl_eth5", "vmicheartbeat"):
        path = rf"SYSTEM\CurrentControlSet\Services\{service}"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path):
                rows.append((f"registry service {service}", service))
        except Exception:
            continue
    return rows


def check_virtual_machine(config: Any = None) -> list[EnvFinding]:
    """Экзамен внутри виртуальной машины.

    Зачем проверять: в ВМ локдаун оболочки обесценивается — хост-система
    остаётся свободной, рядом открыт браузер с ответами, а гость видит
    только «чистый» рабочий стол.

    Признаки: флаг гипервизора (`kern.hv_vmm_present` на macOS, `hypervisor`
    в flags на Linux), строки оборудования (`hw.model`, DMI, BIOS), процессы
    гостевых дополнений (Parallels Tools, VBoxService, vmtoolsd, qemu-ga).

    Флаг гипервизора + совпадение по оборудованию считается подтверждением
    (conf 0.9). Только гостевые дополнения без флага — слабый признак (0.4):
    так выглядит хост, на котором *установлен* VMware, но запущен он не в ВМ.
    """
    try:
        started = time.perf_counter()
        evidence: list[dict[str, Any]] = []
        vendors: set[str] = set()

        if IS_MAC:
            hv = _sysctl("kern.hv_vmm_present")
            if hv and hv.strip() not in ("0", ""):
                evidence.append({"kind": "ядро сообщает о гипервизоре",
                                 "source": "sysctl kern.hv_vmm_present", "value": hv})
            features = _sysctl("machdep.cpu.features") or ""
            if re.search(r"\bVMM\b", features):
                evidence.append({"kind": "флаг VMM в возможностях CPU",
                                 "source": "sysctl machdep.cpu.features", "value": "VMM"})

        for source, value in _vm_hardware_strings():
            for label, rx in _RX_VM_HW:
                if rx.search(value):
                    # «machdep.cpu.features» содержит VMM — он уже учтён выше
                    evidence.append({"kind": "признак гипервизора в данных оборудования",
                                     "source": source, "value": value[:200],
                                     "vendor": label})
                    vendors.add(label)
                    break

        guest = _match_processes(_RX_VM_GUEST)
        for hit in guest:
            vendors.add(hit.label)

        if not evidence and not guest:
            return []

        hypervisor_flag = any("гипервизор" in str(e.get("kind", "")) or "VMM" in str(e.get("value", ""))
                              for e in evidence)
        hardware_match = any(e.get("vendor") for e in evidence)

        if hypervisor_flag and (hardware_match or guest):
            conf, severity = 0.9, Severity.HIGH
        elif hardware_match:
            conf, severity = 0.8, Severity.HIGH
        elif hypervisor_flag:
            conf, severity = 0.6, Severity.MEDIUM
        else:
            conf, severity = 0.4, Severity.LOW

        reasons: list[str] = []
        if vendors:
            reasons.append("признаки гипервизора: " + ", ".join(sorted(vendors)))
        if guest:
            reasons.append(f"запущены гостевые дополнения: {_summarize(guest)}")
        if hypervisor_flag and not vendors:
            reasons.append("ядро сообщает, что система работает под гипервизором")

        detail: dict[str, Any] = {
            "conf": round(conf, 2),
            "summary": "; ".join(reasons),
            "reason": "Экзамен выполняется в виртуальной машине: локдаун оболочки "
                      "не защищает хост-систему",
            "vendors": sorted(vendors),
            "evidence": evidence,
            "guest_tools": _group_hits(guest),
            "platform": sys.platform,
            "check": "check_virtual_machine",
            "check_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return [EnvFinding(EventKind.VIRTUAL_MACHINE, detail, severity)]
    except Exception as exc:
        return _self_error("check_virtual_machine", exc)


# ===========================================================================
# 4. Запись и трансляция экрана
# ===========================================================================
def check_screen_recording(config: Any = None) -> list[EnvFinding]:
    """Запись или трансляция экрана: задание уходит наружу целиком.

    Сильные сигналы — процессы, которые существуют только во время записи или
    захвата: `CptHost` (Zoom демонстрирует экран), `screencapture` (снимок
    экрана macOS выполняется прямо сейчас), OBS/ScreenFlow/Camtasia/Loom.

    Слабые — приложения, которые *умеют* писать экран, но факт записи не
    подтверждён: QuickTime Player, Zoom без помощника демонстрации, VLC,
    ffmpeg. Такой сигнал поднимается до сильного, если в командной строке
    процесса видно устройство захвата (`avfoundation`, `gdigrab`, `x11grab`,
    `screen://`): тогда это уже не «запущен VLC», а «VLC пишет экран».

    Отдельно: сам факт съёмки экрана оболочка обесценивает чёрным кадром в
    защищённом окне — это другой механизм, здесь только фиксация факта.
    """
    try:
        started = time.perf_counter()
        strong = _match_processes(_RX_REC_STRONG)
        weak_all = _match_processes(_RX_REC_WEAK, use_cmdline=False)

        # слабый сигнал -> сильный, если в командной строке есть захват экрана
        promoted: list[tuple[_Hit, str]] = []
        weak: list[_Hit] = []
        strong_pids = {h.proc.pid for h in strong}
        for hit in weak_all:
            # помощник демонстрации Zoom (CptHost) лежит внутри zoom.us.app,
            # поэтому попадает и в сильный, и в слабый список — оставляем
            # его только там, где сигнал определённее
            if hit.proc.pid in strong_pids:
                continue
            cmd = hit.proc.cmdline_l
            marker = next((why for token, why in _CAPTURE_CMDLINE_MARKERS if token in cmd), None)
            if marker:
                promoted.append((hit, marker))
            else:
                weak.append(hit)

        if not strong and not promoted and not weak:
            return []

        conf = 0.0
        reasons: list[str] = []
        if strong:
            conf = max(conf, 0.85)
            reasons.append(f"идёт запись или трансляция экрана: {_summarize(strong)}")
        if promoted:
            conf = max(conf, 0.9)
            reasons.append("процесс захватывает экран: " + ", ".join(
                f"{h.label} (pid {h.proc.pid}, {why})" for h, why in promoted[:4]))
        if weak and conf < 0.4:
            conf = max(conf, 0.35)
            reasons.append("запущено ПО, умеющее писать экран (факт записи не подтверждён): "
                           + _summarize(weak))
        elif weak:
            reasons.append("дополнительно запущено: " + _summarize(weak))

        severity = (Severity.HIGH if conf >= 0.8
                    else Severity.MEDIUM if conf >= 0.5
                    else Severity.LOW)

        detail: dict[str, Any] = {
            "conf": round(conf, 2),
            "summary": "; ".join(reasons),
            "reason": "Содержимое экрана с заданием может записываться или транслироваться",
            "confirmed": _group_hits(strong)
                         + [{**row, "capture": why}
                            for h, why in promoted
                            for row in _group_hits([h])],
            "suspected": _group_hits(weak),
            "platform": sys.platform,
            "check": "check_screen_recording",
            "check_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return [EnvFinding(EventKind.SCREEN_RECORDING, detail, severity)]
    except Exception as exc:
        return _self_error("check_screen_recording", exc)


# ===========================================================================
# 5. Чёрный список процессов
# ===========================================================================
def check_blacklisted_processes(config: Any = None) -> list[EnvFinding]:
    """Мессенджеры, ИИ-клиенты и автоматизация ввода.

    Три группы, разные по весу:

    * ИИ-клиенты (Ollama, LM Studio, GPT4All, десктопные ChatGPT/Claude) —
      подсказка без интернета и без браузера, самый прямой канал (conf 0.85);
    * автоматизация ввода (AutoHotkey, Keyboard Maestro, Cheat Engine) —
      готовый ответ вставляется по хоткею (0.6);
    * мессенджеры (Telegram, Discord, WhatsApp, Slack) — канал связи с
      подсказчиком; запущенный мессенджер сам по себе ничего не доказывает,
      поэтому 0.5, и упор делается на время запуска.

    Процесс, поднятый ПОСЛЕ начала сессии, весит больше: студент открыл
    Telegram на середине экзамена, а не забыл закрыть его утром. Момент
    начала берётся из `config["session_start_ts"]`, если вызывающий его передал.

    Пользовательский список из конфига (`env_blacklist`) обрабатывается
    отдельно: там разрешено простое вхождение подстроки, это воля проктора.
    """
    try:
        started = time.perf_counter()
        procs = _processes()
        ai = _match_processes(_RX_AI, procs, use_cmdline=True)
        automation = _match_processes(_RX_AUTOMATION, procs)
        messengers = _match_processes(_RX_MESSENGER, procs)

        claimed = {h.proc.pid for h in (*ai, *automation, *messengers)}
        custom_words = _cfg_list(config, "env", "blacklist")
        custom = [h for h in _match_custom(custom_words, procs) if h.proc.pid not in claimed]

        if not (ai or automation or messengers or custom):
            return []

        session_start = _cfg_float(config, "env", "session_start_ts", 0.0)
        fresh = _started_after([*ai, *automation, *messengers, *custom], session_start)

        conf = 0.0
        reasons: list[str] = []
        if ai:
            conf = max(conf, 0.85)
            reasons.append(f"запущен ИИ-клиент: {_summarize(ai)}")
        if automation:
            conf = max(conf, 0.6)
            reasons.append(f"запущена автоматизация ввода: {_summarize(automation)}")
        if messengers:
            conf = max(conf, 0.5)
            reasons.append(f"запущен мессенджер: {_summarize(messengers)}")
        if custom:
            conf = max(conf, 0.5)
            reasons.append(f"совпадение с чёрным списком конфига: {_summarize(custom)}")
        if fresh:
            conf = min(conf + 0.1, 0.95)
            reasons.append("запущено уже после начала сессии: " + _summarize(fresh))

        severity = (Severity.HIGH if conf >= 0.8
                    else Severity.MEDIUM if conf >= 0.5
                    else Severity.LOW)

        detail: dict[str, Any] = {
            "conf": round(conf, 2),
            "summary": "; ".join(reasons),
            "reason": "Запущено ПО, через которое можно получить подсказку",
            "ai_clients": _group_hits(ai),
            "automation": _group_hits(automation),
            "messengers": _group_hits(messengers),
            "from_config": _group_hits(custom),
            "started_after_session": _group_hits(fresh),
            "platform": sys.platform,
            "check": "check_blacklisted_processes",
            "check_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return [EnvFinding(EventKind.BLACKLISTED_PROCESS, detail, severity)]
    except Exception as exc:
        return _self_error("check_blacklisted_processes", exc)


# ===========================================================================
# 6. Мониторы
# ===========================================================================
def _displays_mac() -> list[dict[str, Any]]:
    """macOS: экраны из `system_profiler SPDisplaysDataType` (по всем GPU)."""
    data = _system_profiler("SPDisplaysDataType", _TTL_DISPLAYS)
    out: list[dict[str, Any]] = []
    for gpu in data or []:
        if not isinstance(gpu, dict):
            continue
        for screen in gpu.get("spdisplays_ndrvs") or []:
            if not isinstance(screen, dict):
                continue
            mirror = str(screen.get("spdisplays_mirror") or "").lower()
            online = str(screen.get("spdisplays_online") or "").lower()
            out.append({
                "name": str(screen.get("_name") or "экран"),
                "resolution": screen.get("_spdisplays_resolution")
                              or screen.get("_spdisplays_pixels"),
                "connection": str(screen.get("spdisplays_connection_type") or ""),
                "main": str(screen.get("spdisplays_main") or "").endswith("yes"),
                "mirrored": mirror.endswith("on") or mirror == "spdisplays_mirror_on",
                "online": (not online) or online.endswith("yes"),
                "gpu": str(gpu.get("sppci_model") or gpu.get("_name") or ""),
            })
    return out


def _displays_win() -> list[dict[str, Any]]:
    """Windows: мониторы через EnumDisplayMonitors / SM_CMONITORS."""
    if not IS_WIN:
        return []
    out: list[dict[str, Any]] = []
    try:
        import ctypes
        from ctypes import wintypes

        monitors: list[tuple[int, int, int, int]] = []

        proto = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(wintypes.RECT), ctypes.c_double)

        def _cb(_hmon: Any, _hdc: Any, rect: Any, _data: Any) -> int:
            r = rect.contents
            monitors.append((r.left, r.top, r.right, r.bottom))
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(  # type: ignore[attr-defined]
            None, None, proto(_cb), 0)
        for idx, (left, top, right, bottom) in enumerate(monitors):
            out.append({
                "name": f"Монитор {idx + 1}",
                "resolution": f"{right - left} x {bottom - top}",
                "main": left == 0 and top == 0,
                "mirrored": False,
                "online": True,
            })
        if not out:
            count = int(ctypes.windll.user32.GetSystemMetrics(80))  # type: ignore[attr-defined]
            out = [{"name": f"Монитор {i + 1}", "main": i == 0,
                    "mirrored": False, "online": True} for i in range(max(count, 0))]
    except Exception:
        return []
    return out


def _displays_linux() -> list[dict[str, Any]]:
    """Linux: подключённые выходы по `xrandr --listmonitors`."""
    if not IS_LINUX:
        return []
    out: list[dict[str, Any]] = []
    listing = _run(["xrandr", "--listmonitors"], timeout=_T_FAST)
    for line in (listing or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3:
            out.append({"name": parts[-1], "resolution": parts[2],
                        "main": line.lstrip().startswith("0:") or "*" in parts[0],
                        "mirrored": False, "online": True})
    if out:
        return out
    base = Path("/sys/class/drm")
    if base.is_dir():
        for node in sorted(base.glob("card*-*")):
            try:
                status = (node / "status").read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if status == "connected":
                out.append({"name": node.name, "main": False,
                            "mirrored": False, "online": True})
    return out


def check_displays(config: Any = None) -> list[EnvFinding]:
    """Число независимых экранов.

    Второй монитор — классика: задание на одном, подсказка на другом, камера
    видит ровно один честный взгляд в экран.

    Зеркалированные экраны НЕ считаются вторым монитором: проектор на демо-дне
    и внешний монитор в режиме зеркала показывают то же самое, подсказке там
    взяться негде. Логическое число экранов = активные минус лишние зеркала.

    Оболочка считает мониторы своими средствами (Electron `screen`) — это
    второй независимый источник, расхождение между ними само по себе
    подозрительно и видно в отчёте.
    """
    try:
        started = time.perf_counter()
        if _cfg_bool(config, "env", "allow_multiple_displays", False):
            return []

        if IS_MAC:
            screens = _displays_mac()
        elif IS_WIN:
            screens = _displays_win()
        else:
            screens = _displays_linux()

        active = [s for s in screens if s.get("online", True)]
        mirrored = [s for s in active if s.get("mirrored")]
        # все зеркала показывают одну картинку -> это один логический экран
        logical = len(active) - max(len(mirrored) - 1, 0)

        if logical < 2:
            return []

        conf = 0.9 if logical >= 3 else 0.8
        severity = Severity.HIGH if logical >= 3 else Severity.MEDIUM
        names = ", ".join(
            f"{s.get('name')}"
            + (f" {s.get('resolution')}" if s.get("resolution") else "")
            + (" (основной)" if s.get("main") else "")
            + (" (зеркало)" if s.get("mirrored") else "")
            for s in active)

        detail: dict[str, Any] = {
            "conf": conf,
            "summary": f"подключено экранов: {logical} — {names}",
            "reason": "Подключено несколько независимых экранов: на втором экране "
                      "может быть открыта подсказка",
            "count": logical,
            "count_physical": len(active),
            "mirrored": len(mirrored),
            "displays": active,
            "source": "system_profiler" if IS_MAC else sys.platform,
            "platform": sys.platform,
            "check": "check_displays",
            "check_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return [EnvFinding(EventKind.MULTIPLE_DISPLAYS, detail, severity)]
    except Exception as exc:
        return _self_error("check_displays", exc)


# ===========================================================================
# Сборка
# ===========================================================================
def _self_error(check: str, exc: BaseException) -> list[EnvFinding]:
    """Проверка упала. Молчим в находках, но оставляем след в кеше для диагностики.

    Возвращаем пустой список: ошибка проверки — не инцидент студента, и
    превращать её в событие нельзя, иначе баг модуля поднимет risk-score.
    """
    _CACHE.put(f"error:{check}", f"{type(exc).__name__}: {exc}")
    return []


def last_errors() -> dict[str, str]:
    """Ошибки проверок с прошлого прогона — для лога сайдкара."""
    raw = _CACHE.with_prefix("error:")
    return {name: str(value) for name, value in raw.items()}


#: Порядок проверок и их приоритет при дедупликации процессов.
#: Процесс, уже попавший в более важную находку, не дублируется в менее важной:
#: OBS не должен числиться и виртуальной камерой, и записью экрана одновременно.
_CHECKS = (
    ("virtual_camera", check_virtual_camera),
    ("remote_access", check_remote_access),
    ("virtual_machine", check_virtual_machine),
    ("screen_recording", check_screen_recording),
    ("blacklisted_processes", check_blacklisted_processes),
    ("displays", check_displays),
)

#: Ключи detail, в которых лежат списки процессов (для дедупликации).
_PROC_KEYS = ("processes", "guest_tools", "confirmed", "suspected",
              "ai_clients", "automation", "messengers", "from_config",
              "started_after_session")


def run_all_checks(config: Any = None) -> list[EnvFinding]:
    """Прогнать все проверки окружения.

    Возвращает `list[EnvFinding]`. Ни одна ошибка внутри проверки не всплывает
    наружу: упавшая проверка просто не даёт находок (её исключение доступно
    через `last_errors()`).

    После сбора выполняется дедупликация по pid в порядке `_CHECKS`: один и тот
    же процесс не перечисляется в двух находках. Если после дедупликации у
    находки не осталось ни процессов, ни других улик — она отбрасывается.
    """
    findings: list[EnvFinding] = []
    claimed: set[int] = set()

    for name, check in _CHECKS:
        try:
            produced = check(config) or []
        except Exception as exc:  # вторая линия обороны: проверки уже ловят всё сами
            _self_error(name, exc)
            continue
        for finding in produced:
            if not isinstance(finding, EnvFinding):
                continue
            if _dedupe_finding(finding, claimed):
                findings.append(finding)
    return findings


def _row_pids(row: Any) -> set[int]:
    """Все pid, которые покрывает строка detail (представитель + группа)."""
    if not isinstance(row, dict):
        return set()
    pids = {p for p in (row.get("pids") or []) if isinstance(p, int)}
    pid = row.get("pid")
    if isinstance(pid, int):
        pids.add(pid)
    return pids


def _dedupe_finding(finding: EnvFinding, claimed: set[int]) -> bool:
    """Убрать из находки процессы, уже учтённые раньше. False — находка пуста.

    Строка отбрасывается, только если ВСЕ её процессы уже учтены более
    приоритетной находкой: иначе мы потеряли бы часть процессов приложения.
    """
    detail = finding.detail if isinstance(finding.detail, dict) else {}
    had_rows = False
    kept_rows = False
    mine: set[int] = set()

    for key in _PROC_KEYS:
        rows = detail.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        had_rows = True
        fresh = []
        for row in rows:
            pids = _row_pids(row)
            if pids and pids <= claimed:
                continue
            mine |= pids
            fresh.append(row)
        detail[key] = fresh
        kept_rows = kept_rows or bool(fresh)

    # улики, не связанные с процессами: устройства, экраны, порты, оборудование
    non_process = any(detail.get(key) for key in (
        "devices_matched", "plugins_matched", "displays", "ports", "system",
        "evidence", "vendors"))

    if had_rows and not kept_rows and not non_process:
        return False
    claimed |= mine
    return True


def available() -> bool:
    """Модуль способен хоть что-то проверить на этой платформе?

    Процессные проверки требуют psutil; устройства, экраны и гипервизор
    читаются платформенными средствами. False бывает только на экзотике,
    где нет ни psutil, ни знакомой платформы.
    """
    if _psutil() is not None:
        return True
    return IS_MAC or IS_WIN or IS_LINUX


def describe_checks() -> list[dict[str, Any]]:
    """Что умеет модуль прямо сейчас — для `hello` и диагностики."""
    psutil_ok = _psutil() is not None
    return [
        {"check": "virtual_camera", "kind": EventKind.VIRTUAL_CAMERA.value,
         "ready": True, "needs_psutil": False},
        {"check": "remote_access", "kind": EventKind.REMOTE_ACCESS_SOFTWARE.value,
         "ready": psutil_ok or IS_MAC or IS_WIN, "needs_psutil": True},
        {"check": "virtual_machine", "kind": EventKind.VIRTUAL_MACHINE.value,
         "ready": True, "needs_psutil": False},
        {"check": "screen_recording", "kind": EventKind.SCREEN_RECORDING.value,
         "ready": psutil_ok, "needs_psutil": True},
        {"check": "blacklisted_processes", "kind": EventKind.BLACKLISTED_PROCESS.value,
         "ready": psutil_ok, "needs_psutil": True},
        {"check": "displays", "kind": EventKind.MULTIPLE_DISPLAYS.value,
         "ready": True, "needs_psutil": False},
    ]


# ===========================================================================
# Ручной прогон: python3 sidecar/env_checks.py
# ===========================================================================
def _main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Проверки окружения прокторинга (разовый прогон)")
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    parser.add_argument("--allow-multi-display", action="store_true",
                        help="не считать нарушением несколько мониторов")
    args = parser.parse_args()

    config: dict[str, Any] = {"env": {
        "allow_multiple_displays": bool(args.allow_multi_display),
    }}
    try:
        from config import ProctorConfig  # конфиг проекта, если он доступен
        cfg, _warnings = ProctorConfig.load()
        config = cfg.to_dict()
        if args.allow_multi_display:
            config.setdefault("env", {})["allow_multiple_displays"] = True
    except Exception:
        pass

    started = time.perf_counter()
    findings = run_all_checks(config)
    elapsed = (time.perf_counter() - started) * 1000

    if args.json:
        print(json.dumps([f.to_dict() for f in findings], ensure_ascii=False, indent=2))
    else:
        print(f"Платформа: {sys.platform}; проверок: {len(_CHECKS)}; "
              f"psutil: {'да' if _psutil() is not None else 'нет'}; "
              f"время: {elapsed:.0f} мс")
        if not findings:
            print("Находок нет: окружение чистое.")
        for finding in findings:
            print(f"\n[{finding.severity.value.upper()}] {finding.kind.value} "
                  f"(conf {finding.detail.get('conf')})")
            print(f"  причина: {finding.detail.get('reason')}")
            print(f"  найдено: {finding.detail.get('summary')}")
        errors = last_errors()
        if errors:
            print("\nОшибки проверок:")
            for name, message in errors.items():
                print(f"  {name}: {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

"""
Проверки окружения: защита от remote-помощи, подмены видеопотока и подсказки в ухо.

Зачем это нужно. Вся CV-часть смотрит в камеру и доверяет тому, что видит.
Три класса обхода не ловятся кадром принципиально:

  1. Подмена потока. Студент ставит OBS Virtual Camera и отдаёт вместо живого
     видео заранее записанный ролик, где он честно смотрит в экран.
  2. Remote-помощь. За машиной сидит кто-то ещё — через AnyDesk/TeamViewer/VNC
     или просто в соседнем окне мессенджера, куда улетает скриншот задания.
  3. Подсказка в ухо. Наушник или гарнитура со связью: в кадре всё спокойно,
     а ответы студенту диктуют. Угроза У-02.

Все три случая видны не в кадре, а в состоянии операционной системы: список
устройств захвата, список процессов, число экранов, признаки гипервизора,
список активных устройств вывода звука.
Этот модуль — второй, независимый от камеры источник доказательств.

Про аудио отдельно (Р-10)
-------------------------
`check_audio_devices()` — это ЗАМЕНА анализа звука, а не дополнение к нему.
Кейс локальный: в компьютерном классе `VOICE_OTHER` и
`SPEECH_WITHOUT_LIP_MOTION` срабатывают на соседей и превращаются в генератор
ложных обвинений, поэтому в `exam_mode: "classroom"` аудио-канал выключен
целиком. Проверка устройств работает в ОБОИХ режимах и от шума в помещении не
зависит: подключённые наушники — это состояние ОС, а не оценка сигнала.

Граница проверки, которую нельзя забывать при ссылке на У-02: она видит только
устройства, подключённые К ЭТОМУ компьютеру. Гарнитура, спаренная с телефоном
студента, и наушник, воткнутый в телефон, системе не видны — ни одна проверка
модуля не смотрит за пределы экзаменационной машины. Этот путь закрывается
очным контролем в аудитории и fusion-связкой «длинная пауза -> мгновенный
развёрнутый ответ»; проверка устройств — дополнительный детерминированный
сигнал, а не полное закрытие вектора.

Как устроено
------------
`run_all_checks(config)` вызывает семь независимых проверок и возвращает
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
  * спаренные, но не подключённые Bluetooth-наушники не считаются подключёнными:
    в списке пар у любого ноутбука лежат десятки чужих устройств из прошлого;
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
    "check_audio_devices",
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
#: Аудио-устройства — как экраны: наушники могут воткнуть на середине экзамена,
#: и это надо увидеть на следующем цикле проверок, а не через полчаса.
_TTL_AUDIO = 6.0
#: Снимок устройств на старте живёт всю сессию: по нему видно, что появилось
#: во время экзамена. Сбрасывается только `reset_cache()`.
_TTL_BASELINE = 24 * 3600.0


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
#: Наушники и гарнитуры: совпадение по ИМЕНИ устройства вывода. Нужен, потому
#: что транспорт не всегда выдаёт наушники — проводные в разъёме идут как
#: «встроенные» и отличаются только названием («Внешние наушники»).
#: Имена приходят локализованными (macOS отдаёт их на языке системы), поэтому
#: в списке есть и кириллица. Марки, у которых одинаково называются и колонки,
#: и наушники (JBL, Soundcore, Marshall), сюда НЕ включены: лучше пропустить
#: такое устройство в слабый сигнал «внешний вывод», чем выдать ложное.
HEADPHONE_CATALOG: Catalog = (
    ("AirPods", r"\bair\s?pods?\b"),
    ("EarPods", r"\bear\s?pods?\b"),
    ("Beats", r"\bbeats\b|\bpowerbeats\b|\bbeatsx\b"),
    ("наушники", r"наушник|\bheadphones?\b|\bearphones?\b|\bear\s?buds?\b|\bbuds\b"),
    ("гарнитура", r"гарнитур|\bheadsets?\b|hands[\s-]?free|\bhsp\b|\bhfp\b"),
    # Имя активного выхода macOS приходит на языке системы, и «Внешние наушники»
    # на казахской локализации читается как «Сыртқы құлаққап». Кейс проводится
    # в университете имени Ахмет Байтұрсынұлы, то есть kk-локаль на машине жюри
    # вполне вероятна; ru/en-каталога для неё недостаточно. Язык-независимые
    # пути (Linux Active Port, Windows form_factor, смена имени встроенного
    # выхода) работают и без этой строки — она их дополняет, а не заменяет.
    ("құлаққап (kk)", r"құлақ\s?(қап|аспап)|кулакк?ап|құлақшын"),
    ("навушники (uk)", r"навушник|наушнык"),
    ("Kopfhörer (de)", r"kopfh(ö|oe)rer|ohrh(ö|oe)rer"),
    ("Kulaklık (tr)", r"kulakl[ıi][ğg][ıi]|kulakl[ıi]k"),
    ("耳机 / 이어폰 / イヤホン", r"耳机|耳機|イヤホン|이어폰|헤드셋"),
    ("écouteurs / auriculares", r"(é|e)couteur|auricular|aud(í|i)fono|cuffie|"
                                r"s[łl]uchawk|h(ö|o)rlurar|kuulokke|c(ă|a)[şs]ti"),
    ("Sony WH/WF", r"\bw[hf]-?1000\b|\bw[hf]-?c\d|\bwi-?c\d"),
    ("Bose", r"\bbose\b|quiet\s?comfort|\bqc\s?\d{2}\b|\bsoundsport\b"),
    ("Sennheiser", r"\bsennheiser\b|\bmomentum\s?(true\s?)?wireless\b"),
    ("Jabra", r"\bjabra\b|\bevolve\b|\belite\s?\d"),
    ("Poly / Plantronics", r"\bplantronics\b|\bpoly\s|\bvoyager\b|\bblackwire\b|\bsavi\b"),
    ("HyperX", r"\bhyperx\b|\bcloud\s?(ii|alpha|stinger)\b"),
    ("SteelSeries Arctis", r"\bsteel\s?series\b|\barctis\b"),
    ("Razer", r"\brazer\b|\bkraken\b|\bbarracuda\b|\bblackshark\b"),
    ("Logitech headset", r"\blogitech\s?(h\d|g\d|zone|astro)\b|\bastro\s?a\d{2}\b"),
    ("Corsair HS", r"\bcorsair\s|\bhs\d{2}\b|\bvoid\b|\bvirtuoso\b"),
    ("Galaxy / Pixel / FreeBuds", r"galaxy\s?buds|pixel\s?buds|free\s?buds|free\s?lace"),
    ("Nothing Ear", r"\bnothing\s?ear\b"),
    ("Redmi / Mi Buds", r"\b(redmi|mi|poco)\s?(air|buds)\b"),
    ("Soundcore Liberty", r"\bliberty\s?\d\b|\bsoundcore\s?(liberty|space|life)\b"),
)

#: Виртуальные аудио-устройства: маршрутизируют звук мимо динамика и мимо
#: наблюдения. Сами по себе они стоят у половины пользователей (Teams, Zoom,
#: Krisp), поэтому в находку попадают ТОЛЬКО когда выбраны активным выводом.
VIRTUAL_AUDIO_CATALOG: Catalog = (
    ("BlackHole", r"\bblack\s?hole\b"),
    ("Loopback", r"\bloopback\b"),
    ("Soundflower", r"\bsound\s?flower\b"),
    ("VB-Audio / VB-Cable", r"\bvb-?(audio|cable)\b|\bcable\s?(input|output)\b"),
    ("Virtual Audio Cable", r"virtual\s?audio\s?(cable|device)\b"),
    ("VoiceMeeter", r"\bvoice\s?meeter\b"),
    ("Audio Hijack / iShowU", r"\baudio\s?hijack\b|\bishowu\b|\binstant\s?on\b"),
    ("Krisp", r"\bkrisp\b"),
    ("Microsoft Teams Audio", r"teams\s?audio"),
    ("Zoom Audio Device", r"zoom\s?audio"),
    ("Elgato Wave Link", r"wave\s?link\b"),
    ("Discord Audio", r"discord\s?audio"),
    ("OBS Audio", r"obs\s?(virtual\s?)?audio"),
)

_RX_HEADPHONE = _compile(HEADPHONE_CATALOG)
_RX_VIRTUAL_AUDIO = _compile(VIRTUAL_AUDIO_CATALOG)

#: Linux: имя/тип порта вывода, который включается при подключении в разъём
#: 3.5 мм. Это jack sense от драйвера, а не название устройства, поэтому
#: сигнал одинаков на любой локализации системы.
_RX_LINUX_HEADPHONE_PORT = re.compile(
    r"analog-output-(headphones?|headset)|\bheadphones?\b|\bheadset\b|"
    r"\bhands[\s-]?free\b",
    re.IGNORECASE,
)

#: Слова строки из списков оператора (`allowed_audio_devices`,
#: `headphone_names`). Цифры нужны отдельно: модель гарнитуры это «h390»,
#: «wh-1000xm5», «evolve 65».
_RX_WORD = re.compile(r"[0-9a-zа-яёіїєґәғқңөұүһ]+", re.IGNORECASE)

#: Строка порта в `pactl list sinks`:
#: «\t\tanalog-output-headphones: Headphones (type: Headphones, ..., available)»
_RX_PACTL_PORT = re.compile(
    r"^\s+(?P<id>[A-Za-z0-9_.\-\[\]]+):\s+(?P<desc>.+?)\s*\((?P<attrs>[^()]*)\)\s*$"
)

#: Типы устройств Bluetooth, которые являются личным аудио-каналом.
#: `device_minorType` надёжнее имени: имя пользователь меняет как хочет
#: («etoevelina», «К♥»), а тип приходит из профиля устройства.
_BT_HEADPHONE_TYPES = frozenset({
    "headphones", "headset", "hands-free", "handsfree", "hands free",
    "earbuds", "earphones", "audio", "headphone",
})
#: Колонка — тоже внешний вывод, но звук в ней слышат все. Отдельный, слабый
#: сигнал: это не приватный канал подсказки.
_BT_SPEAKER_TYPES = frozenset({"speaker", "speakers", "loudspeaker"})

#: Транспорт CoreAudio -> человекочитаемое название для отчёта.
_AUDIO_TRANSPORT_RU: dict[str, str] = {
    "coreaudio_device_type_builtin": "встроенное",
    "coreaudio_device_type_usb": "USB",
    "coreaudio_device_type_bluetooth": "Bluetooth",
    "coreaudio_device_type_bluetooth_le": "Bluetooth LE",
    "coreaudio_device_type_bluetoothle": "Bluetooth LE",
    "coreaudio_device_type_virtual": "виртуальное",
    "coreaudio_device_type_aggregate": "агрегированное",
    "coreaudio_device_type_hdmi": "HDMI",
    "coreaudio_device_type_displayport": "DisplayPort",
    "coreaudio_device_type_airplay": "AirPlay",
    "coreaudio_device_type_pci": "PCI",
    "coreaudio_device_type_firewire": "FireWire",
    "coreaudio_device_type_thunderbolt": "Thunderbolt",
    "coreaudio_device_type_continuity_capture": "Continuity (iPhone)",
}

#: Form factor конечной точки Windows (PKEY_AudioEndpoint_FormFactor).
_WIN_FORM_FACTOR: dict[int, str] = {
    0: "сетевое устройство", 1: "динамики", 2: "линейный выход",
    3: "наушники", 4: "микрофон", 5: "гарнитура", 6: "телефонная трубка",
    7: "цифровой проход", 8: "S/PDIF", 9: "звук через дисплей",
    10: "тип не указан",
}

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
# 7. Аудио-устройства вывода (замена аудио-анализа; Р-10, угроза У-02)
# ===========================================================================
def _audio_devices() -> list[dict[str, Any]]:
    """Аудио-устройства системы с ролью, транспортом и признаком «активный вывод».

    Схема строки:
        name, transport, transport_raw, builtin, output, input,
        active_output (None — платформа не сообщает), source, manufacturer.
    """
    cached = _CACHE.get("audio_devices", _TTL_AUDIO)
    if cached is not None:
        return list(cached)

    devices: list[dict[str, Any]] = []
    try:
        if IS_MAC:
            devices.extend(_audio_devices_mac())
        elif IS_WIN:
            devices.extend(_audio_devices_win())
        elif IS_LINUX:
            devices.extend(_audio_devices_linux())
    except Exception:
        pass

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for dev in devices:
        key = (str(dev.get("name", "")).lower(), str(dev.get("transport_raw", "")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(dev)
    return _CACHE.put("audio_devices", unique)


def _audio_devices_mac() -> list[dict[str, Any]]:
    """macOS: `system_profiler -json SPAudioDataType`.

    Активный выход помечен `coreaudio_default_audio_output_device: spaudio_yes`
    (или `_properties: coreaudio_default_audio_system_device`). Имена приходят
    на языке системы — «Динамики MacBook Air», — поэтому встроенность
    определяется по транспорту, а не по названию.
    """
    data = _system_profiler("SPAudioDataType", _TTL_AUDIO)
    out: list[dict[str, Any]] = []
    for block in data or []:
        if not isinstance(block, dict):
            continue
        for item in block.get("_items") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("_name") or "").strip()
            if not name:
                continue
            transport_raw = str(item.get("coreaudio_device_transport") or "").strip()
            props = str(item.get("_properties") or "")
            default_out = str(item.get("coreaudio_default_audio_output_device") or "")
            system_out = str(item.get("coreaudio_default_audio_system_device") or "")
            out.append({
                "name": name,
                "transport": _AUDIO_TRANSPORT_RU.get(
                    transport_raw.lower(), transport_raw or "неизвестный интерфейс"),
                "transport_raw": transport_raw,
                "builtin": transport_raw.lower().endswith("builtin"),
                "output": bool(item.get("coreaudio_device_output")
                               or item.get("coreaudio_output_source")),
                "input": bool(item.get("coreaudio_device_input")
                              or item.get("coreaudio_input_source")),
                "active_output": (default_out.endswith("yes")
                                  or system_out.endswith("yes")
                                  or "default_audio_system_device" in props),
                "manufacturer": str(item.get("coreaudio_device_manufacturer") or "") or None,
                "source": "system_profiler",
            })
    return out


def _audio_devices_win() -> list[dict[str, Any]]:
    """Windows: конечные точки вывода из реестра MMDevices.

    Берём только `DeviceState == 1` (устройство активно и подключено). Form
    factor (`3` — наушники, `5` — гарнитура) и шина (`BTHENUM` — Bluetooth)
    приходят прямо из свойств конечной точки, то есть это не догадка по имени.

    Честное ограничение: какое из устройств выбрано системным выводом, реестр
    надёжно не сообщает, поэтому `active_output` здесь None, и решение строится
    на типе устройства, а не на маршруте звука.
    """
    if not IS_WIN:
        return []
    try:
        import winreg  # type: ignore
    except Exception:
        return []

    # PKEY_* конечной точки: имя, описание, form factor, имя перечислителя шины.
    key_friendly = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
    key_desc = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
    key_form = "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},0"
    key_bus = "{a45c254e-df1c-4efd-8020-67d146a850e0},24"

    base = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render")
    out: list[dict[str, Any]] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except Exception:
        return []
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
                    state = 0
                    try:
                        state = int(winreg.QueryValueEx(node, "DeviceState")[0])
                    except Exception:
                        state = 0
                    if state != 1:  # 2 — отключено, 4 — отсутствует, 8 — не воткнуто
                        continue
                    values: dict[str, Any] = {}
                    with winreg.OpenKey(node, "Properties") as props:
                        for prop in (key_friendly, key_desc, key_form, key_bus):
                            try:
                                values[prop] = winreg.QueryValueEx(props, prop)[0]
                            except Exception:
                                continue
            except Exception:
                continue

            name = str(values.get(key_friendly) or values.get(key_desc) or "").strip()
            if not name:
                continue
            try:
                form = int(values.get(key_form, 10))
            except Exception:
                form = 10
            bus = str(values.get(key_bus) or "").strip()
            bus_l = bus.lower()
            if "bthenum" in bus_l or "bthhfenum" in bus_l:
                transport = "Bluetooth"
            elif "usb" in bus_l:
                transport = "USB"
            elif "hdaudio" in bus_l:
                transport = "встроенное"
            else:
                transport = bus or "неизвестный интерфейс"
            out.append({
                "name": name,
                "transport": transport,
                "transport_raw": bus,
                "builtin": "hdaudio" in bus_l and form in (1, 2),
                "output": True,
                "input": False,
                "active_output": None,   # реестр не сообщает текущий вывод
                "form_factor": _WIN_FORM_FACTOR.get(form, "тип не указан"),
                "form_factor_id": form,
                "manufacturer": None,
                "source": "registry",
            })
    finally:
        try:
            root.Close()
        except Exception:
            pass
    return out


def _linux_transport(sink_name: str) -> str:
    """Транспорт по имени sink'а PulseAudio/PipeWire."""
    low = str(sink_name or "").lower()
    if "bluez" in low or "bluetooth" in low:
        return "Bluetooth"
    if "usb" in low:
        return "USB"
    if "hdmi" in low:
        return "HDMI"
    if "pci" in low or "analog" in low:
        return "встроенное"
    return "неизвестный интерфейс"


def _pactl_sinks() -> list[dict[str, Any]]:
    """`pactl list sinks` -> [{name, description, active_port, ports}].

    Нужна именно подробная форма. `pactl list short sinks` не содержит
    `Active Port`, а при подключении наушников в разъём 3.5 мм меняется ТОЛЬКО
    он: имя sink'а, транспорт и `Default Sink` остаются прежними. Без этого
    разбора проводная гарнитура на Linux не видна вообще — а Linux это и есть
    платформа компьютерных классов.

    `_run` выставляет `LC_ALL=C`, поэтому ключи и типы портов приходят на
    английском независимо от языка системы.
    """
    out: list[dict[str, Any]] = []
    text = _run(["pactl", "list", "sinks"], timeout=_T_FAST)
    if not text:
        return out
    cur: dict[str, Any] | None = None
    in_ports = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if not line[0].isspace():                      # «Sink #0»
            if cur is not None:
                out.append(cur)
            cur = ({"name": "", "description": "", "active_port": "",
                    "active_port_label": "", "ports": {}}
                   if line.lower().startswith("sink") else None)
            in_ports = False
            continue
        if cur is None:
            continue
        stripped = line.strip()
        low = stripped.lower()
        if low == "ports:":
            in_ports = True
            continue
        if in_ports:
            match = _RX_PACTL_PORT.match(line)
            if match is not None:
                attrs = match.group("attrs").lower()
                cur["ports"][match.group("id")] = {
                    "description": match.group("desc"),
                    # «not available» = в разъёме ничего нет (jack sense)
                    "available": "not available" not in attrs
                                 and "unavailable" not in attrs,
                    "attrs": match.group("attrs"),
                }
                continue
            in_ports = False                            # секция портов кончилась
        if low.startswith("name:"):
            cur["name"] = stripped.split(":", 1)[1].strip()
        elif low.startswith("description:"):
            cur["description"] = stripped.split(":", 1)[1].strip()
        elif low.startswith("active port:"):
            cur["active_port"] = stripped.split(":", 1)[1].strip()
    if cur is not None:
        out.append(cur)
    for sink in out:
        port = str(sink.get("active_port") or "")
        info = (sink.get("ports") or {}).get(port) or {}
        sink["active_port_label"] = str(info.get("description") or port)
    return out


def _alsa_jack_headphones() -> list[str]:
    """Jack sense через `amixer`: элементы «Headphone Jack» со значением on.

    Фолбэк для машин без PulseAudio/PipeWire. Сигнал тот же, что и
    `Active Port` у pactl — он приходит от драйвера, а не из имени устройства,
    поэтому от локализации не зависит.
    """
    root = Path("/proc/asound")
    if not root.is_dir():
        return []
    found: list[str] = []
    try:
        cards = sorted(root.glob("card[0-9]*"))
    except Exception:
        return []
    for card in cards[:4]:
        index = card.name[4:]
        text = _run(["amixer", "-c", index, "contents"], timeout=_T_FAST) or ""
        control = ""
        for line in text.splitlines():
            low = line.strip().lower()
            if low.startswith("numid="):
                match = re.search(r"name='([^']*)'", line)
                control = match.group(1) if match else ""
                continue
            if not control:
                continue
            if "values=on" in low.replace(" ", ""):
                if ("jack" in control.lower()
                        and _RX_LINUX_HEADPHONE_PORT.search(control)):
                    found.append(f"{control} (card {index})")
                control = ""
    return found


def _audio_devices_linux() -> list[dict[str, Any]]:
    """Linux: выходы PulseAudio/PipeWire (`pactl`), иначе карты ALSA.

    Ключевой момент — `Active Port`. Проводные наушники в разъёме 3.5 мм не
    создают нового sink'а и не меняют `Default Sink`: ядро переключает только
    активный порт на `analog-output-headphones`. Этот факт и есть подключение
    (У-02); он приходит от драйвера, поэтому читается одинаково на русской,
    английской и казахской локализации.

    Признак уезжает в строку устройства как `form_factor: "наушники"` — ровно
    то поле, которое `check_audio_devices()` уже читает для Windows, где тип
    конечной точки тоже сообщает сама система.
    """
    if not IS_LINUX:
        return []
    out: list[dict[str, Any]] = []
    default_sink = ""
    info = _run(["pactl", "info"], timeout=_T_FAST)
    for line in (info or "").splitlines():
        if line.lower().startswith("default sink:"):
            default_sink = line.split(":", 1)[1].strip()
            break

    for sink in _pactl_sinks():
        sink_name = str(sink.get("name") or "")
        if not sink_name:
            continue
        port = str(sink.get("active_port") or "")
        port_label = str(sink.get("active_port_label") or "")
        jack = bool(_RX_LINUX_HEADPHONE_PORT.search(f"{port} {port_label}"))
        transport = _linux_transport(sink_name)
        label = str(sink.get("description") or sink_name)
        out.append({
            "name": f"{label} — {port_label}" if port_label else label,
            "transport": transport,
            "transport_raw": sink_name,
            "builtin": transport == "встроенное",
            "output": True,
            "input": False,
            "active_output": bool(default_sink and sink_name == default_sink),
            "form_factor": "наушники" if jack else "",
            "active_port": port,
            "active_port_label": port_label,
            "jack_sense": jack,
            "manufacturer": None,
            "source": "pactl",
        })
    if out:
        return out

    # Подробная форма недоступна (старый pactl, нет прав) — короткая лучше, чем
    # ничего, но наушники в разъёме в ней не видны: это записано в LIMITATIONS.
    sinks = _run(["pactl", "list", "short", "sinks"], timeout=_T_FAST)
    for line in (sinks or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        sink = parts[1]
        transport = _linux_transport(sink)
        out.append({
            "name": sink, "transport": transport, "transport_raw": sink,
            "builtin": transport == "встроенное", "output": True, "input": False,
            "active_output": bool(default_sink and sink == default_sink),
            "manufacturer": None, "source": "pactl-short",
        })
    if out:
        return out

    jacks = _alsa_jack_headphones()
    cards = Path("/proc/asound/cards")
    if cards.is_file():
        try:
            text = cards.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
        for line in text.splitlines():
            if "]:" not in line:
                continue
            name = line.split("]:", 1)[1].strip()
            if not name:
                continue
            low = name.lower()
            out.append({
                "name": name,
                "transport": "USB" if "usb" in low else "встроенное",
                "transport_raw": "alsa",
                "builtin": "usb" not in low, "output": True, "input": False,
                "active_output": None, "manufacturer": None, "source": "alsa",
            })
    for jack in jacks:
        out.append({
            "name": jack,
            "transport": "встроенное",
            "transport_raw": "alsa-jack",
            "builtin": True, "output": True, "input": False,
            "active_output": True,
            "form_factor": "наушники",
            "jack_sense": True,
            "manufacturer": None, "source": "amixer",
        })
    return out


def _bluetooth_audio_devices() -> list[dict[str, Any]]:
    """Подключённые СЕЙЧАС Bluetooth-аудиоустройства (macOS).

    Разбирается только `device_connected` (и строки с явным
    `device_isconnected: attrib_Yes`). Список спаренных устройств сознательно
    НЕ читается: у любого ноутбука там десятки чужих наушников из прошлого, и
    в отчёте это были бы и ложная улика, и чужие персональные данные.

    Тип берётся из `device_minorType` — имя устройства пользователь меняет
    произвольно, тип приходит из профиля.
    """
    if not IS_MAC:
        return []
    cached = _CACHE.get("bt_audio", _TTL_AUDIO)
    if cached is not None:
        return list(cached)

    data = _system_profiler("SPBluetoothDataType", _TTL_AUDIO)
    rows: list[dict[str, Any]] = []
    paired_audio = 0
    for block in data or []:
        if not isinstance(block, dict):
            continue
        for list_key, entries in block.items():
            if not str(list_key).startswith("device_") or not isinstance(entries, list):
                continue
            connected_list = str(list_key) == "device_connected"
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                for name, props in entry.items():
                    if not isinstance(props, dict):
                        continue
                    minor = str(props.get("device_minorType") or "").strip().lower()
                    major = str(props.get("device_majorType") or "").strip().lower()
                    is_audio = (minor in _BT_HEADPHONE_TYPES
                                or minor in _BT_SPEAKER_TYPES
                                or "audio" in major)
                    if not is_audio:
                        continue
                    flag = str(props.get("device_isconnected") or "").strip().lower()
                    connected = connected_list or flag.endswith("yes")
                    if not connected:
                        paired_audio += 1
                        continue
                    rows.append({
                        "name": str(name).strip() or "Bluetooth-устройство",
                        "minor_type": minor or (major or "audio"),
                        "speaker": minor in _BT_SPEAKER_TYPES,
                        "address": props.get("device_address"),
                        "source": "bluetooth",
                    })
    _CACHE.put("bt_audio_paired_count", paired_audio)
    return _CACHE.put("bt_audio", rows)


def _match_catalog(name: str, catalog: tuple[tuple[str, re.Pattern[str]], ...]
                   ) -> tuple[str, str] | None:
    """Первое совпадение имени устройства с каталогом -> (метка, шаблон)."""
    haystack = str(name or "")
    for label, rx in catalog:
        if rx.search(haystack):
            return label, rx.pattern
    return None


def _audio_signature(dev: dict[str, Any]) -> str:
    """Стабильный ключ устройства для сравнения с базовой линией.

    Активный порт входит в ключ намеренно: на Linux подключение в разъём
    3.5 мм не создаёт нового sink'а, меняется только порт — без него
    «появилось во время экзамена» для проводной гарнитуры не определить.
    """
    name = str(dev.get("name") or "").lower()
    port = str(dev.get("active_port") or "")
    return f"{name}|{dev.get('transport_raw', '')}|{port}"


def _audio_baseline(signatures: set[str]) -> set[str]:
    """Устройства, которых не было на первом прогоне проверки.

    Первый вызов запоминает снимок и возвращает пустое множество: то, что уже
    подключено на старте, — это состояние рабочего места, а не событие.
    Дальше любое новое устройство отмечается как появившееся во время экзамена;
    снимок при этом не обновляется, иначе отметка исчезла бы на следующем цикле.
    """
    baseline = _CACHE.get("audio_baseline", _TTL_BASELINE)
    if baseline is None:
        _CACHE.put("audio_baseline", set(signatures))
        _CACHE.put("audio_baseline_ts", time.time())
        return set()
    return {sig for sig in signatures if sig not in baseline}


def _audio_session_minutes() -> float | None:
    """Сколько минут прошло с первого прогона проверки. None — прогон первый."""
    started = _CACHE.get("audio_baseline_ts", _TTL_BASELINE)
    if started is None:
        return None
    try:
        return max(0.0, (time.time() - float(started)) / 60.0)
    except (TypeError, ValueError):
        return None


def _builtin_output_change(outputs: list[dict[str, Any]]) -> str | None:
    """Имя активного ВСТРОЕННОГО выхода изменилось с первого прогона.

    Язык-независимый признак переключения на разъём 3.5 мм на macOS. Имя
    выхода приходит на языке системы («Динамики MacBook Air» ->
    «Внешние наушники» -> «Сыртқы құлаққап»), а сам факт смены имени при
    неизменном транспорте — нет. Нужен потому, что для встроенного выхода
    фолбэк «активно и не встроенное» не берётся (`builtin=True`), и опора
    остаётся только на каталог имён.

    Возвращает новое имя активного встроенного выхода или None.
    """
    active = next((d for d in outputs
                   if d.get("active_output") and d.get("builtin")), None)
    name = str(active.get("name") or "").strip() if active else ""
    stored = _CACHE.get("audio_builtin_active", _TTL_BASELINE)
    if stored is None:
        _CACHE.put("audio_builtin_active", name)
        return None
    if name and str(stored) and name.lower() != str(stored).lower():
        return name
    return None


def _name_matches(haystack_low: str, needle_low: str) -> bool:
    """Строка оператора против описания устройства: подстрока ИЛИ все слова.

    Имена устройств в ОС содержат лишние слова («Logitech USB Headset H390»,
    «Sony WH-1000XM5»), а оператор пишет «logitech h390» или «sony wh 1000».
    Проверка только по непрерывной подстроке такие записи не находит, то есть
    список оператора молча не работает. Требуются ВСЕ слова, а не любое.
    """
    if not needle_low:
        return False
    if needle_low in haystack_low:
        return True
    words = _RX_WORD.findall(needle_low)
    return bool(words) and all(word in haystack_low for word in words)


def _audio_device_allowed(dev: dict[str, Any],
                          patterns: Sequence[str]) -> str | None:
    """Устройство из списка `env.allowed_audio_devices` -> совпавшая строка.

    Сравниваются имя, производитель, транспорт и тип конечной точки — все эти
    поля уже лежат в строке `_audio_devices()`. Регистр не важен, регулярных
    выражений оператор не пишет.

    Совпадением считается ЛЮБОЕ из двух:

    * строка целиком встречается в описании устройства («usb», «logitech»);
    * ВСЕ слова строки встречаются в описании, пусть и не рядом. Это главный
      случай: ОС отдаёт «Logitech USB Headset H390», а оператор пишет в список
      «logitech h390» — то, как гарнитура называется в накладной. Проверка по
      непрерывной подстроке такую запись молча не находила, и выданная
      преподавателем гарнитура продолжала давать инцидент при заполненном
      списке разрешённых.

    Требуются все слова, а не любое: «logitech h390» не должен разрешать
    чужую гарнитуру только потому, что она тоже Logitech.
    """
    if not patterns:
        return None
    haystack = " ".join(
        str(dev.get(key) or "") for key in
        ("name", "manufacturer", "transport", "transport_raw", "form_factor",
         "match", "minor_type")
    ).lower()
    for pattern in patterns:
        needle = str(pattern).strip().lower()
        if not needle:
            continue
        if _name_matches(haystack, needle):
            return str(pattern).strip()
    return None


#: Вид устройства -> (контекст, действие). Текст события собирается из этих
#: двух строк, поэтому «Попросите отключить устройство» не прилетает монитору
#: по HDMI, а виртуальному выводу достаётся исполнимое действие. Формула гайда:
#: наблюдаемый факт -> контекст -> понятное действие; оценок и обвинений нет.
_AUDIO_REASONS: dict[str, tuple[str, str]] = {
    "headphones": (
        "к компьютеру подключена гарнитура или наушники — звук экзамена "
        "слышит только студент",
        "Попросите отключить устройство и продолжить со встроенным динамиком",
    ),
    "virtual": (
        "вывод звука направлен в виртуальное аудио-устройство — куда уходит "
        "звук экзамена, системе не видно",
        "Попросите переключить вывод звука на встроенный динамик",
    ),
    "speaker": (
        "активна внешняя аудио-колонка — звук экзамена выводится за пределы "
        "компьютера",
        "Проверьте, допустим ли внешний вывод звука по условиям экзамена",
    ),
    "external": (
        "активное устройство вывода звука — не встроенный динамик, звук "
        "экзамена уходит на внешнее устройство",
        "Проверьте, допустим ли внешний вывод звука по условиям экзамена",
    ),
}


def check_audio_devices(config: Any = None) -> list[EnvFinding]:
    """Наушники и гарнитуры: личный аудио-канал во время экзамена (У-02).

    Это ЗАМЕНА анализа звука, а не дополнение к нему (Р-10). Анализ сигнала в
    аудитории меряет помещение, а не студента; состояние ОС от шума не зависит
    вообще. Поэтому проверка работает в обоих режимах `exam_mode` и не требует
    микрофона.

    ЧТО ЭТА ПРОВЕРКА ВИДИТ И ЧЕГО НЕ ВИДИТ — читать до того, как ссылаться
    на неё как на закрытие У-02:

    * видит устройства, подключённые К ЭКЗАМЕНАЦИОННОМУ КОМПЬЮТЕРУ: BT-пару с
      этим ноутбуком, USB-гарнитуру, виртуальный вывод, разъём 3.5 мм
      (macOS — по имени и по смене имени активного встроенного выхода;
      Linux — по `Active Port`; Windows — по типу конечной точки);
    * НЕ видит гарнитуру, спаренную с телефоном студента, и наушник, воткнутый
      в телефон: за пределы этой машины не смотрит ни одна проверка модуля. Это
      не остаточный риск, а отдельный канал: он закрывается очным контролем в
      аудитории и fusion-связкой «длинная пауза -> мгновенный развёрнутый
      ответ», а на самом экзаменационном компьютере канал связи независимо
      ловит `check_blacklisted_processes` (мессенджеры);
    * ложные срабатывания возможны и здесь: внешняя колонка, звук через HDMI
      проектора, выданная преподавателем гарнитура. Поэтому сила сигнала
      градуирована через `conf`, а не сведена к «инцидент / не инцидент», и
      есть список разрешённых устройств.

    Что считается сигналом, от сильного к слабому:

    1. наушники или гарнитура активны как вывод и ПОЯВИЛИСЬ во время сессии
       (0.95) — звук экзамена идёт в ухо студента и больше никуда;
    2. подключённое во время сессии Bluetooth-аудиоустройство типа
       Headphones/Headset (0.9);
    3. то же, но устройство было подключено ДО старта (0.7): это состояние
       рабочего места, решение принимает человек, а не система;
    4. активный вывод — виртуальное аудио-устройство (0.7-0.75): куда уходит
       звук, системе не видно;
    5. наушники подключены, но вывод пока на встроенный динамик (0.5-0.6) —
       переключение занимает один клик;
    6. активный вывод — любое другое не встроенное устройство (0.45-0.5):
       внешняя колонка или звук через HDMI слышны всей аудитории.

    Признак «появилось во время сессии» теперь РАЗВОДИТ случаи по весу, а не
    добавляет 0.05: «забыл наушники в разъёме с утра» не должно само по себе
    пробивать порог предупреждения, а «подключил на середине экзамена» —
    должно. Признак уходит и в текст события отдельной фразой, не только в
    `detail`.

    Две независимые ручки на случай, когда звук разрешён:

    * `env.allowed_audio_devices` — список разрешённых устройств по имени,
      производителю или транспорту («logitech h390», «usb»). Проверяется ПОСЛЕ
      перечисления и снимает только совпавшие устройства: выданная
      преподавателем гарнитура перестаёт быть инцидентом, а виртуальный вывод
      и чужие AirPods рядом — остаются;
    * `env.allow_headphones` — разрешить наушники как класс (аудирование в
      языковом тесте). Гасит только наушники и колонки; виртуальное
      аудио-устройство и внешний вывод продолжают фиксироваться — иначе один
      флаг выключал бы заодно детект подмены аудио-маршрута.
    """
    try:
        started = time.perf_counter()
        if not _cfg_bool(config, "env", "check_audio_devices", True):
            return []
        allow_headphones = _cfg_bool(config, "env", "allow_headphones", False)
        allowed_patterns = _cfg_list(config, "env", "allowed_audio_devices")

        devices = _audio_devices()
        bt_rows = _bluetooth_audio_devices()
        extra_words = [w.strip().lower() for w in _cfg_list(config, "env", "headphone_names")
                       if w.strip()]

        outputs = [d for d in devices if d.get("output")]
        signatures = {_audio_signature(d) for d in outputs}
        signatures |= {f"bt:{str(r.get('name','')).lower()}" for r in bt_rows}
        fresh = _audio_baseline(signatures)
        # смена имени активного встроенного выхода = переключение на разъём,
        # сигнал не зависит от языка системы (см. `_builtin_output_change`)
        jack_switch = _builtin_output_change(outputs)

        matched: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        active_name: str | None = None

        for dev in outputs:
            name = str(dev.get("name") or "")
            low = name.lower()
            active = bool(dev.get("active_output"))
            builtin = bool(dev.get("builtin"))
            if active:
                active_name = name

            hit = _match_catalog(name, _RX_HEADPHONE)
            virtual = _match_catalog(name, _RX_VIRTUAL_AUDIO)
            # то же правило сравнения, что и у списка разрешённых: оператор
            # пишет «sony wh 1000», а ОС отдаёт «Sony WH-1000XM5»
            custom = next((w for w in extra_words if _name_matches(low, w)), None)
            form = str(dev.get("form_factor") or "")

            kind = ""
            label = ""
            pattern = ""
            if hit:
                kind, label, pattern = "headphones", hit[0], hit[1]
            elif custom:
                kind, label, pattern = "headphones", "список проктора", custom
            elif form in ("наушники", "гарнитура"):
                # Тип конечной точки сообщает сама система: Windows —
                # form_factor реестра, Linux — `Active Port` драйвера.
                kind, label, pattern = "headphones", form, "form_factor"
            elif jack_switch and active and builtin and name == jack_switch:
                # Имя активного встроенного выхода сменилось: так выглядит
                # подключение в разъём 3.5 мм на любой локализации.
                kind = "headphones"
                label = "смена активного встроенного выхода"
                pattern = "builtin_output_changed"
            elif virtual and active:
                kind, label, pattern = "virtual", virtual[0], virtual[1]
            elif active and not builtin:
                kind, label = "external", dev.get("transport") or "внешний интерфейс"
            else:
                continue

            signature = _audio_signature(dev)
            appeared = signature in fresh or pattern == "builtin_output_changed"

            # Разрешённые устройства снимаются ПОСЛЕ перечисления и только те,
            # что совпали: виртуальный аудио-маршрут не разрешает ни одна ручка.
            allowed_hit = _audio_device_allowed(dev, allowed_patterns)
            if allowed_hit and kind != "virtual":
                skipped.append({"name": name, "kind": kind,
                                "reason": "allowed_audio_devices",
                                "rule": allowed_hit})
                continue
            if allow_headphones and kind in ("headphones", "speaker"):
                skipped.append({"name": name, "kind": kind,
                                "reason": "allow_headphones", "rule": "env.allow_headphones"})
                continue

            if kind == "headphones":
                # Разница между «подключил во время экзамена» и «забыл
                # в разъёме с утра» должна быть видна в весе, а не в detail.
                conf = (0.95 if appeared else 0.7) if active else (0.6 if appeared else 0.5)
            elif kind == "virtual":
                conf = 0.75 if appeared else 0.7
            else:
                conf = 0.5 if appeared else 0.45

            row = dict(dev)
            row.update({
                "match": label,
                "pattern": pattern,
                "kind": kind,
                "conf": round(conf, 2),
                "active_output": dev.get("active_output"),
                "appeared_during_exam": appeared,
            })
            matched.append(row)

        # Bluetooth: устройство может быть подключено и держать HFP-канал,
        # даже если системный вывод остался на динамике ноутбука.
        known = {str(r.get("name", "")).lower() for r in matched}
        for bt in bt_rows:
            name = str(bt.get("name") or "")
            low = name.lower()
            if low in known:
                continue
            speaker = bool(bt.get("speaker"))
            kind = "speaker" if speaker else "headphones"
            allowed_hit = _audio_device_allowed(dict(bt, transport="Bluetooth"),
                                                allowed_patterns)
            if allowed_hit:
                skipped.append({"name": name, "kind": kind,
                                "reason": "allowed_audio_devices", "rule": allowed_hit})
                continue
            if allow_headphones:
                skipped.append({"name": name, "kind": kind,
                                "reason": "allow_headphones", "rule": "env.allow_headphones"})
                continue
            appeared = f"bt:{low}" in fresh
            if speaker:
                conf = 0.5 if appeared else 0.45
            else:
                conf = 0.9 if appeared else 0.7
            matched.append({
                "name": name,
                "transport": "Bluetooth",
                "transport_raw": "bluetooth",
                "builtin": False,
                "output": True,
                "input": None,
                "active_output": None,
                "kind": "speaker" if speaker else "headphones",
                "match": f"Bluetooth: {bt.get('minor_type')}",
                "pattern": "device_minorType",
                "conf": round(conf, 2),
                "appeared_during_exam": appeared,
                "source": "bluetooth",
            })

        if not matched:
            return []

        matched.sort(key=lambda r: (-float(r.get("conf") or 0.0),
                                    not bool(r.get("active_output"))))
        conf = max(float(r.get("conf") or 0.0) for r in matched)
        appeared_any = any(r.get("appeared_during_exam") for r in matched)
        kinds = {str(r.get("kind")) for r in matched}

        phrases: list[str] = []
        for row in matched[:4]:
            bits = [str(row.get("transport") or "")]
            if row.get("active_output"):
                bits.append("активный вывод")
            elif row.get("active_output") is None:
                bits.append("подключено")
            else:
                bits.append("подключено, вывод на встроенный динамик")
            if row.get("appeared_during_exam"):
                bits.append("появилось во время экзамена")
            phrases.append(f"«{row.get('name')}» ({', '.join(b for b in bits if b)})")
        summary = "; ".join(phrases)

        # Формула гайда: наблюдаемый факт -> контекст -> понятное действие.
        # `reason` и `advice` градуированы по виду устройства и подставляются
        # в текст события движком: иначе и HDMI-монитор, и BlackHole получали
        # бы одно предложение про гарнитуру и неисполнимое «отключите».
        # Обвинений ни в одной строке быть не должно.
        primary = next((k for k in ("headphones", "virtual", "speaker", "external")
                        if k in kinds), "external")
        reason, advice = _AUDIO_REASONS[primary]

        minutes = _audio_session_minutes()
        if appeared_any:
            when = "устройство появилось во время сессии"
            if minutes is not None and minutes >= 1.0:
                when += f", через {int(minutes)} мин от старта"
        else:
            when = ("устройство было подключено до начала сессии: это состояние "
                    "рабочего места, а не событие экзамена")

        if conf >= 0.9:
            severity = Severity.HIGH
        elif conf >= 0.6:
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW

        head = matched[0]
        detail: dict[str, Any] = {
            "conf": round(conf, 2),
            "summary": summary,
            # reason / when / advice подставляются в текст события движком
            # ({reason_sentence}, {when_sentence}, {advice_sentence})
            "reason": reason,
            "when": when,
            "advice": advice,
            "kind": primary,
            # `name` подставляется в текст события движком ({name_colon})
            "name": f"{head.get('name')} ({head.get('transport')})",
            "devices_matched": matched,
            "devices_allowed": skipped,
            "allow_headphones": allow_headphones,
            "allowed_audio_devices": list(allowed_patterns),
            "active_output": active_name,
            "appeared_during_exam": appeared_any,
            "session_minutes": None if minutes is None else round(minutes, 1),
            "jack_switch": jack_switch,
            "all_output_devices": [str(d.get("name")) for d in outputs],
            "bluetooth_connected": [
                {"name": r.get("name"), "type": r.get("minor_type")} for r in bt_rows
            ],
            # только количество: имена спаренных, но не подключённых устройств
            # в отчёт не попадают (чужие персональные данные, см. докстринг)
            "bluetooth_paired_audio_ignored": _CACHE.get(
                "bt_audio_paired_count", _TTL_AUDIO) or 0,
            "exam_mode": str(_cfg(config, "env", "exam_mode", "") or ""),
            "platform": sys.platform,
            "check": "check_audio_devices",
            "check_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return [EnvFinding(EventKind.AUDIO_DEVICE_CONNECTED, detail, severity)]
    except Exception as exc:
        return _self_error("check_audio_devices", exc)


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
    # Процессов не касается, поэтому место в порядке дедупликации неважно.
    ("audio_devices", check_audio_devices),
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
        "evidence", "vendors", "bluetooth_connected"))

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
        # Работает в обоих exam_mode: это чтение состояния ОС, а не анализ звука.
        {"check": "audio_devices", "kind": EventKind.AUDIO_DEVICE_CONNECTED.value,
         "ready": IS_MAC or IS_WIN or IS_LINUX, "needs_psutil": False},
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
    parser.add_argument("--audio-devices", action="store_true",
                        help="показать, что видит проверка аудио-устройств, "
                             "даже если находок нет")
    parser.add_argument("--exam-mode", "--mode", dest="exam_mode", default=None,
                        choices=["classroom", "remote"],
                        help="режим развёртывания (на эту проверку не влияет, "
                             "попадает в detail для отчёта)")
    args = parser.parse_args()

    config: dict[str, Any] = {"env": {
        "allow_multiple_displays": bool(args.allow_multi_display),
    }}
    try:
        from config import ProctorConfig  # конфиг проекта, если он доступен
        cfg, _warnings = ProctorConfig.load(argv=[])
        if args.exam_mode:
            cfg.set_exam_mode(args.exam_mode)
        config = cfg.to_dict()
        if args.allow_multi_display:
            config.setdefault("env", {})["allow_multiple_displays"] = True
    except Exception:
        pass

    if args.audio_devices:
        devices = _audio_devices()
        bt = _bluetooth_audio_devices()
        if args.json:
            print(json.dumps({"audio_devices": devices, "bluetooth_connected": bt},
                             ensure_ascii=False, indent=2))
        else:
            print(f"Аудио-устройства системы ({len(devices)}):")
            for dev in devices:
                roles = ", ".join(r for r in (
                    "вывод" if dev.get("output") else "",
                    "вход" if dev.get("input") else "") if r)
                flags = ", ".join(f for f in (
                    "встроенное" if dev.get("builtin") else "не встроенное",
                    "АКТИВНЫЙ ВЫВОД" if dev.get("active_output") else "") if f)
                print(f"  «{dev.get('name')}» — {dev.get('transport')}; "
                      f"{roles or 'роль не указана'}; {flags}")
            print(f"Подключённые Bluetooth-аудиоустройства: "
                  f"{', '.join(str(r.get('name')) for r in bt) or 'нет'}")
            print()

    started = time.perf_counter()
    findings = run_all_checks(config)
    elapsed = (time.perf_counter() - started) * 1000

    if args.json:
        print(json.dumps([f.to_dict() for f in findings], ensure_ascii=False, indent=2))
    else:
        print(f"Платформа: {sys.platform}; проверок: {len(_CHECKS)}; "
              f"psutil: {'да' if _psutil() is not None else 'нет'}; "
              f"режим: {_cfg(config, 'env', 'exam_mode', 'не задан')}; "
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

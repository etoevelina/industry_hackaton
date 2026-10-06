"""
Единая конфигурация сайдкара.

Все числа, пороги, частоты и пути живут здесь. Детекторы и движки получают
конфиг как плоский dict (`ProctorConfig.to_dict()`) — так требует контракт
(`__init__(self, config: dict)`), поэтому имена полей здесь же являются
именами ключей конфига для соседних модулей.

Переопределение: файл `config.json` в корне репозитория (или путь из --config).
Поддерживаются как плоские ключи, так и вложенные секции — вложенность просто
разворачивается по именам полей:

    {"frame_width": 1280, "capture": {"target_fps": 10}, "risk": {"risk_half_life": 60}}

Неизвестные ключи игнорируются, их список возвращает `apply()` — вызывающий
пишет это в лог, но не падает.

РЕЖИМ РАЗВЁРТЫВАНИЯ `exam_mode` (решение Р-10)
----------------------------------------------
    "classroom" — компьютерный класс, значение по умолчанию;
    "remote"    — сдача из дома, тихая комната.

В `classroom` аудио-анализ выключен ЦЕЛИКОМ: `AudioMonitor` не стартует и микрофон
не захватывается, правила `VOICE_OTHER` и `SPEECH_WITHOUT_LIP_MOTION` в движке не
регистрируются. Причина: в аудитории на 30 человек чужая речь есть всегда, и оба
правила превращаются в генератор ложных обвинений (журнал решений, Р-10).
Угроза У-02 «наушник со связью» закрыта вместо этого детерминированной проверкой
`env_checks.check_audio_devices()` — она работает в обоих режимах.

Приоритет источников режима (далее — важнее):
    значение по умолчанию -> config.json -> переменная окружения
    PROCTOR_EXAM_MODE -> флаг CLI (`--exam-mode remote`, `--remote`, `--classroom`).

Флаг читается двумя путями. Владелец argparse (например `sidecar/main.py`) вызывает
`ProctorConfig.add_cli_arguments(parser)` и затем `cfg.apply_cli_args(args)`. Кроме
того `load()` сам просматривает `sys.argv` на предмет этих флагов, игнорируя всё
остальное, — чтобы режим не зависел от того, какая точка входа запущена. Чтобы
отключить просмотр argv, передайте `load(argv=[])`.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from protocol import (  # noqa: E402  (путь настраиваем выше)
    DEFAULT_WS_HOST,
    DEFAULT_WS_PORT,
    RISK_LOCK,
    RISK_PAUSE,
    RISK_WARN,
    EventKind,
)

#: Корень репозитория: sidecar/config.py -> sidecar -> корень.
ROOT = _HERE.parent

#: Имя файла с переопределениями, которое ищется в корне репозитория.
CONFIG_FILENAME = "config.json"

#: Имена секций, которые читают соседние модули. Из config.json такие ключи
#: передаются их конструкторам как есть (см. ProctorConfig.module_overrides).
SECTION_NAMES = (
    "capture", "objects", "face_mesh", "face", "gaze", "vision", "identity",
    "liveness", "audio", "env", "events", "risk", "fusion", "storage", "report",
)

# ===========================================================================
# Режим развёртывания (Р-10)
# ===========================================================================
#: Компьютерный класс. Значение по умолчанию: кейс — ЛОКАЛЬНЫЙ прокторинг.
EXAM_MODE_CLASSROOM = "classroom"
#: Сдача из дома, тихая комната: аудио-канал осмыслен.
EXAM_MODE_REMOTE = "remote"
EXAM_MODES = (EXAM_MODE_CLASSROOM, EXAM_MODE_REMOTE)

#: Синонимы, которые встречаются в конфигах и в речи. Всё неизвестное трактуется
#: как `classroom` — безопасная сторона: лишний выключенный канал лучше, чем
#: включённый генератор ложных срабатываний.
_EXAM_MODE_ALIASES: dict[str, str] = {
    "classroom": EXAM_MODE_CLASSROOM,
    "class": EXAM_MODE_CLASSROOM,
    "lab": EXAM_MODE_CLASSROOM,
    "local": EXAM_MODE_CLASSROOM,
    "onsite": EXAM_MODE_CLASSROOM,
    "on-site": EXAM_MODE_CLASSROOM,
    "аудитория": EXAM_MODE_CLASSROOM,
    "класс": EXAM_MODE_CLASSROOM,
    "remote": EXAM_MODE_REMOTE,
    "home": EXAM_MODE_REMOTE,
    "online": EXAM_MODE_REMOTE,
    "distance": EXAM_MODE_REMOTE,
    "дом": EXAM_MODE_REMOTE,
    "удалённо": EXAM_MODE_REMOTE,
    "удаленно": EXAM_MODE_REMOTE,
}

#: Переменная окружения — путь для точек входа, чей argparse не знает про режим.
EXAM_MODE_ENV_VAR = "PROCTOR_EXAM_MODE"

#: Правила, которые живут только в `remote`. В `classroom` они не регистрируются:
#: чужая речь в аудитории есть всегда, подтверждать по ней нечего.
AUDIO_ANALYSIS_KINDS: tuple[EventKind, ...] = (
    EventKind.VOICE_OTHER,
    EventKind.SPEECH_WITHOUT_LIP_MOTION,
)

#: Единая формулировка причины. Её показывает HUD, пишет отчёт и возвращает
#: `AudioMonitor.available()`. Формула гайда: факт -> контекст -> что это значит.
CLASSROOM_AUDIO_OFF_REASON = (
    "отключён в режиме аудитории: высокий уровень ложных срабатываний"
)


def normalize_exam_mode(value: Any) -> str:
    """Любое значение -> один из `EXAM_MODES`. Непонятное -> `classroom`."""
    key = str(value or "").strip().lower()
    return _EXAM_MODE_ALIASES.get(key, EXAM_MODE_CLASSROOM)


def exam_mode_from_argv(argv: Sequence[str] | None = None) -> str | None:
    """Вытащить режим из аргументов командной строки, игнорируя всё остальное.

    Понимает `--exam-mode remote`, `--exam-mode=remote`, `--mode remote`,
    а также короткие `--remote` и `--classroom`. Возвращает None, если про режим
    в argv ничего не сказано. Разбор ручной, а не argparse: функцию вызывают из
    `load()`, где чужие флаги не должны приводить к ошибке и выходу из процесса.
    """
    items = list(sys.argv[1:] if argv is None else argv)
    found: str | None = None
    index = 0
    while index < len(items):
        token = str(items[index])
        index += 1
        if token in ("--remote", "--exam-remote"):
            found = EXAM_MODE_REMOTE
            continue
        if token in ("--classroom", "--exam-classroom"):
            found = EXAM_MODE_CLASSROOM
            continue
        name, sep, inline = token.partition("=")
        if name not in ("--exam-mode", "--mode"):
            continue
        if sep:
            found = normalize_exam_mode(inline)
            continue
        if index < len(items) and not str(items[index]).startswith("-"):
            found = normalize_exam_mode(items[index])
            index += 1
    return found


def exam_mode_from_env(env: dict[str, str] | None = None) -> str | None:
    """Режим из `PROCTOR_EXAM_MODE`, если переменная задана непустой строкой."""
    source = os.environ if env is None else env
    raw = str(source.get(EXAM_MODE_ENV_VAR, "") or "").strip()
    return normalize_exam_mode(raw) if raw else None


def _default_confirm_windows() -> dict[str, float]:
    """Сколько секунд условие должно держаться, прежде чем станет событием.

    Быстрые и однозначные сигналы (второе лицо, телефон) подтверждаются быстро,
    «шумные» (взгляд, поворот головы) — дольше, иначе отчёт утонет в мусоре.
    """
    return {
        EventKind.NO_FACE.value: 2.5,
        EventKind.SECOND_FACE.value: 1.0,
        EventKind.IDENTITY_MISMATCH.value: 2.0,
        EventKind.LIVENESS_FAIL.value: 3.0,
        EventKind.PHONE_IN_FRAME.value: 0.8,
        EventKind.PHONE_RAISED.value: 0.6,
        EventKind.PHONE_AIMED_AT_SCREEN.value: 0.6,
        EventKind.FORBIDDEN_OBJECT.value: 2.0,
        EventKind.GAZE_DOWN.value: 1.8,
        EventKind.GAZE_SIDE.value: 1.8,
        EventKind.GAZE_OFF_SCREEN.value: 2.0,
        EventKind.HEAD_TURNED.value: 2.0,
        EventKind.VOICE_OTHER.value: 1.2,
        EventKind.SPEECH_WITHOUT_LIP_MOTION.value: 2.5,
        EventKind.SENSOR_LOST.value: 3.0,
    }


def _default_cooldowns() -> dict[str, float]:
    """Пауза между повторными событиями одного вида — защита от спама."""
    return {
        EventKind.NO_FACE.value: 15.0,
        EventKind.SECOND_FACE.value: 20.0,
        EventKind.IDENTITY_MISMATCH.value: 20.0,
        EventKind.LIVENESS_FAIL.value: 60.0,
        EventKind.PHONE_IN_FRAME.value: 12.0,
        EventKind.PHONE_RAISED.value: 12.0,
        EventKind.PHONE_AIMED_AT_SCREEN.value: 12.0,
        EventKind.FORBIDDEN_OBJECT.value: 30.0,
        EventKind.GAZE_DOWN.value: 10.0,
        EventKind.GAZE_SIDE.value: 10.0,
        EventKind.GAZE_OFF_SCREEN.value: 10.0,
        EventKind.HEAD_TURNED.value: 15.0,
        EventKind.VOICE_OTHER.value: 15.0,
        EventKind.SPEECH_WITHOUT_LIP_MOTION.value: 20.0,
        # Наушники никуда не денутся между циклами проверки окружения: событие
        # должно быть одной записью в отчёте, а не одной записью на каждый опрос.
        # Дедупликацию держит «прогон» в EventEngine (`STATEFUL_EXTERNAL`: одно
        # подключение = один инцидент с растущей длительностью), поэтому
        # cooldown здесь — только страховка для встроенной замены движка.
        # Значение осознанно МЕНЬШЕ окна отпускания прогона (30 с): после
        # отключения устройства инцидент закрывается через 30 с, и повторное
        # подключение должно давать новый инцидент сразу, а не ждать ещё.
        # 120 с (и даже 45 с) ломали повторный показ на защите: второе
        # подключение подряд не давало события вообще.
        EventKind.AUDIO_DEVICE_CONNECTED.value: 20.0,
        EventKind.MULTIPLE_DISPLAYS.value: 120.0,
        EventKind.VIRTUAL_CAMERA.value: 120.0,
        EventKind.REMOTE_ACCESS_SOFTWARE.value: 120.0,
        EventKind.VIRTUAL_MACHINE.value: 300.0,
        EventKind.SCREEN_RECORDING.value: 60.0,
        EventKind.BLACKLISTED_PROCESS.value: 60.0,
        EventKind.SENSOR_LOST.value: 30.0,
        EventKind.WINDOW_BLUR.value: 5.0,
        EventKind.FULLSCREEN_EXIT.value: 5.0,
    }


@dataclass
class ProctorConfig:
    """Все параметры системы в одном месте. Значения по умолчанию — рабочие."""

    # ------------------------------------------------------- режим развёртывания
    #: "classroom" (по умолчанию) | "remote". Управляет аудио-каналом целиком,
    #: см. докстринг модуля и Р-10. Задаётся в config.json, переопределяется
    #: переменной PROCTOR_EXAM_MODE и флагом --exam-mode / --remote / --classroom.
    exam_mode: str = EXAM_MODE_CLASSROOM

    # ------------------------------------------------------------------ транспорт
    ws_host: str = DEFAULT_WS_HOST
    ws_port: int = DEFAULT_WS_PORT
    ws_ping_interval: float = 20.0
    ws_max_message: int = 1 << 20  # 1 МиБ, больше оболочке не нужно

    # -------------------------------------------------------------- захват камеры
    camera_index: int = 0
    frame_width: int = 640
    frame_height: int = 480
    target_fps: int = 15
    camera_reopen_delay: float = 1.5          # пауза перед попыткой переоткрыть
    camera_max_read_failures: int = 15        # подряд неудачных чтений -> переоткрытие
    camera_read_timeout: float = 1.0          # сколько ждать свежий кадр в read()
    camera_probe_max_index: int = 5           # до какого индекса искать камеры

    # ------------------------------------------------- частоты запуска детекторов
    face_every_n_frames: int = 1              # FaceMesh — каждый кадр
    yolo_every_n_frames: int = 3              # YOLO — каждый третий
    identity_interval: float = 3.0            # сверка личности, сек
    env_interval: float = 20.0                # проверки окружения, сек
    audio_interval: float = 0.1               # опрос аудио, 10 Гц
    status_interval: float = 0.5              # status в оболочку, 2 Гц
    risk_decay_interval: float = 1.0          # спад risk-score, сек

    # --------------------------------- подтверждение событий (окна и гистерезис)
    confirm_window: float = 1.5               # дефолт, если вида нет в confirm_windows
    release_window: float = 1.0               # сколько условие должно отсутствовать, чтобы «отпустить»
    cooldown_sec: float = 10.0                # дефолтный cooldown
    confirm_windows: dict[str, float] = field(default_factory=_default_confirm_windows)
    cooldowns: dict[str, float] = field(default_factory=_default_cooldowns)

    # ------------------------------------------------------------ взгляд и голова
    # FaceAnalyzer отдаёт нормированные девиации (|dev| > 1 — выход за персональный
    # порог после калибровки). Основное решение принимается по ним и по gaze_zone;
    # лимиты в градусах — запасной путь, если девиаций в наблюдении нет.
    gaze_dev_limit: float = 1.0               # |gaze_dev_h| / |gaze_dev_v| выше -> мимо экрана
    gaze_off_dev_limit: float = 1.8           # явно вне карты экрана
    head_dev_limit: float = 1.0               # |head_dev_yaw| / |head_dev_pitch|
    gaze_yaw_limit: float = 20.0              # запасной порог по |gaze_yaw|
    gaze_pitch_down_limit: float = 18.0       # запасной порог взгляда вниз
    gaze_off_screen_limit: float = 32.0       # запасной порог «вне экрана»
    head_yaw_limit: float = 25.0              # град, поворот головы
    head_pitch_limit: float = 22.0            # град, наклон головы
    blink_ear_threshold: float = 0.20         # EAR ниже -> глаз закрыт
    liveness_no_blink_sec: float = 35.0       # лицо есть, морганий нет -> подозрение на фото
    mouth_open_speech: float = 0.22           # mouth_open_ratio выше -> губы двигаются

    # ------------------------------------------------------- объекты в кадре (YOLO)
    yolo_model_path: str = "models/yolov8n.pt"      # веса для ultralytics
    yolo_onnx_path: str = "models/yolov8n.onnx"     # веса для onnxruntime
    yolo_conf: float = 0.35
    yolo_imgsz: int = 416
    phone_labels: list[str] = field(default_factory=lambda: ["cell phone", "mobile phone", "phone"])
    forbidden_labels: list[str] = field(default_factory=lambda: ["book", "laptop", "tv", "monitor", "tvmonitor"])
    phone_min_area_ratio: float = 0.0035      # мелкие ложные срабатывания отбрасываем
    phone_raised_margin: float = 0.12         # доля высоты кадра: насколько выше центра лица
    phone_vertical_ratio: float = 1.15        # h/w телефона -> держат вертикально

    # ----------------------------------------------------------------- личность
    identity_threshold: float = 0.35          # косинусная близость эмбеддингов
    identity_fail_streak: int = 3             # подряд несовпадений -> IDENTITY_MISMATCH
    identity_enroll_samples: int = 5

    # -------------------------------------------------------------------- аудио
    # ВНИМАНИЕ: все пороги ниже работают только при exam_mode == "remote".
    # В "classroom" канал выключен целиком и эти значения не читаются.
    audio_sample_rate: int = 16000
    audio_rms_threshold: float = 0.015        # ниже — тишина
    audio_owner_threshold: float = 0.60       # близость к голосу владельца
    audio_enroll_seconds: float = 6.0
    speech_without_lips_sec: float = 2.5      # речь без движения губ — наушник/подсказчик

    # --------------------------------------------------------------- окружение
    env_blacklist: list[str] = field(default_factory=lambda: [
        "teamviewer", "anydesk", "rustdesk", "radmin", "vnc", "realvnc", "tightvnc",
        "ammyy", "supremo", "dwservice", "chrome remote desktop", "parsec",
        "obs", "obs64", "obs-studio", "camtasia", "bandicam", "fraps",
        "zoom", "discord", "skype", "telegram", "whatsapp", "slack",
        "cheatengine", "manycam", "obs-virtualcam", "droidcam", "epoccam", "iriun",
    ])
    env_allow_multiple_displays: bool = False

    # Проверка активных аудио-устройств (замена аудио-анализа для угрозы У-02
    # «наушник со связью»). Работает в ОБОИХ режимах: это чтение состояния ОС,
    # а не анализ сигнала. Важная граница: проверка видит только устройства
    # ЭТОГО компьютера — гарнитура, спаренная с телефоном студента, системе не
    # видна и закрывается очным контролем и fusion-связкой «длинная пауза ->
    # мгновенный развёрнутый ответ».
    env_check_audio_devices: bool = True
    #: Отдельный интервал для аудио-устройств: общий `env_interval` = 20 с даёт
    #: окно обнаружения до 20.7 с, а подключение наушников нужно видеть сразу.
    #: Проверка дешёвая и кеширована (`_TTL_AUDIO` = 6 с в env_checks).
    env_audio_interval: float = 4.0
    #: Разрешённые аудио-устройства: имя, производитель или транспорт
    #: («logitech h390», «usb»), сравнение по подстроке без учёта регистра.
    #: Снимает ТОЛЬКО совпавшие устройства — выданная преподавателем гарнитура
    #: перестаёт быть инцидентом, а виртуальный аудио-вывод и чужие наушники
    #: рядом продолжают фиксироваться. Это рабочая ручка для аудирования,
    #: а не глухой выключатель.
    env_allowed_audio_devices: list[str] = field(default_factory=list)
    #: Разрешить наушники как КЛАСС устройств (аудирование, где звук нужен
    #: всем). Гасит только наушники и колонки; виртуальное аудио-устройство
    #: и внешний вывод продолжают фиксироваться. По умолчанию выключено.
    env_allow_headphones: bool = False
    #: Дополнительные названия наушников/гарнитур — для локальных марок и для
    #: локализаций ОС, которых нет во встроенном каталоге (он покрывает
    #: ru/en/kk/uk/de/tr/zh). Сравнение по подстроке, регистр не важен.
    env_headphone_names: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------- риск
    risk_warn: float = RISK_WARN
    risk_pause: float = RISK_PAUSE
    risk_lock: float = RISK_LOCK
    risk_max: float = 100.0
    risk_half_life: float = 90.0              # сек, за которые вклад события падает вдвое
    risk_send_delta: float = 0.5              # не шлём risk, если изменился меньше, чем на это

    # ------------------------------------------------------ доказательства и пути
    sessions_dir: str = "sessions"
    evidence_subdir: str = "evidence"
    models_dir: str = "models"
    db_filename: str = "evidence.sqlite"
    report_filename: str = "report.html"
    save_evidence: bool = True
    evidence_min_severity: str = "medium"     # ниже этого кадры не сохраняем
    evidence_jpeg_quality: int = 85
    evidence_clip_seconds: float = 15.0
    sign_report: bool = False
    report_key_path: str = ""

    # ------------------------------------------------------------- калибровка
    calibration_samples: int = 30             # кадров на одну точку/стадию
    calibration_timeout: float = 20.0         # сек на стадию, дальше честный провал

    # ------------------------------------------------------------------ каналы
    enable_vision: bool = True
    enable_gaze: bool = True
    enable_identity: bool = True
    #: Разрешение на аудио-анализ. Действует ТОЛЬКО в exam_mode == "remote":
    #: в "classroom" канал принудительно выключен (см. `audio_analysis_enabled`).
    enable_audio: bool = True
    enable_env: bool = True
    headless: bool = False                    # без камеры (проверка протокола)
    mock: bool = False                        # сценарные события для демо
    mock_loop: bool = True
    mock_pause_between_loops: float = 10.0

    # --------------------------------------------------------------- служебное
    log_level: str = "INFO"

    #: Сырые переопределения секций из config.json (например {"objects": {"conf": 0.5}}).
    #: Накладываются поверх рассчитанных секций — так можно донастроить чужой модуль,
    #: не трогая его код и не заводя новое поле здесь.
    module_overrides: dict[str, Any] = field(default_factory=dict)

    # ========================================================= режим и инварианты
    def __post_init__(self) -> None:
        # Намерение пользователя по аудио-каналу хранится отдельно от
        # действующего значения: иначе переключение classroom -> remote
        # не вернуло бы канал обратно.
        self._audio_requested: bool = bool(self.enable_audio)
        self._audio_applied: bool = bool(self.enable_audio)
        self.normalize()

    def normalize(self) -> None:
        """Привести режимные инварианты в согласованное состояние.

        Вызывается после `__init__`, после каждого `apply()` и при смене режима.
        Идемпотентна. Единственный инвариант: в `classroom` аудио-анализ выключен,
        каким бы ни был `enable_audio` в конфиге (Р-10).

        Прямое присваивание полю извне (`cfg.enable_audio = False` из обработчика
        `--no-audio`) распознаётся как новое намерение и не теряется при смене
        режима: сравниваем поле с тем значением, которое записали сами.
        """
        self.exam_mode = normalize_exam_mode(self.exam_mode)
        if not hasattr(self, "_audio_requested"):  # на случай обхода __post_init__
            self._audio_requested = bool(self.enable_audio)
            self._audio_applied = bool(self.enable_audio)
        if bool(self.enable_audio) != bool(self._audio_applied):
            self._audio_requested = bool(self.enable_audio)
        self.enable_audio = bool(self._audio_requested) and self.is_remote
        self._audio_applied = self.enable_audio
        # Аудио-устройства опрашиваются чаще общего цикла проверок ОС, но не
        # чаще раза в секунду: ниже выигрыша нет (в env_checks стоит кеш на 6 с),
        # и не реже самого цикла — иначе отдельный интервал бессмыслен.
        try:
            audio_every = float(self.env_audio_interval)
        except (TypeError, ValueError):
            audio_every = 4.0
        self.env_audio_interval = max(1.0, min(audio_every,
                                               max(1.0, float(self.env_interval))))

    @property
    def is_classroom(self) -> bool:
        return self.exam_mode == EXAM_MODE_CLASSROOM

    @property
    def is_remote(self) -> bool:
        return self.exam_mode == EXAM_MODE_REMOTE

    @property
    def audio_analysis_enabled(self) -> bool:
        """Действующее состояние аудио-канала: режим И разрешение в конфиге."""
        return bool(self._audio_requested) and self.is_remote

    @property
    def audio_off_reason(self) -> str:
        """Почему аудио-канал молчит. Пустая строка — канал включён."""
        if self.audio_analysis_enabled:
            return ""
        if self.is_classroom:
            return CLASSROOM_AUDIO_OFF_REASON
        return "выключен в конфигурации (enable_audio = false)"

    @property
    def disabled_event_kinds(self) -> list[str]:
        """Виды событий, правила для которых в этом режиме не регистрируются."""
        if self.audio_analysis_enabled:
            return []
        return [kind.value for kind in AUDIO_ANALYSIS_KINDS]

    def set_audio_enabled(self, enabled: bool) -> bool:
        """Явно разрешить или запретить аудио-анализ (например по `--no-audio`).

        Запрет сохраняется при смене режима; разрешение действует только в
        `remote`. Возвращает действующее состояние канала.
        """
        self._audio_requested = bool(enabled)
        self.normalize()
        return self.enable_audio

    def set_exam_mode(self, mode: Any) -> str:
        """Сменить режим и пересчитать зависящие от него каналы."""
        self.exam_mode = normalize_exam_mode(mode)
        self.normalize()
        return self.exam_mode

    def exam_mode_info(self) -> dict[str, Any]:
        """Сводка режима для `hello`, лога и шапки отчёта."""
        return {
            "exam_mode": self.exam_mode,
            "audio_analysis": self.audio_analysis_enabled,
            "audio_off_reason": self.audio_off_reason,
            "disabled_kinds": self.disabled_event_kinds,
            # замена аудио-анализа для угрозы «наушник со связью» (У-02)
            "audio_device_check": bool(self.enable_env and self.env_check_audio_devices),
        }

    # ------------------------------------------------------------------ CLI
    @staticmethod
    def add_cli_arguments(parser: Any) -> Any:
        """Зарегистрировать флаги режима в чужом argparse.

        Вызывается владельцем парсера (точка входа), сразу после — `apply_cli_args`.
        """
        group = parser.add_argument_group("режим экзамена")
        group.add_argument(
            "--exam-mode", "--mode", dest="exam_mode", default=None,
            choices=list(EXAM_MODES),
            help="classroom (аудитория, аудио-анализ выключен) | remote (из дома)",
        )
        group.add_argument(
            "--classroom", dest="exam_mode", action="store_const",
            const=EXAM_MODE_CLASSROOM, help="то же, что --exam-mode classroom",
        )
        group.add_argument(
            "--remote", dest="exam_mode", action="store_const",
            const=EXAM_MODE_REMOTE, help="то же, что --exam-mode remote",
        )
        return parser

    def apply_cli_args(self, args: Any) -> None:
        """Наложить разобранные аргументы: режим и явный запрет аудио-канала."""
        mode = getattr(args, "exam_mode", None)
        if mode:
            self.set_exam_mode(mode)
        # `--no-audio` понимается как намерение, а не как разовое значение поля:
        # иначе последующая смена режима вернула бы канал обратно.
        if getattr(args, "no_audio", False):
            self.set_audio_enabled(False)

    # ======================================================== пути и производные
    @property
    def root(self) -> Path:
        return ROOT

    def _abs(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (ROOT / p)

    @property
    def sessions_path(self) -> Path:
        return self._abs(self.sessions_dir)

    @property
    def models_path(self) -> Path:
        return self._abs(self.models_dir)

    @property
    def yolo_model_abs(self) -> Path:
        return self._abs(self.yolo_model_path)

    def to_dict(self) -> dict[str, Any]:
        """Конфиг для соседних модулей (контракт: config: dict).

        Отдаём и плоские ключи, и именованные секции: модули читают конфиг по
        схеме `config[section][key]` -> `config[f"{section}_{key}"]` -> `config[key]`,
        поэтому секции здесь — это способ передать им наши значения, не навязывая
        остальные их дефолты. Плюс абсолютные пути: детекторам не нужно угадывать
        рабочий каталог процесса.
        """
        self.normalize()  # дёшево и идемпотентно; страхует от правки полей «снаружи»
        d = asdict(self)
        d["root"] = str(ROOT)
        d["sessions_path"] = str(self.sessions_path)
        d["models_path"] = str(self.models_path)
        d["yolo_model_abs"] = str(self.yolo_model_abs)
        # Режим виден и плоским ключом: детекторы читают его через свой
        # `_cfg(config, section, key, default)` с откатом на плоский ключ.
        d["exam_mode"] = self.exam_mode
        d["audio_analysis_enabled"] = self.audio_analysis_enabled
        d["audio_off_reason"] = self.audio_off_reason
        d["disabled_event_kinds"] = self.disabled_event_kinds
        d.update(self.sections())
        return d

    def sections(self) -> dict[str, dict[str, Any]]:
        """Секции конфига под интерфейсы соседних модулей."""
        base = self._base_sections()
        for name, override in (self.module_overrides or {}).items():
            if isinstance(override, dict):
                node = base.setdefault(name, {})
                node.update(override)
        # Режим сильнее любой секции из config.json: включить аудио-анализ в
        # аудитории нельзя даже вручную — это решение Р-10, а не настройка.
        base.setdefault("audio", {})["enabled"] = self.audio_analysis_enabled
        base["audio"]["exam_mode"] = self.exam_mode
        base["audio"]["disabled_reason"] = self.audio_off_reason
        base.setdefault("events", {})["disabled_kinds"] = self.disabled_event_kinds
        return base

    def _mode_filtered(self, table: dict[str, float]) -> dict[str, float]:
        """Убрать из таблицы порогов виды событий, выключенные режимом.

        Поля `confirm_windows` / `cooldowns` остаются полными (это значения для
        `remote`), но в секцию `events` уходит только то, что в этом режиме
        действительно может сработать.
        """
        disabled = set(self.disabled_event_kinds)
        return {k: v for k, v in dict(table).items() if k not in disabled}

    def _base_sections(self) -> dict[str, dict[str, Any]]:
        return {
            "capture": {
                "camera_index": self.camera_index,
                "width": self.frame_width,
                "height": self.frame_height,
                "fps": self.target_fps,
            },
            "objects": {
                "enabled": self.enable_vision,
                "imgsz": self.yolo_imgsz,
                "conf": self.yolo_conf,
                "model_path": str(self._abs(self.yolo_onnx_path)),
                "weights_pt": str(self.yolo_model_abs),
            },
            "face_mesh": {
                "max_num_faces": 2,
            },
            "identity": {
                "enabled": self.enable_identity,
                "threshold": self.identity_threshold,
                "check_interval_s": self.identity_interval,
                "enroll_frames": self.identity_enroll_samples,
                "min_enroll_frames": max(3, self.identity_enroll_samples - 2),
                "model_root": str(self.models_path),
            },
            "liveness": {
                "no_blink_timeout_s": self.liveness_no_blink_sec,
            },
            "audio": {
                # В classroom здесь всегда False: AudioMonitor увидит это в
                # конструкторе и не станет даже проверять микрофон.
                "enabled": self.audio_analysis_enabled,
                "exam_mode": self.exam_mode,
                "disabled_reason": self.audio_off_reason,
                "sample_rate": self.audio_sample_rate,
                "rms_gate": self.audio_rms_threshold,
                "owner_threshold": self.audio_owner_threshold,
                "enroll_min_speech_s": self.audio_enroll_seconds,
                "enroll_max_wait_s": self.audio_enroll_seconds * 3.0,
            },
            "env": {
                "blacklist": list(self.env_blacklist),
                "allow_multiple_displays": self.env_allow_multiple_displays,
                "interval": self.env_interval,
                # проверка устройств вывода звука — в обоих режимах
                "exam_mode": self.exam_mode,
                "check_audio_devices": self.env_check_audio_devices,
                # отдельный, более частый интервал: задержка обнаружения
                # наушников не должна равняться общему циклу проверок ОС
                "audio_interval": self.env_audio_interval,
                "allowed_audio_devices": list(self.env_allowed_audio_devices),
                "allow_headphones": self.env_allow_headphones,
                "headphone_names": list(self.env_headphone_names),
            },
            "events": {
                "confirm_window": self.confirm_window,
                "release_window": self.release_window,
                "cooldown_sec": self.cooldown_sec,
                "confirm_windows": self._mode_filtered(self.confirm_windows),
                "cooldowns": self._mode_filtered(self.cooldowns),
                # Движок не должен регистрировать правила выключенного канала:
                # в аудитории VOICE_OTHER и SPEECH_WITHOUT_LIP_MOTION не существуют.
                "disabled_kinds": self.disabled_event_kinds,
                "exam_mode": self.exam_mode,
            },
            "risk": {
                "warn": self.risk_warn,
                "pause": self.risk_pause,
                "lock": self.risk_lock,
                "max": self.risk_max,
                "half_life": self.risk_half_life,
            },
            "storage": {
                "sessions_path": str(self.sessions_path),
                "evidence_subdir": self.evidence_subdir,
                "db_filename": self.db_filename,
                "report_filename": self.report_filename,
                "jpeg_quality": self.evidence_jpeg_quality,
                "clip_seconds": self.evidence_clip_seconds,
                "min_severity": self.evidence_min_severity,
            },
        }

    # ============================================================ переопределения
    def apply(self, overrides: dict[str, Any]) -> list[str]:
        """Наложить словарь переопределений. Возвращает список непонятых ключей.

        Аудио-канал: явный `enable_audio` (или `audio.enabled`) запоминается как
        намерение, но действует только в `remote` — в аудитории канала нет.
        """
        known = {f.name for f in fields(self)}
        unknown: list[str] = []
        for key, value in (overrides or {}).items():
            if key == "enable_audio":
                self._audio_requested = _coerce(True, value)
                self.enable_audio = self._audio_requested
                continue
            if key == "audio" and isinstance(value, dict) and "enabled" in value:
                self._audio_requested = _coerce(True, value["enabled"])
            if key == "exam_mode":
                self.exam_mode = normalize_exam_mode(value)
                continue
            if key in known:
                setattr(self, key, _coerce(getattr(self, key), value))
            elif key in SECTION_NAMES and isinstance(value, dict):
                # секция чужого модуля — передаём как есть, не разбирая
                node = self.module_overrides.setdefault(key, {})
                node.update(value)
            elif isinstance(value, dict):
                # вложенная секция — разворачиваем по именам полей
                unknown.extend(f"{key}.{k}" for k in self.apply(value))
            else:
                unknown.append(key)
        self.normalize()
        return unknown

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        argv: Sequence[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple["ProctorConfig", list[str]]:
        """Собрать конфиг: значения по умолчанию + config.json, если он есть.

        Возвращает (config, предупреждения). Битый JSON не роняет сайдкар —
        попадает в предупреждения, работаем на значениях по умолчанию.

        Режим экзамена накладывается после файла: `PROCTOR_EXAM_MODE`, затем флаг
        из `argv` (`--exam-mode` / `--remote` / `--classroom`). `argv=None` — это
        `sys.argv`; `argv=[]` отключает просмотр командной строки. Чужие флаги
        игнорируются, разбора argparse здесь нет.
        """
        cfg = cls()
        warnings: list[str] = []
        candidates: list[Path] = []
        if path:
            candidates.append(Path(path).expanduser())
        else:
            candidates.append(ROOT / CONFIG_FILENAME)
            candidates.append(_HERE / CONFIG_FILENAME)

        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception as exc:  # битый JSON — не причина падать
                warnings.append(f"не удалось прочитать {candidate}: {exc}")
                continue
            if not isinstance(raw, dict):
                warnings.append(f"{candidate}: ожидался JSON-объект")
                continue
            unknown = cfg.apply(raw)
            if unknown:
                warnings.append(f"{candidate}: неизвестные ключи: {', '.join(sorted(unknown))}")
            warnings.append(f"конфиг загружен из {candidate}")
            break
        else:
            if path:
                warnings.append(f"файл конфига не найден: {path}")

        # --- режим экзамена: окружение, затем командная строка ---
        from_env = exam_mode_from_env(env)
        if from_env:
            cfg.set_exam_mode(from_env)
            warnings.append(f"{EXAM_MODE_ENV_VAR}={from_env}")
        from_cli = exam_mode_from_argv(argv)
        if from_cli:
            cfg.set_exam_mode(from_cli)
            warnings.append(f"режим задан флагом CLI: {from_cli}")

        cfg.normalize()
        if cfg.is_classroom:
            warnings.append(
                "режим аудитории (classroom): аудио-анализ выключен — "
                f"{CLASSROOM_AUDIO_OFF_REASON}; угроза «наушник со связью» "
                "закрыта проверкой активных аудио-устройств"
            )
        else:
            warnings.append(
                "режим remote: аудио-канал "
                + ("включён" if cfg.audio_analysis_enabled else "выключен в конфиге")
            )

        return cfg, warnings

    def confirm_window_for(self, name: str) -> float:
        return float(self.confirm_windows.get(name, self.confirm_window))

    def cooldown_for(self, name: str) -> float:
        return float(self.cooldowns.get(name, self.cooldown_sec))


def _coerce(current: Any, value: Any) -> Any:
    """Привести значение из JSON к типу текущего значения поля."""
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "да")
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return current
    if isinstance(current, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return current
    if isinstance(current, str):
        return str(value)
    if isinstance(current, list):
        return list(value) if isinstance(value, (list, tuple)) else current
    if isinstance(current, dict):
        if isinstance(value, dict):
            merged = dict(current)
            merged.update(value)
            return merged
        return current
    return value


__all__ = [
    "ProctorConfig",
    "ROOT",
    "CONFIG_FILENAME",
    "SECTION_NAMES",
    "EXAM_MODES",
    "EXAM_MODE_CLASSROOM",
    "EXAM_MODE_REMOTE",
    "EXAM_MODE_ENV_VAR",
    "AUDIO_ANALYSIS_KINDS",
    "CLASSROOM_AUDIO_OFF_REASON",
    "normalize_exam_mode",
    "exam_mode_from_argv",
    "exam_mode_from_env",
]

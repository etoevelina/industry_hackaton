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
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

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
        d = asdict(self)
        d["root"] = str(ROOT)
        d["sessions_path"] = str(self.sessions_path)
        d["models_path"] = str(self.models_path)
        d["yolo_model_abs"] = str(self.yolo_model_abs)
        d.update(self.sections())
        return d

    def sections(self) -> dict[str, dict[str, Any]]:
        """Секции конфига под интерфейсы соседних модулей."""
        base = self._base_sections()
        for name, override in (self.module_overrides or {}).items():
            if isinstance(override, dict):
                node = base.setdefault(name, {})
                node.update(override)
        return base

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
                "enabled": self.enable_audio,
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
            },
            "events": {
                "confirm_window": self.confirm_window,
                "release_window": self.release_window,
                "cooldown_sec": self.cooldown_sec,
                "confirm_windows": dict(self.confirm_windows),
                "cooldowns": dict(self.cooldowns),
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
        """Наложить словарь переопределений. Возвращает список непонятых ключей."""
        known = {f.name for f in fields(self)}
        unknown: list[str] = []
        for key, value in (overrides or {}).items():
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
        return unknown

    @classmethod
    def load(cls, path: str | Path | None = None) -> tuple["ProctorConfig", list[str]]:
        """Собрать конфиг: значения по умолчанию + config.json, если он есть.

        Возвращает (config, предупреждения). Битый JSON не роняет сайдкар —
        попадает в предупреждения, работаем на значениях по умолчанию.
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


__all__ = ["ProctorConfig", "ROOT", "CONFIG_FILENAME", "SECTION_NAMES"]

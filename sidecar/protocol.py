"""
Единый контракт обмена между Python-сайдкаром (CV/аудио) и Electron-оболочкой.

ЭТОТ ФАЙЛ — ИСТОЧНИК ИСТИНЫ. Любой модуль, который порождает или потребляет
события, импортирует типы отсюда и не изобретает свои строки.

Транспорт: WebSocket на 127.0.0.1, JSON-сообщения, по одному на фрейм.
Направление указано в докстринге каждого типа сообщения.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

PROTOCOL_VERSION = 1
DEFAULT_WS_HOST = "127.0.0.1"
DEFAULT_WS_PORT = 8787


# --------------------------------------------------------------------------
# Типы сообщений в конверте {"v":1,"type":<MsgType>,"ts":float,...}
# --------------------------------------------------------------------------
class MsgType(str, Enum):
    # sidecar -> shell
    HELLO = "hello"              # возможности сайдкара при подключении
    STATUS = "status"            # ~2 Гц: fps, живые признаки, состояние сессии
    EVENT = "event"              # зафиксированный инцидент
    RISK = "risk"                # обновление risk-score с разложением
    CALIBRATION = "calibration"  # прогресс/результат калибровки
    VERDICT = "verdict"          # требование действия: warn / pause / lock
    ERROR = "error"

    # shell -> sidecar
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    CALIBRATE = "calibrate"        # {"stage": "gaze_center"|"gaze_grid"|"voice"|"identity"}
    TELEMETRY = "telemetry"        # поток из renderer: клавиатура/ответы (для fusion)
    SHELL_EVENT = "shell_event"    # инцидент, замеченный оболочкой (blur, хоткей, мониторы)
    COMMAND = "command"            # {"name": "snapshot"|"reset_risk"|"export_report"}


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Channel(str, Enum):
    """Источник сигнала — нужен для объяснимости отчёта."""
    VISION = "vision"
    GAZE = "gaze"
    IDENTITY = "identity"
    AUDIO = "audio"
    ENVIRONMENT = "environment"
    SHELL = "shell"
    FUSION = "fusion"
    SYSTEM = "system"


class EventKind(str, Enum):
    """Полный словарь инцидентов. Новых строк вне этого enum быть не должно."""

    # --- объекты в кадре (YOLO) ---
    PHONE_IN_FRAME = "PHONE_IN_FRAME"              # телефон виден где-то в кадре
    PHONE_RAISED = "PHONE_RAISED"                  # поднят к уровню лица
    PHONE_AIMED_AT_SCREEN = "PHONE_AIMED_AT_SCREEN"  # вертикальный + поднят + взгляд в него
    FORBIDDEN_OBJECT = "FORBIDDEN_OBJECT"          # книга/ноутбук/монитор в кадре

    # --- лицо и присутствие ---
    NO_FACE = "NO_FACE"
    SECOND_FACE = "SECOND_FACE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"        # за компьютером другой человек
    LIVENESS_FAIL = "LIVENESS_FAIL"                # подозрение на фото/зацикленное видео

    # --- взгляд и голова ---
    GAZE_DOWN = "GAZE_DOWN"
    GAZE_SIDE = "GAZE_SIDE"
    GAZE_OFF_SCREEN = "GAZE_OFF_SCREEN"            # точка взгляда вне карты экрана
    HEAD_TURNED = "HEAD_TURNED"

    # --- аудио ---
    VOICE_OTHER = "VOICE_OTHER"                    # чужой голос
    SPEECH_WITHOUT_LIP_MOTION = "SPEECH_WITHOUT_LIP_MOTION"  # наушник со связью

    # --- окружение (проверки ОС) ---
    AUDIO_DEVICE_CONNECTED = "AUDIO_DEVICE_CONNECTED"  # наушники/гарнитура во время экзамена
    VIRTUAL_CAMERA = "VIRTUAL_CAMERA"
    REMOTE_ACCESS_SOFTWARE = "REMOTE_ACCESS_SOFTWARE"
    VIRTUAL_MACHINE = "VIRTUAL_MACHINE"
    SCREEN_RECORDING = "SCREEN_RECORDING"
    MULTIPLE_DISPLAYS = "MULTIPLE_DISPLAYS"
    BLACKLISTED_PROCESS = "BLACKLISTED_PROCESS"

    # --- оболочка ---
    WINDOW_BLUR = "WINDOW_BLUR"
    FULLSCREEN_EXIT = "FULLSCREEN_EXIT"
    SHORTCUT_BLOCKED = "SHORTCUT_BLOCKED"
    CLIPBOARD_PASTE = "CLIPBOARD_PASTE"
    DEVTOOLS_ATTEMPT = "DEVTOOLS_ATTEMPT"

    # --- поведение ввода ---
    PASTE_BURST = "PASTE_BURST"                    # вставка большого блока текста
    TYPING_ANOMALY = "TYPING_ANOMALY"              # ритм набора не похож на базовый

    # --- fusion (связки сигналов — ядро ценности продукта) ---
    FUSION_GAZE_THEN_ANSWER = "FUSION_GAZE_THEN_ANSWER"
    FUSION_BLUR_THEN_ANSWER = "FUSION_BLUR_THEN_ANSWER"
    FUSION_PHONE_THEN_ANSWER = "FUSION_PHONE_THEN_ANSWER"

    # --- служебное ---
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_ENDED = "SESSION_ENDED"
    CALIBRATION_DONE = "CALIBRATION_DONE"
    SENSOR_LOST = "SENSOR_LOST"                    # камера/микрофон пропали


#: Вклад события в risk-score. Подбирается эмпирически, меняется только здесь.
RISK_WEIGHTS: dict[EventKind, float] = {
    EventKind.PHONE_IN_FRAME: 25.0,
    EventKind.PHONE_RAISED: 35.0,
    EventKind.PHONE_AIMED_AT_SCREEN: 45.0,
    EventKind.FORBIDDEN_OBJECT: 12.0,

    EventKind.NO_FACE: 18.0,
    EventKind.SECOND_FACE: 50.0,
    EventKind.IDENTITY_MISMATCH: 60.0,
    EventKind.LIVENESS_FAIL: 45.0,

    EventKind.GAZE_DOWN: 8.0,
    EventKind.GAZE_SIDE: 8.0,
    EventKind.GAZE_OFF_SCREEN: 10.0,
    EventKind.HEAD_TURNED: 6.0,

    EventKind.VOICE_OTHER: 40.0,
    EventKind.SPEECH_WITHOUT_LIP_MOTION: 45.0,

    # Замена аудио-анализа в режиме аудитории: детерминированно, ложных не даёт.
    # Вес высокий — наушники во время экзамена это прямой признак внешней подсказки.
    EventKind.AUDIO_DEVICE_CONNECTED: 40.0,
    EventKind.VIRTUAL_CAMERA: 70.0,
    EventKind.REMOTE_ACCESS_SOFTWARE: 60.0,
    EventKind.VIRTUAL_MACHINE: 30.0,
    EventKind.SCREEN_RECORDING: 35.0,
    EventKind.MULTIPLE_DISPLAYS: 25.0,
    EventKind.BLACKLISTED_PROCESS: 30.0,

    EventKind.WINDOW_BLUR: 15.0,
    EventKind.FULLSCREEN_EXIT: 15.0,
    EventKind.SHORTCUT_BLOCKED: 5.0,
    EventKind.CLIPBOARD_PASTE: 10.0,
    EventKind.DEVTOOLS_ATTEMPT: 25.0,

    EventKind.PASTE_BURST: 30.0,
    EventKind.TYPING_ANOMALY: 15.0,

    EventKind.FUSION_GAZE_THEN_ANSWER: 55.0,
    EventKind.FUSION_BLUR_THEN_ANSWER: 55.0,
    EventKind.FUSION_PHONE_THEN_ANSWER: 65.0,

    EventKind.SESSION_STARTED: 0.0,
    EventKind.SESSION_ENDED: 0.0,
    EventKind.CALIBRATION_DONE: 0.0,
    EventKind.SENSOR_LOST: 20.0,
}

#: Пороги реакции по накопленному risk-score.
RISK_WARN = 30.0
RISK_PAUSE = 60.0
RISK_LOCK = 90.0


class VerdictAction(str, Enum):
    NONE = "none"
    WARN = "warn"      # мягкое предупреждение в HUD
    PAUSE = "pause"    # тест приостановлен, нужен возврат в рамку
    LOCK = "lock"      # сессия закрыта, формируется отчёт


@dataclass
class Evidence:
    """Доказательство инцидента. Пути — относительно каталога сессии."""
    frame_path: str | None = None          # jpeg-кроп момента
    clip_path: str | None = None           # 15-секундный клип вокруг момента
    bbox: list[float] | None = None        # [x, y, w, h] в пикселях кадра
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProctorEvent:
    """Инцидент. Неизменяемая единица доказательной базы."""
    kind: EventKind
    severity: Severity
    channel: Channel
    confidence: float = 1.0
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    duration: float = 0.0                  # сколько держалось условие, сек
    message: str = ""                      # человекочитаемо, по-русски, для отчёта
    detail: dict[str, Any] = field(default_factory=dict)
    evidence: Evidence | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["severity"] = self.severity.value
        d["channel"] = self.channel.value
        return d

    @property
    def weight(self) -> float:
        return RISK_WEIGHTS.get(self.kind, 10.0)


def envelope(msg_type: MsgType, **payload: Any) -> dict[str, Any]:
    """Собрать конверт сообщения протокола."""
    return {"v": PROTOCOL_VERSION, "type": msg_type.value, "ts": time.time(), **payload}


def encode(msg: dict[str, Any]) -> str:
    return json.dumps(msg, ensure_ascii=False, default=str)


def decode(raw: str | bytes) -> dict[str, Any]:
    return json.loads(raw)


def event_message(event: ProctorEvent) -> dict[str, Any]:
    return envelope(MsgType.EVENT, event=event.to_dict())

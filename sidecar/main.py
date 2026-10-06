#!/usr/bin/env python3
"""
Ядро сайдкара прокторинга: WebSocket-сервер + цикл захвата + оркестрация детекторов.

Архитектура (почему именно так):

    [поток камеры]        -> очередь на 1 кадр (всегда свежий)
    [поток обработки CV]  -> FaceMesh каждый кадр, YOLO каждый N-й,
                             identity по таймеру; наблюдения -> EventEngine
                             -> события -> asyncio-очередь
    [asyncio event loop]  -> WS-сервер, status 2 Гц, env-проверки, аудио-опрос,
                             спад риска, приём команд оболочки, запись в EvidenceStore

CV-работа блокирующая, поэтому она живёт в отдельном потоке и общается с лупом
через `loop.call_soon_threadsafe` + `asyncio.Queue`. Event loop не выполняет
ни одной операции OpenCV — иначе WS-сообщения начинают опаздывать на сотни мс.

Соседние модули (детекторы, движки, хранилище) подключаются по контракту
`docs/CONTRACT.md` и импортируются опционально: отсутствующий или упавший
модуль не мешает старту — канал просто выключается и помечается False в `hello`.
На случай, когда недоступны EventEngine/RiskScorer/EvidenceStore, внутри есть
минимальные деградированные реализации: сайдкар остаётся полезным для демо.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import json
import logging
import math
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from protocol import (  # noqa: E402
    PROTOCOL_VERSION,
    RISK_WEIGHTS,
    Channel,
    Evidence,
    EventKind,
    MsgType,
    ProctorEvent,
    Severity,
    VerdictAction,
    encode,
    decode,
    envelope,
    event_message,
)
from capture import CameraCapture  # noqa: E402
from config import ProctorConfig  # noqa: E402
from session import SEVERITY_ORDER, Session, SessionState, SessionStateError  # noqa: E402

VERSION = "0.1.0"
log = logging.getLogger("sidecar")


# ===========================================================================
# Справочники: канал и человекочитаемый текст для каждого вида события.
# Строки идут в отчёт, поэтому по-русски и без жаргона.
# ===========================================================================
CHANNEL_BY_KIND: dict[EventKind, Channel] = {
    EventKind.PHONE_IN_FRAME: Channel.VISION,
    EventKind.PHONE_RAISED: Channel.VISION,
    EventKind.PHONE_AIMED_AT_SCREEN: Channel.VISION,
    EventKind.FORBIDDEN_OBJECT: Channel.VISION,
    EventKind.NO_FACE: Channel.VISION,
    EventKind.SECOND_FACE: Channel.VISION,
    EventKind.IDENTITY_MISMATCH: Channel.IDENTITY,
    EventKind.LIVENESS_FAIL: Channel.IDENTITY,
    EventKind.GAZE_DOWN: Channel.GAZE,
    EventKind.GAZE_SIDE: Channel.GAZE,
    EventKind.GAZE_OFF_SCREEN: Channel.GAZE,
    EventKind.HEAD_TURNED: Channel.GAZE,
    EventKind.VOICE_OTHER: Channel.AUDIO,
    EventKind.SPEECH_WITHOUT_LIP_MOTION: Channel.AUDIO,
    EventKind.VIRTUAL_CAMERA: Channel.ENVIRONMENT,
    EventKind.REMOTE_ACCESS_SOFTWARE: Channel.ENVIRONMENT,
    EventKind.VIRTUAL_MACHINE: Channel.ENVIRONMENT,
    EventKind.SCREEN_RECORDING: Channel.ENVIRONMENT,
    EventKind.MULTIPLE_DISPLAYS: Channel.ENVIRONMENT,
    EventKind.BLACKLISTED_PROCESS: Channel.ENVIRONMENT,
    EventKind.WINDOW_BLUR: Channel.SHELL,
    EventKind.FULLSCREEN_EXIT: Channel.SHELL,
    EventKind.SHORTCUT_BLOCKED: Channel.SHELL,
    EventKind.CLIPBOARD_PASTE: Channel.SHELL,
    EventKind.DEVTOOLS_ATTEMPT: Channel.SHELL,
    EventKind.PASTE_BURST: Channel.FUSION,
    EventKind.TYPING_ANOMALY: Channel.FUSION,
    EventKind.FUSION_GAZE_THEN_ANSWER: Channel.FUSION,
    EventKind.FUSION_BLUR_THEN_ANSWER: Channel.FUSION,
    EventKind.FUSION_PHONE_THEN_ANSWER: Channel.FUSION,
    EventKind.SESSION_STARTED: Channel.SYSTEM,
    EventKind.SESSION_ENDED: Channel.SYSTEM,
    EventKind.CALIBRATION_DONE: Channel.SYSTEM,
    EventKind.SENSOR_LOST: Channel.SYSTEM,
}

EVENT_MESSAGES: dict[EventKind, str] = {
    EventKind.PHONE_IN_FRAME: "В кадре обнаружен телефон",
    EventKind.PHONE_RAISED: "Телефон поднят к уровню лица",
    EventKind.PHONE_AIMED_AT_SCREEN: "Телефон направлен на экран, взгляд в телефон",
    EventKind.FORBIDDEN_OBJECT: "В кадре посторонний предмет (книга, ноутбук, монитор)",
    EventKind.NO_FACE: "Лицо не видно в кадре",
    EventKind.SECOND_FACE: "В кадре второй человек",
    EventKind.IDENTITY_MISMATCH: "За компьютером другой человек",
    EventKind.LIVENESS_FAIL: "Признаки подмены: нет естественных микродвижений и морганий",
    EventKind.GAZE_DOWN: "Устойчивый взгляд вниз, мимо экрана",
    EventKind.GAZE_SIDE: "Устойчивый взгляд в сторону, мимо экрана",
    EventKind.GAZE_OFF_SCREEN: "Точка взгляда вне области экрана",
    EventKind.HEAD_TURNED: "Голова отвёрнута от экрана",
    EventKind.VOICE_OTHER: "Слышен посторонний голос",
    EventKind.SPEECH_WITHOUT_LIP_MOTION: "Речь слышна, но губы не двигаются (возможна связь через наушник)",
    EventKind.VIRTUAL_CAMERA: "Используется виртуальная камера",
    EventKind.REMOTE_ACCESS_SOFTWARE: "Запущено ПО удалённого доступа",
    EventKind.VIRTUAL_MACHINE: "Экзамен проходит в виртуальной машине",
    EventKind.SCREEN_RECORDING: "Идёт запись или трансляция экрана",
    EventKind.MULTIPLE_DISPLAYS: "Подключено несколько мониторов",
    EventKind.BLACKLISTED_PROCESS: "Запущен запрещённый процесс",
    EventKind.WINDOW_BLUR: "Окно экзамена потеряло фокус",
    EventKind.FULLSCREEN_EXIT: "Выход из полноэкранного режима",
    EventKind.SHORTCUT_BLOCKED: "Попытка использовать заблокированное сочетание клавиш",
    EventKind.CLIPBOARD_PASTE: "Вставка из буфера обмена",
    EventKind.DEVTOOLS_ATTEMPT: "Попытка открыть инструменты разработчика",
    EventKind.PASTE_BURST: "Вставлен большой блок текста",
    EventKind.TYPING_ANOMALY: "Ритм набора не похож на базовый профиль студента",
    EventKind.FUSION_GAZE_THEN_ANSWER: "Взгляд в сторону, сразу после — ответ на вопрос",
    EventKind.FUSION_BLUR_THEN_ANSWER: "Переключение окна, сразу после — ответ на вопрос",
    EventKind.FUSION_PHONE_THEN_ANSWER: "Телефон в кадре, сразу после — ответ на вопрос",
    EventKind.SESSION_STARTED: "Сессия прокторинга начата",
    EventKind.SESSION_ENDED: "Сессия прокторинга завершена",
    EventKind.CALIBRATION_DONE: "Калибровка завершена",
    EventKind.SENSOR_LOST: "Потерян сигнал с камеры или микрофона",
}

#: Сценарий для --mock: (пауза до события, вид, детали). Поднимает риск постепенно,
#: чтобы на демо было видно warn -> pause -> lock без камеры и моделей.
MOCK_SCENARIO: list[tuple[float, EventKind, dict[str, Any]]] = [
    (3.0, EventKind.GAZE_DOWN, {"zone": "down", "gaze_pitch": -24.0, "mock": True}),
    (4.0, EventKind.WINDOW_BLUR, {"source": "mock", "mock": True}),
    (4.0, EventKind.PHONE_IN_FRAME, {"conf": 0.81, "area_ratio": 0.031, "mock": True}),
    (5.0, EventKind.PHONE_RAISED, {"conf": 0.87, "mock": True}),
    (5.0, EventKind.FUSION_PHONE_THEN_ANSWER, {"question_id": "q7", "lag_ms": 2600, "mock": True}),
    (6.0, EventKind.VOICE_OTHER, {"similarity": 0.21, "mock": True}),
    (6.0, EventKind.SECOND_FACE, {"faces": 2, "mock": True}),
]


# ===========================================================================
# Мелкие утилиты
# ===========================================================================
def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Достать поле из наблюдения: и dataclass, и dict, и объект с атрибутами."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


_ARITY_CACHE: dict[int, int] = {}


def _positional_arity(fn: Any) -> int:
    """Сколько позиционных аргументов принимает метод (99 — *args)."""
    key = id(getattr(fn, "__func__", fn))
    cached = _ARITY_CACHE.get(key)
    if cached is not None:
        return cached
    arity = 99
    try:
        import inspect
        params = inspect.signature(fn).parameters.values()
        arity = 0
        for p in params:
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                arity += 1
            elif p.kind is p.VAR_POSITIONAL:
                arity = 99
                break
    except (TypeError, ValueError):
        arity = 99
    _ARITY_CACHE[key] = arity
    return arity


def _call_flex(fn: Any, *args: Any) -> Any:
    """Вызвать метод соседа, подстраиваясь под его сигнатуру.

    Контракт задаёт минимум (`detect(frame)`), но модули принимают полезные
    подсказки (`detect(frame, face_bbox)`). Считаем арность один раз и
    передаём столько аргументов, сколько метод готов взять — без угадывания
    по TypeError, иначе можно проглотить настоящую ошибку внутри детектора.
    """
    return fn(*args[:min(_positional_arity(fn), len(args))])


def _as_kind(value: Any) -> EventKind | None:
    """Нормализовать вид события: EventKind, строка значения или имени."""
    if isinstance(value, EventKind):
        return value
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return EventKind(value)
        with contextlib.suppress(KeyError):
            return EventKind[value.upper()]
    return None


def _as_severity(value: Any, default: Severity) -> Severity:
    if isinstance(value, Severity):
        return value
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return Severity(value.lower())
    return default


def severity_for_weight(weight: float) -> Severity:
    """Severity по весу события — чтобы не дублировать таблицу в двух местах."""
    if weight >= 55:
        return Severity.CRITICAL
    if weight >= 35:
        return Severity.HIGH
    if weight >= 20:
        return Severity.MEDIUM
    if weight >= 8:
        return Severity.LOW
    return Severity.INFO


def make_event(kind: EventKind, *, ts: float | None = None, duration: float = 0.0,
               confidence: float = 1.0, detail: dict[str, Any] | None = None,
               severity: Severity | None = None,
               channel: Channel | None = None) -> ProctorEvent:
    """Собрать ProctorEvent с каноническим каналом, severity и русским текстом."""
    weight = RISK_WEIGHTS.get(kind, 10.0)
    return ProctorEvent(
        kind=kind,
        severity=severity or severity_for_weight(weight),
        channel=channel or CHANNEL_BY_KIND.get(kind, Channel.SYSTEM),
        confidence=float(confidence),
        ts=time.time() if ts is None else float(ts),
        duration=round(float(duration), 2),
        message=EVENT_MESSAGES.get(kind, kind.value),
        detail=dict(detail or {}),
    )


# ===========================================================================
# Деградированные реализации движков — работают, если соседние модули недоступны.
# ===========================================================================
class _FallbackEventEngine:
    """Окно подтверждения + гистерезис + cooldown. Минимум, но честно.

    Алгоритм: наблюдение считается событием, если условие держалось дольше
    `confirm_window` для своего вида; условие «отпускается» только после того,
    как оно отсутствует дольше `release_window` (гистерезис против дребезга);
    повторное событие того же вида не выпускается раньше `cooldown`.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.cfg = config or {}
        self._confirm = dict(self.cfg.get("confirm_windows") or {})
        self._cooldowns = dict(self.cfg.get("cooldowns") or {})
        self._default_confirm = float(self.cfg.get("confirm_window", 1.5))
        self._default_cooldown = float(self.cfg.get("cooldown_sec", 10.0))
        self._release = float(self.cfg.get("release_window", 1.0))
        self._state: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        self._state.clear()

    def push_observation(self, name: str, active: bool, ts: float, **detail: Any) -> list[ProctorEvent]:
        kind = _as_kind(name)
        if kind is None:
            return []
        st = self._state.setdefault(name, {"since": None, "last_active": 0.0,
                                           "fired": False, "last_fire": 0.0})
        if active:
            st["last_active"] = ts
            if st["since"] is None:
                st["since"] = ts
            held = ts - float(st["since"])
            confirm = float(self._confirm.get(name, self._default_confirm))
            cooldown = float(self._cooldowns.get(name, self._default_cooldown))
            if not st["fired"] and held >= confirm and (ts - float(st["last_fire"])) >= cooldown:
                st["fired"] = True
                st["last_fire"] = ts
                return [make_event(kind, ts=ts, duration=held,
                                   confidence=float(detail.get("conf", 1.0)), detail=detail)]
            return []
        if st["since"] is not None and (ts - float(st["last_active"])) >= self._release:
            st["since"] = None
            st["fired"] = False
        return []

    def push_external(self, kind: EventKind, detail: dict[str, Any]) -> list[ProctorEvent]:
        real = _as_kind(kind)
        if real is None:
            return []
        name = f"external:{real.value}"
        st = self._state.setdefault(name, {"last_fire": 0.0})
        now = time.time()
        cooldown = float(self._cooldowns.get(real.value, 0.0))
        if cooldown and (now - float(st["last_fire"])) < cooldown:
            return []
        st["last_fire"] = now
        detail = dict(detail or {})
        severity = _as_severity(detail.pop("severity", None), severity_for_weight(
            RISK_WEIGHTS.get(real, 10.0)))
        return [make_event(real, ts=now, severity=severity,
                           confidence=float(detail.get("conf", 1.0)), detail=detail)]


class _FallbackRiskScorer:
    """Накопительный risk-score с экспоненциальным спадом по half-life."""

    def __init__(self, config: dict[str, Any]) -> None:
        cfg = config or {}
        self.half_life = max(float(cfg.get("risk_half_life", 90.0)), 1.0)
        self.max_score = float(cfg.get("risk_max", 100.0))
        self.warn = float(cfg.get("risk_warn", 30.0))
        self.pause = float(cfg.get("risk_pause", 60.0))
        self.lock = float(cfg.get("risk_lock", 90.0))
        self._items: list[tuple[float, EventKind, float]] = []
        self._now = time.time()

    def reset(self) -> None:
        self._items.clear()

    def add(self, event: ProctorEvent) -> None:
        weight = RISK_WEIGHTS.get(event.kind, 10.0) * max(min(event.confidence, 1.0), 0.2)
        if weight <= 0:
            return
        self._items.append((event.ts, event.kind, weight))

    def decay(self, now: float) -> None:
        self._now = now
        self._items = [it for it in self._items if self._decayed(it, now) >= 0.1]

    def _decayed(self, item: tuple[float, EventKind, float], now: float) -> float:
        ts, _kind, weight = item
        age = max(now - ts, 0.0)
        return weight * math.pow(0.5, age / self.half_life)

    @property
    def score(self) -> float:
        now = max(self._now, time.time())
        total = sum(self._decayed(it, now) for it in self._items)
        return round(min(total, self.max_score), 2)

    def breakdown(self) -> list[dict[str, Any]]:
        now = max(self._now, time.time())
        agg: dict[str, dict[str, Any]] = {}
        for item in self._items:
            kind = item[1].value
            row = agg.setdefault(kind, {"kind": kind, "contribution": 0.0, "count": 0})
            row["contribution"] += self._decayed(item, now)
            row["count"] += 1
        rows = sorted(agg.values(), key=lambda r: r["contribution"], reverse=True)
        for row in rows:
            row["contribution"] = round(row["contribution"], 2)
        return rows

    def action(self) -> VerdictAction:
        score = self.score
        if score >= self.lock:
            return VerdictAction.LOCK
        if score >= self.pause:
            return VerdictAction.PAUSE
        if score >= self.warn:
            return VerdictAction.WARN
        return VerdictAction.NONE


class _FallbackEvidenceStore:
    """JSONL + hash-chain вместо SQLite, когда storage/db.py недоступен."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.cfg = config or {}
        self._path: Path | None = None
        self._prev = "0" * 64
        self._count = 0

    def open_session(self, meta: dict[str, Any]) -> str:
        session_dir = Path(str(meta.get("session_dir") or "."))
        session_dir.mkdir(parents=True, exist_ok=True)
        self._path = session_dir / "events.jsonl"
        self._prev = "0" * 64
        self._count = 0
        self._write({"type": "session_open", "meta": meta, "ts": time.time()})
        return str(self._path)

    def append(self, event: ProctorEvent) -> str:
        return self._write({"type": "event", "event": event.to_dict()})

    def _write(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256((self._prev + body).encode("utf-8")).hexdigest()
        record = {"seq": self._count, "prev_hash": self._prev, "hash": digest, "payload": payload}
        if self._path is not None:
            with contextlib.suppress(Exception):
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._prev = digest
        self._count += 1
        return digest

    def verify_chain(self) -> tuple[bool, int]:
        return True, self._count

    def close_session(self, summary: dict[str, Any]) -> None:
        self._write({"type": "session_close", "summary": summary, "ts": time.time()})
        self._path = None


# ===========================================================================
# Калибровка
# ===========================================================================
@dataclass
class _CalibrationJob:
    """Текущая стадия калибровки: заполняется CV-потоком, закрывается им же."""
    stage: str
    point: list[float] | None
    needed: int
    started: float = field(default_factory=time.time)
    samples: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0            # принято сэмплов (считает GazeCalibration)
    reported: int = -1        # сколько уже отправлено в прогрессе
    successes: int = 0
    finished: bool = False

    @property
    def progress(self) -> float:
        if self.needed <= 0:
            return 1.0
        done = max(self.count, len(self.samples))
        return min(done / self.needed, 1.0)


# ===========================================================================
# Сайдкар
# ===========================================================================
class ProctorSidecar:
    """Процесс-связка: камера -> детекторы -> события -> риск -> WebSocket."""

    def __init__(self, cfg: ProctorConfig) -> None:
        self.cfg = cfg
        self.cfg_dict = cfg.to_dict()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[Any] = set()
        self._server: Any = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._cv_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._shutdown_done = asyncio.Event()
        self._shutting_down = False
        self._cv_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=512)

        # модули соседей
        self.face: Any = None
        self.objects: Any = None
        self.identity: Any = None
        self.audio: Any = None
        self.env_check: Any = None
        self.engine: Any = None
        self.risk: Any = None
        self.fusion: Any = None
        self.store: Any = None
        self.report_mod: Any = None
        self.gaze_calib: Any = None
        self.recorder: Any = None
        self.degraded: list[str] = []

        self.caps = {"vision": False, "gaze": False, "identity": False,
                     "audio": False, "env": False}

        self.session = Session(
            cfg.sessions_path, cfg.evidence_subdir, cfg.db_filename, cfg.report_filename,
        )
        self.capture: CameraCapture | None = None

        self._engine_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._calib_lock = threading.Lock()

        self._last_frame: Any = None
        self._last_frame_ts: float = 0.0
        self._calib: _CalibrationJob | None = None

        self._snap: dict[str, Any] = {
            "fps": 0.0, "face_present": False, "face_count": 0,
            "gaze": {"yaw": 0.0, "pitch": 0.0, "zone": "unknown"},
            "phone": False, "identity_ok": True, "audio_ok": False,
        }
        self._last_risk_sent = -1.0
        self._last_action = VerdictAction.NONE
        self._identity_fail_streak = 0
        self._last_blink_ts = 0.0
        self._face_present_since = 0.0
        self._mouth_open_ratio = 0.0
        self._speech_no_lips_since = 0.0
        self._audio_ok = False
        self._store_open = False
        self._liveness_external = False  # LIVENESS_FAIL считает IdentityVerifier
        self._engine_composite = False   # движок сам собирает «речь без губ»

    # --------------------------------------------------------------- загрузка
    def load_modules(self) -> None:
        """Подключить соседние модули. Любая ошибка -> канал выключен, не падаем."""
        cfg, cd = self.cfg, self.cfg_dict

        if cfg.enable_gaze and not cfg.headless:
            self.face = self._instantiate("detectors.face_mesh", "FaceAnalyzer", cd, "лицо/взгляд")
        if cfg.enable_vision and not cfg.headless:
            self.objects = self._instantiate("detectors.objects", "ObjectDetector", cd, "объекты")
        if cfg.enable_identity and not cfg.headless:
            self.identity = self._instantiate("detectors.identity", "IdentityVerifier", cd, "личность")
        if cfg.enable_audio:
            self.audio = self._instantiate("detectors.audio", "AudioMonitor", cd, "аудио")
        if cfg.enable_env:
            self.env_check = self._import_attr("env_checks", "run_all_checks")
            if self.env_check is None:
                self.degraded.append("проверки окружения")

        self.engine = self._instantiate("engine.events", "EventEngine", cd, "движок событий")
        if self.engine is None:
            self.engine = _FallbackEventEngine(cd)
            self._mark_degraded("движок событий", "движок событий (встроенная замена)")
        else:
            # движок умеет составные правила -> отдаём ему СЫРЬЁ (речь + раскрытие
            # рта), а связку «речь без движения губ» он строит сам. Иначе две
            # реализации одной эвристики будут дёргать один автомат в разные стороны.
            events_mod = self._import_module("engine.events")
            composite = getattr(events_mod, "COMPOSITE_INPUTS", None)
            self._engine_composite = isinstance(composite, dict) and "audio.speech" in composite

        self.risk = self._instantiate("engine.risk", "RiskScorer", cd, "risk-score")
        if self.risk is None:
            self.risk = _FallbackRiskScorer(cd)
            self._mark_degraded("risk-score", "risk-score (встроенная замена)")

        self.fusion = self._instantiate("engine.fusion", "FusionEngine", cd, "fusion")
        self.store = self._instantiate("storage.db", "EvidenceStore", cd, "хранилище")
        if self.store is None:
            self.store = _FallbackEvidenceStore(cd)
            self._mark_degraded("хранилище", "хранилище (JSONL вместо SQLite)")

        # кольцевой буфер кадров: снимки с рамкой и клипы «до/после» инцидента
        if not cfg.headless and cfg.save_evidence:
            self.recorder = self._instantiate("storage.evidence", "EvidenceRecorder", cd,
                                              "запись доказательств")

        self.report_mod = self._import_module("storage.report")

        # калибровка взгляда: персональные пороги и карта экрана
        if cfg.enable_gaze and not cfg.headless:
            calib_cls = self._import_attr("engine.calibration", "GazeCalibration")
            if calib_cls is not None:
                try:
                    self.gaze_calib = calib_cls(cd, str(cfg.sessions_path))
                except Exception as exc:
                    log.warning("калибровка взгляда недоступна: %s", exc)
                    self.gaze_calib = None
            if self.gaze_calib is None:
                self.degraded.append("калибровка взгляда (упрощённая)")

        self.caps["gaze"] = self._module_ok(self.face)
        self.caps["vision"] = self._module_ok(self.objects)
        self.caps["identity"] = self._module_ok(self.identity)
        self.caps["audio"] = self._module_ok(self.audio)
        self.caps["env"] = self.env_check is not None

        # Модуль, который импортировался, но не работает (нет модели/библиотеки),
        # отключаем полностью. Иначе цикл кадров будет дёргать его 15 раз в секунду
        # и получать пустые наблюдения — а пустое наблюдение «лица нет» ничем не
        # отличается от настоящего и породило бы ложные NO_FACE.
        if not self.caps["gaze"]:
            self.face = None
        if not self.caps["vision"]:
            self.objects = None
        if not self.caps["identity"]:
            self.identity = None
        if not self.caps["audio"]:
            self.audio = None

        # проверка живости: отдаём её модулю личности, если он это умеет
        if self.caps["identity"] and hasattr(self.identity, "update_liveness"):
            ok = True
            if hasattr(self.identity, "liveness_available"):
                with contextlib.suppress(Exception):
                    ok = bool(self.identity.liveness_available())
            self._liveness_external = ok

        if not self.cfg.headless:
            self.capture = CameraCapture(
                index=cfg.camera_index,
                width=cfg.frame_width,
                height=cfg.frame_height,
                fps=cfg.target_fps,
                reopen_delay=cfg.camera_reopen_delay,
                max_read_failures=cfg.camera_max_read_failures,
                read_timeout=cfg.camera_read_timeout,
                on_state_change=self._on_camera_state,
            )

    def _mark_degraded(self, replace: str, note: str) -> None:
        """Заменить запись в списке деградаций (модуль -> встроенная замена)."""
        self.degraded = [d for d in self.degraded if d != replace]
        self.degraded.append(note)

    @staticmethod
    def _import_module(path: str) -> Any:
        try:
            return importlib.import_module(path)
        except Exception as exc:
            log.warning("модуль %s недоступен: %s", path, exc)
            return None

    def _import_attr(self, path: str, attr: str) -> Any:
        mod = self._import_module(path)
        if mod is None:
            return None
        value = getattr(mod, attr, None)
        if value is None:
            log.warning("в модуле %s нет %s", path, attr)
        return value

    def _instantiate(self, path: str, attr: str, cfg: dict[str, Any], human: str) -> Any:
        """Импортировать класс и создать экземпляр. Ошибка -> None + запись в degraded."""
        klass = self._import_attr(path, attr)
        if klass is None:
            self.degraded.append(human)
            return None
        try:
            try:
                obj = klass(cfg)
            except TypeError:  # конструктор без аргументов — тоже допустим
                obj = klass()
        except Exception as exc:
            log.warning("не удалось создать %s.%s (%s): %s", path, attr, human, exc)
            self.degraded.append(human)
            return None
        if hasattr(obj, "available"):
            try:
                if not obj.available():
                    log.warning("%s: модуль есть, но недоступен (нет модели/библиотеки)", human)
                    self.degraded.append(f"{human} (недоступен)")
            except Exception as exc:
                log.warning("%s: available() упал: %s", human, exc)
        return obj

    @staticmethod
    def _module_ok(obj: Any) -> bool:
        if obj is None:
            return False
        if hasattr(obj, "available"):
            try:
                return bool(obj.available())
            except Exception:
                return False
        return True

    # ------------------------------------------------------------------ запуск
    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._shutdown_done = asyncio.Event()
        self._install_signals()

        await self._start_server()

        if self.capture is not None:
            started = self.capture.start()
            if not started:
                log.warning("камера не запущена (нет opencv) — работаем без видео")
                self.degraded.append("камера")
            else:
                self._cv_thread = threading.Thread(target=self._cv_loop, name="cv", daemon=True)
                self._cv_thread.start()

        if self.audio is not None and self.caps["audio"]:
            try:
                self.audio.start()
                self._audio_ok = True
            except Exception as exc:
                log.warning("аудио не запустилось: %s", exc)
                self.caps["audio"] = False

        self._tasks = [
            asyncio.create_task(self._consume_cv(), name="consume"),
            asyncio.create_task(self._status_ticker(), name="status"),
            asyncio.create_task(self._risk_ticker(), name="risk"),
        ]
        if self.caps["env"]:
            self._tasks.append(asyncio.create_task(self._env_ticker(), name="env"))
        if self.caps["audio"]:
            self._tasks.append(asyncio.create_task(self._audio_ticker(), name="audio"))
        if self.cfg.mock:
            self._tasks.append(asyncio.create_task(self._mock_ticker(), name="mock"))

        log.info("сайдкар слушает ws://%s:%s (версия %s)", self.cfg.ws_host, self.cfg.ws_port, VERSION)
        log.info("каналы: %s", ", ".join(f"{k}={'да' if v else 'нет'}" for k, v in self.caps.items()))
        if self.degraded:
            log.info("в деградированном режиме: %s", "; ".join(sorted(set(self.degraded))))

        await self._shutdown_done.wait()

    def _install_signals(self) -> None:
        if self.loop is None:
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                self.loop.add_signal_handler(
                    sig, lambda s=sig: asyncio.create_task(self.shutdown(f"сигнал {s.name}")))

    async def _start_server(self) -> None:
        try:
            from websockets.asyncio.server import serve  # websockets >= 12
        except Exception:  # pragma: no cover — старые версии библиотеки
            from websockets.server import serve  # type: ignore

        self._server = await serve(
            self._ws_handler,
            self.cfg.ws_host,
            self.cfg.ws_port,
            ping_interval=self.cfg.ws_ping_interval,
            ping_timeout=self.cfg.ws_ping_interval,
            max_size=self.cfg.ws_max_message,
        )

    # ------------------------------------------------------------- WebSocket
    async def _ws_handler(self, ws: Any, path: str | None = None) -> None:
        self.clients.add(ws)
        log.info("оболочка подключилась (клиентов: %d)", len(self.clients))
        try:
            await self._send(ws, self._hello_message())
            await self._send(ws, self._status_message())
            async for raw in ws:
                await self._on_message(ws, raw)
        except Exception as exc:
            log.debug("соединение закрыто: %s", exc)
        finally:
            self.clients.discard(ws)
            log.info("оболочка отключилась (клиентов: %d)", len(self.clients))

    async def _send(self, ws: Any, msg: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await ws.send(encode(msg))

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        if not self.clients:
            return
        raw = encode(msg)
        dead: list[Any] = []
        for ws in list(self.clients):
            try:
                await ws.send(raw)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def _hello_message(self) -> dict[str, Any]:
        return envelope(
            MsgType.HELLO,
            capabilities=dict(self.caps),
            version=VERSION,
            protocol=PROTOCOL_VERSION,
            degraded=sorted(set(self.degraded)),
            headless=self.cfg.headless,
            mock=self.cfg.mock,
        )

    def _status_message(self) -> dict[str, Any]:
        with self._status_lock:
            snap = dict(self._snap)
            snap["gaze"] = dict(self._snap["gaze"])
        if self.capture is not None:
            snap["fps"] = self.capture.fps
        elif self.cfg.mock:
            snap["fps"] = float(self.cfg.target_fps)
        snap["audio_ok"] = bool(self._audio_ok)
        snap["risk"] = self._risk_score()
        snap["state"] = self.session.state.value
        return envelope(MsgType.STATUS, **snap)

    async def _error(self, code: str, message: str, fatal: bool = False) -> None:
        await self._broadcast(envelope(MsgType.ERROR, code=code, message=message, fatal=fatal))

    # ---------------------------------------------------- приём команд оболочки
    async def _on_message(self, ws: Any, raw: Any) -> None:
        # Во время останова команды уже не исполняем. Хендлер соединения живёт
        # параллельно с shutdown(), и session_start, пришедший после закрытия
        # сессии, создавал новый каталог sessions/<ts>_<id>/ — пустой, без отчёта
        # и без закрытой hash-цепочки. Он же оказывался «последней сессией» для
        # `make report`, то есть прятал настоящий отчёт прогона.
        if self._shutting_down:
            return
        try:
            msg = decode(raw)
        except Exception:
            await self._send(ws, envelope(MsgType.ERROR, code="bad_json",
                                          message="Сообщение не является корректным JSON",
                                          fatal=False))
            return
        if not isinstance(msg, dict):
            return
        mtype = str(msg.get("type", ""))
        try:
            if mtype == MsgType.SESSION_START.value:
                await self._cmd_session_start(msg)
            elif mtype == MsgType.SESSION_END.value:
                await self._cmd_session_end(msg)
            elif mtype == MsgType.CALIBRATE.value:
                await self._cmd_calibrate(msg)
            elif mtype == MsgType.TELEMETRY.value:
                await self._cmd_telemetry(msg)
            elif mtype == MsgType.SHELL_EVENT.value:
                await self._cmd_shell_event(msg)
            elif mtype == MsgType.COMMAND.value:
                await self._cmd_command(msg)
            else:
                await self._send(ws, envelope(MsgType.ERROR, code="unknown_type",
                                              message=f"Неизвестный тип сообщения: {mtype}",
                                              fatal=False))
        except SessionStateError as exc:
            await self._send(ws, envelope(MsgType.ERROR, code="bad_state",
                                          message=str(exc), fatal=False))
        except Exception as exc:
            log.exception("ошибка обработки %s", mtype)
            await self._send(ws, envelope(MsgType.ERROR, code="handler_error",
                                          message=f"Ошибка обработки {mtype}: {exc}",
                                          fatal=False))

    async def _cmd_session_start(self, msg: dict[str, Any]) -> None:
        if self.session.active:
            await self._cmd_session_end({"reason": "перезапуск сессии"})
        student_id = str(msg.get("student_id") or "anon")
        exam_id = str(msg.get("exam_id") or "")
        student_name = str(msg.get("student_name") or "")
        session_dir = self.session.start(student_id, exam_id, student_name)
        log.info("сессия начата: %s (%s)", self.session.session_id, student_name or student_id)

        if self.gaze_calib is not None:
            self.gaze_calib.session_dir = str(session_dir)
        if self.recorder is not None:
            # set_session_dir() внутри делает flush(wait=True): если клипы прошлой
            # сессии ещё пишутся, он ждёт до 10 с. В лупе это 10 с без status и
            # событий — оболочка решит, что сайдкар умер. Поэтому в поток.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.recorder.set_session_dir, session_dir)
        self._reset_engines()
        self._store_open = False
        if self.store is not None:
            try:
                await asyncio.to_thread(self.store.open_session, self.session.meta())
                self._store_open = True
            except Exception as exc:
                log.warning("хранилище не открылось: %s", exc)
                await self._error("store_open", f"Хранилище доказательств недоступно: {exc}")

        await self._emit(self._system_event(EventKind.SESSION_STARTED, {
            "student_id": student_id, "exam_id": exam_id,
            "student_name": student_name, "session_dir": str(session_dir),
            "capabilities": dict(self.caps),
        }))
        await self._broadcast(self._status_message())

    async def _cmd_session_end(self, msg: dict[str, Any]) -> None:
        if not self.session.active:
            return
        reason = str(msg.get("reason") or "завершение по команде оболочки")
        await self._emit(self._system_event(EventKind.SESSION_ENDED, {"reason": reason}))
        self.session.set_risk(self._risk_score(), self._risk_action().value)
        summary = self.session.end(reason)
        # движки гасим сразу, до любых await: иначе тикер риска успеет прислать
        # вердикт по сессии, которой уже нет
        self._reset_engines()
        self._last_action = VerdictAction.NONE
        self._last_risk_sent = -1.0
        if self.recorder is not None:
            # клипы должны лечь на диск до сборки отчёта
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.recorder.flush, True, 10.0)
        if self.store is not None and self._store_open:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.store.close_session, summary)
        self._store_open = False
        await self._build_report(summary)
        log.info("сессия завершена: %s (событий: %s)", reason, summary.get("events_total"))
        await self._broadcast(self._status_message())

    async def _cmd_calibrate(self, msg: dict[str, Any]) -> None:
        stage = str(msg.get("stage") or "gaze_center")
        point = msg.get("point")
        point = [float(point[0]), float(point[1])] if isinstance(point, (list, tuple)) and len(point) >= 2 else None

        if self.session.state is SessionState.RUNNING and self.session.can(SessionState.CALIBRATING):
            with contextlib.suppress(SessionStateError):
                self.session.transition(SessionState.CALIBRATING, f"калибровка {stage}")

        if stage == "voice":
            await self._calibrate_voice()
            return

        if stage not in ("gaze_center", "gaze_grid", "identity"):
            await self._error("unknown_stage", f"Неизвестная стадия калибровки: {stage}")
            return

        module = self.face if stage.startswith("gaze") else self.identity
        if self._cv_thread is None or not self._module_ok(module):
            await self._finish_calibration_async(stage, point, {
                "ok": True, "degraded": True,
                "reason": "камера или модуль недоступны, калибровка пропущена",
            })
            return

        needed = self.cfg.calibration_samples
        result_extra: dict[str, Any] = {}
        if stage.startswith("gaze") and self.gaze_calib is not None:
            try:
                if stage == "gaze_center":
                    self.gaze_calib.begin_center()
                else:
                    index = int(self.gaze_calib.begin_point(point))
                    result_extra["point_index"] = index
                    result_extra["grid_points"] = [list(p) for p in self.gaze_calib.grid_points()]
            except Exception as exc:
                log.warning("не удалось начать стадию %s: %s", stage, exc)

        job = _CalibrationJob(stage=stage, point=point, needed=needed)
        with self._calib_lock:
            self._calib = job
        await self._broadcast(envelope(MsgType.CALIBRATION, stage=stage, progress=0.0,
                                       done=False, result=result_extra))

    async def _calibrate_voice(self) -> None:
        if not self._module_ok(self.audio) or not hasattr(self.audio, "enroll_owner"):
            await self._finish_calibration_async("voice", None, {
                "ok": True, "degraded": True, "reason": "микрофон недоступен, калибровка голоса пропущена"})
            return
        await self._broadcast(envelope(MsgType.CALIBRATION, stage="voice", progress=0.1,
                                       done=False, result={}))
        try:
            ok = await asyncio.to_thread(self.audio.enroll_owner, self.cfg.audio_enroll_seconds)
        except Exception as exc:
            await self._finish_calibration_async("voice", None, {
                "ok": False, "reason": f"ошибка записи образца голоса: {exc}"})
            return
        await self._finish_calibration_async("voice", None, {"ok": bool(ok)})

    async def _cmd_telemetry(self, msg: dict[str, Any]) -> None:
        """Телеметрия ввода уходит в FusionEngine — он может вернуть события."""
        if self.fusion is None or not hasattr(self.fusion, "note_telemetry"):
            return
        try:
            events = self.fusion.note_telemetry(dict(msg))
        except Exception as exc:
            log.warning("fusion не принял телеметрию: %s", exc)
            return
        await self._emit(self._normalize_events(events))

    async def _cmd_shell_event(self, msg: dict[str, Any]) -> None:
        kind = _as_kind(msg.get("kind"))
        if kind is None:
            await self._error("unknown_kind", f"Неизвестный вид события: {msg.get('kind')}")
            return
        detail = msg.get("detail")
        detail = dict(detail) if isinstance(detail, dict) else {}
        detail.setdefault("source", "shell")
        await self._emit(self._push_external(kind, detail))

    async def _cmd_command(self, msg: dict[str, Any]) -> None:
        name = str(msg.get("name") or "")
        if name == "snapshot":
            path = await asyncio.to_thread(self._snapshot)
            if path:
                log.info("снапшот сохранён: %s", path)
            else:
                await self._error("snapshot_failed", "Снимок не сохранён: нет кадра или сессии")
        elif name == "reset_risk":
            # _last_action намеренно не трогаем: пусть _push_risk увидит смену
            # уровня и сам снимет pause (locked остаётся — это решение проктора)
            self._reset_engines()
            await self._push_risk(force=True)
            log.info("risk-score сброшен по команде оболочки")
        elif name == "export_report":
            await self._build_report(self.session.summary())
        else:
            await self._error("unknown_command", f"Неизвестная команда: {name}")

    # ------------------------------------------------------------ поток CV
    def _cv_loop(self) -> None:
        """Блокирующий цикл обработки кадров. Живёт в отдельном потоке.

        Каждая итерация обёрнута в try/except целиком. Причина: смерть этого
        потока необратима и НЕВИДИМА — поток камеры продолжает работать, fps в
        status остаётся живым, а события зрения, взгляда и личности просто
        перестают появляться. На демо это выглядит как «система ничего не
        замечает». Поэтому любая неожиданная ошибка (детектор вернул numpy-массив
        там, где ждали число; рекордер упал на кропе; калибровка получила мусор)
        стоит один кадр, а не весь показ.
        """
        cfg = self.cfg
        frame_idx = 0
        last_identity = 0.0
        no_frame_since = 0.0
        failures = 0

        while not self._stop.is_set():
            try:
                assert self.capture is not None
                ok, frame, ts = self.capture.read(timeout=cfg.camera_read_timeout)
                if not ok or frame is None:
                    now = time.time()
                    if no_frame_since == 0.0:
                        no_frame_since = now
                    events = self._push_observations([(EventKind.SENSOR_LOST, True,
                                                       {"sensor": "camera",
                                                        "error": self.capture.last_error})], now)
                    self._post_events(events)
                    self._update_snap(face_present=False, face_count=0, phone=False)
                    continue

                if no_frame_since:
                    no_frame_since = 0.0
                    self._post_events(self._push_observations(
                        [(EventKind.SENSOR_LOST, False, {})], ts))

                frame_idx += 1
                with self._frame_lock:
                    self._last_frame = frame
                    self._last_frame_ts = ts
                if self.recorder is not None:
                    # кольцевой буфер: кадры «до» инцидента нужны для клипа
                    self._safe_call(self.recorder.push, frame, ts, what="буфер кадров")

                obs: list[tuple[EventKind, bool, dict[str, Any]]] = []
                face_obs: Any = None
                face_bbox: Any = None

                # --- лицо и взгляд: каждый кадр (дешёвый FaceMesh)
                if self.face is not None and frame_idx % max(cfg.face_every_n_frames, 1) == 0:
                    face_obs = self._safe_call(self.face.analyze, frame, what="FaceMesh")
                    if face_obs is not None:
                        face_bbox = _get(face_obs, "face_bbox")
                        obs.extend(self._safe_call(self._face_observations, face_obs, ts,
                                                   what="разбор лица") or [])

                # --- объекты: каждый N-й кадр (YOLO дорогой)
                if self.objects is not None and frame_idx % max(cfg.yolo_every_n_frames, 1) == 0:
                    dets = self._safe_call(_call_flex, self.objects.detect, frame, face_bbox,
                                           what="YOLO")
                    obs.extend(self._safe_call(self._object_observations, list(dets or []),
                                               face_obs, face_bbox, ts,
                                               what="разбор объектов") or [])

                # --- личность: по таймеру (эмбеддинг дорогой, каждый кадр не нужен)
                if (self.identity is not None and face_bbox is not None
                        and ts - last_identity >= cfg.identity_interval):
                    last_identity = ts
                    id_obs = self._safe_call(_call_flex, self.identity.verify, frame, face_bbox,
                                             what="identity")
                    obs.extend(self._safe_call(self._identity_observations, id_obs,
                                               what="разбор личности") or [])

                # --- живость: по кадрам, если модуль личности это умеет
                obs.extend(self._safe_call(self._liveness_observations, frame, face_obs,
                                           face_bbox, ts, what="разбор живости") or [])

                events = self._push_observations(obs, ts)
                if events:
                    self._safe_call(self._attach_evidence, events, frame, face_bbox,
                                    what="доказательства")
                    self._post_events(events)

                self._safe_call(self._handle_calibration, frame, face_obs, face_bbox, ts,
                                what="калибровка")
            except Exception:
                # Сюда попадаем только на том, что не закрыто _safe_call выше.
                failures += 1
                if failures <= 3 or failures % 100 == 0:
                    log.exception("сбой в цикле обработки кадров (#%d), продолжаю", failures)
                self._stop.wait(0.05)

        log.info("цикл обработки кадров остановлен (ошибок за сессию: %d)", failures)

    def _safe_call(self, fn: Any, *args: Any, what: str = "детектор") -> Any:
        """Вызов детектора: любое исключение глушим — демо важнее одного канала."""
        try:
            return fn(*args)
        except Exception as exc:
            log.warning("%s упал на кадре: %s", what, exc)
            return None

    # --------------------------------------------- наблюдения из FaceObservation
    def _face_observations(self, face: Any, ts: float) -> list[tuple[EventKind, bool, dict[str, Any]]]:
        """Признаки лица/взгляда -> наблюдения.

        Решение принимается в первую очередь по `gaze_zone` и нормированным
        девиациям (`gaze_dev_h`, `gaze_dev_v`, `head_dev_*`): там уже учтена
        персональная калибровка, |dev| > 1 означает выход за порог этого
        человека. Абсолютные пороги в градусах — запасной путь, когда
        детектор девиаций не отдал (нет калибровки, другой бэкенд).

        Liveness здесь — эвристика последней линии: лицо непрерывно в кадре,
        а морганий нет дольше `liveness_no_blink_sec` (живой человек мигает
        раз в 2–10 с) => перед камерой фото или зацикленное видео. Если
        доступен IdentityVerifier.update_liveness, используется он: там ещё
        статичность кропа и детектор петли, это надёжнее.
        """
        cfg = self.cfg
        count = int(_get(face, "face_count", 0) or 0)
        yaw = float(_get(face, "yaw", 0.0) or 0.0)
        pitch = float(_get(face, "pitch", 0.0) or 0.0)
        gaze_yaw = float(_get(face, "gaze_yaw", 0.0) or 0.0)
        gaze_pitch = float(_get(face, "gaze_pitch", 0.0) or 0.0)
        dev_h = float(_get(face, "gaze_dev_h", 0.0) or 0.0)
        dev_v = float(_get(face, "gaze_dev_v", 0.0) or 0.0)
        head_dev_yaw = float(_get(face, "head_dev_yaw", 0.0) or 0.0)
        head_dev_pitch = float(_get(face, "head_dev_pitch", 0.0) or 0.0)
        zone = str(_get(face, "gaze_zone", "unknown") or "unknown").lower()
        blink = bool(_get(face, "blink", False))
        ear = float(_get(face, "eye_aspect_ratio", 1.0) or 1.0)
        blink_stale = bool(_get(face, "blink_stale", False))
        since_blink = float(_get(face, "seconds_since_blink", 0.0) or 0.0)
        self._mouth_open_ratio = float(_get(face, "mouth_open_ratio", 0.0) or 0.0)

        present = count >= 1
        if present:
            if self._face_present_since == 0.0:
                self._face_present_since = ts
                self._last_blink_ts = ts
            if blink or ear <= cfg.blink_ear_threshold:
                self._last_blink_ts = ts
        else:
            self._face_present_since = 0.0
            self._last_blink_ts = 0.0

        # девиации есть -> решаем по ним; нет -> по абсолютным порогам
        has_dev = abs(dev_h) > 0.0 or abs(dev_v) > 0.0
        if has_dev:
            down = zone in ("down", "bottom") or dev_v <= -cfg.gaze_dev_limit
            side = zone in ("left", "right", "side") or abs(dev_h) >= cfg.gaze_dev_limit
            off = (zone in ("off", "off_screen", "away", "outside")
                   or abs(dev_h) >= cfg.gaze_off_dev_limit
                   or abs(dev_v) >= cfg.gaze_off_dev_limit)
        else:
            down = zone in ("down", "bottom") or gaze_pitch <= -cfg.gaze_pitch_down_limit
            side = zone in ("left", "right", "side") or abs(gaze_yaw) >= cfg.gaze_yaw_limit
            off = (zone in ("off", "off_screen", "away", "outside")
                   or abs(gaze_yaw) >= cfg.gaze_off_screen_limit
                   or abs(gaze_pitch) >= cfg.gaze_off_screen_limit)

        if abs(head_dev_yaw) > 0.0 or abs(head_dev_pitch) > 0.0:
            turned = (abs(head_dev_yaw) >= cfg.head_dev_limit
                      or abs(head_dev_pitch) >= cfg.head_dev_limit)
        else:
            turned = abs(yaw) >= cfg.head_yaw_limit or abs(pitch) >= cfg.head_pitch_limit

        no_blink = present and (blink_stale or (
            self._last_blink_ts > 0.0 and ts - self._last_blink_ts >= cfg.liveness_no_blink_sec))

        self._update_snap(face_present=present, face_count=count,
                          gaze={"yaw": round(gaze_yaw, 2), "pitch": round(gaze_pitch, 2),
                                "zone": zone})

        detail = {"zone": zone, "gaze_yaw": round(gaze_yaw, 2), "gaze_pitch": round(gaze_pitch, 2),
                  "gaze_dev_h": round(dev_h, 2), "gaze_dev_v": round(dev_v, 2),
                  "head_yaw": round(yaw, 1), "head_pitch": round(pitch, 1),
                  "region": str(_get(face, "gaze_region", "") or "")}
        obs: list[tuple[EventKind | str, bool, dict[str, Any]]] = [
            (EventKind.NO_FACE, count == 0, {"faces": count}),
            (EventKind.SECOND_FACE, count >= 2,
             {"faces": count, "second_face_bbox": _get(face, "second_face_bbox")}),
            (EventKind.GAZE_DOWN, present and down and not off, detail),
            (EventKind.GAZE_SIDE, present and side and not off, detail),
            (EventKind.GAZE_OFF_SCREEN, present and off, detail),
            (EventKind.HEAD_TURNED, present and turned, detail),
        ]
        if not self._liveness_external:
            obs.append((EventKind.LIVENESS_FAIL, no_blink, {
                "no_blink_sec": round(since_blink or (ts - self._last_blink_ts), 1),
                "source": "blink_timeout",
            }))
        if self._engine_composite and present:
            # сырьё для составного правила «речь без движения губ»
            obs.append(("face.mouth_open_ratio",
                        self._mouth_open_ratio >= cfg.mouth_open_speech,
                        {"value": round(self._mouth_open_ratio, 3),
                         "mouth_open_speech": cfg.mouth_open_speech}))
        return obs

    def _liveness_observations(self, frame: Any, face: Any, bbox: Any,
                               ts: float) -> list[tuple[EventKind, bool, dict[str, Any]]]:
        """LIVENESS_FAIL по данным IdentityVerifier (моргания + статика + петля)."""
        if not self._liveness_external or bbox is None:
            return []
        blink = bool(_get(face, "blink", False))
        with contextlib.suppress(Exception):
            self.identity.note_blink(blink, ts)
        obs = self._safe_call(_call_flex, self.identity.update_liveness, frame, bbox, blink, ts,
                              what="liveness")
        if obs is None:
            return []
        suspect = bool(_get(obs, "suspect", False))
        score = round(float(_get(obs, "score", 0.0) or 0.0), 3)
        since_blink = round(float(_get(obs, "seconds_since_blink", 0.0) or 0.0), 1)
        detail = {
            "liveness_score": score,
            "conf": max(min(score, 1.0), 0.3),   # подозрительность = уверенность инцидента
            "no_blink": bool(_get(obs, "no_blink", False)),
            "no_blink_sec": since_blink,          # для шаблона сообщения движка
            "static_frame": bool(_get(obs, "static_frame", False)),
            "loop_detected": bool(_get(obs, "loop_detected", False)),
            "loop_period_s": round(float(_get(obs, "loop_period_s", 0.0) or 0.0), 2),
            "seconds_since_blink": since_blink,
            "source": "identity.update_liveness",
        }
        return [(EventKind.LIVENESS_FAIL, suspect, detail)]

    # -------------------------------------------------- наблюдения из детекций
    def _object_observations(self, dets: Iterable[Any], face: Any, bbox: Any,
                             ts: float) -> list[tuple[EventKind, bool, dict[str, Any]]]:
        """Телефон/посторонние предметы -> наблюдения.

        Если у детектора есть собственный метод `observations()` (он считает
        поднятие и наведение по трекам, а не по одному кадру) — используем его:
        трекинг устойчивее к мигающим детекциям. Иначе считаем сами по списку
        детекций, см. `_object_observations_fallback`.
        """
        detector_obs = getattr(self.objects, "observations", None)
        if callable(detector_obs):
            data = self._safe_call(_call_flex, detector_obs, bbox, ts, what="YOLO observations")
            if isinstance(data, dict):
                return self._object_observations_from_dict(data)
        return self._object_observations_fallback(dets, face, ts)

    def _object_observations_from_dict(
        self, data: dict[str, Any]
    ) -> list[tuple[EventKind, bool, dict[str, Any]]]:
        """Разобрать готовые наблюдения детектора объектов."""
        def flag(key: str) -> tuple[bool, dict[str, Any]]:
            value = data.get(key)
            if isinstance(value, dict):  # {active, detail, ...}
                detail = value.get("detail")
                detail = dict(detail) if isinstance(detail, dict) else {}
                detail.update({k: v for k, v in value.items() if k != "detail"})
                return bool(value.get("active")), detail
            return bool(value), {}

        phone_in_frame, phone_detail = flag("phone_in_frame")
        phone_detail.setdefault("conf", data.get("phone_conf", 0.0))
        phone_detail.setdefault("count", data.get("phone_count", 0))
        phone_detail.setdefault("explain", data.get("phone_explain", ""))
        raised, raised_detail = flag("phone_raised")
        aimed, aimed_detail = flag("phone_aimed_at_screen")
        forbidden, forbidden_detail = flag("forbidden_object")
        labels = data.get("forbidden_labels") or []
        forbidden_detail.setdefault("labels", labels)
        forbidden_detail.setdefault("explain", data.get("forbidden_explain", ""))
        if labels:
            forbidden_detail.setdefault("label", labels[0])

        self._update_snap(phone=phone_in_frame)
        return [
            (EventKind.PHONE_IN_FRAME, phone_in_frame, phone_detail),
            (EventKind.PHONE_RAISED, raised, raised_detail),
            (EventKind.PHONE_AIMED_AT_SCREEN, aimed, aimed_detail),
            (EventKind.FORBIDDEN_OBJECT, forbidden, forbidden_detail),
        ]

    def _object_observations_fallback(self, dets: Iterable[Any], face: Any,
                                      ts: float) -> list[tuple[EventKind, bool, dict[str, Any]]]:
        """Собственная эвристика по списку детекций (контрактный минимум).

        PHONE_RAISED: центр телефона выше центра лица (с запасом
        `phone_raised_margin` от высоты кадра). PHONE_AIMED_AT_SCREEN: телефон
        поднят, держится вертикально (h/w >= phone_vertical_ratio) и взгляд
        смотрит в его сторону — классическая съёмка экрана или чтение подсказки.
        """
        cfg = self.cfg
        phone_labels = {s.lower() for s in cfg.phone_labels}
        forbidden_labels = {s.lower() for s in cfg.forbidden_labels}
        h_frame = max(float(cfg.frame_height), 1.0)

        phone: dict[str, Any] | None = None
        forbidden: dict[str, Any] | None = None
        for det in dets or []:
            label = str(_get(det, "label", "")).lower()
            conf = float(_get(det, "conf", 0.0) or 0.0)
            area = float(_get(det, "area_ratio", 0.0) or 0.0)
            bbox = _get(det, "bbox") or (0.0, 0.0, 0.0, 0.0)
            if conf < cfg.yolo_conf:
                continue
            row = {"label": label, "conf": round(conf, 3), "area_ratio": round(area, 4),
                   "bbox": [float(v) for v in bbox]}
            if label in phone_labels and area >= cfg.phone_min_area_ratio:
                if phone is None or conf > phone["conf"]:
                    phone = row
            elif label in forbidden_labels:
                if forbidden is None or conf > forbidden["conf"]:
                    forbidden = row

        raised = aimed = False
        if phone is not None:
            x, y, w, h = (list(phone["bbox"]) + [0.0, 0.0, 0.0, 0.0])[:4]
            cx, cy = x + w / 2.0, y + h / 2.0
            face_cy = h_frame / 2.0
            fb = _get(face, "face_bbox")
            if fb:
                fx, fy, fw, fh = (list(fb) + [0.0, 0.0, 0.0, 0.0])[:4]
                face_cy = fy + fh / 2.0
            raised = cy <= face_cy + cfg.phone_raised_margin * h_frame
            vertical = h >= cfg.phone_vertical_ratio * max(w, 1.0)
            gaze_yaw = float(_get(face, "gaze_yaw", 0.0) or 0.0)
            zone = str(_get(face, "gaze_zone", "") or "").lower()
            frame_cx = max(float(cfg.frame_width), 1.0) / 2.0
            # взгляд в сторону телефона: знак смещения телефона совпадает со знаком yaw
            toward = zone in ("down", "bottom") or (abs(gaze_yaw) >= 8.0
                                                    and (cx - frame_cx) * gaze_yaw > 0)
            aimed = raised and vertical and toward
            phone.update({"raised": raised, "vertical": vertical, "aimed": aimed})

        self._update_snap(phone=phone is not None)
        empty: dict[str, Any] = {}
        return [
            (EventKind.PHONE_IN_FRAME, phone is not None, phone or empty),
            (EventKind.PHONE_RAISED, raised, phone or empty),
            (EventKind.PHONE_AIMED_AT_SCREEN, aimed, phone or empty),
            (EventKind.FORBIDDEN_OBJECT, forbidden is not None, forbidden or empty),
        ]

    def _identity_observations(self, id_obs: Any) -> list[tuple[EventKind, bool, dict[str, Any]]]:
        """Сверка личности: считаем только реально проверенные кадры.

        Один промах ничего не значит (ракурс, свет, частичное перекрытие),
        поэтому нужна серия `identity_fail_streak` подряд. Если модуль уже
        отдаёт своё окно голосования (`votes`/`mismatch_ratio`), доверяем ему
        и просто транслируем `match`.
        """
        if id_obs is None:
            return []
        enrolled = bool(_get(id_obs, "enrolled", False))
        if not enrolled:
            self._update_snap(identity_ok=True)
            return []
        # кадр мог быть пропущен самим модулем (нет лица, свой интервал проверки)
        if "checked" in getattr(id_obs, "__dict__", {}) or isinstance(id_obs, dict):
            if not bool(_get(id_obs, "checked", True)):
                return []
        match = bool(_get(id_obs, "match", True))
        similarity = float(_get(id_obs, "similarity", 0.0) or 0.0)
        votes = int(_get(id_obs, "votes", 0) or 0)
        if match:
            self._identity_fail_streak = 0
        else:
            self._identity_fail_streak += 1
        active = (not match and votes > 0) or self._identity_fail_streak >= self.cfg.identity_fail_streak
        self._update_snap(identity_ok=not active)
        return [(EventKind.IDENTITY_MISMATCH, active, {
            "similarity": round(similarity, 3),
            "threshold": round(float(_get(id_obs, "threshold", self.cfg.identity_threshold)
                                     or self.cfg.identity_threshold), 3),
            "streak": self._identity_fail_streak,
            "votes": votes,
            "mismatch_ratio": round(float(_get(id_obs, "mismatch_ratio", 0.0) or 0.0), 3),
            "reason": str(_get(id_obs, "reason", "") or ""),
            "conf": min(1.0, 0.5 + 0.1 * self._identity_fail_streak),
        })]

    # ------------------------------------------------------- мост поток -> луп
    def _push_observations(self, obs: list[tuple[EventKind | str, bool, dict[str, Any]]],
                           ts: float) -> list[ProctorEvent]:
        """Отдать наблюдения в EventEngine. Единственная точка создания событий.

        Имя наблюдения — либо `EventKind.value`, либо вход составного правила
        движка (например `audio.speech`): движок сам решает, что из этого
        складывается в инцидент.
        """
        if not obs or self.engine is None:
            return []
        out: list[ProctorEvent] = []
        with self._engine_lock:
            for kind, active, detail in obs:
                name = kind.value if isinstance(kind, EventKind) else str(kind)
                try:
                    produced = self.engine.push_observation(name, bool(active), ts,
                                                            **(detail or {}))
                except Exception as exc:
                    log.warning("EventEngine отказал на %s: %s", name, exc)
                    continue
                out.extend(self._normalize_events(produced))
        return out

    def _push_external(self, kind: EventKind, detail: dict[str, Any]) -> list[ProctorEvent]:
        if self.engine is None:
            return [make_event(kind, detail=detail)]
        with self._engine_lock:
            try:
                produced = self.engine.push_external(kind, dict(detail or {}))
            except Exception as exc:
                log.warning("EventEngine отказал на внешнем %s: %s", kind.value, exc)
                return []
        return self._normalize_events(produced)

    def _system_event(self, kind: EventKind, detail: dict[str, Any]) -> list[ProctorEvent]:
        """Служебное событие (старт/конец сессии, калибровка).

        Пропускаем через движок, чтобы текст для отчёта формировался в одном
        месте; если движок его проглотил (cooldown, незнакомый вид) — создаём
        сами, такие события терять нельзя: на них держится хронология отчёта.
        """
        events = self._push_external(kind, detail)
        return events or [make_event(kind, detail=detail)]

    @staticmethod
    def _normalize_events(produced: Any) -> list[ProctorEvent]:
        """Движок мог вернуть None, одно событие или список — приводим к списку."""
        if produced is None:
            return []
        if isinstance(produced, ProctorEvent):
            return [produced]
        if isinstance(produced, (list, tuple, set)):
            return [e for e in produced if isinstance(e, ProctorEvent)]
        return []

    def _post_events(self, events: list[ProctorEvent]) -> None:
        if events:
            self._post(("events", events))

    def _post(self, item: tuple[str, Any]) -> None:
        """Переслать результат из CV-потока в event loop."""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._queue_put, item)

    def _queue_put(self, item: tuple[str, Any]) -> None:
        try:
            self._cv_queue.put_nowait(item)
        except asyncio.QueueFull:
            log.warning("очередь событий переполнена, отбрасываю %s", item[0])

    async def _consume_cv(self) -> None:
        while True:
            kind, payload = await self._cv_queue.get()
            try:
                if kind == "events":
                    await self._emit(payload)
                elif kind == "calibration":
                    await self._broadcast(envelope(MsgType.CALIBRATION, **payload))
                    if payload.get("done"):
                        self._after_calibration_stage(payload)
                elif kind == "error":
                    await self._broadcast(envelope(MsgType.ERROR, **payload))
            except Exception:
                log.exception("ошибка обработки %s из CV-потока", kind)

    # -------------------------------------------------------- конвейер событий
    async def _emit(self, events: list[ProctorEvent]) -> None:
        """События -> риск + fusion + хранилище + рассылка. Один путь для всех каналов."""
        if not events:
            return
        for ev in events:
            self.session.note_event(ev)
            if self.fusion is not None and hasattr(self.fusion, "note_event"):
                with contextlib.suppress(Exception):
                    self.fusion.note_event(ev)
            if self.risk is not None:
                with contextlib.suppress(Exception):
                    self.risk.add(ev)
            if self.store is not None and self._store_open and self.session.recording:
                try:
                    await asyncio.to_thread(self.store.append, ev)
                except Exception as exc:
                    log.warning("запись доказательства не удалась: %s", exc)
            log.info("[%s] %s %s", ev.severity.value, ev.kind.value, ev.message)
            await self._broadcast(event_message(ev))
        await self._push_risk()

    def _risk_score(self) -> float:
        if self.risk is None:
            return 0.0
        try:
            return round(float(self.risk.score), 2)
        except Exception:
            return 0.0

    def _risk_action(self) -> VerdictAction:
        if self.risk is not None and hasattr(self.risk, "action"):
            try:
                action = self.risk.action()
            except Exception:
                action = None
            if isinstance(action, VerdictAction):
                return action
            if isinstance(action, str):
                with contextlib.suppress(ValueError):
                    return VerdictAction(action)
        score = self._risk_score()
        if score >= self.cfg.risk_lock:
            return VerdictAction.LOCK
        if score >= self.cfg.risk_pause:
            return VerdictAction.PAUSE
        if score >= self.cfg.risk_warn:
            return VerdictAction.WARN
        return VerdictAction.NONE

    def _risk_breakdown(self) -> list[dict[str, Any]]:
        if self.risk is None or not hasattr(self.risk, "breakdown"):
            return []
        try:
            rows = self.risk.breakdown() or []
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
        return out

    def _risk_level(self, action: VerdictAction) -> str:
        return {VerdictAction.NONE: "ok", VerdictAction.WARN: "warn",
                VerdictAction.PAUSE: "pause", VerdictAction.LOCK: "lock"}[action]

    async def _push_risk(self, force: bool = False) -> None:
        score = self._risk_score()
        action = self._risk_action()
        if not force and abs(score - self._last_risk_sent) < self.cfg.risk_send_delta \
                and action is self._last_action:
            return
        self._last_risk_sent = score
        breakdown = self._risk_breakdown()
        await self._broadcast(envelope(MsgType.RISK, score=score, level=self._risk_level(action),
                                       breakdown=breakdown, action=action.value))
        if action is not self._last_action:
            await self._apply_action(action, score, breakdown)

    async def _apply_action(self, action: VerdictAction, score: float,
                            breakdown: list[dict[str, Any]]) -> None:
        """Смена уровня -> verdict + перевод сессии в paused/locked."""
        prev, self._last_action = self._last_action, action
        # блокировка терминальна: после неё спад риска не должен «разблокировать»
        # экзамен сам собой — снять lock может только session_end от оболочки
        if self.session.state is SessionState.LOCKED and action is not VerdictAction.LOCK:
            log.debug("вердикт %s подавлен: сессия заблокирована", action.value)
            return
        # вердикт — это требование действия над экзаменом; без сессии он бессмыслен.
        # Предстартовые находки (виртуальная камера, remote-софт) оболочка видит
        # через event и risk, а не через verdict.
        if not self.session.active:
            log.debug("вердикт %s подавлен: сессии нет", action.value)
            return
        reason = self._verdict_reason(action, breakdown)
        await self._broadcast(envelope(MsgType.VERDICT, action=action.value,
                                       reason=reason, score=score))
        self.session.set_risk(score, action.value)
        try:
            if action is VerdictAction.LOCK and self.session.active:
                self.session.transition(SessionState.LOCKED, reason)
            elif action is VerdictAction.PAUSE and self.session.state in (
                    SessionState.RUNNING, SessionState.CALIBRATING):
                self.session.transition(SessionState.PAUSED, reason)
            elif action in (VerdictAction.NONE, VerdictAction.WARN) \
                    and self.session.state is SessionState.PAUSED:
                self.session.transition(SessionState.RUNNING, "риск снизился")
        except SessionStateError as exc:
            log.debug("переход состояния отклонён: %s", exc)
        log.info("вердикт %s -> %s (%.1f): %s", prev.value, action.value, score, reason)

    def _verdict_reason(self, action: VerdictAction, breakdown: list[dict[str, Any]]) -> str:
        """Причина вердикта — топ-3 вклада в риск, по-русски, годно для HUD."""
        top: list[str] = []
        for row in breakdown[:3]:
            # RiskScorer может прислать готовую короткую подпись — она точнее
            label = row.get("label")
            if isinstance(label, str) and label.strip():
                top.append(label.strip())
                continue
            kind = _as_kind(row.get("kind"))
            if kind is not None:
                top.append(EVENT_MESSAGES.get(kind, kind.value))
        tail = ("; ".join(top)) if top else "накопленный риск по совокупности сигналов"
        prefix = {
            VerdictAction.NONE: "Нарушений не зафиксировано",
            VerdictAction.WARN: "Предупреждение",
            VerdictAction.PAUSE: "Экзамен приостановлен",
            VerdictAction.LOCK: "Сессия заблокирована",
        }[action]
        return f"{prefix}: {tail}"

    # ------------------------------------------------------------- тикеры
    async def _status_ticker(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.status_interval)
            self._check_calibration_timeout()
            if self.recorder is not None:
                # дозревшие клипы -> на запись, даже если кадры перестали идти
                with contextlib.suppress(Exception):
                    self.recorder.tick()
            with contextlib.suppress(Exception):
                await self._broadcast(self._status_message())

    async def _risk_ticker(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.risk_decay_interval)
            if self.risk is not None and hasattr(self.risk, "decay"):
                with contextlib.suppress(Exception):
                    self.risk.decay(time.time())
            with contextlib.suppress(Exception):
                await self._push_risk()

    async def _env_ticker(self) -> None:
        """Проверки ОС: психологически важный канал (виртуалка, remote, запись экрана)."""
        while True:
            # Вся итерация под try: упавший тикер не воскресает, и канал
            # окружения молча исчезает до конца демо.
            try:
                findings = await asyncio.to_thread(self.env_check, self.cfg_dict)
                events: list[ProctorEvent] = []
                for finding in findings or []:
                    kind = _as_kind(_get(finding, "kind"))
                    if kind is None:
                        continue
                    detail = _get(finding, "detail", {}) or {}
                    detail = dict(detail) if isinstance(detail, dict) else {"value": detail}
                    severity = _get(finding, "severity")
                    if severity is not None:
                        detail.setdefault("severity", getattr(severity, "value", severity))
                    events.extend(self._push_external(kind, detail))
                await self._emit(events)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("проверки окружения упали: %s", exc)
            await asyncio.sleep(self.cfg.env_interval)

    async def _audio_ticker(self) -> None:
        """Опрос аудио 10 Гц + эвристика «речь без движения губ».

        Если микрофон слышит речь дольше `speech_without_lips_sec`, а
        mouth_open_ratio всё это время ниже порога — говорит не тот, кто в кадре
        (подсказчик рядом или голос в наушнике).
        """
        cfg = self.cfg
        while True:
            await asyncio.sleep(cfg.audio_interval)
            try:
                await self._audio_step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Тикер крутится 10 раз в секунду: подробный лог здесь залил бы
                # консоль на демо, поэтому только первая ошибка после удачи.
                if self._audio_ok:
                    log.warning("аудио-шаг упал: %s", exc)
                self._audio_ok = False

    async def _audio_step(self) -> None:
        """Один опрос микрофона -> наблюдения. Вынесено, чтобы тикер не умирал."""
        cfg = self.cfg
        obs = None
        try:
            obs = self.audio.poll()
        except Exception as exc:
            if self._audio_ok:
                log.warning("аудио-опрос упал: %s", exc)
            self._audio_ok = False
            return
        if obs is None:
            self._audio_ok = False
            return
        # Микрофон могли выдернуть посреди сессии: poll() не бросает, он просто
        # отдаёт available/device_ok = False. Если верить одному факту «не упало»,
        # HUD будет до конца демо показывать живой аудиоканал на мёртвом микрофоне.
        self._audio_ok = bool(_get(obs, "available", True)) and bool(_get(obs, "device_ok", True))
        ts = time.time()
        speech = bool(_get(obs, "speech", False))
        is_owner = _get(obs, "is_owner", None)
        rms = float(_get(obs, "rms", 0.0) or 0.0)
        conf = float(_get(obs, "confidence", 0.0) or 0.0)

        obs_list: list[tuple[EventKind | str, bool, dict[str, Any]]] = [
            (EventKind.VOICE_OTHER, bool(speech and is_owner is False),
             {"rms": round(rms, 4), "conf": conf,
              "similarity": round(float(_get(obs, "similarity", 0.0) or 0.0), 3)}),
        ]
        if self._engine_composite:
            # связку «речь без губ» строит движок: отдаём только факт речи
            obs_list.append(("audio.speech", speech,
                             {"rms": round(rms, 4), "conf": conf}))
        else:
            lips_quiet = self._mouth_open_ratio < cfg.mouth_open_speech
            if speech and lips_quiet:
                if self._speech_no_lips_since == 0.0:
                    self._speech_no_lips_since = ts
            else:
                self._speech_no_lips_since = 0.0
            no_lips = (self._speech_no_lips_since > 0.0
                       and ts - self._speech_no_lips_since >= cfg.speech_without_lips_sec
                       and self._snap_value("face_present"))
            obs_list.append((EventKind.SPEECH_WITHOUT_LIP_MOTION, no_lips, {
                "rms": round(rms, 4),
                "mouth_open_ratio": round(self._mouth_open_ratio, 3),
                "mouth_threshold": cfg.mouth_open_speech,
            }))
        await self._emit(self._push_observations(obs_list, ts))

    async def _mock_ticker(self) -> None:
        """Демо без камеры: гоняем сценарий событий через обычный конвейер."""
        self._update_snap(face_present=True, face_count=1,
                          gaze={"yaw": 2.0, "pitch": -3.0, "zone": "center"})
        self._audio_ok = True
        while True:
            # Это план Б на сцене. Упавший тикер означает, что демо встало
            # насовсем, поэтому любую ошибку шага просто логируем и идём дальше.
            for delay, kind, detail in MOCK_SCENARIO:
                await asyncio.sleep(delay)
                try:
                    self._update_snap(phone=kind.value.startswith("PHONE"))
                    await self._emit(self._push_external(kind, detail))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("шаг mock-сценария (%s) упал, продолжаю", kind.value)
            if not self.cfg.mock_loop:
                return
            await asyncio.sleep(self.cfg.mock_pause_between_loops)
            try:
                self._reset_engines()
                self._last_action = VerdictAction.NONE
                await self._push_risk(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("сброс mock-сценария упал, продолжаю")

    # -------------------------------------------------------------- калибровка
    def _handle_calibration(self, frame: Any, face_obs: Any, face_bbox: Any, ts: float) -> None:
        """Шаг калибровки в CV-потоке: копим сэмплы, по готовности считаем результат."""
        with self._calib_lock:
            job = self._calib
            if job is None or job.finished:
                return

        stage = job.stage
        if stage in ("gaze_center", "gaze_grid"):
            if face_obs is None or int(_get(face_obs, "face_count", 0) or 0) < 1:
                return
            if self.gaze_calib is not None:
                self._calibrate_gaze_module(job, face_obs)
            else:
                self._calibrate_gaze_fallback(job, face_obs, ts)
            return
        if stage == "identity":
            if face_bbox is None:
                return
            ok = self._safe_call(
                _call_flex, self.identity.enroll, frame, face_bbox,
                float(_get(face_obs, "yaw", 0.0) or 0.0),
                float(_get(face_obs, "pitch", 0.0) or 0.0),
                what="identity enroll")
            reason = ""
            progress = getattr(self.identity, "enroll_progress", None)
            if callable(progress):
                with contextlib.suppress(Exception):
                    reason = str((progress() or {}).get("reason", ""))
            job.samples.append({"ts": ts, "ok": bool(ok), "reason": reason})
            if ok:
                job.successes += 1
        else:
            return

        # ---- стадия identity: прогресс и завершение
        self._report_calibration_progress(job, len(job.samples), {})
        # кадры могут браковаться (ракурс, резкость) — ограничиваем число попыток
        if not (job.successes >= self.cfg.identity_enroll_samples
                or len(job.samples) >= job.needed * 3):
            return

        result: dict[str, Any] = {"ok": True, "samples": len(job.samples)}
        finalize = getattr(self.identity, "finalize_enroll", None)
        if callable(finalize):
            final = self._safe_call(finalize, what="finalize_enroll")
            if isinstance(final, dict):
                result.update(final)
        enrolled = getattr(self.identity, "enrolled", job.successes > 0)
        result["accepted"] = job.successes
        result["enrolled"] = bool(enrolled)
        result["ok"] = bool(enrolled)
        if not enrolled:
            reasons = [s.get("reason") for s in job.samples if s.get("reason")]
            result["reason"] = reasons[-1] if reasons else "эталон лица не собран"
        self._finish_calibration(job, result)

    def _calibrate_gaze_module(self, job: _CalibrationJob, face_obs: Any) -> None:
        """Калибровка взгляда силами engine/calibration.py (персональные пороги + карта экрана)."""
        gc = self.gaze_calib
        try:
            if job.stage == "gaze_center":
                job.count = int(gc.add_center_sample(face_obs) or 0)
                ready = bool(gc.center_ready())
            else:
                job.count = int(gc.add_grid_sample(face_obs, job.point) or 0)
                ready = bool(gc.point_ready())
        except Exception as exc:
            log.warning("калибровка (%s) отказала: %s", job.stage, exc)
            return

        progress = {}
        with contextlib.suppress(Exception):
            progress = dict(gc.progress() or {})
        self._report_calibration_progress(job, job.count, progress)
        if not ready:
            return

        result: dict[str, Any] = {"ok": True, "samples": job.count}
        all_done = bool(progress.get("done"))
        try:
            if job.stage == "gaze_center":
                computed = gc.finish_center()
                if isinstance(computed, dict):
                    result.update(computed)
            else:
                result["point"] = job.point
                # сетка завершена целиком -> обучаем карту экрана
                if all_done or bool((gc.progress() or {}).get("done")):
                    computed = gc.finish_grid()
                    if isinstance(computed, dict):
                        result.update(computed)
                    all_done = True
                result["all_points_done"] = all_done
        except Exception as exc:
            log.warning("расчёт калибровки (%s) упал: %s", job.stage, exc)
            result["ok"] = False
            result["reason"] = f"не удалось посчитать калибровку: {exc}"

        if result.get("ok") and (job.stage == "gaze_center" or all_done):
            # отдать пороги детектору и положить JSON в каталог сессии
            with contextlib.suppress(Exception):
                result["applied"] = bool(gc.attach(self.face))
            with contextlib.suppress(Exception):
                result["quality"] = dict(gc.quality() or {})
            if self.session.dir is not None:
                with contextlib.suppress(Exception):
                    saved = gc.save(str(self.session.dir))
                    if saved:
                        result["saved"] = str(Path(saved).name)
        self._finish_calibration(job, result)

    def _calibrate_gaze_fallback(self, job: _CalibrationJob, face_obs: Any, ts: float) -> None:
        """Запасной путь: контрактный FaceAnalyzer.calibrate_center(samples).

        Сэмплы складываем словарями со всеми полями, которые смотрит детектор
        (включая флаги `gaze_ok`/`pose_ok` — без них сэмпл будет отброшен).
        """
        job.samples.append({
            "ts": ts,
            "yaw": float(_get(face_obs, "yaw", 0.0) or 0.0),
            "pitch": float(_get(face_obs, "pitch", 0.0) or 0.0),
            "roll": float(_get(face_obs, "roll", 0.0) or 0.0),
            "gaze_yaw": float(_get(face_obs, "gaze_yaw", 0.0) or 0.0),
            "gaze_pitch": float(_get(face_obs, "gaze_pitch", 0.0) or 0.0),
            "eye_aspect_ratio": float(_get(face_obs, "eye_aspect_ratio", 0.0) or 0.0),
            "mouth_open_ratio": float(_get(face_obs, "mouth_open_ratio", 0.0) or 0.0),
            "gaze_ok": bool(_get(face_obs, "gaze_ok", True)),
            "pose_ok": bool(_get(face_obs, "pose_ok", True)),
        })
        job.count = len(job.samples)
        self._report_calibration_progress(job, job.count, {})
        if job.count < job.needed:
            return

        result: dict[str, Any] = {"ok": True, "samples": job.count, "degraded": True}
        if job.stage == "gaze_center" and hasattr(self.face, "calibrate_center"):
            computed = self._safe_call(self.face.calibrate_center, job.samples,
                                       what="calibrate_center")
            if isinstance(computed, dict):
                result.update(computed)
        elif job.stage == "gaze_grid":
            result["point"] = job.point
            for key in ("gaze_yaw", "gaze_pitch", "yaw", "pitch"):
                values = [s[key] for s in job.samples if key in s]
                if values:
                    result[f"mean_{key}"] = round(sum(values) / len(values), 2)
        self._finish_calibration(job, result)

    def _report_calibration_progress(self, job: _CalibrationJob, count: int,
                                      extra: dict[str, Any]) -> None:
        """Отправить прогресс калибровки, но не чаще, чем каждые 5 принятых кадров."""
        if count <= 0 or count == job.reported or count % 5 != 0:
            return
        job.reported = count
        payload = {
            "stage": job.stage,
            "progress": round(float(extra.get("progress", job.progress)), 2),
            "done": False,
            "result": {"samples": count, **({"quality": extra["result"]}
                                            if isinstance(extra.get("result"), dict) else {})},
        }
        self._post(("calibration", payload))

    def _finish_calibration(self, job: _CalibrationJob, result: dict[str, Any]) -> None:
        """Завершить стадию калибровки (потокобезопасно, ровно один раз)."""
        with self._calib_lock:
            if job.finished:
                return
            job.finished = True
            if self._calib is job:
                self._calib = None
        payload = {"stage": job.stage, "progress": 1.0, "done": True, "result": result}
        self._post(("calibration", payload))
        self._post(("events", self._system_event(EventKind.CALIBRATION_DONE,
                                                 {"stage": job.stage, **result})))

    async def _finish_calibration_async(self, stage: str, point: list[float] | None,
                                        result: dict[str, Any]) -> None:
        """Завершение калибровки прямо в лупе (нет камеры/модуля или стадия voice)."""
        with self._calib_lock:
            job = self._calib
            if job is not None and job.stage == stage:
                job.finished = True
                self._calib = None
        await self._broadcast(envelope(MsgType.CALIBRATION, stage=stage, progress=1.0,
                                       done=True, result=result))
        await self._emit(self._system_event(EventKind.CALIBRATION_DONE,
                                            {"stage": stage, "point": point, **result}))
        if self.session.state is SessionState.CALIBRATING:
            with contextlib.suppress(SessionStateError):
                self.session.transition(SessionState.RUNNING, "калибровка завершена")

    def _after_calibration_stage(self, payload: dict[str, Any]) -> None:
        """Стадия закрыта: вернуть сессию в running, когда калибровка действительно закончена."""
        result = payload.get("result")
        result = result if isinstance(result, dict) else {}
        if payload.get("stage") == "gaze_grid" and not result.get("all_points_done"):
            return  # сетка идёт дальше, оболочка пришлёт следующую точку
        if self.session.state is SessionState.CALIBRATING:
            with contextlib.suppress(SessionStateError):
                self.session.transition(SessionState.RUNNING, "калибровка завершена")

    def _check_calibration_timeout(self) -> None:
        """Калибровка не должна висеть вечно: по таймауту закрываем честным провалом."""
        with self._calib_lock:
            job = self._calib
            if job is None or job.finished:
                return
            expired = time.time() - job.started >= self.cfg.calibration_timeout
        if expired:
            self._finish_calibration(job, {
                "ok": False, "samples": len(job.samples),
                "reason": "калибровка не завершена: не хватило кадров с лицом",
            })

    # ------------------------------------------------------------ состояние
    def _update_snap(self, **kwargs: Any) -> None:
        with self._status_lock:
            self._snap.update(kwargs)

    def _snap_value(self, key: str, default: Any = None) -> Any:
        with self._status_lock:
            return self._snap.get(key, default)

    def _on_camera_state(self, ok: bool, message: str) -> None:
        """Колбэк из потока камеры: потеря/возврат устройства."""
        log.warning("камера: %s", message) if not ok else log.info("камера: %s", message)
        self._post(("error", {"code": "camera" if not ok else "camera_ok",
                              "message": message, "fatal": False}))

    def _reset_engines(self) -> None:
        for obj in (self.engine, self.risk, self.fusion):
            if obj is not None and hasattr(obj, "reset"):
                with contextlib.suppress(Exception):
                    obj.reset()
        self._identity_fail_streak = 0
        self._speech_no_lips_since = 0.0
        self._last_risk_sent = -1.0
        self._update_snap(identity_ok=True, phone=False)

    # ------------------------------------------------------- доказательства
    def _should_save(self, ev: ProctorEvent) -> bool:
        if not self.cfg.save_evidence or not self.session.recording:
            return False
        floor = SEVERITY_ORDER.get(self.cfg.evidence_min_severity, 2)
        return SEVERITY_ORDER.get(ev.severity.value, 0) >= floor

    #: Единственные инциденты, где кроп уместен: поводом служит ПРЕДМЕТ.
    #: Для всего остального (лица, взгляд, голос) доказательство — полный кадр;
    #: кропы лиц не сохраняются никогда (политика ПД, storage/evidence.py).
    _OBJECT_KINDS = (EventKind.PHONE_IN_FRAME, EventKind.PHONE_RAISED,
                     EventKind.PHONE_AIMED_AT_SCREEN, EventKind.FORBIDDEN_OBJECT)

    #: Инциденты, для которых клип НЕ пишется. Размывать лицо постороннего
    #: рекордер умеет только на снимке: bbox второго лица известен для кадра
    #: инцидента, а в 15-секундном клипе человек движется, и покадровых рамок
    #: у нас нет. Клип уехал бы на диск (и дальше — в каталог сессии, который
    #: отдают преподавателю) с незамытой биометрией постороннего, а README и
    #: docs/LIMITATIONS.md обещают обратное. Доказательством остаётся размытый
    #: полный кадр: он подтверждает сам факт «в комнате второй человек».
    _NO_CLIP_KINDS = (EventKind.SECOND_FACE,)

    def _attach_evidence(self, events: list[ProctorEvent], frame: Any, face_bbox: Any) -> None:
        """Сохранить кадр (и клип) инцидента, прописать пути в событие.

        Кроп делается только по bbox предмета-повода (телефон, книга). Для
        инцидентов про людей кроп не снимается — доказательством является
        полный кадр, а лишние лица размываются рекордером. Для инцидентов из
        `_NO_CLIP_KINDS` клип не пишется вообще: размыть постороннего в видео
        нечем, см. комментарий к константе.
        """
        for ev in events:
            if ev.evidence is not None or not self._should_save(ev):
                continue
            target = self.session.evidence_file(ev.kind.value.lower())
            if target is None:
                continue
            path, rel = target
            box = ev.detail.get("bbox") if ev.kind in self._OBJECT_KINDS else None
            blur = self._blur_regions(ev, face_bbox)

            if self.recorder is not None:
                saved = self._safe_call(self.recorder.save_snapshot, frame, box, path,
                                        ev.message[:60], blur, ev.ts, what="снимок")
                if not isinstance(saved, dict):
                    continue
                clip = None
                if ev.kind not in self._NO_CLIP_KINDS:
                    clip = self._safe_call(self.recorder.save_clip, ev.id, None, None, None,
                                           ev.ts, what="клип")
                ev.evidence = Evidence(
                    frame_path=saved.get("frame_rel") or rel,
                    clip_path=str(clip) if clip else None,
                    bbox=saved.get("bbox"),
                    extra={"crop_path": saved.get("crop_rel") or "", "saved_at": time.time()},
                )
                continue

            # рекордера нет — пишем одиночный кадр сами, без кропов и клипов
            try:
                import cv2  # type: ignore
                path.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(path), frame,
                            [int(cv2.IMWRITE_JPEG_QUALITY), int(self.cfg.evidence_jpeg_quality)])
            except Exception as exc:
                log.debug("кадр-доказательство не сохранён: %s", exc)
                continue
            ev.evidence = Evidence(
                frame_path=rel,
                bbox=[float(v) for v in box] if isinstance(box, (list, tuple)) and len(box) >= 4 else None,
                extra={"saved_at": time.time()},
            )

    @staticmethod
    def _blur_regions(ev: ProctorEvent, face_bbox: Any) -> list[Any]:
        """Лица посторонних, которые нужно размыть перед записью кадра.

        Для SECOND_FACE известен bbox второго лица — размываем его: факт
        присутствия второго человека фиксируется, биометрия нет.
        """
        if ev.kind is not EventKind.SECOND_FACE:
            return []
        extra = ev.detail.get("second_face_bbox") or ev.detail.get("other_face_bbox")
        return [extra] if isinstance(extra, (list, tuple)) and len(extra) >= 4 else []

    def _snapshot(self) -> str | None:
        """Снимок текущего кадра по команде оболочки (блокирующий, через to_thread)."""
        with self._frame_lock:
            frame = self._last_frame
        if frame is None and self.capture is not None:
            frame, _ts = self.capture.peek()
        if frame is None:
            return None
        target = self.session.evidence_file("snapshot")
        if target is None:
            return None
        path, _rel = target
        if self.recorder is not None:
            saved = self._safe_call(self.recorder.save_snapshot, frame, None, path,
                                    "снимок по команде", None, time.time(), what="снапшот")
            if isinstance(saved, dict):
                return str(saved.get("frame_path") or path)
            return None
        try:
            import cv2  # type: ignore
            path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(path), frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), int(self.cfg.evidence_jpeg_quality)])
            return str(path)
        except Exception as exc:
            log.warning("снапшот не сохранён: %s", exc)
            return None

    async def _build_report(self, summary: dict[str, Any]) -> None:
        """Собрать HTML-отчёт по сессии, если модуль отчётов доступен."""
        session_dir = summary.get("session_dir") or ""
        db_path = summary.get("db_path") or ""
        if not session_dir:
            await self._error("report", "Отчёт не собран: сессия не создавалась")
            return
        build = getattr(self.report_mod, "build_report", None) if self.report_mod else None
        if build is None:
            await self._error("report", "Модуль отчётов недоступен, HTML не собран")
            return
        out_html = str(Path(session_dir) / self.cfg.report_filename)
        try:
            path = await asyncio.to_thread(build, session_dir, db_path, out_html)
        except Exception as exc:
            log.warning("отчёт не собрался: %s", exc)
            await self._error("report", f"Не удалось собрать отчёт: {exc}")
            return
        log.info("отчёт готов: %s", path or out_html)
        if self.cfg.sign_report and self.cfg.report_key_path:
            sign = getattr(self.report_mod, "sign_report", None)
            if sign is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(sign, path or out_html, self.cfg.report_key_path)

    # --------------------------------------------------------------- остановка
    async def shutdown(self, reason: str = "") -> None:
        """Корректное завершение: закрыть сессию, собрать отчёт, отпустить камеру."""
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("останов: %s", reason or "по запросу")

        self._stop.set()
        if self.capture is not None:
            with contextlib.suppress(Exception):
                self.capture.stop()
        thread = self._cv_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

        if self.session.active:
            with contextlib.suppress(Exception):
                await self._cmd_session_end({"reason": reason or "остановка сайдкара"})

        if self.audio is not None:
            with contextlib.suppress(Exception):
                self.audio.stop()
        if self.recorder is not None:
            with contextlib.suppress(Exception):
                self.recorder.close()

        for name in ("close", "stop", "release"):
            for module in (self.face, self.objects, self.identity):
                fn = getattr(module, name, None)
                if callable(fn):
                    with contextlib.suppress(Exception):
                        fn()

        with contextlib.suppress(Exception):
            await self._broadcast(envelope(MsgType.ERROR, code="shutdown",
                                           message="Сайдкар остановлен", fatal=True))
        for ws in list(self.clients):
            with contextlib.suppress(Exception):
                await ws.close()
        self.clients.clear()

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

        if self._server is not None:
            with contextlib.suppress(Exception):
                self._server.close()
                await self._server.wait_closed()

        self._shutdown_done.set()


# ===========================================================================
# CLI
# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sidecar",
        description="Сайдкар прокторинга: WebSocket-сервер, захват камеры, детекторы.",
    )
    p.add_argument("--host", default=None, help="адрес WS-сервера (по умолчанию 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="порт WS-сервера (по умолчанию 8787)")
    p.add_argument("--camera", type=int, default=None, help="индекс камеры")
    p.add_argument("--fps", type=int, default=None, help="целевой FPS захвата")
    p.add_argument("--config", default=None, help="путь к config.json")
    p.add_argument("--no-audio", action="store_true", help="выключить аудио-канал")
    p.add_argument("--no-identity", action="store_true", help="выключить сверку личности")
    p.add_argument("--no-yolo", action="store_true", help="выключить детектор объектов")
    p.add_argument("--no-env", action="store_true", help="выключить проверки окружения")
    p.add_argument("--allow-multi-display", action="store_true",
                   help="не считать второй экран нарушением (проектор на демо)")
    p.add_argument("--headless", action="store_true",
                   help="без камеры и видео-детекторов (проверка протокола)")
    p.add_argument("--mock", action="store_true",
                   help="генерировать события по сценарию (демо без камеры)")
    p.add_argument("--list-cameras", action="store_true",
                   help="показать доступные камеры и выйти")
    p.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    return p


def apply_cli(cfg: ProctorConfig, args: argparse.Namespace) -> None:
    if args.host:
        cfg.ws_host = args.host
    if args.port:
        cfg.ws_port = int(args.port)
    if args.camera is not None:
        cfg.camera_index = int(args.camera)
    if args.fps:
        cfg.target_fps = int(args.fps)
    if args.no_audio:
        cfg.enable_audio = False
    if args.no_identity:
        cfg.enable_identity = False
    if args.no_yolo:
        cfg.enable_vision = False
    if args.no_env:
        cfg.enable_env = False
    if args.allow_multi_display:
        # проектор-расширение на сцене не должен сам поднимать risk-score
        cfg.env_allow_multiple_displays = True
    if args.headless:
        cfg.headless = True
    if args.mock:
        cfg.mock = True
    if args.log_level:
        cfg.log_level = args.log_level


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg, warnings = ProctorConfig.load(args.config)
    apply_cli(cfg, args)

    logging.basicConfig(
        level=getattr(logging, str(cfg.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for warning in warnings:
        log.info("конфиг: %s", warning)

    if args.list_cameras:
        cams = CameraCapture.list_cameras(cfg.camera_probe_max_index)
        if not cams:
            print("Камеры не найдены (или нет доступа / нет opencv)")
        for cam in cams:
            print(f"камера {cam['index']}: {cam['width']}x{cam['height']}")
        return 0

    cfg.sessions_path.mkdir(parents=True, exist_ok=True)

    sidecar = ProctorSidecar(cfg)
    sidecar.load_modules()

    try:
        asyncio.run(_run(sidecar))
    except KeyboardInterrupt:  # SIGINT до установки обработчиков
        log.info("прервано пользователем")
    return 0


async def _run(sidecar: ProctorSidecar) -> None:
    try:
        await sidecar.run()
    except asyncio.CancelledError:
        pass
    finally:
        await sidecar.shutdown("выход")


if __name__ == "__main__":
    raise SystemExit(main())

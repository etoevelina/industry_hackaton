#!/usr/bin/env python3
"""
Мок-сайдкар: тот же WebSocket-протокол, но события берутся из сценария.

Зачем он нужен
--------------
1. UI можно писать параллельно с CV: оболочке нужен поток `status`/`event`/
   `risk`/`verdict`, а не камера. Мок даёт ровно его и стартует за десятые доли
   секунды, без mediapipe, YOLO и прав на камеру.
2. Репетиция демо. Сценарий воспроизводится один в один, и формулировки в ленте
   инцидентов можно отрепетировать вслух, не изображая нарушителя перед камерой.
3. План Б на демо-дне. Если камера не открылась, разрешения не выданы или
   упала модель — запускается мок, и показ идёт по тому же тексту
   (`memory/demo-runbook.md`, план Б по каждому шагу).

Что он эмулирует, а что нет
---------------------------
Эмулирует: `hello` с набором каналов, `status` ~2 Гц, `event`, `risk` с
разложением, `verdict`, `calibration`, обработку всех сообщений оболочки
(`session_start`, `session_end`, `calibrate`, `telemetry`, `shell_event`,
`command`). Risk-score считается тем же способом, что и в бою: вес из
`RISK_WEIGHTS`, умноженный на `confidence`, с экспоненциальным спадом по
half-life, пороги `RISK_WARN/PAUSE/LOCK` из `protocol.py`.

Не эмулирует: доказательную базу (кадры, клипы, SQLite с hash-chain) и отчёт.
События приходят без `evidence` — оболочка должна это выдерживать, и мок как раз
проверяет, что выдерживает.

Сценарий
--------
Берётся из `scripts/demo_scenario.json` (этот файл пишет другой агент). Если
файла нет или он не читается, используется встроенный `DEFAULT_SCENARIO`,
повторяющий демо-план: телефон -> Alt+Tab -> подмена личности -> виртуальная
камера -> чужой голос -> fusion-связка -> рост risk-score до блокировки.

Формат — намеренно терпимый, чтобы руками править было легко:

    {
      "name": "Демо",
      "loop": true,
      "pause_between_loops": 10,
      "steps": [
        {"delay": 4.0, "say": "Телефон в кадре",
         "event": "PHONE_IN_FRAME", "conf": 0.82,
         "detail": {"area_ratio": 0.031},
         "status": {"phone": true}},
        {"delay": 2.0, "events": ["GAZE_DOWN", "HEAD_TURNED"]},
        {"delay": 1.0, "calibration": {"stage": "identity", "progress": 1.0, "done": true}},
        {"delay": 1.0, "risk": "reset", "say": "Проктор сбросил риск"},
        {"delay": 1.0, "verdict": {"action": "lock", "reason": "..."}}
      ]
    }

Верхним уровнем может быть и просто список шагов. Синонимы ключей:
`delay` = `wait` = `after` = `sleep` = `pause`; `event` = `kind`;
`detail` = `details`; `say` = `note` = `comment`; `conf` = `confidence`.
Неизвестный `EventKind` пропускается с предупреждением, а не роняет прогон.

Запуск
------
    python3 sidecar/tools/mock_sidecar.py                 # встроенный сценарий
    python3 sidecar/tools/mock_sidecar.py --speed 2       # вдвое быстрее
    python3 sidecar/tools/mock_sidecar.py --loop          # по кругу
    python3 sidecar/tools/mock_sidecar.py --port 8788     # другой порт
    python3 sidecar/tools/mock_sidecar.py --list          # показать сценарий и выйти

Из зависимостей нужны только `websockets` и стандартная библиотека. Если
доступны `sidecar/main.py` и `sidecar/config.py`, мок берёт оттуда русские
тексты событий, таблицу каналов и half-life — чтобы лента инцидентов в моке
и в бою выглядела одинаково. Нет их — работают встроенные копии.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent          # sidecar/tools
_SIDECAR = _HERE.parent                          # sidecar
_ROOT = _SIDECAR.parent                          # корень репозитория
for _path in (str(_SIDECAR), str(_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from protocol import (  # noqa: E402
    DEFAULT_WS_HOST,
    DEFAULT_WS_PORT,
    PROTOCOL_VERSION,
    RISK_LOCK,
    RISK_PAUSE,
    RISK_WARN,
    RISK_WEIGHTS,
    Channel,
    EventKind,
    MsgType,
    ProctorEvent,
    Severity,
    VerdictAction,
    decode,
    encode,
    envelope,
)

VERSION = "0.1.0-mock"
log = logging.getLogger("mock-sidecar")

#: Путь к сценарию по умолчанию. Файл пишет другой агент — нас устраивает и
#: его отсутствие.
SCENARIO_PATH = _ROOT / "scripts" / "demo_scenario.json"


# ===========================================================================
# Справочники: берём из main.py, если он импортируется, иначе свои
# ===========================================================================
def _load_shared() -> tuple[dict[EventKind, str], dict[EventKind, Channel], bool]:
    """Подтянуть тексты и каналы из боевого сайдкара.

    `main.py` импортирует `capture`, который грузит OpenCV лениво, поэтому
    импорт безопасен и без cv2. Любая ошибка — работаем на своих копиях.
    """
    try:
        from main import CHANNEL_BY_KIND, EVENT_MESSAGES  # type: ignore
        return dict(EVENT_MESSAGES), dict(CHANNEL_BY_KIND), True
    except Exception:
        return dict(_FALLBACK_MESSAGES), dict(_FALLBACK_CHANNELS), False


#: Встроенные русские тексты — на случай, если main.py недоступен.
_FALLBACK_MESSAGES: dict[EventKind, str] = {
    EventKind.PHONE_IN_FRAME: "В кадре обнаружен телефон",
    EventKind.PHONE_RAISED: "Телефон поднят к уровню лица",
    EventKind.PHONE_AIMED_AT_SCREEN: "Телефон направлен на экран, взгляд в телефон",
    EventKind.FORBIDDEN_OBJECT: "В кадре посторонний предмет",
    EventKind.NO_FACE: "Лицо не видно в кадре",
    EventKind.SECOND_FACE: "В кадре второй человек",
    EventKind.IDENTITY_MISMATCH: "За компьютером другой человек",
    EventKind.LIVENESS_FAIL: "Признаки подмены: нет морганий и микродвижений",
    EventKind.GAZE_DOWN: "Устойчивый взгляд вниз, мимо экрана",
    EventKind.GAZE_SIDE: "Устойчивый взгляд в сторону, мимо экрана",
    EventKind.GAZE_OFF_SCREEN: "Точка взгляда вне области экрана",
    EventKind.HEAD_TURNED: "Голова отвёрнута от экрана",
    EventKind.VOICE_OTHER: "Слышен посторонний голос",
    EventKind.SPEECH_WITHOUT_LIP_MOTION: "Речь слышна, но губы не двигаются",
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

_FALLBACK_CHANNELS: dict[EventKind, Channel] = {
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

EVENT_MESSAGES, CHANNEL_BY_KIND, _FROM_MAIN = _load_shared()


def _as_kind(value: Any) -> EventKind | None:
    """EventKind из чего угодно: сам enum, значение или имя."""
    if isinstance(value, EventKind):
        return value
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return EventKind(value.strip())
        with contextlib.suppress(KeyError):
            return EventKind[value.strip().upper()]
    return None


def _as_severity(value: Any, default: Severity) -> Severity:
    if isinstance(value, Severity):
        return value
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return Severity(value.strip().lower())
    return default


def severity_for_weight(weight: float) -> Severity:
    """Та же шкала, что в боевом сайдкаре: severity выводится из веса."""
    if weight >= 55:
        return Severity.CRITICAL
    if weight >= 35:
        return Severity.HIGH
    if weight >= 20:
        return Severity.MEDIUM
    if weight >= 8:
        return Severity.LOW
    return Severity.INFO


def _as_channel(value: Any, default: Channel) -> Channel:
    if isinstance(value, Channel):
        return value
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return Channel(value.strip().lower())
    return default


def make_event(kind: EventKind, *, confidence: float = 1.0, duration: float = 0.0,
               detail: dict[str, Any] | None = None,
               severity: Severity | None = None,
               channel: Channel | None = None,
               message: str | None = None) -> ProctorEvent:
    """Собрать ProctorEvent так же, как это делает боевой сайдкар."""
    weight = RISK_WEIGHTS.get(kind, 10.0)
    body = dict(detail or {})
    body.setdefault("mock", True)
    return ProctorEvent(
        kind=kind,
        severity=severity or severity_for_weight(weight),
        channel=channel or CHANNEL_BY_KIND.get(kind, Channel.SYSTEM),
        confidence=round(float(confidence), 3),
        ts=time.time(),
        duration=round(float(duration), 2),
        message=message or EVENT_MESSAGES.get(kind, kind.value),
        detail=body,
    )


# ===========================================================================
# Risk-score: копия боевой арифметики
# ===========================================================================
class MockRisk:
    """Накопительный risk-score с экспоненциальным спадом.

    Вклад события = `RISK_WEIGHTS[kind] * clamp(confidence, 0.2, 1.0)`,
    затухает как `0.5 ** (age / half_life)`. Та же формула, что у боевого
    RiskScorer, — иначе цифры на репетиции и на защите не совпадут.
    """

    def __init__(self, half_life: float = 90.0, max_score: float = 100.0) -> None:
        self.half_life = max(float(half_life), 1.0)
        self.max_score = float(max_score)
        self._items: list[tuple[float, EventKind, float]] = []

    def reset(self) -> None:
        self._items.clear()

    def add(self, event: ProctorEvent) -> None:
        weight = RISK_WEIGHTS.get(event.kind, 10.0) * max(min(event.confidence, 1.0), 0.2)
        if weight > 0:
            self._items.append((event.ts, event.kind, weight))

    def decay(self, now: float | None = None) -> None:
        """Выбросить вклады, усохшие до незначимых. Чистит память за долгий прогон."""
        now = time.time() if now is None else now
        self._items = [it for it in self._items if self._value(it, now) >= 0.1]

    def _value(self, item: tuple[float, EventKind, float], now: float) -> float:
        ts, _kind, weight = item
        return weight * math.pow(0.5, max(now - ts, 0.0) / self.half_life)

    @property
    def score(self) -> float:
        now = time.time()
        return round(min(sum(self._value(it, now) for it in self._items), self.max_score), 2)

    def breakdown(self) -> list[dict[str, Any]]:
        now = time.time()
        agg: dict[str, dict[str, Any]] = {}
        for item in self._items:
            key = item[1].value
            row = agg.setdefault(key, {"kind": key, "contribution": 0.0, "count": 0})
            row["contribution"] += self._value(item, now)
            row["count"] += 1
        rows = sorted(agg.values(), key=lambda r: r["contribution"], reverse=True)
        for row in rows:
            row["contribution"] = round(row["contribution"], 2)
        return rows

    def action(self) -> VerdictAction:
        score = self.score
        if score >= RISK_LOCK:
            return VerdictAction.LOCK
        if score >= RISK_PAUSE:
            return VerdictAction.PAUSE
        if score >= RISK_WARN:
            return VerdictAction.WARN
        return VerdictAction.NONE


_LEVELS = {VerdictAction.NONE: "ok", VerdictAction.WARN: "warn",
           VerdictAction.PAUSE: "pause", VerdictAction.LOCK: "lock"}

_VERDICT_PREFIX = {
    VerdictAction.NONE: "Нарушений не зафиксировано",
    VerdictAction.WARN: "Предупреждение",
    VerdictAction.PAUSE: "Экзамен приостановлен",
    VerdictAction.LOCK: "Сессия заблокирована",
}


# ===========================================================================
# Сценарий
# ===========================================================================
@dataclass
class Step:
    """Один шаг сценария: подождать, затем применить всё, что в нём описано."""
    delay: float = 0.0
    say: str = ""
    show: str = ""                                  # что делает ведущий на сцене
    events: list[ProctorEvent] = field(default_factory=list)
    status: dict[str, Any] = field(default_factory=dict)
    risk: str = ""                                  # "reset"
    verdict: dict[str, Any] | None = None
    calibration: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass
class Scenario:
    name: str = "встроенный сценарий"
    steps: list[Step] = field(default_factory=list)
    loop: bool = False
    pause_between_loops: float = 10.0
    source: str = "встроенный"

    @property
    def duration(self) -> float:
        return round(sum(s.delay for s in self.steps), 1)


#: Встроенный сценарий демо — план Б, когда `scripts/demo_scenario.json`
#: недоступен. Повторяет `memory/demo-runbook.md`: телефон -> локдаун ->
#: подмена личности -> виртуальная камера -> чужой голос -> fusion-связка.
#:
#: Акты разделены сбросом риска (`risk: "reset"` — то же, что команда
#: `reset_risk` от оболочки). Это не подгонка под красивую картинку, а то, как
#: система и задумана: каждый вектор демонстрируется с нуля, решение о
#: продолжении принимает проктор, а инциденты из доказательной базы никуда не
#: исчезают. Без сброса первый же вектор (виртуальная камера весит 70,
#: подмена личности — 60) закрыл бы сессию на тридцатой секунде, и главный
#: аргумент защиты — fusion-связка — остался бы за кадром.
#:
#: `conf` подобраны так, чтобы на боевых весах из `protocol.py` каждый акт
#: уверенно проходил свой порог и не дребезжал на границе:
#:
#:   акт 1  телефон               48.9  WARN
#:   акт 2  локдаун               36.1  WARN
#:   акт 3  подмена личности      84.7  PAUSE
#:   акт 4  виртуальная камера    96.2  LOCK
#:   акт 5  голос + fusion       100.0  LOCK
DEFAULT_SCENARIO: list[dict[str, Any]] = [
    {
        "delay": 2.0,
        "title": "Заход",
        "say": "Нарушений нет: risk-score ноль, все пять каналов живы",
        "show": "Окно экзамена, HUD зелёный, лента инцидентов пуста",
        "status": {"fps": 14.8, "face_present": True, "face_count": 1,
                   "identity_ok": True, "audio_ok": True, "phone": False,
                   "state": "running",
                   "gaze": {"yaw": 1.5, "pitch": -2.0, "zone": "center"}},
    },

    # ---------------- акт 1: телефон (требование кейса, проходим быстро) ----
    {
        "delay": 4.0,
        "title": "Телефон в кадре",
        "say": "Требование кейса по детекции устройств — это покажут все, идём быстро",
        "show": "Поднять телефон в кадр: рамка на телефоне, инцидент в ленте",
        "event": "PHONE_IN_FRAME", "conf": 0.82, "duration": 1.1,
        "detail": {"label": "cell phone", "area_ratio": 0.031,
                   "bbox": [412.0, 288.0, 74.0, 126.0]},
        "status": {"phone": True},
    },
    {
        "delay": 5.0,
        "title": "Телефон в кадре",
        "say": "Мало увидеть телефон — важно отличить «лежит на столе» "
               "от «поднят и наведён на экран»",
        "show": "Навести телефон на экран: risk пересёк 30, HUD жёлтый",
        "event": "PHONE_AIMED_AT_SCREEN", "conf": 0.65, "duration": 2.4,
        "detail": {"label": "cell phone", "raised": True, "vertical": True,
                   "aimed": True, "gaze_zone": "down",
                   "explain": "телефон поднят выше центра лица, держится "
                              "вертикально, взгляд направлен в его сторону"},
        "status": {"gaze": {"yaw": -4.0, "pitch": -26.0, "zone": "down"}},
    },
    {
        "delay": 5.0,
        "title": "Решение принимает человек",
        "say": "Предупреждение снимает проктор, а не система. "
               "Счётчик обнуляется, инцидент в доказательной базе остаётся",
        "show": "Кнопка проктора «продолжить», risk снова 0, лента не очищается",
        "risk": "reset",
        "status": {"phone": False,
                   "gaze": {"yaw": 2.0, "pitch": -3.0, "zone": "center"}},
    },

    # ---------------- акт 2: локдаун и защита экрана ------------------------
    {
        "delay": 5.0,
        "title": "Локдаун",
        "say": "Alt+Tab перехвачен, до системы он не доходит, факт попытки записан",
        "show": "Нажать Alt+Tab: окно не переключилось, в ленте инцидент",
        "event": "SHORTCUT_BLOCKED", "conf": 1.0,
        "detail": {"combo": "Alt+Tab", "source": "shell",
                   "action": "сочетание перехвачено, фокус остался в окне экзамена"},
    },
    {
        "delay": 3.0,
        "title": "Локдаун",
        "say": "Снимок экрана перехвачен — в файле будет чёрный прямоугольник. "
               "Мы не только фиксируем съёмку, мы обесцениваем её результат",
        "show": "Сделать снимок экрана и открыть его: окно экзамена чёрное",
        "event": "SHORTCUT_BLOCKED", "conf": 1.0,
        "detail": {"combo": "Cmd+Shift+4", "source": "shell",
                   "action": "окно исключено из захвата (contentProtection)"},
    },
    {
        "delay": 3.0,
        "title": "Локдаун",
        "say": "Cmd+Tab из пользовательского процесса не блокируется ничем, "
               "кроме kernel-драйвера. Мы это не прячем: фокус возвращается "
               "за 0.4 с, а сам факт попадает в отчёт",
        "show": "Нажать Cmd+Tab: окно моргнуло и вернулось, WINDOW_BLUR с длительностью",
        "event": "WINDOW_BLUR", "conf": 0.9, "duration": 0.42,
        "detail": {"source": "shell", "ms": 420,
                   "action": "фокус возвращён принудительно"},
    },
    {
        "delay": 3.0,
        "title": "Локдаун",
        "say": "Выход из полного экрана — тоже инцидент с длительностью",
        "show": "risk ~36, HUD показывает четыре события оболочки",
        "event": "FULLSCREEN_EXIT", "conf": 0.9, "duration": 1.8,
        "detail": {"source": "shell", "action": "полный экран восстановлен"},
    },
    {
        "delay": 5.0,
        "title": "Решение принимает человек",
        "show": "Проктор снимает предупреждение, идём к следующему вектору",
        "risk": "reset",
    },

    # ---------------- акт 3: подмена человека (первый громкий момент) -------
    {
        "delay": 5.0,
        "title": "Подмена человека после калибровки",
        "say": "В момент пересадки в кадре два лица",
        "show": "Второй участник садится на место первого: две рамки в превью",
        "event": "SECOND_FACE", "conf": 0.70, "duration": 1.3,
        "detail": {"faces": 2},
        "status": {"face_count": 2},
    },
    {
        "delay": 5.0,
        "title": "Подмена человека после калибровки",
        "say": "Классический прокторинг проверяет личность один раз на входе. "
               "Дальше за клавиатуру может сесть кто угодно. У нас сверка идёт "
               "каждые три секунды всю сессию, решение — по скользящему окну",
        "show": "risk за 60, тест на паузе, кроп лица подшит к инциденту",
        "event": "IDENTITY_MISMATCH", "conf": 0.85, "duration": 3.2,
        "detail": {"similarity": 0.19, "threshold": 0.35, "fail_streak": 3,
                   "explain": "три проверки подряд ниже порога близости эмбеддингов"},
        "status": {"identity_ok": False, "face_count": 1},
    },
    {
        "delay": 6.0,
        "title": "Решение принимает человек",
        "say": "Паузу снимает проктор — система не выносит приговор сама",
        "show": "Возврат первого участника на место, экзамен продолжен",
        "risk": "reset",
        "status": {"identity_ok": True},
    },

    # ---------------- акт 4: виртуальная камера и живость -------------------
    {
        "delay": 5.0,
        "title": "Виртуальная камера и живость",
        "say": "Подменить видеопоток не получится: мы видим само устройство "
               "в списке камер системы",
        "show": "Запустить OBS с виртуальной камерой: инцидент появляется мгновенно",
        "event": "VIRTUAL_CAMERA", "conf": 0.90,
        "detail": {"summary": "в системе зарегистрировано устройство захвата: "
                              "OBS Virtual Camera",
                   "devices_matched": [{"name": "OBS Virtual Camera",
                                        "source": "system_profiler",
                                        "software": "OBS Virtual Camera"}],
                   "all_capture_devices": ["FaceTime HD Camera",
                                           "OBS Virtual Camera"],
                   "check": "check_virtual_camera"},
    },
    {
        "delay": 6.0,
        "title": "Виртуальная камера и живость",
        "say": "И даже если бы устройство называлось безобидно — в кадре нет "
               "живого человека: морганий нет, микродвижений нет, кадры идут "
               "по кругу с периодом 12 секунд",
        "show": "risk за 90, сессия заблокирована, HUD красный",
        "event": "LIVENESS_FAIL", "conf": 0.80, "duration": 12.0,
        "detail": {"no_blink_sec": 38.4, "static_frame": True,
                   "loop_detected": True, "loop_period_sec": 12.0,
                   "source": "identity.update_liveness"},
    },
    {
        "delay": 6.0,
        "title": "Решение принимает человек",
        "say": "Блокировку снимает проктор, инциденты остаются в отчёте",
        "show": "Выключить виртуальную камеру, вернуть живое видео",
        "risk": "reset",
    },

    # ---------------- акт 5: аудио и fusion — главный момент защиты ---------
    {
        "delay": 5.0,
        "title": "Аудио",
        "say": "Слышен посторонний голос: профиль не совпадает с образцом студента",
        "show": "Второй участник произносит фразу из-за кадра",
        "event": "VOICE_OTHER", "conf": 0.85, "duration": 2.8,
        "detail": {"similarity": 0.21, "owner_threshold": 0.60, "rms": 0.041},
        "status": {"audio_ok": True},
    },
    {
        "delay": 6.0,
        "title": "Аудио",
        "say": "Речь есть, губы не двигаются — значит, в ухе наушник",
        "show": "У студента рот закрыт, звук идёт: SPEECH_WITHOUT_LIP_MOTION",
        "event": "SPEECH_WITHOUT_LIP_MOTION", "conf": 0.80, "duration": 3.1,
        "detail": {"rms": 0.038, "mouth_open_ratio": 0.04, "threshold": 0.22,
                   "held_sec": 3.1},
    },
    {
        "delay": 5.0,
        "title": "Fusion поведения и содержания",
        "say": "Точка взгляда ушла за правый нижний край экрана. Телефона в кадре "
               "нет, второго человека нет — по отдельности это ничего не доказывает",
        "show": "Отвести взгляд вправо-вниз и держать 5 секунд",
        "event": "GAZE_OFF_SCREEN", "conf": 0.90, "duration": 5.2,
        "detail": {"zone": "right_down", "gaze_dev_h": 2.1, "gaze_dev_v": -1.9,
                   "region": "за правым нижним краем карты экрана"},
        "status": {"gaze": {"yaw": 21.4, "pitch": -14.8, "zone": "right_down"}},
    },
    {
        "delay": 5.0,
        "title": "Fusion поведения и содержания",
        "say": "А вот связка: взгляд отведён вправо-вниз на 5.2 с, через 1.1 с "
               "введено 312 символов со средним интервалом 11 мс при базовой "
               "скорости студента 170 мс. Ни один сигнал по отдельности ничего "
               "не доказывает. Вместе — это связка с таймингами в отчёте",
        "show": "Вернуться к экрану и вставить длинный ответ: risk 100, блокировка",
        "event": "FUSION_GAZE_THEN_ANSWER", "conf": 0.92,
        "detail": {
            "question_id": "q7",
            "gaze_zone": "right_down",
            "gaze_duration_ms": 5200,
            "lag_ms": 1100,
            "answer_chars": 312,
            "mean_interval_ms": 11,
            "baseline_interval_ms": 170,
            "explain": "Взгляд отведён вправо-вниз на 5.2 с, через 1.1 с введено "
                       "312 символов со средним интервалом 11 мс при базовой "
                       "скорости студента 170 мс",
        },
        "status": {"gaze": {"yaw": 2.0, "pitch": -3.0, "zone": "center"}},
    },
    {
        "delay": 4.0,
        "title": "Отчёт",
        "say": "Risk-score достиг порога блокировки. Сессия закрыта, формируется "
               "отчёт: каждая запись связана хешем с предыдущей",
        "show": "Открыть HTML-отчёт: integrity score, таймлайн, доказательства",
    },
]


def _pick(src: dict[str, Any], *names: str, default: Any = None) -> Any:
    """Первое найденное значение из синонимов ключа."""
    for name in names:
        if name in src and src[name] is not None:
            return src[name]
    return default


#: Поля снимка состояния, которые шаг `{"type": "status", ...}` задаёт плоско.
#: `risk` сюда НЕ входит намеренно: счёт считает MockRisk, иначе число в HUD
#: разойдётся с разложением в сообщении `risk`.
_STATUS_FIELDS = ("fps", "face_present", "face_count", "gaze", "phone",
                  "identity_ok", "audio_ok", "state")

#: Команды шага `{"type": "control", "name": ...}` — те же имена, что в
#: протокольном сообщении `command` от оболочки.
_CONTROL_RESET = ("reset_risk", "reset", "сброс")


def _parse_step(raw: Any, index: int) -> Step | None:
    """Разобрать один шаг сценария. Мусор пропускаем с предупреждением.

    Поддерживаются две записи одного и того же:

    * короткая, удобная руками — ключи `event`/`events`, `status` (объектом),
      `risk: "reset"`, `verdict`, `calibration`, `error`;
    * раскладка `scripts/demo_scenario.json` — `{"type": "event"|"status"|
      "control", ...}`, где поля статуса лежат плоско в шаге, команда названа
      в `name`, а реплики ведущего — в `demo: {title, say, show}`.
    """
    if isinstance(raw, str):                      # краткая форма: только событие
        raw = {"event": raw}
    if not isinstance(raw, dict):
        log.warning("шаг %d пропущен: ожидался объект, получено %s", index, type(raw).__name__)
        return None

    step = Step()
    try:
        step.delay = max(float(_pick(raw, "delay", "wait", "after", "sleep",
                                     "pause", "delay_s", default=0.0)), 0.0)
    except (TypeError, ValueError):
        step.delay = 0.0

    # реплики ведущего: либо плоско в шаге, либо в блоке demo
    demo = raw.get("demo") if isinstance(raw.get("demo"), dict) else {}
    title = str(_pick(raw, "title", default="") or demo.get("title") or "")
    say = str(_pick(raw, "say", "note", "comment", default="") or demo.get("say") or "")
    step.say = f"{title} — {say}" if title and say else (say or title)
    step.show = str(_pick(raw, "show", default="") or demo.get("show") or "")

    step_type = str(raw.get("type") or "").strip().lower()

    status = _pick(raw, "status", "snapshot", default=None)
    if isinstance(status, dict):
        step.status = dict(status)
    if step_type == "status" or any(f in raw for f in _STATUS_FIELDS):
        # поля статуса лежат плоско в самом шаге
        for name in _STATUS_FIELDS:
            if name in raw:
                step.status.setdefault(name, raw[name])

    if step_type == "control":
        name = str(raw.get("name") or "").strip().lower()
        if name in _CONTROL_RESET:
            step.risk = "reset"
        elif name:
            log.debug("шаг %d: команда %r в моке только протоколируется", index, name)
    else:
        raw_risk = _pick(raw, "risk", default=None)
        # у шага статуса `risk` — это число для HUD, а не команда сброса
        if isinstance(raw_risk, str) and raw_risk.strip().lower() in ("reset", "сброс"):
            step.risk = "reset"

    for key, target in (("verdict", "verdict"), ("calibration", "calibration"),
                        ("error", "error")):
        value = _pick(raw, key, default=None)
        if isinstance(value, dict):
            setattr(step, target, dict(value))

    # события: `event`/`kind` (одно) и `events`/`kinds` (список)
    entries: list[Any] = []
    single = _pick(raw, "event", "kind", default=None)
    if single is not None:
        entries.append(raw if isinstance(single, str) else single)
    many = _pick(raw, "events", "kinds", default=None)
    if isinstance(many, (list, tuple)):
        entries.extend(many)

    for entry in entries:
        event = _parse_event(entry, index)
        if event is not None:
            step.events.append(event)

    return step


def _parse_event(entry: Any, index: int) -> ProctorEvent | None:
    """Разобрать описание события: строка с видом или объект с деталями."""
    if isinstance(entry, str):
        entry = {"event": entry}
    if not isinstance(entry, dict):
        return None
    kind = _as_kind(_pick(entry, "event", "kind", default=None))
    if kind is None:
        log.warning("шаг %d: неизвестный вид события %r — пропущен",
                    index, _pick(entry, "event", "kind", default=None))
        return None
    detail = _pick(entry, "detail", "details", "payload", default=None)
    detail = dict(detail) if isinstance(detail, dict) else {}
    try:
        conf = float(_pick(entry, "conf", "confidence", default=1.0))
    except (TypeError, ValueError):
        conf = 1.0
    try:
        duration = float(_pick(entry, "duration", "held", default=0.0))
    except (TypeError, ValueError):
        duration = 0.0
    severity = _as_severity(_pick(entry, "severity", default=None),
                            severity_for_weight(RISK_WEIGHTS.get(kind, 10.0)))
    channel = _as_channel(_pick(entry, "channel", default=None),
                          CHANNEL_BY_KIND.get(kind, Channel.SYSTEM))
    message = _pick(entry, "message", "text", default=None)
    return make_event(kind, confidence=conf, duration=duration, detail=detail,
                      severity=severity, channel=channel,
                      message=str(message) if message else None)


def load_scenario(path: Path | str | None = None, *, loop_default: bool = False) -> Scenario:
    """Загрузить сценарий из JSON, при любой проблеме — встроенный.

    Файл `scripts/demo_scenario.json` пишет другой агент; его может не быть,
    он может быть битым или не того формата — ни один из этих случаев не
    должен мешать запуску мока перед демо.
    """
    candidate = Path(path).expanduser() if path else SCENARIO_PATH
    raw: Any = None
    source = "встроенный"

    if candidate.is_file():
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            source = str(candidate)
        except Exception as exc:
            log.warning("сценарий %s не прочитан (%s) — беру встроенный", candidate, exc)
            raw = None
    elif path:
        log.warning("файл сценария не найден: %s — беру встроенный", candidate)
    else:
        log.info("сценарий %s отсутствует — работаю по встроенному", candidate)

    name = "встроенный сценарий демо"
    loop = loop_default
    pause = 10.0
    steps_raw: Any

    if isinstance(raw, dict):
        steps_raw = _pick(raw, "steps", "script", "scenario", "events", default=None)
        if not isinstance(steps_raw, list):
            log.warning("в сценарии %s нет списка steps — беру встроенный", candidate)
            steps_raw = DEFAULT_SCENARIO
            source = "встроенный"
        else:
            name = str(_pick(raw, "name", "title", default=candidate.name))
            loop = bool(_pick(raw, "loop", "repeat", default=loop_default))
            try:
                pause = float(_pick(raw, "pause_between_loops", "loop_pause", default=10.0))
            except (TypeError, ValueError):
                pause = 10.0
    elif isinstance(raw, list):
        steps_raw = raw
        name = candidate.name
    else:
        steps_raw = DEFAULT_SCENARIO
        source = "встроенный"

    steps = [s for s in (_parse_step(item, i) for i, item in enumerate(steps_raw, 1))
             if s is not None]
    if not steps:
        log.warning("сценарий пуст после разбора — беру встроенный")
        steps = [s for s in (_parse_step(item, i)
                             for i, item in enumerate(DEFAULT_SCENARIO, 1)) if s is not None]
        source = "встроенный"
        name = "встроенный сценарий демо"

    return Scenario(name=name, steps=steps, loop=loop,
                    pause_between_loops=max(pause, 0.0), source=source)


# ===========================================================================
# Мок-сайдкар
# ===========================================================================
class MockSidecar:
    """WebSocket-сервер, говорящий по протоколу боевого сайдкара."""

    def __init__(self, scenario: Scenario, *, host: str = DEFAULT_WS_HOST,
                 port: int = DEFAULT_WS_PORT, speed: float = 1.0,
                 loop_scenario: bool | None = None, autostart: bool = True,
                 half_life: float = 90.0, status_interval: float = 0.5,
                 fps: float = 15.0) -> None:
        self.scenario = scenario
        self.host = host
        self.port = port
        self.speed = max(float(speed), 0.05)
        self.loop_scenario = scenario.loop if loop_scenario is None else bool(loop_scenario)
        self.autostart = autostart
        self.status_interval = max(float(status_interval), 0.1)
        self.fps = float(fps)

        # half-life делится на speed: --speed сжимает сценарий по времени, и
        # спад риска обязан сжаться вместе с ним. Иначе на `--speed 10` вклады
        # почти не успевают затухнуть, и дуга risk-score (WARN -> PAUSE -> LOCK)
        # на репетиции выглядит иначе, чем на живом показе.
        self.half_life = max(float(half_life), 1.0) / self.speed
        self.risk = MockRisk(half_life=self.half_life)
        self.clients: set[Any] = set()
        self._server: Any = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._player: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()

        self.state = "idle"
        self.session_meta: dict[str, Any] = {}
        self._last_action = VerdictAction.NONE
        self._last_risk_sent = -1.0
        self._events_total = 0

        self._snap: dict[str, Any] = {
            "fps": 0.0, "face_present": False, "face_count": 0,
            "gaze": {"yaw": 0.0, "pitch": 0.0, "zone": "unknown"},
            "phone": False, "identity_ok": True, "audio_ok": False,
        }
        # для мини-fusion по телеметрии оболочки
        self._last_gaze_event = 0.0
        self._last_blur_event = 0.0
        self._last_phone_event = 0.0

    # ------------------------------------------------------------------ запуск
    async def run(self) -> None:
        await self._serve()
        self._install_signals()
        self._tasks = [
            asyncio.create_task(self._status_ticker(), name="status"),
            asyncio.create_task(self._risk_ticker(), name="risk"),
        ]
        log.info("мок-сайдкар слушает ws://%s:%s (версия %s)", self.host, self.port, VERSION)
        log.info("сценарий: %s (%s), шагов: %d, длительность ~%.0f с, speed=%.2gx, loop=%s",
                 self.scenario.name, self.scenario.source, len(self.scenario.steps),
                 self.scenario.duration / self.speed, self.speed, "да" if self.loop_scenario else "нет")
        log.info("тексты событий: %s; half-life риска %.1f с; пороги warn/pause/lock "
                 "%.0f/%.0f/%.0f", "из sidecar/main.py" if _FROM_MAIN else "встроенные",
                 self.half_life, RISK_WARN, RISK_PAUSE, RISK_LOCK)
        try:
            await self._stop.wait()
        finally:
            await self.shutdown("выход")

    async def _serve(self) -> None:
        try:
            from websockets.asyncio.server import serve  # websockets >= 12
        except Exception:                                 # старые версии библиотеки
            from websockets.server import serve  # type: ignore
        self._server = await serve(self._handler, self.host, self.port,
                                   ping_interval=20.0, ping_timeout=20.0)

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self._stop.set)

    async def shutdown(self, reason: str) -> None:
        log.info("останов: %s", reason)
        for task in [*self._tasks, self._player]:
            if task is not None:
                task.cancel()
        for task in [*self._tasks, self._player]:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._tasks.clear()
        self._player = None
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    def stop(self) -> None:
        self._stop.set()

    # -------------------------------------------------------------- WebSocket
    async def _handler(self, ws: Any, path: str | None = None) -> None:
        self.clients.add(ws)
        log.info("оболочка подключилась (клиентов: %d)", len(self.clients))
        try:
            await self._send(ws, self._hello())
            await self._send(ws, self._status())
            await self._send(ws, self._risk_message())
            if self.autostart and self._player is None:
                self._start_player("подключилась оболочка")
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

    def _hello(self) -> dict[str, Any]:
        return envelope(
            MsgType.HELLO,
            capabilities={"vision": True, "gaze": True, "identity": True,
                          "audio": True, "env": True},
            version=VERSION,
            protocol=PROTOCOL_VERSION,
            degraded=["мок-режим: события из сценария, камера и микрофон не используются"],
            headless=True,
            mock=True,
            scenario={"name": self.scenario.name, "source": self.scenario.source,
                      "steps": len(self.scenario.steps), "speed": self.speed,
                      "loop": self.loop_scenario},
        )

    def _status(self) -> dict[str, Any]:
        snap = dict(self._snap)
        snap["gaze"] = dict(self._snap["gaze"])
        snap["fps"] = self.fps if self.state != "idle" or self._player else 0.0
        snap["risk"] = self.risk.score
        snap["state"] = self.state
        return envelope(MsgType.STATUS, **snap)

    def _risk_message(self) -> dict[str, Any]:
        action = self.risk.action()
        return envelope(MsgType.RISK, score=self.risk.score, level=_LEVELS[action],
                        breakdown=self.risk.breakdown(), action=action.value)

    # ------------------------------------------------- приём команд оболочки
    async def _on_message(self, ws: Any, raw: Any) -> None:
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
                await self._cmd_command(ws, msg)
            else:
                await self._send(ws, envelope(MsgType.ERROR, code="unknown_type",
                                              message=f"Неизвестный тип сообщения: {mtype}",
                                              fatal=False))
        except Exception as exc:
            log.exception("ошибка обработки %s", mtype)
            await self._send(ws, envelope(MsgType.ERROR, code="handler_error",
                                          message=f"Ошибка обработки {mtype}: {exc}",
                                          fatal=False))

    async def _cmd_session_start(self, msg: dict[str, Any]) -> None:
        self.session_meta = {
            "student_id": str(msg.get("student_id") or "anon"),
            "exam_id": str(msg.get("exam_id") or ""),
            "student_name": str(msg.get("student_name") or ""),
        }
        self.state = "running"
        self.risk.reset()
        self._last_action = VerdictAction.NONE
        self._last_risk_sent = -1.0
        self._events_total = 0
        log.info("сессия начата: %s", self.session_meta.get("student_name")
                 or self.session_meta.get("student_id"))
        await self._emit(make_event(EventKind.SESSION_STARTED,
                                    detail=dict(self.session_meta)))
        self._start_player("session_start от оболочки")
        await self._broadcast(self._status())

    async def _cmd_session_end(self, msg: dict[str, Any]) -> None:
        reason = str(msg.get("reason") or "завершение по команде оболочки")
        await self._emit(make_event(EventKind.SESSION_ENDED,
                                    detail={"reason": reason,
                                            "events_total": self._events_total,
                                            "final_risk": self.risk.score}))
        self.state = "idle"
        self._stop_player()
        log.info("сессия завершена: %s (событий: %d, риск %.1f)",
                 reason, self._events_total, self.risk.score)
        await self._broadcast(self._status())

    async def _cmd_calibrate(self, msg: dict[str, Any]) -> None:
        """Калибровка в моке проходит мгновенно и честно помечается как degraded."""
        stage = str(msg.get("stage") or "gaze_center")
        if self.state == "idle":
            self.state = "calibrating"
            await self._broadcast(self._status())
        for progress in (0.0, 0.45, 0.8):
            await self._broadcast(envelope(MsgType.CALIBRATION, stage=stage,
                                           progress=progress, done=False, result={}))
            await asyncio.sleep(0.35 / self.speed)
        await self._broadcast(envelope(
            MsgType.CALIBRATION, stage=stage, progress=1.0, done=True,
            result={"ok": True, "degraded": True, "mock": True,
                    "reason": "мок-режим: калибровка имитирована, камера не используется",
                    "samples": 30}))
        await self._emit(make_event(EventKind.CALIBRATION_DONE,
                                    detail={"stage": stage, "degraded": True}))
        if self.state == "calibrating":
            self.state = "running"
            await self._broadcast(self._status())

    async def _cmd_telemetry(self, msg: dict[str, Any]) -> None:
        """Мини-fusion: ответ сразу после «взгляда в сторону» или вставки.

        Полноценный FusionEngine живёт в `sidecar/engine/fusion.py`. Здесь
        нужна только минимальная корреляция, чтобы UI можно было отлаживать:
        если оболочка присылает `paste` или `answer_submit` в пределах 8 с
        после события взгляда/блюра/телефона — мок выдаёт соответствующую
        fusion-связку с таймингами из телеметрии.
        """
        kind = str(msg.get("kind") or "")
        now = time.time()
        window = 8.0

        if kind == "paste":
            length = int(msg.get("length") or 0)
            if length >= 200:
                await self._emit(make_event(
                    EventKind.PASTE_BURST, confidence=0.9,
                    detail={"question_id": msg.get("question_id"), "length": length,
                            "source": str(msg.get("source") or "clipboard")}))
        if kind not in ("paste", "answer_submit"):
            return

        pairs = (
            (self._last_gaze_event, EventKind.FUSION_GAZE_THEN_ANSWER, "взгляд уходил со экрана"),
            (self._last_blur_event, EventKind.FUSION_BLUR_THEN_ANSWER, "окно теряло фокус"),
            (self._last_phone_event, EventKind.FUSION_PHONE_THEN_ANSWER, "телефон был в кадре"),
        )
        best = max((p for p in pairs if p[0] and now - p[0] <= window),
                   key=lambda p: p[0], default=None)
        if best is None:
            return
        ts, fusion_kind, why = best
        stats = msg.get("typing_stats") if isinstance(msg.get("typing_stats"), dict) else {}
        await self._emit(make_event(fusion_kind, confidence=0.85, detail={
            "question_id": msg.get("question_id"),
            "lag_ms": int((now - ts) * 1000),
            "answer_chars": int(msg.get("length") or stats.get("chars") or 0),
            "mean_interval_ms": stats.get("mean_ms"),
            "trigger": why,
            "source": "mock_fusion",
        }))

    async def _cmd_shell_event(self, msg: dict[str, Any]) -> None:
        """Инциденты оболочки пропускаем насквозь — так отлаживается локдаун."""
        kind = _as_kind(msg.get("kind"))
        if kind is None:
            await self._broadcast(envelope(MsgType.ERROR, code="unknown_kind",
                                           message=f"Неизвестный вид события: {msg.get('kind')}",
                                           fatal=False))
            return
        detail = msg.get("detail")
        detail = dict(detail) if isinstance(detail, dict) else {}
        detail.setdefault("source", "shell")
        await self._emit(make_event(kind, detail=detail))

    async def _cmd_command(self, ws: Any, msg: dict[str, Any]) -> None:
        name = str(msg.get("name") or "")
        if name == "snapshot":
            log.info("snapshot: в мок-режиме кадра нет, запрос принят")
            await self._broadcast(self._status())
        elif name == "reset_risk":
            self.risk.reset()
            self._last_action = VerdictAction.NONE
            if self.state in ("paused", "locked"):
                self.state = "running"
            log.info("risk-score сброшен по команде оболочки")
            await self._push_risk(force=True)
            await self._broadcast(self._status())
        elif name == "export_report":
            log.info("export_report: в мок-режиме отчёт не формируется "
                     "(нет доказательной базы)")
            await self._broadcast(self._status())
        else:
            await self._send(ws, envelope(MsgType.ERROR, code="unknown_command",
                                          message=f"Неизвестная команда: {name}",
                                          fatal=False))

    # -------------------------------------------------------- события и риск
    async def _emit(self, event: ProctorEvent) -> None:
        """Отправить событие, обновить риск и, если уровень сменился, вердикт."""
        self._events_total += 1
        self.risk.add(event)
        if event.kind in (EventKind.GAZE_DOWN, EventKind.GAZE_SIDE,
                          EventKind.GAZE_OFF_SCREEN, EventKind.HEAD_TURNED):
            self._last_gaze_event = event.ts
        elif event.kind in (EventKind.WINDOW_BLUR, EventKind.FULLSCREEN_EXIT,
                            EventKind.SHORTCUT_BLOCKED):
            self._last_blur_event = event.ts
        elif event.kind.value.startswith("PHONE"):
            self._last_phone_event = event.ts
        await self._broadcast(envelope(MsgType.EVENT, event=event.to_dict()))
        await self._push_risk()

    async def _push_risk(self, force: bool = False) -> None:
        score = self.risk.score
        action = self.risk.action()
        if not force and abs(score - self._last_risk_sent) < 0.5 and action is self._last_action:
            return
        self._last_risk_sent = score
        await self._broadcast(self._risk_message())
        if action is not self._last_action:
            await self._apply_action(action, score)

    async def _apply_action(self, action: VerdictAction, score: float) -> None:
        """Смена уровня риска -> verdict + перевод состояния сессии."""
        prev, self._last_action = self._last_action, action
        # блокировка терминальна: спад риска не должен разблокировать экзамен сам
        if self.state == "locked" and action is not VerdictAction.LOCK:
            return
        top = [EVENT_MESSAGES.get(_as_kind(row["kind"]) or EventKind.SESSION_STARTED, row["kind"])
               for row in self.risk.breakdown()[:3]]
        tail = "; ".join(top) if top else "накопленный риск по совокупности сигналов"
        reason = f"{_VERDICT_PREFIX[action]}: {tail}"
        await self._broadcast(envelope(MsgType.VERDICT, action=action.value,
                                       reason=reason, score=score))
        if action is VerdictAction.LOCK:
            self.state = "locked"
        elif action is VerdictAction.PAUSE:
            self.state = "paused"
        elif self.state == "paused":
            self.state = "running"
        log.info("вердикт %s -> %s (%.1f)", prev.value, action.value, score)
        await self._broadcast(self._status())

    def _apply_status(self, patch: dict[str, Any]) -> None:
        """Наложить правку на снимок состояния (gaze сливается, не затирается)."""
        for key, value in patch.items():
            if key == "gaze" and isinstance(value, dict):
                self._snap["gaze"].update(value)
            elif key in self._snap:
                self._snap[key] = value
            elif key == "state":
                self.state = str(value)
            elif key == "fps":
                with contextlib.suppress(TypeError, ValueError):
                    self.fps = float(value)

    # ------------------------------------------------------------------ тикеры
    async def _status_ticker(self) -> None:
        """Оболочка считает тишину дольше 15 с мёртвым сокетом — шлём 2 Гц."""
        while True:
            await asyncio.sleep(self.status_interval)
            with contextlib.suppress(Exception):
                await self._broadcast(self._status())

    async def _risk_ticker(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            with contextlib.suppress(Exception):
                self.risk.decay()
                await self._push_risk()

    # ---------------------------------------------------------------- сценарий
    def _start_player(self, why: str) -> None:
        if self._player is not None and not self._player.done():
            return
        log.info("запуск сценария (%s)", why)
        self._player = asyncio.create_task(self._play(), name="scenario")

    def _stop_player(self) -> None:
        if self._player is not None:
            self._player.cancel()
            self._player = None

    async def _play(self) -> None:
        """Проигрывать сценарий: шаг за шагом, с учётом --speed и --loop."""
        try:
            while True:
                for number, step in enumerate(self.scenario.steps, 1):
                    await asyncio.sleep(step.delay / self.speed)
                    await self._run_step(number, step)
                if not self.loop_scenario:
                    log.info("сценарий отыгран до конца (событий: %d, риск %.1f)",
                             self._events_total, self.risk.score)
                    return
                log.info("сценарий завершён, пауза %.1f с и повтор",
                         self.scenario.pause_between_loops / self.speed)
                await asyncio.sleep(self.scenario.pause_between_loops / self.speed)
                self.risk.reset()
                self._last_action = VerdictAction.NONE
                if self.state in ("paused", "locked"):
                    self.state = "running"
                await self._push_risk(force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("сценарий упал — мок продолжает отдавать status")

    async def _run_step(self, number: int, step: Step) -> None:
        if step.say:
            log.info("[%d/%d] %s", number, len(self.scenario.steps), step.say)
        if step.show:
            # подсказка ведущему: что сделать на сцене в этот момент
            log.info("        на экране: %s", step.show)
        if step.status:
            self._apply_status(step.status)
            await self._broadcast(self._status())
        if step.risk == "reset":
            self.risk.reset()
            self._last_action = VerdictAction.NONE
            if self.state in ("paused", "locked"):
                self.state = "running"
            await self._push_risk(force=True)
            await self._broadcast(self._status())
        if step.calibration:
            payload = {"stage": "gaze_center", "progress": 1.0, "done": True, "result": {}}
            payload.update(step.calibration)
            await self._broadcast(envelope(MsgType.CALIBRATION, **payload))
        for event in step.events:
            event.ts = time.time()                 # сценарий может идти по кругу
            await self._emit(event)
        if step.verdict:
            action = VerdictAction.NONE
            with contextlib.suppress(ValueError):
                action = VerdictAction(str(step.verdict.get("action", "none")))
            await self._broadcast(envelope(
                MsgType.VERDICT, action=action.value,
                reason=str(step.verdict.get("reason") or _VERDICT_PREFIX[action]),
                score=float(step.verdict.get("score", self.risk.score))))
            self._last_action = action
            if action is VerdictAction.LOCK:
                self.state = "locked"
            elif action is VerdictAction.PAUSE:
                self.state = "paused"
            await self._broadcast(self._status())
        if step.error:
            await self._broadcast(envelope(
                MsgType.ERROR,
                code=str(step.error.get("code") or "mock_error"),
                message=str(step.error.get("message") or "Ошибка из сценария"),
                fatal=bool(step.error.get("fatal", False))))


# ===========================================================================
# CLI
# ===========================================================================
class _HandshakeNoiseFilter(logging.Filter):
    """Убрать трейсбек «opening handshake failed» из лога.

    Он появляется, когда кто-то открыл TCP-соединение и закрыл его, не начав
    WebSocket-рукопожатие — именно так работает проверка «поднялся ли порт»
    в Makefile и в лончере оболочки. Двадцать строк трейсбека в логе перед
    выходом на сцену читаются как авария, хотя это штатная ситуация.
    Настоящие ошибки сервера фильтр не трогает.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "opening handshake failed" not in record.getMessage()
        except Exception:
            return True


def _half_life_from_config() -> float:
    """Half-life риска из конфига проекта, если он доступен."""
    try:
        from config import ProctorConfig  # type: ignore
        cfg, _warnings = ProctorConfig.load()
        return float(cfg.risk_half_life)
    except Exception:
        return 90.0


def _describe(scenario: Scenario, speed: float) -> str:
    lines = [f"Сценарий: {scenario.name}  (источник: {scenario.source})",
             f"Шагов: {len(scenario.steps)}; длительность "
             f"{scenario.duration:.1f} с / {scenario.duration / speed:.1f} с при speed={speed:g}",
             ""]
    clock = 0.0
    for number, step in enumerate(scenario.steps, 1):
        clock += step.delay
        kinds = ", ".join(e.kind.value for e in step.events) or "—"
        extra = []
        if step.status:
            extra.append("status")
        if step.risk:
            extra.append(f"risk:{step.risk}")
        if step.verdict:
            extra.append("verdict")
        if step.calibration:
            extra.append("calibration")
        if step.error:
            extra.append("error")
        lines.append(f"  {number:2d}. +{step.delay:5.1f} с (t={clock:6.1f}) {kinds}"
                     + (f"  [{', '.join(extra)}]" if extra else ""))
        if step.say:
            lines.append(f"      {step.say}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mock_sidecar",
        description="Мок-сайдкар прокторинга: протокол тот же, события из сценария",
    )
    parser.add_argument("--host", default=DEFAULT_WS_HOST, help="адрес WS-сервера")
    parser.add_argument("--port", type=int, default=DEFAULT_WS_PORT, help="порт WS-сервера")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="ускорение сценария (2 — вдвое быстрее, 0.5 — вдвое медленнее)")
    parser.add_argument("--loop", action="store_true",
                        help="повторять сценарий по кругу")
    parser.add_argument("--no-loop", action="store_true",
                        help="не повторять, даже если в JSON стоит loop")
    parser.add_argument("--scenario", default=None,
                        help=f"путь к JSON-сценарию (по умолчанию {SCENARIO_PATH})")
    parser.add_argument("--no-autostart", action="store_true",
                        help="ждать session_start, а не стартовать при подключении")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="остановиться через N секунд (для проверок и CI)")
    parser.add_argument("--list", action="store_true",
                        help="показать разобранный сценарий и выйти")
    parser.add_argument("--log-level", default="INFO",
                        help="DEBUG/INFO/WARNING")
    args = parser.parse_args(argv)

    level = getattr(logging, str(args.log_level).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # «server listening», «connection open» и прочий внутренний поток библиотеки
    # на репетиции только мешает: о тех же событиях пишет наш собственный лог
    if level > logging.DEBUG:
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("websockets.server").addFilter(_HandshakeNoiseFilter())

    loop_flag: bool | None = None
    if args.loop:
        loop_flag = True
    if args.no_loop:
        loop_flag = False

    scenario = load_scenario(args.scenario, loop_default=bool(args.loop))
    speed = max(float(args.speed), 0.05)

    if args.list:
        print(_describe(scenario, speed))
        return 0

    mock = MockSidecar(scenario, host=args.host, port=args.port, speed=speed,
                       loop_scenario=loop_flag, autostart=not args.no_autostart,
                       half_life=_half_life_from_config())

    async def _runner() -> None:
        task = asyncio.create_task(mock.run())
        if args.duration and args.duration > 0:
            await asyncio.sleep(float(args.duration))
            mock.stop()
        await task

    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        log.info("прервано пользователем")
    except OSError as exc:
        log.error("не удалось поднять сервер на %s:%s — %s", args.host, args.port, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

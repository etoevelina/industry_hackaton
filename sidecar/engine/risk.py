"""
Risk-score: накопление, затухание и объяснимый разбор.

Три решения, без которых шкала нечестная.

1. **Вклад = вес * уверенность.** Веса берутся ТОЛЬКО из
   `protocol.RISK_WEIGHTS` — единственное место, где их можно менять. Слабое
   срабатывание детектора (уверенность 0.6) не должно весить как очевидное.

2. **Экспоненциальное затухание по half-life** (по умолчанию 180 с, ключ
   `risk_half_life`). Без спада одна случайная ошибка на второй минуте теста
   навсегда клеймит студента: счёт только растёт, и к концу экзамена любой
   выходит нарушителем. Со спадом счёт отражает ТЕКУЩУЮ картину: единичный
   инцидент тает, систематическое поведение — накапливается быстрее, чем тает.

3. **Разложение вместо числа.** `breakdown()` возвращает вклад по видам
   событий с количеством и суммой. HUD и отчёт показывают не «риск 64», а
   «телефон в кадре ×2 +41, взгляд вниз ×3 +18» — преподаватель видит,
   из чего сложилась цифра, и может поспорить с конкретным пунктом.

Залипание уровня: вверх реакция мгновенная, вниз — не чаще одной ступени в
`risk_level_hold` секунд (10 с). Иначе на границе порога HUD мигает
«предупреждение/норма» несколько раз в секунду и теряет доверие.
"""
from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_SIDECAR = Path(__file__).resolve().parent.parent
if str(_SIDECAR) not in sys.path:
    sys.path.insert(0, str(_SIDECAR))

from protocol import (  # noqa: E402  (путь настраиваем выше)
    RISK_LOCK,
    RISK_PAUSE,
    RISK_WARN,
    RISK_WEIGHTS,
    EventKind,
    ProctorEvent,
    VerdictAction,
)

#: Порядок уровней для сравнения «выше/ниже».
ACTION_ORDER: tuple[VerdictAction, ...] = (
    VerdictAction.NONE, VerdictAction.WARN, VerdictAction.PAUSE, VerdictAction.LOCK,
)

#: Уровень для HUD по действию.
ACTION_LEVEL: dict[VerdictAction, str] = {
    VerdictAction.NONE: "ok",
    VerdictAction.WARN: "warn",
    VerdictAction.PAUSE: "pause",
    VerdictAction.LOCK: "lock",
}

#: Короткие русские названия видов событий для разбора (отчёт и HUD).
KIND_LABEL: dict[EventKind, str] = {
    EventKind.PHONE_IN_FRAME: "телефон в кадре",
    EventKind.PHONE_RAISED: "телефон поднят к лицу",
    EventKind.PHONE_AIMED_AT_SCREEN: "телефон направлен на экран",
    EventKind.FORBIDDEN_OBJECT: "посторонний предмет",
    EventKind.NO_FACE: "лица нет в кадре",
    EventKind.SECOND_FACE: "второе лицо в кадре",
    EventKind.IDENTITY_MISMATCH: "другой человек за компьютером",
    EventKind.LIVENESS_FAIL: "подозрение на фото вместо человека",
    EventKind.GAZE_DOWN: "взгляд вниз",
    EventKind.GAZE_SIDE: "взгляд в сторону",
    EventKind.GAZE_OFF_SCREEN: "взгляд вне экрана",
    EventKind.HEAD_TURNED: "голова отвернута",
    EventKind.VOICE_OTHER: "посторонний голос",
    EventKind.SPEECH_WITHOUT_LIP_MOTION: "речь без движения губ",
    EventKind.VIRTUAL_CAMERA: "виртуальная камера",
    EventKind.REMOTE_ACCESS_SOFTWARE: "ПО удалённого доступа",
    EventKind.VIRTUAL_MACHINE: "виртуальная машина",
    EventKind.SCREEN_RECORDING: "запись экрана",
    EventKind.MULTIPLE_DISPLAYS: "несколько мониторов",
    EventKind.BLACKLISTED_PROCESS: "запрещённый процесс",
    EventKind.WINDOW_BLUR: "уход из окна экзамена",
    EventKind.FULLSCREEN_EXIT: "выход из полного экрана",
    EventKind.SHORTCUT_BLOCKED: "заблокированный хоткей",
    EventKind.CLIPBOARD_PASTE: "вставка из буфера",
    EventKind.DEVTOOLS_ATTEMPT: "попытка открыть devtools",
    EventKind.PASTE_BURST: "вставка крупного блока текста",
    EventKind.TYPING_ANOMALY: "аномалия ритма набора",
    EventKind.FUSION_GAZE_THEN_ANSWER: "взгляд в сторону → готовый ответ",
    EventKind.FUSION_BLUR_THEN_ANSWER: "переключение окна → готовый ответ",
    EventKind.FUSION_PHONE_THEN_ANSWER: "телефон → готовый ответ",
    EventKind.SENSOR_LOST: "пропал источник данных",
    EventKind.SESSION_STARTED: "начало сессии",
    EventKind.SESSION_ENDED: "конец сессии",
    EventKind.CALIBRATION_DONE: "калибровка завершена",
}

#: Ниже этого вклад считается истаявшим и запись выбрасывается (экономия памяти).
_PRUNE_FLOOR = 0.05


@dataclass
class _Item:
    """Один учтённый инцидент."""
    ts: float
    kind: EventKind
    base: float          # вес * уверенность на момент события
    event_id: str
    confidence: float
    message: str


class RiskScorer:
    """Накопительный risk-score с затуханием, разбором и залипанием уровня.

    Контракт (docs/CONTRACT.md): add / decay / score / breakdown / action.
    Дополнительно: reset(), level(), explain(), top_reason(), to_dict().
    """

    def __init__(self, config: dict | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        cfg = config or {}
        self.cfg = cfg
        self._clock: Callable[[], float] = clock or time.time
        self.half_life = max(_as_float(cfg.get("risk_half_life"), 180.0), 1.0)
        self.max_score = max(_as_float(cfg.get("risk_max"), 100.0), 1.0)
        self.warn = _as_float(cfg.get("risk_warn"), RISK_WARN)
        self.pause = _as_float(cfg.get("risk_pause"), RISK_PAUSE)
        self.lock = _as_float(cfg.get("risk_lock"), RISK_LOCK)
        #: Минимальная уверенность: событие с conf=0 всё равно что-то значит.
        self.min_confidence = _as_float(cfg.get("risk_min_confidence"), 0.2)
        #: Сколько держать уровень перед понижением на одну ступень.
        self.level_hold = max(_as_float(cfg.get("risk_level_hold"), 10.0), 0.0)

        self._items: list[_Item] = []
        self._totals: dict[EventKind, dict[str, Any]] = {}
        self._now = self._clock()
        self._action = VerdictAction.NONE
        self._action_changed = self._now
        self._peak = 0.0

    # ----------------------------------------------------------------- сброс
    def reset(self) -> None:
        """Обнулить счёт (новая сессия или команда reset_risk)."""
        self._items.clear()
        self._totals.clear()
        self._now = self._clock()
        self._action = VerdictAction.NONE
        self._action_changed = self._now
        self._peak = 0.0

    def available(self) -> bool:
        return True

    # ------------------------------------------------------------ накопление
    def add(self, event: ProctorEvent) -> None:
        """Учесть инцидент: score += weight * confidence.

        Вес — только из RISK_WEIGHTS. Служебные события (вес 0) не копятся,
        но попадают в пожизненные счётчики: отчёту полезно знать, что они были.
        """
        kind = getattr(event, "kind", None)
        if not isinstance(kind, EventKind):
            return
        weight = RISK_WEIGHTS.get(kind, 10.0)
        confidence = _clamp(_as_float(getattr(event, "confidence", 1.0), 1.0),
                            self.min_confidence, 1.0)
        ts = _as_float(getattr(event, "ts", None), self._clock())
        self._now = max(self._now, ts)

        row = self._totals.setdefault(kind, {"count": 0, "first_ts": ts, "last_ts": ts})
        row["count"] += 1
        row["last_ts"] = max(row["last_ts"], ts)
        row["first_ts"] = min(row["first_ts"], ts)

        base = weight * confidence
        if base <= 0:
            return
        self._items.append(_Item(
            ts=ts,
            kind=kind,
            base=base,
            event_id=str(getattr(event, "id", "")),
            confidence=confidence,
            message=str(getattr(event, "message", "") or ""),
        ))
        self._peak = max(self._peak, self.score)

    # -------------------------------------------------------------- затухание
    def decay(self, now: float | None = None) -> None:
        """Передвинуть «сейчас» и выбросить истаявшие вклады.

        Сам спад считается аналитически в `_value()`, поэтому вызывать decay
        можно с любой частотой — результат от неё не зависит. Метод нужен
        только чтобы список не рос бесконечно и чтобы score знал текущее время.
        """
        now = _as_float(now, self._clock())
        self._now = max(self._now, now)
        if self._items:
            self._items = [it for it in self._items
                           if self._value(it, self._now) >= _PRUNE_FLOOR]

    def _eval_ts(self) -> float:
        """Момент, на который считаем счёт: «сейчас», но не раньше известного."""
        return max(self._now, self._clock())

    def _value(self, item: _Item, now: float) -> float:
        age = max(now - item.ts, 0.0)
        return item.base * math.pow(0.5, age / self.half_life)

    # ------------------------------------------------------------------ счёт
    @property
    def score(self) -> float:
        """Текущий счёт, 0..risk_max (клиппинг обязателен: сумма может уйти за 100)."""
        now = self._eval_ts()
        total = sum(self._value(it, now) for it in self._items)
        return round(_clamp(total, 0.0, self.max_score), 2)

    @property
    def raw_score(self) -> float:
        """Сумма вкладов без клиппинга — видно, насколько «зашкалило»."""
        now = self._eval_ts()
        return round(sum(self._value(it, now) for it in self._items), 2)

    @property
    def peak(self) -> float:
        return round(max(self._peak, self.score), 2)

    # ------------------------------------------------------------- разложение
    def breakdown(self) -> list[dict[str, Any]]:
        """Вклад по видам событий, по убыванию вклада.

        Поля строки: kind, label (по-русски), contribution (текущий вклад с
        учётом спада), count (сколько вкладов ещё живо), count_total (сколько
        было за сессию), weight (вес вида), share (доля в текущем счёте, %),
        last_ts, age_sec, example (текст последнего такого инцидента).
        Первые три поля — то, что рисует HUD; остальные нужны отчёту.
        """
        now = self._eval_ts()
        agg: dict[EventKind, dict[str, Any]] = {}
        for item in self._items:
            value = self._value(item, now)
            if value < _PRUNE_FLOOR:
                continue
            row = agg.get(item.kind)
            if row is None:
                row = agg[item.kind] = {
                    "kind": item.kind.value,
                    "label": KIND_LABEL.get(item.kind, item.kind.value),
                    "contribution": 0.0,
                    "count": 0,
                    "weight": RISK_WEIGHTS.get(item.kind, 10.0),
                    "last_ts": item.ts,
                    "example": item.message,
                }
            row["contribution"] += value
            row["count"] += 1
            if item.ts >= row["last_ts"]:
                row["last_ts"] = item.ts
                if item.message:
                    row["example"] = item.message

        total = sum(r["contribution"] for r in agg.values()) or 1.0
        rows = sorted(agg.values(), key=lambda r: r["contribution"], reverse=True)
        for row in rows:
            kind = EventKind(row["kind"])
            row["contribution"] = round(row["contribution"], 2)
            row["share"] = round(100.0 * row["contribution"] / total, 1)
            row["count_total"] = int(self._totals.get(kind, {}).get("count", row["count"]))
            row["age_sec"] = round(max(now - row["last_ts"], 0.0), 1)
        return rows

    def lifetime_counts(self) -> dict[str, int]:
        """Сколько инцидентов каждого вида было за сессию (без учёта спада)."""
        return {kind.value: int(row["count"]) for kind, row in self._totals.items()}

    # --------------------------------------------------------------- вердикт
    def action(self) -> VerdictAction:
        """Действие по порогам с залипанием уровня.

        Вверх — сразу: если риск вырос, предупреждать надо немедленно.
        Вниз — не быстрее одной ступени за `risk_level_hold` секунд, иначе на
        границе порога HUD мигает и выглядит как баг, а не как контроль.
        """
        now = self._eval_ts()
        raw = self._raw_action(self.score)
        cur_idx = ACTION_ORDER.index(self._action)
        raw_idx = ACTION_ORDER.index(raw)

        if raw_idx > cur_idx:
            self._action = raw
            self._action_changed = now
            return self._action
        if raw_idx < cur_idx:
            if (now - self._action_changed) + 1e-6 >= self.level_hold:
                self._action = ACTION_ORDER[max(raw_idx, cur_idx - 1)]
                self._action_changed = now
            return self._action
        return self._action

    def _raw_action(self, score: float) -> VerdictAction:
        if score >= self.lock:
            return VerdictAction.LOCK
        if score >= self.pause:
            return VerdictAction.PAUSE
        if score >= self.warn:
            return VerdictAction.WARN
        return VerdictAction.NONE

    def level(self) -> str:
        """Уровень для HUD: ok / warn / pause / lock."""
        return ACTION_LEVEL.get(self.action(), "ok")

    # ------------------------------------------------------------ объяснение
    def top_reason(self) -> str:
        """Главный вклад в текущий счёт — человекочитаемо."""
        rows = self.breakdown()
        if not rows:
            return "нарушений не зафиксировано"
        row = rows[0]
        count = f" ×{row['count']}" if row["count"] > 1 else ""
        return f"{row['label']}{count} (+{row['contribution']:.0f})"

    def explain(self, limit: int = 3) -> str:
        """Фраза для вердикта и отчёта: из чего сложился счёт."""
        rows = self.breakdown()[:max(limit, 1)]
        if not rows:
            return f"Риск {self.score:.0f}: нарушений не зафиксировано."
        parts = []
        for row in rows:
            count = f" ×{row['count']}" if row["count"] > 1 else ""
            parts.append(f"{row['label']}{count} +{row['contribution']:.0f}")
        return f"Риск {self.score:.0f} из {self.max_score:.0f}: " + "; ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Снимок состояния для отчёта и summary сессии."""
        return {
            "score": self.score,
            "raw_score": self.raw_score,
            "peak": self.peak,
            "action": self.action().value,
            "level": self.level(),
            "half_life": self.half_life,
            "thresholds": {"warn": self.warn, "pause": self.pause, "lock": self.lock},
            "breakdown": self.breakdown(),
            "lifetime_counts": self.lifetime_counts(),
            "explain": self.explain(),
        }


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


__all__ = ["RiskScorer", "KIND_LABEL", "ACTION_ORDER", "ACTION_LEVEL"]

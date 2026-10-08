"""
Fusion: корреляция наблюдений камеры с телеметрией ввода.

Главная ценность системы. Отдельный сигнал почти ничего не доказывает:
студент имеет право посмотреть в сторону, потянуться, переключить окно,
быстро набирать текст. Доказательной становится СВЯЗКА и её тайминг:
взгляд ушёл с экрана на четыре секунды — и через полторы секунды в поле
ответа появилось триста символов вставкой. Именно такие пары этот модуль
находит, описывает по-русски и отдаёт как отдельные FUSION_* события.

Что держим в памяти:
* скользящее окно CV- и shell-событий (60 с) — кандидаты на «левую половину»
  связки; каждое событие используется в связке не более одного раза, иначе
  один blur породил бы три инцидента на три следующих ответа;
* базовая линия набора студента — median и робастная сигма (1.4826*MAD) по
  первым N межклавишным интервалам. Median и MAD, а не mean и std: одна
  пауза «задумался на минуту» сдвигает среднее так, что профиль перестаёт
  что-либо значить, а медиану не трогает;
* текущий «бурст» набора — непрерывный ряд символьных нажатий с интервалом
  ниже порога.

Правила (каждое порождает отдельное событие с описанием ОБЕИХ половин связки):
  1. GAZE_SIDE / GAZE_OFF_SCREEN / GAZE_DOWN длительностью >= 3 с, затем в
     течение 5 с — paste или бурст >= 150 символов со средним интервалом
     < 40 % от базового  =>  FUSION_GAZE_THEN_ANSWER
  2. WINDOW_BLUR / FULLSCREEN_EXIT, затем в течение 8 с — paste или ответ
     =>  FUSION_BLUR_THEN_ANSWER
  3. PHONE_IN_FRAME / PHONE_RAISED / PHONE_AIMED_AT_SCREEN, затем ответ в
     течение 10 с  =>  FUSION_PHONE_THEN_ANSWER
  4. answer_submit с time_to_answer_ms < 3000 при length > 200, либо одиночная
     вставка >= 300 символов  =>  PASTE_BURST
  5. typing_stats.mean_ms отклоняется от базовой линии более чем на k робастных
     сигм  =>  TYPING_ANOMALY

ОДНО НАБЛЮДЕНИЕ — ОДНО ОБВИНЕНИЕ. Для защиты опаснее пропуска самоподтверждение:
когда одно наблюдение раскладывается на три «независимых» признака и риск
складывается трижды. Так ошиблись в команде Duolingo English Test — «нерегулярный
ритм набора» стоял в списке подтверждающих признаков и подтверждал сам себя;
флаг убрали и добавили правило «не применять его, если других подозрительных
действий не наблюдалось». У нас было то же самое: на ОДИН отправленный ответ
PASTE_BURST (30) + TYPING_ANOMALY (15) + FUSION_BLUR_THEN_ANSWER (55) = 100,
то есть автоблокировка (RISK_LOCK = 90) с одного события. Поэтому работают три
дедупликации:

  * на один «ответ» — не больше одной связки: та, чьё правило весит больше
    (RISK_WEIGHTS). Остальные совпавшие триггеры перечисляются в detail["also"]:
    они видны в отчёте, но не умножают risk-score на пустом месте;
  * пока связка по вопросу жива (fusion_dedup_sec), вторая связка по тому же
    вопросу не выпускается. Вставка и следующая за ней отправка ответа — одно
    наблюдение, а не два, и правила 1-3 не должны «добирать» второй вес на
    втором сообщении телеметрии об одном и том же эпизоде;
  * при выпущенной связке TYPING_ANOMALY по тому же вопросу подавляется:
    клавиатурный тайминг уже учтён ВНУТРИ связки (правило 1 отбирает правую
    половину по base_ratio, то есть по тем же межклавишным интервалам, из
    которых рождается TYPING_ANOMALY), и второй раз считать его нельзя.

PASTE_BURST при этом остаётся — у него есть собственный канал наблюдения, не
выводимый из тайминга: счётчик символьных нажатий по вопросу (`_typed_chars`).
Он отвечает на вопрос «хватает ли набранного руками, чтобы объяснить длину
ответа», а не «быстро ли печатали». Именно это отличает нас от наивного
«быстро печатает — значит списывает».

Каждое подавление записывается: в detail связки (ключ "suppressed") и в
stats()["suppressed"]. Молча не подавляется ничего — иначе отчёт расходится с
кодом, а на защите ловят именно на расхождении документа и кода.

ПРИВАТНОСТЬ (жёсткое требование кейса). Внутрь попадают только межклавишные
интервалы в миллисекундах, классы клавиш и длины текста в символах. Ни одного
символа клавиши, ни строки ответа, ни содержимого буфера обмена здесь нет и
быть не может — их не присылает даже оболочка.
"""
from __future__ import annotations

import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque

_SIDECAR = Path(__file__).resolve().parent.parent
if str(_SIDECAR) not in sys.path:
    sys.path.insert(0, str(_SIDECAR))

from protocol import (  # noqa: E402  (путь настраиваем выше)
    RISK_WEIGHTS,
    Channel,
    EventKind,
    ProctorEvent,
    Severity,
    safe_label,
)

_EPS = 1e-6

#: Метка «вопрос не назван». Оболочка отдаёт пустую строку, если у вопроса в
#: банке нет id; события такого эпизода всё равно обязаны дедуплицироваться
#: между собой, иначе снимается и потолок наблюдения в `engine/risk.py`.
_ANON_QUESTION = "\x00без-вопроса"

#: Параметры fusion. Переопределяются одноимёнными ключами конфига.
DEFAULTS: dict[str, Any] = {
    "fusion_window_sec": 60.0,          # скользящее окно хранения событий
    "fusion_baseline_keystrokes": 40,   # сколько интервалов берём в базовую линию
    "fusion_baseline_min": 12,          # минимум, с которого линия уже что-то значит
    "fusion_keystroke_min_ms": 15.0,    # ниже — артефакт автоповтора, не нажатие
    "fusion_keystroke_max_ms": 2000.0,  # выше — пауза на размышление, не ритм

    "fusion_gaze_min_sec": 3.0,         # правило 1: сколько держался взгляд в сторону
    "fusion_gaze_window_sec": 5.0,      # правило 1: окно «затем ответ»
    "fusion_blur_window_sec": 8.0,      # правило 2
    "fusion_phone_window_sec": 10.0,    # правило 3

    "fusion_burst_chars": 150,          # длина бурста, подозрительная сама по себе
    "fusion_burst_ratio": 0.4,          # интервал < 40 % от базового
    "fusion_burst_abs_ms": 45.0,        # запасной порог, пока базы ещё нет
    "fusion_paste_min_chars": 20,       # вставка меньше — не «ответ»

    "fusion_fast_answer_ms": 3000,      # правило 4: ответ быстрее этого
    "fusion_fast_answer_chars": 200,    # правило 4: и длиннее этого
    "fusion_paste_burst_chars": 300,    # правило 4: одиночная крупная вставка
    # правило 4: какая доля длины ответа должна быть объяснена нажатиями клавиш
    # по этому вопросу, чтобы «быстрый длинный ответ» не считался вставкой
    "fusion_explained_ratio": 0.5,

    "fusion_typing_k": 3.0,             # правило 5: сколько робастных сигм
    "fusion_typing_min_chars": 20,      # правило 5: короткий ответ ничего не значит

    "fusion_cooldown_sec": 20.0,        # пауза между событиями одного вида
    # Дедупликация по одному наблюдению: сколько секунд связка по вопросу
    # «занимает» это наблюдение. Пока она жива, второе правило на том же
    # наблюдении события не выпускает, а записывает факт подавления.
    "fusion_dedup_sec": 20.0,
}

#: Какие события могут быть «левой половиной» связки.
GAZE_TRIGGERS = (EventKind.GAZE_SIDE, EventKind.GAZE_OFF_SCREEN, EventKind.GAZE_DOWN)
BLUR_TRIGGERS = (EventKind.WINDOW_BLUR, EventKind.FULLSCREEN_EXIT)
PHONE_TRIGGERS = (EventKind.PHONE_IN_FRAME, EventKind.PHONE_RAISED,
                  EventKind.PHONE_AIMED_AT_SCREEN)

#: События, которые fusion не рассматривает (включая свои — защита от петли).
IGNORED_TRIGGERS = frozenset({
    EventKind.FUSION_GAZE_THEN_ANSWER, EventKind.FUSION_BLUR_THEN_ANSWER,
    EventKind.FUSION_PHONE_THEN_ANSWER, EventKind.PASTE_BURST,
    EventKind.TYPING_ANOMALY, EventKind.SESSION_STARTED, EventKind.SESSION_ENDED,
    EventKind.CALIBRATION_DONE,
})

#: Виды «ответа», которые принимает каждое правило.
RULE_RESPONSES: dict[str, frozenset[str]] = {
    "gaze_then_answer": frozenset({"paste", "typing_burst"}),
    "blur_then_answer": frozenset({"paste", "answer_submit", "typing_burst"}),
    "phone_then_answer": frozenset({"paste", "answer_submit", "typing_burst"}),
}

#: Русские названия триггеров для текста события.
TRIGGER_RU: dict[EventKind, str] = {
    EventKind.GAZE_SIDE: "взгляд уходил в сторону",
    EventKind.GAZE_OFF_SCREEN: "взгляд уходил за пределы экрана",
    EventKind.GAZE_DOWN: "взгляд уходил вниз",
    EventKind.WINDOW_BLUR: "окно экзамена теряло фокус",
    EventKind.FULLSCREEN_EXIT: "был выход из полноэкранного режима",
    EventKind.PHONE_IN_FRAME: "телефон был в кадре",
    EventKind.PHONE_RAISED: "телефон был поднят к лицу",
    EventKind.PHONE_AIMED_AT_SCREEN: "телефон был направлен на экран",
}

#: Русские названия «ответа».
RESPONSE_RU = {
    "paste": "вставка из буфера",
    "answer_submit": "отправка ответа",
    "typing_burst": "скоростной набор",
}


def severity_for_weight(weight: float) -> Severity:
    if weight >= 55:
        return Severity.CRITICAL
    if weight >= 35:
        return Severity.HIGH
    if weight >= 20:
        return Severity.MEDIUM
    if weight >= 8:
        return Severity.LOW
    return Severity.INFO


@dataclass
class _Trigger:
    """Левая половина связки — событие камеры или оболочки."""
    kind: EventKind
    ts: float                    # когда движок зафиксировал событие
    fire_duration: float         # duration на момент фиксации
    event: ProctorEvent | None   # ссылка: EventEngine продлевает duration на месте
    detail: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    message: str = ""
    used: bool = False

    @property
    def duration(self) -> float:
        """Актуальная длительность условия (движок обновляет её на месте)."""
        if self.event is not None:
            return max(float(getattr(self.event, "duration", 0.0) or 0.0),
                       self.fire_duration)
        return self.fire_duration

    @property
    def start_ts(self) -> float:
        return self.ts - self.fire_duration

    @property
    def end_ts(self) -> float:
        """Момент, когда условие перестало наблюдаться (или «сейчас», если идёт)."""
        return self.ts + max(0.0, self.duration - self.fire_duration)


@dataclass
class _Baseline:
    """Персональный профиль набора по первым N интервалам."""
    samples: list[float] = field(default_factory=list)
    mean_ms: float = 0.0     # median — устойчив к паузам на размышление
    std_ms: float = 0.0      # 1.4826 * MAD — робастная сигма
    frozen: bool = False

    #: Ниже этого медианного интервала клавиатурные каналы СЛАБЫ, и это надо
    #: сказать вслух. 100 мс — это 10 нажатий в секунду; так печатает и очень
    #: быстрый машинист, и испорченная калибровка, и различить их по одному
    #: профилю нельзя. Поэтому метка ниже ничего не утверждает о студенте.
    FAST_BASELINE_MS = 100.0

    def as_dict(self, ready: bool) -> dict[str, Any]:
        out = {"mean_ms": round(self.mean_ms, 1), "std_ms": round(self.std_ms, 1),
               "samples": len(self.samples), "ready": ready, "metric": "median+MAD"}
        # Базовая линия ОСЛАБЛЯЕТ оба клавиатурных канала: от неё зависит и
        # порог правила 5, и порог бурста (`_burst_cutoff_ms`). Поэтому
        # неправдоподобно быстрый профиль — это не курьёз, а признак того, что
        # калибровку намеренно «помяли»: студент стучит по клавиатуре в начале
        # сессии, профиль уезжает вдвое вниз, и оба канала глохнут разом.
        # Остановить сессию из-за этого нельзя (ложное срабатывание = отказ в
        # экзамене), но молчать тоже нельзя: факт уходит в отчёт человеку.
        if ready and 0 < self.mean_ms < _Baseline.FAST_BASELINE_MS:
            out["weak_keyboard_channel"] = True
            out["weak_keyboard_note"] = (
                f"базовый интервал {self.mean_ms:.0f} мс — это около "
                f"{1000.0 / self.mean_ms:.0f} нажатий в секунду. Порог правила 5 "
                "и порог бурста выводятся ИЗ этого числа, поэтому на такой базе "
                "оба клавиатурных канала срабатывают заметно реже обычного. "
                "Причина может быть любой из двух: студент действительно быстро "
                "печатает, либо калибровка была испорчена в начале сессии "
                "(дробь по клавиатуре). Различить их по одному профилю нельзя, "
                "и вывода о студенте отсюда не следует — это предупреждение о "
                "СЛАБОСТИ канала, а не о нарушении. Опирайтесь на камеру, "
                "окружение и правило 4 (оно считает нажатия, а не их тайминг)"
            )
        return out


class FusionEngine:
    """Коррелирует CV-события с телеметрией ввода.

    Контракт (docs/CONTRACT.md): note_event(event) и note_telemetry(msg).
    Дополнительно: reset(), baseline(), stats().

    Вызывается из одного потока (asyncio-луп сайдкара), внутренних блокировок
    нет намеренно — лишний lock в горячем пути ничего здесь не защищает.
    """

    def __init__(self, config: dict | None = None) -> None:
        cfg = config or {}
        self.cfg = cfg
        self._p = {key: cfg.get(key, default) for key, default in DEFAULTS.items()}

        self.window_sec = _f(self._p["fusion_window_sec"], 60.0)
        self.baseline_target = int(_f(self._p["fusion_baseline_keystrokes"], 40.0))
        self.baseline_min = int(_f(self._p["fusion_baseline_min"], 12.0))
        self.key_min_ms = _f(self._p["fusion_keystroke_min_ms"], 15.0)
        self.key_max_ms = _f(self._p["fusion_keystroke_max_ms"], 2000.0)
        self.gaze_min_sec = _f(self._p["fusion_gaze_min_sec"], 3.0)
        self.gaze_window = _f(self._p["fusion_gaze_window_sec"], 5.0)
        self.blur_window = _f(self._p["fusion_blur_window_sec"], 8.0)
        self.phone_window = _f(self._p["fusion_phone_window_sec"], 10.0)
        self.burst_chars = int(_f(self._p["fusion_burst_chars"], 150.0))
        self.burst_ratio = _f(self._p["fusion_burst_ratio"], 0.4)
        self.burst_abs_ms = _f(self._p["fusion_burst_abs_ms"], 45.0)
        self.paste_min_chars = int(_f(self._p["fusion_paste_min_chars"], 20.0))
        self.fast_answer_ms = _f(self._p["fusion_fast_answer_ms"], 3000.0)
        self.fast_answer_chars = int(_f(self._p["fusion_fast_answer_chars"], 200.0))
        self.paste_burst_chars = int(_f(self._p["fusion_paste_burst_chars"], 300.0))
        self.explained_ratio = _clamp(_f(self._p["fusion_explained_ratio"], 0.5), 0.0, 1.0)
        self.typing_k = _f(self._p["fusion_typing_k"], 3.0)
        self.typing_min_chars = int(_f(self._p["fusion_typing_min_chars"], 20.0))
        self.cooldown = _f(self._p["fusion_cooldown_sec"], 20.0)
        self.dedup_sec = max(_f(self._p["fusion_dedup_sec"], 20.0), 0.0)

        self._triggers: Deque[_Trigger] = deque()
        self._base = _Baseline()
        self._last_fire: dict[str, float] = {}
        self._now = 0.0
        self._fired = 0
        self._links: list[dict[str, Any]] = []

        # текущий бурст набора
        self._burst_chars = 0
        self._burst_sum_ms = 0.0
        self._burst_start = 0.0
        self._burst_fired = False
        self._burst_question = ""

        # контекст вопроса
        self._question = ""
        self._question_shown_ts = 0.0
        self._question_difficulty = 0
        self._keystrokes = 0
        self._pastes = 0
        #: Символьные нажатия по каждому вопросу за всю сессию. Нужны правилу 4:
        #: оболочка в answer_submit присылает полную длину поля, а не набранное
        #: за этот показ вопроса, и без этого счётчика повторный заход в уже
        #: отвеченный вопрос выглядит как мгновенная вставка готового ответа.
        self._typed_chars: dict[str, int] = {}
        #: След уже выпущенного события по вопросу: {"kind", "ts", "detail",
        #: "link_row"}. Нужен дедупликации — по нему видно, что наблюдение по
        #: этому вопросу уже оплачено, и куда записать факт подавления.
        self._link_by_question: dict[str, dict[str, Any]] = {}
        self._paste_by_question: dict[str, dict[str, Any]] = {}
        #: Журнал подавлений (последние 200) — уходит в stats() и в отчёт.
        self._suppressions: list[dict[str, Any]] = []

    # ----------------------------------------------------------------- сброс
    def reset(self) -> None:
        """Новая сессия: профиль набора и окно событий строятся с нуля."""
        self._triggers.clear()
        self._base = _Baseline()
        self._last_fire.clear()
        self._links.clear()
        self._now = 0.0
        self._fired = 0
        self._reset_burst()
        self._question = ""
        self._question_shown_ts = 0.0
        self._question_difficulty = 0
        self._keystrokes = 0
        self._pastes = 0
        self._typed_chars.clear()
        self._link_by_question.clear()
        self._paste_by_question.clear()
        self._suppressions.clear()

    def available(self) -> bool:
        return True

    # --------------------------------------------------------- приём событий
    def note_event(self, event: ProctorEvent) -> None:
        """Запомнить CV/shell-событие как возможную левую половину связки."""
        kind = getattr(event, "kind", None)
        if not isinstance(kind, EventKind) or kind in IGNORED_TRIGGERS:
            return
        if kind not in GAZE_TRIGGERS and kind not in BLUR_TRIGGERS and kind not in PHONE_TRIGGERS:
            return
        ts = _f(getattr(event, "ts", None), time.time())
        self._now = max(self._now, ts)
        duration = _f(getattr(event, "duration", 0.0), 0.0)
        detail = getattr(event, "detail", None)
        self._triggers.append(_Trigger(
            kind=kind,
            ts=ts,
            fire_duration=duration,
            event=event,
            detail=dict(detail) if isinstance(detail, dict) else {},
            confidence=_clamp(_f(getattr(event, "confidence", 1.0), 1.0), 0.05, 1.0),
            message=str(getattr(event, "message", "") or ""),
        ))
        self._prune(ts)

    def _prune(self, now: float) -> None:
        horizon = now - self.window_sec
        while self._triggers and self._triggers[0].end_ts < horizon:
            self._triggers.popleft()

    # ------------------------------------------------------ приём телеметрии
    def note_telemetry(self, msg: dict) -> list[ProctorEvent]:
        """Обработать сообщение телеметрии по контракту.

        Виды: keystroke / paste / answer_submit / question_shown.
        Возвращает список готовых fusion-событий (обычно пустой).
        """
        if not isinstance(msg, dict):
            return []
        kind = str(msg.get("kind") or "").strip().lower()
        ts = _ts(msg.get("ts"))
        self._now = max(self._now, ts)
        self._prune(ts)

        if kind == "question_shown":
            return self._on_question_shown(msg, ts)
        if kind == "keystroke":
            return self._on_keystroke(msg, ts)
        if kind == "paste":
            return self._on_paste(msg, ts)
        if kind == "answer_submit":
            return self._on_answer(msg, ts)
        return []

    # ---------------------------------------------------------- question_shown
    def _on_question_shown(self, msg: dict, ts: float) -> list[ProctorEvent]:
        self._question = safe_label(msg.get("question_id"))
        self._question_shown_ts = ts
        self._question_difficulty = int(_f(msg.get("difficulty"), 0.0))
        self._reset_burst()
        return []

    # ----------------------------------------------------------------- набор
    def _on_keystroke(self, msg: dict, ts: float) -> list[ProctorEvent]:
        """Интервал нажатия: пополняет базовую линию и ведёт бурст.

        Символы клавиш сюда не приходят — только interval_ms и key_class.
        """
        interval = _f(msg.get("interval_ms"), 0.0)
        key_class = str(msg.get("key_class") or "char").strip().lower()
        question = safe_label(msg.get("question_id")) or self._question
        self._keystrokes += 1

        if key_class != "char":
            # навигация и модификаторы ритм не характеризуют и бурст не ломают
            return []

        # счётчик «сколько реально набрано руками» по вопросу (правило 4)
        if question:
            if len(self._typed_chars) > 256 and question not in self._typed_chars:
                self._typed_chars.clear()   # защита от неограниченного роста
            self._typed_chars[question] = self._typed_chars.get(question, 0) + 1

        # В профиль идут только интервалы, похожие на живой набор: ниже
        # burst_abs_ms руками не печатают, это вставка или автоповтор, и такой
        # интервал испортил бы базовую линию, с которой потом сравниваем.
        base_floor = max(self.key_min_ms, self.burst_abs_ms)
        if base_floor <= interval <= self.key_max_ms and not self._base.frozen:
            self._base.samples.append(interval)
            if len(self._base.samples) >= self.baseline_target:
                self._freeze_baseline()
            elif len(self._base.samples) >= self.baseline_min:
                self._recompute_baseline()

        cutoff = self._burst_cutoff_ms()
        if interval <= 0:
            # первое нажатие по вопросу: интервала нет, бурст не ломаем
            return []
        if interval < cutoff:
            if self._burst_chars == 0:
                self._burst_start = ts - interval / 1000.0
                self._burst_question = question
                self._burst_fired = False
            self._burst_chars += 1
            self._burst_sum_ms += interval
            if self._burst_chars >= self.burst_chars and not self._burst_fired:
                self._burst_fired = True
                return self._correlate(self._burst_response(ts), ts)
            return []
        self._reset_burst()
        return []

    def _reset_burst(self) -> None:
        self._burst_chars = 0
        self._burst_sum_ms = 0.0
        self._burst_start = 0.0
        self._burst_fired = False
        self._burst_question = ""

    def _burst_cutoff_ms(self) -> float:
        """Порог интервала, ниже которого набор считается «нечеловеческим».

        При готовой базовой линии — доля от личной медианы (40 %). Пока линии
        нет — абсолютный порог: устойчивый набор быстрее ~22 символов в секунду
        руками не делается, это вставка или скрипт.
        """
        if self._baseline_ready():
            return max(self._base.mean_ms * self.burst_ratio, 5.0)
        return self.burst_abs_ms

    def _burst_response(self, ts: float) -> dict[str, Any]:
        """Описание бурста.

        `anchor_ts` — НАЧАЛО бурста, а не момент, когда набралось 150 символов.
        Связка проверяется по началу: подозрительно именно то, что шквал набора
        начался сразу после возврата взгляда. Сам набор 150 символов на 30 мс
        занимает 4.5 с и иначе съедал бы всё пятисекундное окно правила.
        """
        mean = self._burst_sum_ms / max(self._burst_chars, 1)
        ratio = (mean / self._base.mean_ms) if self._baseline_ready() and self._base.mean_ms else None
        return {
            "kind": "typing_burst",
            "ts": ts,
            "anchor_ts": self._burst_start,
            "started_at": _hhmmss(self._burst_start),
            "question_id": self._burst_question or self._question,
            "chars": self._burst_chars,
            "mean_ms": round(mean, 1),
            "span_sec": round(max(ts - self._burst_start, 0.0), 1),
            "base_ratio": None if ratio is None else round(ratio, 2),
            "cutoff_ms": round(self._burst_cutoff_ms(), 1),
        }

    # ---------------------------------------------------------------- вставка
    def _on_paste(self, msg: dict, ts: float) -> list[ProctorEvent]:
        length = int(_f(msg.get("length"), 0.0))
        self._pastes += 1
        question = safe_label(msg.get("question_id")) or self._question
        out: list[ProctorEvent] = []

        # правило 4 (вторая половина): одиночная крупная вставка — это PASTE_BURST
        if length >= self.paste_burst_chars:
            detail = {
                "rule": "large_paste",
                "response": {
                    "kind": "paste", "ts": ts, "time": _hhmmss(ts),
                    "question_id": question, "length": length,
                    "source": str(msg.get("source") or "clipboard"),
                    # `origin` сюда не копируется. Поле не входит ни в один
                    # белый список, приходит из сети свободной строкой и
                    # уносило URL со текстом ответа в сообщение события, в
                    # payload и в хеш-цепочку. Откуда пришла вставка, видно
                    # по `source`; адрес источника — содержание работы, а не
                    # условие экзамена, и в журнале ему места нет.
                },
                "threshold_chars": self.paste_burst_chars,
            }
            message = (f"Вставка {length} символов из буфера обмена в вопрос "
                       f"{question or '—'} (порог {self.paste_burst_chars}) в {_hhmmss(ts)}")
            event = self._emit(EventKind.PASTE_BURST, ts, detail, message,
                               confidence=_clamp(0.7 + length / 4000.0, 0.7, 1.0))
            if event is not None:
                out.append(event)
                self._remember(self._paste_by_question, question, {
                    "kind": EventKind.PASTE_BURST.value, "ts": ts,
                    "detail": detail, "link_row": None,
                })

        if length < self.paste_min_chars:
            return out

        response = {
            "kind": "paste", "ts": ts, "time": _hhmmss(ts), "question_id": question,
            "length": length, "source": str(msg.get("source") or "clipboard"),
        }
        out.extend(self._correlate(response, ts))
        return out

    # ----------------------------------------------------------------- ответ
    def _on_answer(self, msg: dict, ts: float) -> list[ProctorEvent]:
        """Отправка ответа: правило 4 (вставка), связка (1-3), правило 5 (ритм).

        Порядок вызовов здесь — часть логики, а не стиль. Сначала связка,
        потом TYPING_ANOMALY: решение о подавлении ритма принимается ПО ФАКТУ
        выпущенной связки, а не по предположению, что она выпустится.

        Какие каналы наблюдения тут вообще есть:
        * правило 4 (PASTE_BURST) — счётчик символьных нажатий по вопросу
          против длины ответа. Это отдельный канал, независимый от тайминга;
        * связка (правила 1-3) — наблюдение камеры/оболочки (левая половина)
          плюс факт появления ответа, и для бурста ещё клавиатурный тайминг;
        * правило 5 (TYPING_ANOMALY) — только клавиатурный тайминг.
        Третий канал целиком лежит внутри второго, поэтому вместе они не
        предъявляются. Первый остаётся: он опирается на то, чего в двух
        других нет.
        """
        length = int(_f(msg.get("length"), 0.0))
        t2a = _f(msg.get("time_to_answer_ms"), 0.0)
        stats = msg.get("typing_stats") if isinstance(msg.get("typing_stats"), dict) else {}
        mean_ms = _f(stats.get("mean_ms"), 0.0)
        std_ms = _f(stats.get("std_ms"), 0.0)
        chars = int(_f(stats.get("chars"), float(length)))
        question = safe_label(msg.get("question_id")) or self._question
        out: list[ProctorEvent] = []

        response = {
            "kind": "answer_submit", "ts": ts, "time": _hhmmss(ts),
            "question_id": question, "length": length,
            "time_to_answer_ms": int(t2a),
            "typing_stats": {"mean_ms": round(mean_ms, 1), "std_ms": round(std_ms, 1),
                             "chars": chars},
        }

        # правило 4: ответ длиннее порога появился быстрее порога.
        #
        # `time_to_answer_ms` оболочка считает от ПОКАЗА вопроса, а `length` — это
        # полная длина поля, а не набранное за этот показ. Поэтому самого по себе
        # «400 символов за 2 с» недостаточно: оболочка шлёт answer_submit при любом
        # уходе с вопроса, и возврат к уже отвеченному вопросу с последующим
        # «Далее» через две секунды выглядел бы как вставка 200 симв./с.
        # Третье условие: набранного с клавиатуры по этому вопросу не хватает,
        # чтобы объяснить длину ответа (вставка символьных нажатий не даёт).
        #
        # ЭТО И ЕСТЬ НЕЗАВИСИМЫЙ КАНАЛ. `_typed_chars` считает СОБЫТИЯ нажатий
        # с key_class == "char" и инкрементируется в `_on_keystroke` до всех
        # проверок интервала: ни interval_ms, ни базовая линия, ни порог бурста
        # в него не входят (см. комментарий у самого счётчика). Поэтому условие
        # «нажатий меньше, чем нужно, чтобы набрать такой ответ» не выводится
        # из тайминга и не повторяет ни TYPING_ANOMALY, ни правую половину
        # связки. Студент, который действительно печатает быстро, даёт нажатия
        # на всю длину ответа — explained становится True и правило молчит;
        # именно это отличает проверку от наивного «быстро печатает — значит
        # списывает», за которое бьют и суды, и EU AI Act.
        typed = self._typed_chars.get(question, 0)
        explained = typed >= self.explained_ratio * length
        fast_long = (t2a > 0 and t2a < self.fast_answer_ms
                     and length > self.fast_answer_chars and not explained)
        # По этому вопросу PASTE_BURST мог быть уже выпущен на самой вставке.
        # Тогда «ответ появился быстрее, чем его можно набрать» — следствие той
        # же вставки, а не второе наблюдение. Раньше это держал только
        # глобальный cooldown по виду события, то есть побочный эффект
        # настройки, а не правило; теперь дедупликация явная и по вопросу.
        paste_held = self._claim(self._paste_by_question, question, ts) if fast_long else None
        if fast_long and paste_held is not None:
            self._suppress(paste_held, EventKind.PASTE_BURST,
                           "вставка по этому вопросу уже учтена как PASTE_BURST; "
                           "быстрый длинный ответ — её следствие, не второе "
                           "наблюдение", ts, question)
        elif fast_long:
            speed = length / max(t2a / 1000.0, 0.001)
            detail = {
                "rule": "fast_long_answer",
                "response": dict(response),
                "chars_per_sec": round(speed, 1),
                "typed_chars": typed,
                "thresholds": {"time_to_answer_ms": self.fast_answer_ms,
                               "length": self.fast_answer_chars,
                               "explained_ratio": self.explained_ratio},
            }
            message = (f"Ответ на {length} символов отправлен через "
                       f"{t2a / 1000.0:.1f} с после показа вопроса {question or '—'} — "
                       f"это {speed:.0f} символов в секунду, набрать вручную невозможно "
                       f"(с клавиатуры по этому вопросу зафиксировано {typed} нажатий)")
            event = self._emit(EventKind.PASTE_BURST, ts, detail, message, confidence=0.95)
            if event is not None:
                out.append(event)

        # связки (правила 1-3) — ДО правила 5, см. докстринг метода
        out.extend(self._correlate(response, ts))

        # правило 5: ритм набора не похож на личную базу.
        #
        # Подавляется ТОЛЬКО если живая связка действительно построена на тех
        # же межклавишных интервалах, то есть `_covers_typing()`. Сначала
        # проверяем, сработало ли правило вообще: подавлять и писать в журнал
        # нужно только то, что иначе стало бы событием, иначе «suppressed»
        # появится в каждой связке и перестанет что-то значить.
        z = self._typing_z(mean_ms, chars)
        if z is not None:
            link_held = self._claim(self._link_by_question, question, ts)
            if link_held is not None and self._covers_typing(link_held):
                self._suppress(link_held, EventKind.TYPING_ANOMALY,
                               f"ритм набора (z={z:.1f}) уже учтён внутри связки "
                               f"{link_held.get('kind') or '—'} как её правая половина: "
                               f"правая половина — бурст, отобранный по тем же "
                               f"межклавишным интервалам; второй раз одно "
                               f"наблюдение не начисляется",
                               ts, question)
            else:
                anomaly = self._typing_anomaly(ts, response, mean_ms, std_ms,
                                               chars, question, z)
                if anomaly is not None:
                    out.append(anomaly)
        return out

    def _covers_typing(self, link_held: dict[str, Any]) -> bool:
        """Содержит ли эта связка клавиатурный тайминг в своей правой половине.

        Отвечает на вопрос, от которого зависит, честно ли подавлять правило 5.
        Содержит ровно одна комбинация: `gaze_then_answer` + правая половина
        `typing_burst`. Там правая половина отобрана `_burst_ok_for_gaze()` по
        межклавишным интервалам, и предъявлять TYPING_ANOMALY рядом значило бы
        начислить одно наблюдение дважды.

        Во всех остальных случаях связка про тайминг НИЧЕГО не знает, и это
        видно прямо по `RULE_RESPONSES`:

        * `blur_then_answer` и `phone_then_answer` принимают `answer_submit`,
          а `_burst_ok_for_gaze()` для них не вызывается вовсе — их правая
          половина это «ответ появился в окне», и только;
        * любая связка с правой половиной `paste` тайминга не проверяет даже у
          gaze: `_burst_ok_for_gaze()` возвращает True первой же строкой.

        Раньше подавлялось всё подряд, и в журнал уходило обоснование «ритм
        уже учтён внутри связки» для связок, в которых ритма нет. Это хуже
        самого потерянного события: утверждение в журнале опровергается
        чтением `RULE_RESPONSES`, а на защите ловят именно на этом. Студент,
        который вышел из окна, вернулся и отправил ответ с ритмом в девять
        сигм от собственной базы, обязан получить оба наблюдения: они
        независимы, и каждое опирается на то, чего в другом нет.
        """
        detail = link_held.get("detail")
        if not isinstance(detail, dict):
            return False
        if str(detail.get("rule") or "") != "gaze_then_answer":
            return False
        response = detail.get("response")
        if not isinstance(response, dict):
            return False
        return str(response.get("kind") or "") == "typing_burst"

    # ------------------------------------------------- правило 5: TYPING_ANOMALY
    def _typing_z(self, mean_ms: float, chars: int) -> float | None:
        """Сработало ли правило 5, и с какой z-оценкой.

        Вынесено из `_typing_anomaly` отдельно, потому что вызывающему нужно
        знать ответ ДО решения о выпуске события: при живой связке по вопросу
        событие не выпускается, но сам факт срабатывания записывается в
        «suppressed». Условия — ровно те же, что были, и только здесь.
        """
        if not self._baseline_ready() or mean_ms <= 0 or chars < self.typing_min_chars:
            return None
        sigma = max(self._base.std_ms, 1.0)
        delta = mean_ms - self._base.mean_ms
        if delta >= 0:
            return None
        z = abs(delta) / sigma
        if z < self.typing_k:
            return None
        return z

    def _typing_anomaly(self, ts: float, response: dict, mean_ms: float,
                        std_ms: float, chars: int, question: str,
                        z: float | None = None) -> ProctorEvent | None:
        """Отклонение среднего интервала от базовой линии больше k робастных сигм.

        Сравнение идёт с median+MAD по первым N интервалам этой же сессии, то
        есть с самим студентом, а не с «средним человеком». Короткие ответы не
        проверяются: на десяти нажатиях любое среднее случайно.

        Считается ТОЛЬКО отклонение «быстрее базы», и вот почему. Оболочка
        присылает `typing_stats.mean_ms` — арифметическое среднее интервалов
        длиной до 5 с, а база — медиана интервалов до 2 с: пауза «задумался»
        попадает в среднее и не попадает в медиану. Поэтому среднее может быть
        больше базы без всякой подмены. Пример: 95 интервалов по 150 мс и пять
        пауз по 4 с дают mean_ms = 343 мс при базе 150 мс и MAD-сигме 30 мс,
        то есть z = 6.4 — «медленнее» сработало бы почти на каждом ответе
        честного студента. В обратную сторону артефакта нет: пауза среднее
        только поднимает, поэтому mean_ms НИЖЕ базы на k сигм паузами
        объяснить нельзя — это и есть признак чужого ритма или вставки.

        ВАЖНО: это правило стоит на одном наблюдении со связкой и выпускается
        только тогда, когда связки по вопросу нет (см. `_on_answer`). Это тот
        самый случай, который команда Duolingo English Test нашла у себя:
        признак ритма набора подтверждал сам себя. Отдельно он имеет право
        звучать, вместе со связкой — нет.
        """
        if z is None:
            z = self._typing_z(mean_ms, chars)
        if z is None:
            return None
        delta = mean_ms - self._base.mean_ms
        faster = True
        detail = {
            "rule": "typing_anomaly",
            "response": dict(response),
            "baseline": self._base.as_dict(True),
            "z": round(z, 2),
            "k": self.typing_k,
            "direction": "faster" if faster else "slower",
            "delta_ms": round(delta, 1),
        }
        message = (
            f"Ритм набора в вопросе {question or '—'} не похож на собственный: "
            f"средний интервал {mean_ms:.0f} мс против личной базы "
            f"{self._base.mean_ms:.0f} мс "
            f"({'быстрее' if faster else 'медленнее'} на "
            f"{abs(delta):.0f} мс, {z:.1f} робастных сигм при пороге {self.typing_k:.0f}; "
            f"MAD-сигма {self._base.std_ms:.0f} мс, {chars} символов)")
        return self._emit(EventKind.TYPING_ANOMALY, ts, detail, message,
                          confidence=_clamp(0.5 + 0.1 * z, 0.5, 1.0))

    # ------------------------------------- дедупликация по одному наблюдению
    def _claim(self, store: dict[str, dict[str, Any]], question: str,
               ts: float) -> dict[str, Any] | None:
        """След события, уже «оплатившего» наблюдение по этому вопросу.

        Возвращает запись, если по `question` событие из этого хранилища
        выпущено не раньше, чем `dedup_sec` секунд назад. Иначе None: либо
        по вопросу ещё ничего не было, либо прошлый эпизод уже закрыт и новое
        наблюдение имеет право на собственный вклад в risk.

        Без question_id метка берётся общая (`_ANON_QUESTION`), и дедупликация
        вырождается во временное окно. Раньше здесь был ранний выход, и это
        был выбор «падать открыто»: банк вопросов без id (оболочка отдаёт в
        таком случае пустую строку) снимал дедупликацию целиком, вместе с
        потолком наблюдения в `engine/risk.py`, и один эпизод давал полные 100
        с автоблокировкой — ровно то, против чего писались все эти правила.
        Окно по времени — оценка грубая, но в правильную сторону: двум
        событиям в пределах `dedup_sec` без признака вопроса честнее быть
        одним наблюдением, чем тремя независимыми обвинениями.
        """
        question = question or _ANON_QUESTION
        if self.dedup_sec <= 0:
            return None
        rec = store.get(question)
        if rec is None:
            return None
        if ts - _f(rec.get("ts"), 0.0) > self.dedup_sec + _EPS:
            return None
        return rec

    def _remember(self, store: dict[str, dict[str, Any]], question: str,
                  rec: dict[str, Any]) -> None:
        """Запомнить след события по вопросу (с защитой от роста словаря).

        Пустая метка кладётся под `_ANON_QUESTION`, иначе `_claim()` искал бы
        след, которого никто не оставил, и дедупликация без id не работала бы
        даже как временное окно.
        """
        question = question or _ANON_QUESTION
        if len(store) > 256 and question not in store:
            store.clear()
        store[question] = rec

    def _suppress(self, rec: dict[str, Any], kind: EventKind, reason: str,
                  ts: float, question: str) -> None:
        """Записать подавление: в detail победившего события и в журнал.

        Факт подавления обязан быть виден. Запись идёт в три места, потому что
        ни одного из них по отдельности не хватает:
        * detail["suppressed"] того события, которое забрало наблюдение себе —
          это читает отчёт, но событие может быть уже отправлено и записано,
          и тогда правка словаря до него не доедет;
        * строка в self._links — живёт в stats() до конца сессии;
        * self._suppressions — общий журнал, по нему видно и само подавление,
          и чем оно вызвано, даже если связка выпущена на другом сообщении.
        """
        entry = {"kind": kind.value, "reason": reason}
        detail = rec.get("detail")
        if isinstance(detail, dict):
            lst = detail.setdefault("suppressed", [])
            if isinstance(lst, list) and not any(
                    isinstance(x, dict) and x.get("kind") == kind.value for x in lst):
                lst.append(dict(entry))
        link_row = rec.get("link_row")
        if isinstance(link_row, dict):
            row_lst = link_row.setdefault("suppressed", [])
            if isinstance(row_lst, list) and kind.value not in row_lst:
                row_lst.append(kind.value)
        self._suppressions.append({
            **entry,
            "ts": round(ts, 3),
            "time": _hhmmss(ts),
            "question_id": question,
            "by": str(rec.get("kind") or ""),
        })
        if len(self._suppressions) > 200:
            del self._suppressions[:-200]

    # --------------------------------------------------- правила 1-3: связки
    def _correlate(self, response: dict[str, Any], ts: float) -> list[ProctorEvent]:
        """Найти триггер, объясняющий этот «ответ», и выпустить связку.

        Среди всех подходящих правил берём самое весомое (RISK_WEIGHTS), чтобы
        один ответ не дал три события и не утроил risk-score. Остальные
        совпадения попадают в detail["also"] — в отчёте они видны.

        Второй уровень дедупликации — между вызовами. Один эпизод приходит
        двумя сообщениями телеметрии: paste, а через секунду answer_submit по
        тому же вопросу. Матчинг внутри одного вызова их не связывает, и до
        правки эпизод давал две связки (FUSION_GAZE на вставке и
        FUSION_BLUR на отправке) — 110 веса на одно наблюдение. Поэтому если
        связка по вопросу уже выпущена и ещё жива, вторая не выпускается:
        её правило уходит в "suppressed" первой.
        """
        kind_name = str(response.get("kind") or "")
        # момент, по которому меряем связку: для бурста — его начало
        anchor = _f(response.get("anchor_ts"), ts)
        matches: list[tuple[float, EventKind, str, _Trigger, float, float]] = []

        for rule, kinds, window, event_kind in (
            ("gaze_then_answer", GAZE_TRIGGERS, self.gaze_window,
             EventKind.FUSION_GAZE_THEN_ANSWER),
            ("blur_then_answer", BLUR_TRIGGERS, self.blur_window,
             EventKind.FUSION_BLUR_THEN_ANSWER),
            ("phone_then_answer", PHONE_TRIGGERS, self.phone_window,
             EventKind.FUSION_PHONE_THEN_ANSWER),
        ):
            if kind_name not in RULE_RESPONSES[rule]:
                continue
            if rule == "gaze_then_answer" and not self._burst_ok_for_gaze(response):
                continue
            trig = self._find_trigger(kinds, window, anchor,
                                      min_duration=self.gaze_min_sec if rule == "gaze_then_answer" else 0.0)
            if trig is None:
                continue
            gap = anchor - trig.end_ts
            matches.append((RISK_WEIGHTS.get(event_kind, 10.0), event_kind, rule,
                            trig, gap, window))

        if not matches:
            return []
        matches.sort(key=lambda m: (-m[0], m[4]))
        weight, event_kind, rule, trig, gap, window = matches[0]

        question = str(response.get("question_id") or "")
        held = self._claim(self._link_by_question, question, ts)
        if held is not None:
            # Наблюдение по этому вопросу уже оплачено живой связкой. Триггер
            # НЕ помечаем использованным: он не был предъявлен как инцидент и
            # может ещё объяснить другой эпизод.
            self._suppress(held, event_kind,
                           f"связка по вопросу уже выпущена "
                           f"({held.get('kind') or '—'}), правило {rule} описывает "
                           f"то же наблюдение", ts, question)
            return []

        also = [{"kind": m[3].kind.value, "rule": m[2],
                 "gap_sec": round(max(m[4], 0.0), 1),
                 "duration": round(m[3].duration, 2)} for m in matches[1:]]

        trig.used = True
        detail = {
            "rule": rule,
            "window_sec": window,
            "gap_sec": round(gap, 2),
            "trigger": {
                "kind": trig.kind.value,
                "ts": round(trig.ts, 3),
                "time": _hhmmss(trig.ts),
                "started_at": _hhmmss(trig.start_ts),
                "duration": round(trig.duration, 2),
                "confidence": round(trig.confidence, 2),
                "zone": trig.detail.get("zone", trig.detail.get("gaze_zone", "")),
                "message": trig.message,
                # Ссылка на событие-триггер и его кадр: связка фиксируется в
                # момент ответа, а смотреть нужно на момент сигнала. Кадр
                # сайдкар приложил к тому же объекту события до записи в
                # цепочку, поэтому к моменту связки путь уже известен.
                "event_id": str(getattr(trig.event, "id", "") or ""),
                "frame_path": _trigger_frame_path(trig.event),
            },
            "response": dict(response),
            "baseline": self._base.as_dict(self._baseline_ready()),
        }
        if also:
            detail["also"] = also

        message = self._link_message(rule, trig, response, gap, window)
        confidence = self._link_confidence(trig, response, gap, window)
        event = self._emit(event_kind, ts, detail, message, confidence=confidence)
        if event is None:
            return []
        link_row: dict[str, Any] = {
            "kind": event_kind.value, "ts": ts, "rule": rule,
            "trigger": trig.kind.value, "gap_sec": round(gap, 2),
            "question_id": question,
        }
        self._links.append(link_row)
        if len(self._links) > 200:
            del self._links[:-200]
        self._remember(self._link_by_question, question, {
            "kind": event_kind.value, "ts": ts, "detail": detail,
            "link_row": link_row,
        })
        return [event]

    def _burst_ok_for_gaze(self, response: dict[str, Any]) -> bool:
        """Правило 1 принимает бурст только с нужной длиной и скоростью.

        Честно о границах этой проверки. При готовой базовой линии условие
        `base_ratio < burst_ratio` выполняется ВСЕГДА: бурст набирается из
        интервалов ниже `_burst_cutoff_ms()`, а этот порог и есть
        `base.mean_ms * burst_ratio`, поэтому средний интервал бурста заведомо
        ниже порога. То есть проверка скорости здесь не добавляет независимого
        наблюдения — она тавтологична по построению бурста; реально отбирает
        правую половину условие длины (>= fusion_burst_chars), а скорость
        работает только пока базы нет (абсолютный порог).

        Отсюда и подавление TYPING_ANOMALY при выпущенной связке: правая
        половина связки построена на тех же межклавишных интервалах, и считать
        их вторым обвинением нельзя. Выдавать это за два независимых канала —
        ровно та ошибка, на которой ловят на защите.
        """
        if response.get("kind") != "typing_burst":
            return True
        if int(_f(response.get("chars"), 0.0)) < self.burst_chars:
            return False
        ratio = response.get("base_ratio")
        if ratio is None:
            # базовой линии нет — судим по абсолютному порогу
            return _f(response.get("mean_ms"), 1e9) < self.burst_abs_ms
        return _f(ratio, 1.0) < self.burst_ratio

    def _find_trigger(self, kinds: tuple[EventKind, ...], window: float, ts: float,
                      min_duration: float = 0.0) -> _Trigger | None:
        """Ближайший подходящий неиспользованный триггер нужного вида.

        «Подходящий» = ответ случился после начала условия и не позже, чем
        через `window` секунд после его окончания. Отрицательный разрыв
        (ответ ещё во время условия) допускается специально: набирать текст,
        не глядя на экран, — ровно тот случай, который мы ищем.
        """
        best: _Trigger | None = None
        best_gap = 1e9
        for trig in self._triggers:
            if trig.used or trig.kind not in kinds:
                continue
            if min_duration and trig.duration + _EPS < min_duration:
                continue
            if ts + _EPS < trig.start_ts:
                continue
            gap = ts - trig.end_ts
            if gap > window + _EPS:
                continue
            if abs(gap) < abs(best_gap):
                best, best_gap = trig, gap
        return best

    def _link_message(self, rule: str, trig: _Trigger, response: dict[str, Any],
                      gap: float, window: float) -> str:
        """Человекочитаемое описание связки: обе половины и тайминги."""
        left = TRIGGER_RU.get(trig.kind, trig.kind.value)
        dur = trig.duration
        question = response.get("question_id") or "—"
        when = "через" if gap >= 0 else "ещё за"
        gap_txt = f"{abs(gap):.1f} с"

        if trig.kind in BLUR_TRIGGERS:
            left_txt = f"{left} в {_hhmmss(trig.ts)}"
        else:
            left_txt = f"{left} {dur:.1f} с (с {_hhmmss(trig.start_ts)})"

        kind_name = response.get("kind")
        if kind_name == "paste":
            right_txt = (f"вставка {int(_f(response.get('length'), 0.0))} символов "
                         f"в вопрос {question}")
        elif kind_name == "answer_submit":
            t2a = _f(response.get("time_to_answer_ms"), 0.0) / 1000.0
            right_txt = (f"отправлен ответ на {int(_f(response.get('length'), 0.0))} "
                         f"символов (вопрос {question}, набран за {t2a:.1f} с)")
        else:
            chars = int(_f(response.get("chars"), 0.0))
            mean = _f(response.get("mean_ms"), 0.0)
            ratio = response.get("base_ratio")
            tail = ""
            if ratio is not None:
                tail = (f" — {_f(ratio, 0.0) * 100:.0f}% от личной базы "
                        f"{self._base.mean_ms:.0f} мс")
            right_txt = (f"набрано {chars} символов со средним интервалом "
                         f"{mean:.0f} мс{tail} (вопрос {question})")

        if gap >= 0:
            middle = f"{when} {gap_txt} после этого"
        else:
            middle = f"{when} {gap_txt} до конца этого"
        return (f"{left_txt.capitalize()}; {middle} — {right_txt}. "
                f"Окно связки {window:.0f} с.")

    def _link_confidence(self, trig: _Trigger, response: dict[str, Any],
                         gap: float, window: float) -> float:
        """Уверенность связки: чем теснее тайминг и длиннее условие, тем выше.

        База 0.6; плотный разрыв (ответ сразу) добавляет до 0.2; уверенность
        самого триггера даёт ещё до 0.2. Связка на границе окна остаётся
        инцидентом, но весит заметно меньше очевидной.
        """
        tightness = 1.0 - _clamp(max(gap, 0.0) / max(window, _EPS), 0.0, 1.0)
        conf = 0.6 + 0.2 * tightness + 0.2 * _clamp(trig.confidence, 0.0, 1.0)
        if response.get("kind") == "typing_burst":
            ratio = response.get("base_ratio")
            if ratio is not None and _f(ratio, 1.0) < self.burst_ratio / 2.0:
                conf += 0.05
        return round(_clamp(conf, 0.3, 1.0), 3)

    # ------------------------------------------------------- базовая линия
    def _recompute_baseline(self) -> None:
        values = self._base.samples
        median = _median(values)
        mad = _median([abs(v - median) for v in values])
        sigma = 1.4826 * mad
        # пол сигмы: у очень ровного набора MAD вырождается в нуль, и тогда
        # любое отклонение даёт бесконечную z-оценку
        self._base.mean_ms = median
        self._base.std_ms = max(sigma, 0.08 * median, 5.0)

    def _freeze_baseline(self) -> None:
        self._recompute_baseline()
        self._base.frozen = True

    def _baseline_ready(self) -> bool:
        return len(self._base.samples) >= self.baseline_min and self._base.mean_ms > 0

    def baseline(self) -> dict[str, Any]:
        """Профиль набора студента — его показывает отчёт."""
        return self._base.as_dict(self._baseline_ready())

    # ------------------------------------------------------------- выпуск
    def _emit(self, kind: EventKind, ts: float, detail: dict[str, Any],
              message: str, confidence: float = 1.0) -> ProctorEvent | None:
        """Собрать событие с учётом cooldown по виду."""
        last = self._last_fire.get(kind.value, 0.0)
        if last and (ts - last) + _EPS < self.cooldown:
            return None
        self._last_fire[kind.value] = ts
        self._fired += 1
        weight = RISK_WEIGHTS.get(kind, 10.0)
        return ProctorEvent(
            kind=kind,
            severity=severity_for_weight(weight),
            channel=Channel.FUSION,
            confidence=_clamp(confidence, 0.05, 1.0),
            ts=ts,
            duration=0.0,
            message=message,
            detail=detail,
        )

    # ------------------------------------------------------------ диагностика
    def stats(self) -> dict[str, Any]:
        """Состояние движка — для status/отчёта и отладки демо."""
        return {
            "baseline": self.baseline(),
            "triggers_in_window": len(self._triggers),
            "keystrokes": self._keystrokes,
            "pastes": self._pastes,
            "fired": self._fired,
            "links": list(self._links[-10:]),
            # Что было подавлено дедупликацией: без этого списка подавление
            # выглядит как пропуск детектора, а отчёт — как неполный.
            "suppressed": list(self._suppressions[-10:]),
            "suppressed_total": len(self._suppressions),
            "dedup_sec": self.dedup_sec,
            "burst": {"chars": self._burst_chars,
                      "cutoff_ms": round(self._burst_cutoff_ms(), 1)},
            "question": self._question,
        }


# ===========================================================================
# помощники
# ===========================================================================
def _f(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _ts(value: Any) -> float:
    """Время из телеметрии: секунды unix. Миллисекунды распознаём и делим."""
    ts = _f(value, 0.0)
    if ts > 1e11:        # похоже на Date.now() в миллисекундах
        ts /= 1000.0
    if ts < 1e6:         # нуля или performance.now() быть не должно
        return time.time()
    return ts


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _hhmmss(ts: float) -> str:
    """Локальное время без даты — так читается в отчёте."""
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except (ValueError, OSError, OverflowError):
        return "--:--:--"


def _trigger_frame_path(event: Any) -> str:
    """Путь кадра события-триггера (относительно каталога сессии) или ""."""
    evidence = getattr(event, "evidence", None)
    if isinstance(evidence, dict):
        return str(evidence.get("frame_path") or "")
    return str(getattr(evidence, "frame_path", "") or "")


__all__ = ["FusionEngine", "DEFAULTS", "GAZE_TRIGGERS", "BLUR_TRIGGERS",
           "PHONE_TRIGGERS", "severity_for_weight"]

"""
Risk-score: накопление, затухание и объяснимый разбор.

Три решения, без которых шкала нечестная.

1. **Вклад = вес * уверенность.** Веса берутся ТОЛЬКО из
   `protocol.RISK_WEIGHTS` — единственное место, где их можно менять. Слабое
   срабатывание детектора (уверенность 0.6) не должно весить как очевидное.

2. **Экспоненциальное затухание по half-life** (поставляемый конфиг — 90 с, ключ
   `risk_half_life`). Без спада одна случайная ошибка на второй минуте теста
   навсегда клеймит студента: счёт только растёт, и к концу экзамена любой
   выходит нарушителем. Со спадом счёт отражает ТЕКУЩУЮ картину: единичный
   инцидент тает, систематическое поведение — накапливается быстрее, чем тает.

3. **Разложение вместо числа.** `breakdown()` возвращает вклад по видам
   событий с количеством и суммой. HUD и отчёт показывают не «риск 64», а
   «телефон в кадре ×2 +41, взгляд вниз ×3 +18» — преподаватель видит,
   из чего сложилась цифра, и может поспорить с конкретным пунктом.

4. **Потолок вклада на одно наблюдаемое событие** (`risk_event_cap`, по
   умолчанию `RISK_LOCK - 5`). Сложение вкладов честно только пока слагаемые —
   разные наблюдения. Один отправленный ответ умел дать PASTE_BURST (30) +
   TYPING_ANOMALY (15) + FUSION_BLUR_THEN_ANSWER (55) = 100 и автоблокировку
   с одного события; дедупликация в `engine/fusion.py` убрала повтор правил,
   а этот потолок — последний предохранитель на стороне счёта: ни при какой
   комбинации правил одного наблюдения счёт не дотягивается до порога LOCK.
   Решение «закрыть сессию» требует либо второго независимого наблюдения,
   либо устойчивого поведения — так и написано в позиционировании: решение
   принимает человек.

5. **Лестницы наблюдения** (`protocol.ESCALATION_LADDERS`). Потолок из п. 4
   группирует события по метке вопроса, и для событий камеры он не работает:
   у них нет ни `question_id`, ни ответа, к которому их отнести. Поэтому один
   телефон, попавший в кадр и затем поднятый и наведённый на экран, давал
   PHONE_IN_FRAME (25) + PHONE_RAISED (35) + PHONE_AIMED_AT_SCREEN (45) = 105
   и доводил счёт до порога блокировки с ОДНОГО наблюдаемого объекта. Это не
   три обвинения, а три показания об одном и том же, каждое точнее
   предыдущего. Виды, объявленные ступенями одной лестницы, дают за прогон
   вклад ТОЛЬКО высшей достигнутой ступени: 25 -> 35 -> 45. Эскалация
   по-прежнему повышает тревогу (старшая ступень весит больше младшей) и
   никогда её не снижает — уже начисленное не отзывается, просто новое не
   добавляется. Прогон лестницы размечает `engine/events.py`
   (`detail["escalation"]["run"]`); если разметки нет — события пришли мимо
   движка, например от мок-сайдкара, — счёт группирует ступени сам, по
   таблице из `protocol.py` и скользящему окну. Счёт не обязан верить движку
   правил на слово, это тот же принцип, что и у потолка в п. 4.

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
    ESCALATION_LADDERS,
    LADDER_LABELS,
    RISK_LOCK,
    RISK_PAUSE,
    RISK_WARN,
    RISK_WEIGHTS,
    EventKind,
    ProctorEvent,
    VerdictAction,
    ladder_gap,
    ladder_merge_subject,
    ladder_of,
    ladder_same_subject,
    ladder_subject,
    ladder_top_weight,
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

#: Потолок суммарного вклада ОДНОГО наблюдаемого события (ключ `risk_event_cap`).
#:
#: Пять пунктов ниже порога LOCK: автоматическая блокировка — единственное
#: необратимое действие системы, и она не имеет права стоять на одном
#: наблюдении. PAUSE (60) с одного события остаётся: это призыв к человеку,
#: а не приговор.
_EVENT_CAP_DEFAULT = RISK_LOCK - 5.0

#: Сколько секунд события считаются одним наблюдением (ключ `risk_event_window`).
#:
#: Два события по одному вопросу, разделённые большим интервалом, — это два
#: эпизода, и складываться они обязаны. Значение согласовано с окнами fusion:
#: самое широкое окно связки — 10 с (телефон), дедупликация связок и cooldown
#: по виду события — 20 с. Короче 20 с окно делать нельзя: тогда правила
#: одного эпизода разъехались бы по разным наблюдениям и потолок перестал
#: бы их ограничивать.
_EVENT_WINDOW_DEFAULT = 20.0

#: Самый тяжёлый одиночный вес. Потолок не может быть ниже: иначе он молча
#: переоценивал бы одно правило (например VIRTUAL_CAMERA, 70) вместо того,
#: чтобы запрещать суммирование повторов одного наблюдения.
_MAX_WEIGHT = max(RISK_WEIGHTS.values(), default=0.0)

#: Префикс ключа прогона лестницы, когда счёт группирует ступени сам (события
#: пришли мимо движка). Тот же вид, что и у `engine/events.py`: «/», «:» и «#»
#: в метку вопроса не проходят (`protocol.safe_label`), поэтому с ключами
#: наблюдений по вопросу такие ключи не столкнутся.
_LADDER_PREFIX = "ladder/"

#: Малая поправка на сравнение float'ов «на грани» — как в engine/events.py.
_EPS = 1e-6


@dataclass
class _Item:
    """Один учтённый инцидент."""
    ts: float
    kind: EventKind
    base: float          # вес * уверенность, УЖЕ с учётом потолка наблюдения
    event_id: str
    confidence: float
    message: str
    raw_base: float = 0.0    # сколько дал бы вес * уверенность без потолка
    obs_key: str = ""        # наблюдение, к которому отнесён вклад ("" — своё)
    ladder_key: str = ""     # прогон лестницы наблюдения ("" — вид сам по себе)
    folded: bool = False     # вклад уже приписан к наблюдению связки
    #: Вид, который уточнил эту ступень и забрал её вклад (см.
    #: `_ladder_reattribute`). База такого вклада — 0, но запись остаётся: в
    #: разборе ступень обязана быть видна с пометкой «уточнено», а не исчезать.
    refined_by: str = ""


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
        # 180 — страховка на случай, когда ключа в конфиге нет вовсе. Рабочее
        # значение задаёт `sidecar/config.py` (90 с), и именно его называют
        # README и docs/LIMITATIONS.md: расхождение жило только в докстринге,
        # а читают его первым.
        self.half_life = max(_as_float(cfg.get("risk_half_life"), 180.0), 1.0)
        self.max_score = max(_as_float(cfg.get("risk_max"), 100.0), 1.0)
        self.warn = _as_float(cfg.get("risk_warn"), RISK_WARN)
        self.pause = _as_float(cfg.get("risk_pause"), RISK_PAUSE)
        self.lock = _as_float(cfg.get("risk_lock"), RISK_LOCK)
        #: Минимальная уверенность: событие с conf=0 всё равно что-то значит.
        self.min_confidence = _as_float(cfg.get("risk_min_confidence"), 0.2)
        #: Сколько держать уровень перед понижением на одну ступень.
        self.level_hold = max(_as_float(cfg.get("risk_level_hold"), 10.0), 0.0)
        #: Потолок вклада одного наблюдения. Ниже самого тяжёлого одиночного
        #: веса не опускаем даже по конфигу — см. _MAX_WEIGHT.
        self.event_cap = max(_as_float(cfg.get("risk_event_cap"), _EVENT_CAP_DEFAULT),
                             _MAX_WEIGHT)
        self.event_window = max(_as_float(cfg.get("risk_event_window"),
                                          _EVENT_WINDOW_DEFAULT), 0.0)

        self._items: list[_Item] = []
        self._totals: dict[EventKind, dict[str, Any]] = {}
        #: Скользящее окно для событий ввода без метки вопроса — см.
        #: `_anon_obs_key()`. Без него пустой question_id снимал потолок.
        self._anon_key: str = ""
        self._anon_last_ts: float | None = None
        self._anon_seq: int = 0
        #: Наблюдение -> {"first_ts", "last_ts", "base"}: сколько уже начислено.
        self._observations: dict[str, dict[str, float]] = {}
        #: Сколько веса отрезал потолок за сессию — это видно в отчёте.
        self._capped = 0.0
        #: Прогоны лестниц, когда их приходится опознавать самому (нет разметки
        #: движка): лестница -> {"key", "subject", "last_ts"}.
        self._ladder_runs: dict[str, dict[str, Any]] = {}
        self._ladder_seq: dict[str, int] = {}
        #: Все прогоны, которые счёт ВИДЕЛ, по лестницам: ключ прогона ->
        #: лестница. `_ladder_seq` считает только прогоны, опознанные здесь
        #: самостоятельно, поэтому на сессии с живым движком он оставался
        #: пустым — и `escalation_report()` печатал несходящуюся пару
        #: «свёрнуто 1422, прогонов 0» ровно в том единственном месте, которое
        #: обязано объяснять свёртку. Считаем по ключам: откуда пришёл ключ, от
        #: движка или из опознания, для числа прогонов неважно.
        self._ladder_runs_seen: dict[str, str] = {}
        #: Сколько веса не начислено, потому что это была младшая ступень уже
        #: достигнутого наблюдения. Молча сворачивать нельзя — отчёт обязан
        #: показать, что счёт ограничен правилом, а не что детектор промолчал.
        self._ladder_folded = 0.0
        self._ladder_folded_kinds: dict[str, int] = {}
        self._now = self._clock()
        self._action = VerdictAction.NONE
        self._action_changed = self._now
        self._peak = 0.0

    # ----------------------------------------------------------------- сброс
    def reset(self) -> None:
        """Обнулить счёт (новая сессия или команда reset_risk)."""
        self._items.clear()
        self._totals.clear()
        self._observations.clear()
        self._anon_key = ""
        self._anon_last_ts = None
        self._anon_seq = 0
        self._capped = 0.0
        self._ladder_runs.clear()
        self._ladder_seq.clear()
        self._ladder_runs_seen.clear()
        self._ladder_folded = 0.0
        self._ladder_folded_kinds.clear()
        self._now = self._clock()
        self._action = VerdictAction.NONE
        self._action_changed = self._now
        self._peak = 0.0

    def available(self) -> bool:
        return True

    # ------------------------------------------------------------ накопление
    def add(self, event: ProctorEvent) -> None:
        """Учесть инцидент: score += weight * confidence, но не выше потолка.

        Вес — только из RISK_WEIGHTS. Служебные события (вес 0) не копятся,
        но попадают в пожизненные счётчики: отчёту полезно знать, что они были.

        Потолок наблюдения (`risk_event_cap`). События, описывающие ОДИН
        наблюдаемый эпизод (один и тот же question_id в пределах
        `risk_event_window` секунд), складываются не дальше потолка. Правила
        уже дедуплицированы в `engine/fusion.py`, но счёт не обязан верить
        движку правил на слово: даже если завтра появится четвёртое правило
        по тому же ответу, «сто из одного события» не получится. Срезанный
        вес не исчезает бесследно — он виден в to_dict()["event_cap"].
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

        raw_base = weight * confidence
        if raw_base <= 0:
            return
        obs_key = _observation_key(event)
        if not obs_key and _is_input_event(event):
            # Событие канала ввода без метки вопроса. Раньше здесь не было
            # ничего, и потолок наблюдения просто не применялся: пустой
            # question_id снимал и дедупликацию в fusion, и группировку здесь,
            # и один эпизод (уход из окна -> вставка -> быстрый ответ) давал
            # 100 и автоблокировку вместо 85 и приостановки. Падать открыто
            # на отсутствии метки нельзя: метка приходит из банка вопросов,
            # то есть из внешних данных, и её отсутствие — не признак
            # независимости наблюдений. Откатываемся на временное окно.
            obs_key = self._anon_obs_key(ts)
        if obs_key:
            self._fold_trigger(obs_key, event, ts)

        base = raw_base
        # Лестница наблюдения (п. 5): за прогон начисляется вклад только
        # высшей достигнутой ступени, поэтому потолок этой группы — вклад
        # самой ступени, а не общий `event_cap`. Младшая ступень после старшей
        # не добавляет ничего, старшая после младшей — ровно разницу.
        ladder_key = self._ladder_obs_key(event, kind, ts)
        if ladder_key:
            spec = ladder_of(kind)
            self._ladder_runs_seen.setdefault(
                ladder_key, spec[0] if spec else "")
            base = self._fit_to_cap(ladder_key, base, ts, cap=raw_base,
                                    ladder=kind)
        if obs_key:
            base = self._fit_to_cap(obs_key, base, ts)
        if base <= 0:
            # Потолок наблюдения или лестница исчерпаны: событие уже учтено в
            # счётчиках и в отчёте, но повторного вклада в risk не даёт.
            return
        item = _Item(
            ts=ts,
            kind=kind,
            base=base,
            event_id=str(getattr(event, "id", "")),
            confidence=confidence,
            message=str(getattr(event, "message", "") or ""),
            raw_base=raw_base,
            obs_key=obs_key,
            ladder_key=ladder_key,
        )
        self._items.append(item)
        if ladder_key:
            self._ladder_reattribute(ladder_key, item)
        self._peak = max(self._peak, self.score)

    def _ladder_reattribute(self, ladder_key: str, top: _Item) -> None:
        """Весь вклад прогона лестницы — ДОСТИГНУТОЙ ступени, а не младшим.

        Арифметика свёртки считалась верно, а распределение между ступенями
        было вывернуто. `_fit_to_cap` кладёт в `_Item.base` ПРИРАЩЕНИЕ, поэтому
        на главном сценарии (телефон: в кадре 25 -> поднят 35 -> наведён 45,
        всё за 0.7 с) вклады раскладывались так:

            PHONE_IN_FRAME         19.89  (55.5% счёта)
            PHONE_AIMED_AT_SCREEN   8.00  (22.3%)
            PHONE_RAISED            7.98  (22.3%)

        и `top_reason()` называл главной причиной тревоги «телефон в кадре
        (+20)» — САМОЕ СЛАБОЕ показание эпизода, — а основание, «наведён на
        экран», выглядело мелкой добавкой. Эту же фразу печатали HUD, summary
        и вердикт, тогда как кольцо в отчёте (`compute_integrity`) ставило
        первым PHONE_AIMED_AT_SCREEN: студент на экране и комиссия в отчёте
        читали про один эпизод разные главные причины.

        Смысл лестницы в том, что старшая ступень ПЕРЕПРОВЕРЯЕТ младшую и
        описывает то же наблюдение строже. Значит и вклад принадлежит ей, а
        младшие ступени обязаны показывать 0 с пометкой «уточнено» — что они и
        делают: запись остаётся в `_items` (её видно в разборе и в журнале), но
        с нулевой базой.

        Сумма не меняется: переносится ровно то, что уже было начислено, —
        поэтому ни потолок наблюдения, ни потолок лестницы этим не обходятся.
        Меняется точка отсчёта затухания: вес эпизода стоит в момент, когда
        эпизод дошёл до своей высшей ступени, а не когда начался.
        """
        moved = 0.0
        folded = False
        for item in self._items:
            if item is top or item.ladder_key != ladder_key or item.base <= 0:
                continue
            moved += item.base
            item.base = 0.0
            item.refined_by = top.kind.value
            folded = folded or item.folded
        if moved > 0:
            top.base += moved
            # Связка уже приписала вклад прогона наблюдению по вопросу —
            # признак обязан переехать вместе с вкладом, иначе `_fold_trigger`
            # посчитает этот вклад ещё раз.
            top.folded = top.folded or folded

    def _group(self, obs_key: str, ts: float,
               window: float | None = None) -> dict[str, float]:
        """Группа наблюдения: та же, если жива, иначе новая с этого момента.

        `window` — сколько группа живёт с ПЕРВОГО вклада. По умолчанию
        `event_window`. Для прогона лестницы передаётся `math.inf`: конец
        прогона определяет тот, кто выдал ключ (движок — по снятию условия,
        счёт — по разрыву между ступенями), и ограничивать группу ещё и
        временем значило бы на длинном эпизоде начислить лестницу второй раз.
        """
        limit = self.event_window if window is None else window
        group = self._observations.get(obs_key)
        if group is not None and (ts - group["first_ts"]) <= limit:
            group["last_ts"] = max(group.get("last_ts", ts), ts)
            return group
        if len(self._observations) > 256:
            horizon = ts - max(self.event_window, self.half_life)
            self._observations = {k: g for k, g in self._observations.items()
                                  if g.get("last_ts", g["first_ts"]) >= horizon}
        group = self._observations[obs_key] = {"first_ts": ts, "last_ts": ts,
                                               "base": 0.0}
        return group

    def _ladder_obs_key(self, event: ProctorEvent, kind: EventKind,
                        ts: float) -> str:
        """Ключ прогона лестницы наблюдения ("" — вид не стоит на лестнице).

        Разметку делает `engine/events.py`: там есть состояния условий, и он
        знает, когда эпизод закончился. Если разметки нет (события пришли мимо
        движка — мок-сайдкар, воспроизведение записи, чужой источник), прогон
        опознаётся здесь по таблице `protocol.ESCALATION_LADDERS` и разрыву
        между ступенями. Это тот же принцип, что у потолка наблюдения: счёт не
        обязан верить движку правил на слово и обязан работать без него.

        ЧЕЙ `escalation.run` МЫ ЧИТАЕМ. Разметка — служебная связь между
        событиями, и принимать её можно только у вида, который на лестнице
        действительно стоит. Раньше `detail["escalation"]["run"]` читался ДО
        проверки `ladder_of(kind)`, а `_ladder_annotate` перезаписывает
        `escalation` только у лестничных видов — поэтому на любом НЕлестничном
        виде присланный ключ доезжал до счёта как есть. Группа получала потолок
        `cap=raw_base` текущего события, то есть все обвинения сессии
        сворачивались в один самый тяжёлый вес: последовательность, которую
        реально шлёт оболочка (SHORTCUT_BLOCKED, WINDOW_BLUR, FULLSCREEN_EXIT,
        DEVTOOLS_ATTEMPT...), превращалась из `pause` в `none` одним
        добавленным ключом. Теперь:

        1. вид не на лестнице — разметка не читается вообще;
        2. ключ обязан быть строкой формата `ladder/<лестница>/<субъект>#<n>`
           ИМЕННО ТОЙ лестницы, на которой стоит этот вид. Чужую лестницу или
           произвольную строку счёт опознаёт сам по ESCALATION_LADDERS.

        Поля `detail`, которыми владеет движок, вдобавок срезаются на границе
        сокета (`main._push_external`): это два независимых заслона, и ни один
        не полагается на второй.
        """
        spec = ladder_of(kind)
        if spec is None:
            return ""
        ladder = spec[0]
        detail = getattr(event, "detail", None)
        detail = detail if isinstance(detail, dict) else {}
        ann = detail.get("escalation")
        if isinstance(ann, dict):
            key = str(ann.get("run") or "").strip()
            if key.startswith(f"{_LADDER_PREFIX}{ladder}/"):
                return key
        subject = ladder_subject(ladder, detail)
        level = int(spec[1])
        run = self._ladder_runs.get(ladder)
        if run is not None:
            fresh = (ts - _as_float(run.get("last_ts"), 0.0)) <= ladder_gap(ladder) + _EPS
            known = str(run.get("subject") or "")
            if fresh and ladder_same_subject(known, subject, ladder):
                # Разрыв отсчитывается от последней НОВОЙ ступени — так же, как
                # в движке (`_ladder_annotate`). Повтор уже достигнутой ступени
                # прогон не продлевает: поток повторов с интервалом короче
                # `ladder_gap` держал бы прогон бессмертным, и всё, что в него
                # попадёт, сворачивалось бы в ноль.
                if level > int(run.get("level", -1)):
                    run["level"] = level
                    run["last_ts"] = max(_as_float(run.get("last_ts"), ts), ts)
                run["subject"] = ladder_merge_subject(known, subject)
                return str(run["key"])
        seq = int(self._ladder_seq.get(ladder, 0)) + 1
        self._ladder_seq[ladder] = seq
        key = f"{_LADDER_PREFIX}{ladder}/{subject or '-'}#{seq}"
        self._ladder_runs[ladder] = {"key": key, "subject": subject,
                                     "last_ts": ts, "level": level}
        return key

    def _anon_obs_key(self, ts: float) -> str:
        """Признак наблюдения для события ввода без метки вопроса.

        Группирует по времени: события канала ввода, пришедшие в пределах
        `event_window` друг от друга, считаются одним наблюдением. Окно
        скользящее, а не нарезанное по границам (`ts // window`), иначе
        эпизод, попавший на границу бакета, разъехался бы на два наблюдения —
        то есть на тот же обход потолка, только реже.
        """
        if self.event_window <= 0:
            return ""
        last = self._anon_last_ts
        if last is not None and (ts - last) <= self.event_window:
            self._anon_last_ts = max(last, ts)
            return self._anon_key
        self._anon_seq += 1
        self._anon_key = f"\x00ввод/{self._anon_seq}"
        self._anon_last_ts = ts
        return self._anon_key

    def _fold_trigger(self, obs_key: str, event: ProctorEvent, ts: float) -> None:
        """Приписать левую половину связки к тому же наблюдению.

        Связка FUSION_* весит 55-65, но её левая половина (WINDOW_BLUR,
        GAZE_*, PHONE_*) уже начислена отдельным событием своего канала — она
        приходит раньше и своим весом (8-45). Это одно наблюдение, посчитанное
        дважды: сначала как факт, потом внутри связки. Снимать уже начисленное
        задним числом нельзя (событие ушло в отчёт с этим вкладом), но потолок
        наблюдения обязан его ВИДЕТЬ: иначе 15 (blur) + 24 (вставка) + 51
        (связка) = 90 снова дают автоблокировку с одного ответа.

        Левая половина опознаётся по detail["trigger"] самой связки: вид
        события и его ts движок записал туда сам, и ровно по этой паре мы
        находим уже учтённый вклад. Каждый вклад приписывается к наблюдению не
        более одного раза (`folded`), иначе две связки подряд посчитали бы
        один blur дважды.

        Если левая половина стоит на лестнице наблюдения (телефон, взгляд),
        приписывается вклад ВСЕГО прогона лестницы, а не одной её ступени:
        после п. 5 вклад телефона распределён по трём записям
        (25 + 10 + 10 = 45), и перенос только последней занизил бы то, что
        потолок наблюдения обязан видеть. Все ступени прогона помечаются
        перенесёнными сразу.
        """
        detail = getattr(event, "detail", None)
        if not isinstance(detail, dict):
            return
        trigger = detail.get("trigger")
        if not isinstance(trigger, dict):
            return
        try:
            kind = EventKind(str(trigger.get("kind") or ""))
        except ValueError:
            return
        trigger_ts = _as_float(trigger.get("ts"), 0.0)
        if trigger_ts <= 0:
            return
        for item in reversed(self._items):
            if item.folded or item.kind is not kind:
                continue
            if item.obs_key and not item.ladder_key:
                # Вклад уже отнесён к своему наблюдению по метке вопроса.
                continue
            if abs(item.ts - trigger_ts) > self.event_window:
                continue
            if item.ladder_key:
                run = self._observations.get(item.ladder_key)
                moved = _as_float(run.get("base") if run else None, item.base)
                first_ts = item.ts
                for sibling in self._items:
                    if sibling.ladder_key == item.ladder_key:
                        sibling.folded = True
                        first_ts = min(first_ts, sibling.ts)
                self._group(obs_key, min(first_ts, ts))["base"] += moved
                return
            item.obs_key = obs_key
            item.folded = True
            self._group(obs_key, min(item.ts, ts))["base"] += item.base
            return

    def _fit_to_cap(self, obs_key: str, raw_base: float, ts: float,
                    cap: float | None = None,
                    ladder: EventKind | None = None) -> float:
        """Урезать вклад до остатка потолка по этому наблюдению.

        Считаем по НЕзатухшим базам, а не по текущим значениям: все правила по
        одному ответу приходят в пределах секунд, спад между ними неразличим,
        а результат так не зависит от того, когда вызвали decay().

        `cap` — потолок именно этой группы. Для прогона лестницы он равен
        вкладу текущей ступени: группа «дотягивается» до высшей достигнутой
        ступени и не выше, поэтому старшая ступень добавляет разницу, а
        младшая после старшей — ничего. `ladder` включает отдельный учёт
        свёрнутого веса: лестница и потолок наблюдения — разные правила, и в
        отчёте они не имеют права выглядеть одним числом.
        """
        if not obs_key:
            # Без признака наблюдения группировать нечего: событие само себе
            # наблюдение, и его одиночный вес потолок не трогает (_MAX_WEIGHT).
            return raw_base
        group = self._group(obs_key, ts,
                            window=math.inf if ladder is not None else None)
        limit = self.event_cap if cap is None else cap
        room = _clamp(limit - group["base"], 0.0, raw_base)
        group["base"] += room
        if room < raw_base:
            if ladder is not None:
                self._ladder_folded += raw_base - room
                name = ladder.value
                self._ladder_folded_kinds[name] = (
                    self._ladder_folded_kinds.get(name, 0) + 1)
            else:
                self._capped += raw_base - room
        return room

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
            # Ступень, чей вклад уточнила старшая (`_ladder_reattribute`),
            # имеет нулевую базу и по порогу спада выпала бы сразу. Её
            # оставляем: в разборе она обязана быть видна с пометкой
            # «уточнено», иначе свёртка выглядит как пропавшее наблюдение.
            # Переживает она ровно столько, сколько живёт её старшая ступень:
            # у них один прогон, и чистит их один и тот же порог ниже.
            alive = {it.ladder_key for it in self._items
                     if it.base > 0 and it.ladder_key
                     and self._value(it, self._now) >= _PRUNE_FLOOR}
            self._items = [
                it for it in self._items
                if self._value(it, self._now) >= _PRUNE_FLOOR
                or (it.refined_by and it.ladder_key in alive)]

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

        Ступень лестницы, вклад которой уточнила старшая ступень
        (`_ladder_reattribute`), остаётся в разборе строкой с `contribution: 0`
        и полем `refined_by`. Выбрасывать её нельзя: по отчёту должно быть
        видно, что показание было и чем именно оно уточнено, — иначе свёртка
        выглядит как пропавшее наблюдение. Такие строки стоят в конце (сортируем
        по вкладу), а `top_reason`/`explain` их не называют: нулевой вклад не
        может быть причиной тревоги.
        """
        now = self._eval_ts()
        agg: dict[EventKind, dict[str, Any]] = {}
        for item in self._items:
            value = self._value(item, now)
            if value < _PRUNE_FLOOR and not item.refined_by:
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
            if item.refined_by:
                row["refined_by"] = item.refined_by
                continue
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

    def contributions(self) -> list[dict[str, Any]]:
        """Начисленные вклады как есть: (ts, база, вид) по порядку.

        База — это вес * уверенность УЖЕ после лестницы наблюдения и потолка
        наблюдения, то есть ровно то, из чего `score` складывает счёт по
        формуле спада `_value`. Нужно отчёту: график risk-score обязан
        восстанавливаться по журналу теми же правилами, а не одной только
        таблицей весов, иначе кривая в документе противоречит числу в том же
        документе (`storage/report.py::_risk_items_runtime`).

        Ступени с нулевой базой (уточнённые старшей ступенью) отдаются тоже —
        с `refined_by`: читателю видно, что показание было.
        """
        return [{
            "ts": item.ts,
            "base": round(item.base, 4),
            "kind": item.kind.value,
            "event_id": item.event_id,
            "raw_base": round(item.raw_base, 4),
            "refined_by": item.refined_by,
        } for item in self._items]

    def escalation_report(self) -> dict[str, Any]:
        """Лестницы наблюдения: правило, ступени и сколько веса свёрнуто."""
        return {
            "rule": ("за один прогон лестницы начисляется вклад только высшей "
                     "достигнутой ступени"),
            "folded_weight": round(self._ladder_folded, 2),
            "folded_by_kind": dict(self._ladder_folded_kinds),
            # Сколько РАЗНЫХ прогонов лестницы счёт видел за сессию. Пара
            # «свёрнуто N / прогонов M» обязана сходиться: свёртка без прогона
            # не бывает.
            "runs": _count_by_value(self._ladder_runs_seen),
            "ladders": {
                ladder: {
                    "label": LADDER_LABELS.get(ladder, ladder),
                    "steps": [
                        [{"kind": kind.value,
                          "weight": RISK_WEIGHTS.get(kind, 10.0)}
                         for kind in level_kinds]
                        for level_kinds in levels
                    ],
                    "top_weight": ladder_top_weight(ladder),
                    "sum_if_added": round(
                        sum(RISK_WEIGHTS.get(kind, 10.0)
                            for level_kinds in levels for kind in level_kinds), 2),
                }
                for ladder, levels in ESCALATION_LADDERS.items()
            },
        }

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
        rows = [r for r in rows if r["contribution"] > 0] or rows
        row = rows[0]
        count = f" ×{row['count']}" if row["count"] > 1 else ""
        return f"{row['label']}{count} (+{row['contribution']:.0f})"

    def explain(self, limit: int = 3) -> str:
        """Фраза для вердикта и отчёта: из чего сложился счёт."""
        rows = [r for r in self.breakdown()
                if r["contribution"] > 0][:max(limit, 1)]
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
            # Потолок вклада одного наблюдения и сколько веса он срезал за
            # сессию. Молча срезать нельзя: отчёт обязан показывать, что счёт
            # ограничен правилом, а не что детектор промолчал.
            "event_cap": {"cap": round(self.event_cap, 2),
                          "window_sec": self.event_window,
                          "discarded": round(self._capped, 2)},
            # Лестницы наблюдения: правило, таблица ступеней и сколько веса не
            # начислено, потому что это была младшая ступень уже достигнутого
            # наблюдения. Число нужно защите: «риск 45, а не 105» должно быть
            # подтверждено записью, а не словами.
            "escalation": self.escalation_report(),
            "breakdown": self.breakdown(),
            "lifetime_counts": self.lifetime_counts(),
            "explain": self.explain(),
        }


def _is_input_event(event: ProctorEvent) -> bool:
    """Событие канала ввода: в detail есть описание «ответа».

    Правила ввода (вставка, ритм, связки) кладут в detail словарь `response`;
    события камеры и обстановки его не имеют и каждое честно остаётся
    самостоятельным наблюдением. По наличию `response` и отличаем одно от
    другого, не угадывая по виду события.
    """
    detail = getattr(event, "detail", None)
    if not isinstance(detail, dict):
        return False
    return isinstance(detail.get("response"), dict)


def _observation_key(event: ProctorEvent) -> str:
    """Признак «одного наблюдаемого события» — id вопроса, если он есть.

    Все правила ввода (вставка, ритм, связки) кладут в detail описание ответа
    с `question_id`; события камеры и окружения его не имеют и каждое остаётся
    самостоятельным наблюдением. Ничего не угадываем: нет question_id — нет
    и группировки.
    """
    detail = getattr(event, "detail", None)
    if not isinstance(detail, dict):
        return ""
    response = detail.get("response")
    if isinstance(response, dict):
        qid = str(response.get("question_id") or "").strip()
        if qid:
            return qid
    return str(detail.get("question_id") or "").strip()


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def _count_by_value(mapping: dict[str, str]) -> dict[str, int]:
    """{ключ прогона: лестница} -> {лестница: сколько прогонов}."""
    out: dict[str, int] = {}
    for name in mapping.values():
        if name:
            out[name] = out.get(name, 0) + 1
    return out


__all__ = ["RiskScorer", "KIND_LABEL", "ACTION_ORDER", "ACTION_LEVEL"]

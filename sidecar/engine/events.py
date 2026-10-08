"""
Движок событий: превращает шумный поток покадровых наблюдений в редкие
обоснованные инциденты.

Зачем нужен отдельный слой. Детекторы отдают наблюдение на КАЖДОМ кадре: при
15 fps взгляд, ушедший вниз на четыре секунды, — это шестьдесят «нарушений».
Отчёт из такого потока нечитаем, а HUD мигает. Поэтому между наблюдениями и
`ProctorEvent` стоят три механизма:

1. **Окно подтверждения** (`confirm_sec`). Инцидент рождается только если
   условие держалось непрерывно не меньше своего окна. Мгновенный промах
   детектора (блик, поворот головы на секунду) событием не становится.

2. **Гистерезис** (`release_sec != confirm_sec`). Условие считается снятым
   только после `release_sec` непрерывного отсутствия. Без этого на границе
   порога (взгляд дрожит вокруг 20°) наблюдение щёлкает active/inactive каждые
   два кадра, окно подтверждения обнуляется, и либо не рождается ничего, либо
   рождается очередь дублей. Release специально короче confirm: отпускать надо
   быстрее, чем подтверждать, иначе система залипнет на уже снятом нарушении.

3. **Cooldown на тип** (`cooldown_sec`). Повторный инцидент того же вида не
   выпускается раньше cooldown. При этом ПРОДОЛЖАЮЩЕЕСЯ условие не плодит
   события вообще: пока оно держится, движок обновляет `duration` и `message`
   уже выпущенного инцидента (объект тот же, его видят и хранилище, и fusion).
   Новый инцидент того же вида возможен только после honest-release и только
   если с предыдущего прошло не меньше cooldown.

ЛЕСТНИЦЫ НАБЛЮДЕНИЯ. Три механизма выше убирают дубли ОДНОГО вида события. Они
ничего не могут сделать с эскалацией: телефон, попавший в кадр и затем поднятый
и наведённый на экран, рождает три РАЗНЫХ вида за полторы секунды, и в сумме
они дают 105 — порог блокировки с одного наблюдаемого объекта. Таблица
`protocol.ESCALATION_LADDERS` объявляет такие последовательности, а движок
размечает каждое их событие полем `detail["escalation"]`: номер ступени, прогон
лестницы (`run`), что эта ступень уточняет (`refines`) и чем уточнена она сама
(`superseded_by`). По этой разметке `engine/risk.py` начисляет вклад ТОЛЬКО
высшей достигнутой ступени прогона. Младшему событию движок ставит
`detail["refined_by"]`: из журнала оно не исчезает, его код остаётся
пригодным для апелляции, но вторым обвинением в счёте не становится.

Прогон лестницы — это один наблюдаемый объект. Он заканчивается, когда снято
показание, которым он оценён (для покадровых правил это видит `_close`,
см. `_ladder_release`), или когда новых
ступеней не было дольше `protocol.LADDER_GAP_SEC` (для мгновенных внешних
событий это единственный признак). Если ступени пришли с РАЗНЫМИ признаками
субъекта (`track_id`, `pid`), прогоны разные и вклады складываются как прежде:
второй телефон или вторая программа захвата — это второе наблюдение.

СОСТОЯНИЯ ОКРУЖЕНИЯ. Находки env-проверок из `STATEFUL_EXTERNAL` (сейчас это
`audio_device_connected`) — состояние, а не момент: проверка присылает их
каждый цикл, пока устройство подключено. Они идут через тот же конечный
автомат, что и покадровые наблюдения, поэтому одно подключение даёт ОДИН
инцидент с растущей длительностью, отчёт может сказать «устройство было
подключено с 14:03 по 14:21», а risk-score не получает новый вклад на каждый
цикл проверки. Условие снимается по времени (`expire`), потому что
env-проверка не присылает «снято» — находка просто пропадает из результата.

Составное правило `speech_without_lip_motion` собирается из двух независимых
каналов: `audio.speech` (VAD) и `face.mouth_open_ratio` (FaceMesh). Речь при
закрытом рте и при наличии лица в кадре означает, что источник звука — не тот,
кто в кадре; условие проходит обычный конвейер подтверждения с confirm 1.5 с.

ФОРМУЛИРОВКИ. Каждый `message_template` построен по формуле визуального гайда:
наблюдаемый факт → контекст с числами (длительность, во сколько раз превышен
персональный порог, доля кадра, близость к эталону) → понятное действие для
человека. Обвинительных слов в текстах нет и быть не должно: движок сообщает
наблюдение, решение принимает преподаватель (`memory/decisions.md` Р-11).
Плохо: «студент списывает с телефона». Хорошо: «в кадре зафиксирован телефон,
3.2 с — откройте запись для контекста».

КОДЫ ИНЦИДЕНТОВ. `EVENT_CODES` даёт каждому `EventKind` короткий стабильный код
(`VIS_101`, `GAZE_203`, `IDN_301`, `ENV_504`). Код кладётся в `detail["code"]`,
оттуда его показывают HUD и отчёт. Коды НЕ МЕНЯЮТСЯ: по ним студент подаёт
апелляцию, а комиссия поднимает эпизод — см. комментарий у таблицы.

РЕЖИМ РАЗВЁРТЫВАНИЯ. `exam_mode` (`classroom` по умолчанию, `remote`) решает,
какие правила вообще попадают в реестр движка. Аудио-анализ (`voice_other`,
`speech_without_lip_motion`) в аудитории не регистрируется: там он срабатывает
на соседей и превращается в генератор ложных обвинений — `decisions.md` Р-10.
Угроза наушника закрыта детерминированной проверкой `audio_device_connected`,
она работает в обоих режимах.

Конфиг правил — модульный словарь `RULES`, его можно крутить без правки логики.
Значения из `sidecar/config.py` (`confirm_windows`, `cooldowns`,
`release_window`) переопределяют табличные, если заданы.

Приватность: движок не хранит кадры и ничего не пишет на диск, только числа из
`detail`.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_SIDECAR = Path(__file__).resolve().parent.parent
if str(_SIDECAR) not in sys.path:
    sys.path.insert(0, str(_SIDECAR))

from protocol import (  # noqa: E402  (путь настраиваем выше)
    ESCALATION_LADDERS,
    LADDER_LABELS,
    RISK_WEIGHTS,
    Channel,
    EventKind,
    ProctorEvent,
    Severity,
    ladder_gap,
    ladder_merge_subject,
    ladder_of,
    ladder_same_subject,
    ladder_steps,
    ladder_subject,
    ladder_top_weight,
)

#: Защита от сравнения float'ов «на грани»: 1.7999999 >= 1.8 должно быть True.
_EPS = 1e-6


def severity_for_weight(weight: float) -> Severity:
    """Severity по весу события из RISK_WEIGHTS — таблица не дублируется."""
    if weight >= 55:
        return Severity.CRITICAL
    if weight >= 35:
        return Severity.HIGH
    if weight >= 20:
        return Severity.MEDIUM
    if weight >= 8:
        return Severity.LOW
    return Severity.INFO


# ===========================================================================
# Коды инцидентов.
#
# ВНИМАНИЕ: КОДЫ СТАБИЛЬНЫ И НЕИЗМЕНЯЕМЫ. Код попадает в отчёт, в подпись
# hash-chain и в HUD; по нему студент подаёт апелляцию, а комиссия поднимает
# эпизод спустя недели. Переназначить код у существующего вида — значит
# рассинхронизировать уже выданные отчёты с системой. Новый вид события
# получает НОВЫЙ свободный номер в своём блоке; освободившиеся номера
# повторно не используются.
#
# Схема: префикс по каналу + три цифры. Блок номеров закреплён за каналом:
#   VIS_1xx  — объекты, лицо, присутствие в кадре (Channel.VISION)
#   GAZE_2xx — взгляд и поворот головы       (Channel.GAZE)
#   IDN_3xx  — личность и живость            (Channel.IDENTITY)
#   AUD_4xx  — анализ звука                  (Channel.AUDIO, только remote)
#   ENV_5xx  — проверки окружения и ОС       (Channel.ENVIRONMENT)
#   SHL_6xx  — защищённая оболочка           (Channel.SHELL)
#   FUS_7xx  — связки сигналов и ввод        (Channel.FUSION)
#   SYS_9xx  — служебные события сессии      (Channel.SYSTEM)
# ===========================================================================
EVENT_CODES: dict[EventKind, str] = {
    # --- зрение ------------------------------------------------------------
    EventKind.PHONE_IN_FRAME: "VIS_101",
    EventKind.PHONE_RAISED: "VIS_102",
    EventKind.PHONE_AIMED_AT_SCREEN: "VIS_103",
    EventKind.FORBIDDEN_OBJECT: "VIS_110",
    EventKind.NO_FACE: "VIS_120",
    EventKind.SECOND_FACE: "VIS_121",

    # --- взгляд ------------------------------------------------------------
    EventKind.GAZE_DOWN: "GAZE_201",
    EventKind.GAZE_SIDE: "GAZE_202",
    EventKind.GAZE_OFF_SCREEN: "GAZE_203",
    EventKind.HEAD_TURNED: "GAZE_210",

    # --- личность ----------------------------------------------------------
    EventKind.IDENTITY_MISMATCH: "IDN_301",
    EventKind.LIVENESS_FAIL: "IDN_302",

    # --- звук (регистрируется только в exam_mode = remote, Р-10) -----------
    EventKind.VOICE_OTHER: "AUD_401",
    EventKind.SPEECH_WITHOUT_LIP_MOTION: "AUD_402",

    # --- окружение ---------------------------------------------------------
    EventKind.VIRTUAL_CAMERA: "ENV_501",
    EventKind.REMOTE_ACCESS_SOFTWARE: "ENV_502",
    EventKind.VIRTUAL_MACHINE: "ENV_503",
    # AUDIO_DEVICE_CONNECTED — проверка устройств, а не анализ сигнала, поэтому
    # код из блока окружения (Р-10: детерминированная замена аудио-анализа).
    EventKind.AUDIO_DEVICE_CONNECTED: "ENV_504",
    EventKind.SCREEN_RECORDING: "ENV_505",
    EventKind.MULTIPLE_DISPLAYS: "ENV_506",
    EventKind.BLACKLISTED_PROCESS: "ENV_507",

    # --- оболочка ----------------------------------------------------------
    EventKind.WINDOW_BLUR: "SHL_601",
    EventKind.FULLSCREEN_EXIT: "SHL_602",
    EventKind.SHORTCUT_BLOCKED: "SHL_603",
    EventKind.CLIPBOARD_PASTE: "SHL_604",
    EventKind.DEVTOOLS_ATTEMPT: "SHL_605",
    # Фактический режим запуска оболочки. Вид не проходит через движок
    # (`main.py::_cmd_shell_config` кладёт событие сам), но код обязан жить в
    # канонической таблице: иначе отчёт печатает UNK_000. Номер согласован с
    # `main.SHELL_CONFIG_CODE` — свободное начало блока оболочки.
    EventKind.SHELL_CONFIG: "SHL_600",

    # --- ввод и связки -----------------------------------------------------
    EventKind.PASTE_BURST: "FUS_701",
    EventKind.TYPING_ANOMALY: "FUS_702",
    EventKind.FUSION_GAZE_THEN_ANSWER: "FUS_710",
    EventKind.FUSION_BLUR_THEN_ANSWER: "FUS_711",
    EventKind.FUSION_PHONE_THEN_ANSWER: "FUS_712",

    # --- служебное ---------------------------------------------------------
    EventKind.SESSION_STARTED: "SYS_901",
    EventKind.SESSION_ENDED: "SYS_902",
    EventKind.CALIBRATION_DONE: "SYS_903",
    EventKind.SENSOR_LOST: "SYS_910",
}

#: Код для вида, которого нет в таблице. Такое означает, что в protocol.py
#: добавили EventKind и забыли код: в отчёте честнее показать UNK_000, чем
#: выдать чужой код или промолчать.
UNKNOWN_CODE = "UNK_000"


def code_for(kind: Any) -> str:
    """Стабильный код инцидента для отчёта и HUD."""
    if not isinstance(kind, EventKind):
        kind = _as_kind(kind)
    if kind is None:
        return UNKNOWN_CODE
    return EVENT_CODES.get(kind, UNKNOWN_CODE)


@dataclass(frozen=True)
class Rule:
    """Правило подтверждения для одного наблюдаемого условия.

    confirm_sec   — сколько условие должно держаться, чтобы стать инцидентом;
    release_sec   — сколько оно должно отсутствовать, чтобы считаться снятым;
    cooldown_sec  — минимальная пауза между повторными инцидентами этого вида;
    message_template — русский текст для отчёта по формуле «факт → контекст с
    числами → действие для человека», подстановки см. _message_vars().
    """
    kind: EventKind
    severity: Severity
    channel: Channel
    confirm_sec: float
    release_sec: float
    cooldown_sec: float
    message_template: str


def _rule(kind: EventKind, channel: Channel, confirm: float, release: float,
          cooldown: float, template: str, severity: Severity | None = None) -> Rule:
    return Rule(
        kind=kind,
        severity=severity or severity_for_weight(RISK_WEIGHTS.get(kind, 10.0)),
        channel=channel,
        confirm_sec=confirm,
        release_sec=release,
        cooldown_sec=cooldown,
        message_template=template,
    )


# ===========================================================================
# Реестр правил. Ключ — имя наблюдения в нижнем регистре; EventKind.value
# нормализуется к нему же, поэтому push_observation принимает и 'GAZE_DOWN',
# и 'gaze_down'.
#
# Тексты: наблюдаемый факт → контекст с числами → действие для человека.
# Ни одна формулировка не называет причину наблюдения и не оценивает студента:
# «зафиксирован», «не совпадает», «не двигаются» — да; «списывает», «пытается
# обмануть» — нет. Причину устанавливает преподаватель, открыв запись.
# ===========================================================================
RULES: dict[str, Rule] = {
    # --- объекты в кадре -------------------------------------------------
    "phone_in_frame": _rule(
        EventKind.PHONE_IN_FRAME, Channel.VISION, 0.8, 0.6, 12.0,
        "В кадре зафиксирован телефон{held_comma}{conf_area}. "
        "Откройте запись для контекста."),
    "phone_raised": _rule(
        EventKind.PHONE_RAISED, Channel.VISION, 0.6, 0.6, 12.0,
        "Телефон поднят к уровню лица{held_for}{conf_area}. "
        "Откройте запись для контекста."),
    # Текст перечисляет только то, что действительно проверено эвристикой
    # detectors/objects.py::_eval_aimed (поднят, вертикальный бокс, приближение,
    # выдержка). Направление взгляда она не использует, и утверждать его в
    # доказательном отчёте нельзя — подробности всегда лежат в detail.explain.
    # Поэтому текст говорит о совпадении геометрии, а не о съёмке экрана как
    # установленном факте.
    "phone_aimed_at_screen": _rule(
        EventKind.PHONE_AIMED_AT_SCREEN, Channel.VISION, 0.6, 0.6, 12.0,
        "Телефон поднят, держится вертикально и приближается к камере"
        "{held_comma}{conf_area}. Геометрия совпадает со съёмкой экрана. "
        "Откройте запись для контекста."),
    "forbidden_object": _rule(
        EventKind.FORBIDDEN_OBJECT, Channel.VISION, 2.0, 1.5, 30.0,
        "В кадре посторонний предмет: {label_ru}{held_comma}{conf_area}. "
        "Сверьтесь с правилами экзамена и откройте запись для контекста."),

    # --- лицо и присутствие ----------------------------------------------
    "no_face": _rule(
        EventKind.NO_FACE, Channel.VISION, 2.5, 1.0, 15.0,
        "Лицо не видно в кадре{held_comma}. Наблюдение по видео на это время "
        "недоступно. Попросите студента вернуться в кадр."),
    "second_face": _rule(
        EventKind.SECOND_FACE, Channel.VISION, 1.0, 1.5, 20.0,
        "В кадре одновременно {faces} {faces_word}{held_comma}. "
        "Проверьте последние 30 секунд записи."),
    "identity_mismatch": _rule(
        EventKind.IDENTITY_MISMATCH, Channel.IDENTITY, 2.0, 2.0, 20.0,
        "Совпадение с эталоном ниже порога{sim_note}{streak_note}{held_par}. "
        "Освещение и ракурс тоже снижают близость. Подтвердите личность."),
    "liveness_fail": _rule(
        EventKind.LIVENESS_FAIL, Channel.IDENTITY, 3.0, 2.0, 60.0,
        "Лицо в кадре есть, морганий нет{no_blink_note}. Так ведёт себя "
        "фотография или зацикленная запись. Попросите студента моргнуть и "
        "повернуть голову."),

    # --- взгляд и голова --------------------------------------------------
    "gaze_down": _rule(
        EventKind.GAZE_DOWN, Channel.GAZE, 1.8, 0.8, 10.0,
        "Взгляд отведён вниз{held_comma}{dev_phrase}. "
        "Требуется проверка эпизода."),
    "gaze_side": _rule(
        EventKind.GAZE_SIDE, Channel.GAZE, 1.8, 0.8, 10.0,
        "Взгляд отведён {zone_ru}{held_comma}{dev_phrase}. "
        "Требуется проверка эпизода."),
    "gaze_off_screen": _rule(
        EventKind.GAZE_OFF_SCREEN, Channel.GAZE, 2.0, 0.8, 10.0,
        "Взгляд вне экрана{held_comma}: точка взгляда не попадает в карту "
        "монитора{dev_phrase}. Требуется проверка эпизода."),
    "head_turned": _rule(
        EventKind.HEAD_TURNED, Channel.GAZE, 2.0, 1.0, 15.0,
        "Голова отвернута от экрана{held_comma}{dev_phrase}{angles_note}. "
        "Требуется проверка эпизода."),

    # --- аудио (только exam_mode = remote, см. REMOTE_ONLY_RULES и Р-10) ----
    "voice_other": _rule(
        EventKind.VOICE_OTHER, Channel.AUDIO, 1.2, 1.5, 15.0,
        "Микрофон слышит голос, не совпадающий с образцом студента"
        "{held_comma}{rms_conf}. Откройте запись для контекста."),
    "speech_without_lip_motion": _rule(
        EventKind.SPEECH_WITHOUT_LIP_MOTION, Channel.AUDIO, 1.5, 1.0, 20.0,
        "Речь слышна{held_comma}, губы в это время не двигаются{mouth_note}. "
        "Источник звука — не студент в кадре. "
        "Откройте запись для контекста."),

    # --- окружение ---------------------------------------------------------
    # Р-10: замена аудио-анализа в аудитории. Проверка детерминированная
    # (устройство либо подключено, либо нет), поэтому работает в обоих режимах
    # и подтверждения по времени не требует.
    #
    # Контекст и действие приходят из detail (`reason`, `when`, `advice`):
    # проверка различает гарнитуру, виртуальный аудио-вывод, внешнюю колонку и
    # звук через HDMI, и у каждого случая своё исполнимое действие. Один общий
    # текст про гарнитуру ломал формулу гайда ровно на действии — «попросите
    # отключить устройство» нельзя выполнить применительно к монитору.
    # release_sec = 30: env-проверка повторяет находку каждый цикл, пока
    # устройство подключено, поэтому инцидент ведётся как «прогон» —
    # см. push_external и STATEFUL_EXTERNAL.
    "audio_device_connected": _rule(
        EventKind.AUDIO_DEVICE_CONNECTED, Channel.ENVIRONMENT, 0.0, 30.0, 45.0,
        "Во время экзамена активно аудио-устройство{name_colon}."
        "{reason_sentence}{when_sentence}{advice_sentence}"),
    "virtual_camera": _rule(
        EventKind.VIRTUAL_CAMERA, Channel.ENVIRONMENT, 0.0, 1.0, 120.0,
        "Изображение приходит с виртуальной камеры{name_par}. Что показывает "
        "физическая камера, системе не видно. "
        "Переключите источник на встроенную камеру."),
    "remote_access_software": _rule(
        EventKind.REMOTE_ACCESS_SOFTWARE, Channel.ENVIRONMENT, 0.0, 1.0, 120.0,
        "Запущено ПО удалённого доступа{name_colon}. Через него экраном может "
        "управлять другой компьютер. Попросите закрыть программу."),
    "virtual_machine": _rule(
        EventKind.VIRTUAL_MACHINE, Channel.ENVIRONMENT, 0.0, 1.0, 300.0,
        "Экзамен идёт в виртуальной машине{name_par}. Часть проверок "
        "окружения в ней недостоверна. Решите, допустима ли такая среда."),
    "screen_recording": _rule(
        EventKind.SCREEN_RECORDING, Channel.ENVIRONMENT, 0.0, 1.0, 60.0,
        "Идёт запись или трансляция экрана{name_colon}. Содержимое билета "
        "может уйти за пределы аудитории. Попросите остановить запись."),
    "multiple_displays": _rule(
        EventKind.MULTIPLE_DISPLAYS, Channel.ENVIRONMENT, 0.0, 1.0, 120.0,
        "Подключено больше одного монитора{displays_note}. Второй экран вне "
        "поля зрения камеры. Попросите отключить дополнительный монитор."),
    "blacklisted_process": _rule(
        EventKind.BLACKLISTED_PROCESS, Channel.ENVIRONMENT, 0.0, 1.0, 60.0,
        # Стем без глагола: summary из env_checks сам начинается с «запущен …»,
        # иначе выходит «Запущен процесс …: запущен ИИ-клиент …».
        "Проверка процессов{name_colon}. "
        "Сверьтесь с правилами экзамена и попросите закрыть эти программы."),

    # --- оболочка ----------------------------------------------------------
    "window_blur": _rule(
        EventKind.WINDOW_BLUR, Channel.SHELL, 0.0, 0.5, 5.0,
        "Фокус ушёл из окна экзамена: активно другое приложение{held_par}. "
        "Окно возвращено автоматически. Откройте запись для контекста."),
    "fullscreen_exit": _rule(
        EventKind.FULLSCREEN_EXIT, Channel.SHELL, 0.0, 0.5, 5.0,
        "Окно экзамена вышло из полноэкранного режима{held_par}. "
        "Проверьте, что студент вернулся в полный экран."),
    "shortcut_blocked": _rule(
        EventKind.SHORTCUT_BLOCKED, Channel.SHELL, 0.0, 0.5, 10.0,
        "Сочетание клавиш{combo_note} перехвачено оболочкой и не выполнено. "
        "Факт зафиксирован в отчёте, отдельное действие не требуется."),
    "clipboard_paste": _rule(
        EventKind.CLIPBOARD_PASTE, Channel.SHELL, 0.0, 0.5, 5.0,
        "Вставка из буфера обмена{length_note}. Откуда взят текст, системе не "
        "видно. Откройте запись для контекста."),
    "devtools_attempt": _rule(
        EventKind.DEVTOOLS_ATTEMPT, Channel.SHELL, 0.0, 0.5, 15.0,
        "Зафиксировано обращение к инструментам разработчика. Доступ закрыт "
        "оболочкой. Откройте запись для контекста."),

    # --- поведение ввода (приходят от fusion как внешние) ------------------
    "paste_burst": _rule(
        EventKind.PASTE_BURST, Channel.FUSION, 0.0, 0.5, 15.0,
        "В поле ответа одной вставкой появился крупный блок текста"
        "{length_note}. Откройте запись для контекста."),
    "typing_anomaly": _rule(
        EventKind.TYPING_ANOMALY, Channel.FUSION, 0.0, 0.5, 30.0,
        "Ритм набора отличается от базового профиля студента{dev_phrase}. "
        "Так выглядит и волнение, и внешний источник текста. "
        "Требуется проверка эпизода."),

    # --- fusion -------------------------------------------------------------
    # Fusion — связка двух сигналов по времени, а не установленный факт. Текст
    # обязан сохранять эту разницу: событие просит проверить эпизод.
    "fusion_gaze_then_answer": _rule(
        EventKind.FUSION_GAZE_THEN_ANSWER, Channel.FUSION, 0.0, 0.5, 20.0,
        "Взгляд уходил с экрана, сразу после этого появился готовый ответ"
        "{lag_note}. Совпадение по времени двух сигналов. "
        "Требуется проверка эпизода."),
    "fusion_blur_then_answer": _rule(
        EventKind.FUSION_BLUR_THEN_ANSWER, Channel.FUSION, 0.0, 0.5, 20.0,
        "Фокус уходил из окна экзамена, сразу после возврата появился готовый "
        "ответ{lag_note}. Совпадение по времени двух сигналов. "
        "Требуется проверка эпизода."),
    "fusion_phone_then_answer": _rule(
        EventKind.FUSION_PHONE_THEN_ANSWER, Channel.FUSION, 0.0, 0.5, 20.0,
        "Телефон был в кадре, сразу после этого появился готовый ответ"
        "{lag_note}. Совпадение по времени двух сигналов. "
        "Требуется проверка эпизода."),

    # --- служебное ----------------------------------------------------------
    "sensor_lost": _rule(
        EventKind.SENSOR_LOST, Channel.SYSTEM, 3.0, 1.5, 30.0,
        "Пропал источник данных{sensor_note}{held_comma}. Канал наблюдения "
        "не работает. Проверьте подключение камеры и микрофона."),
    "session_started": _rule(
        EventKind.SESSION_STARTED, Channel.SYSTEM, 0.0, 0.5, 0.0,
        "Сессия прокторинга начата. Каналы наблюдения активны, "
        "дополнительное действие не требуется."),
    "session_ended": _rule(
        EventKind.SESSION_ENDED, Channel.SYSTEM, 0.0, 0.5, 0.0,
        "Сессия прокторинга завершена. Отчёт формируется, "
        "дополнительное действие не требуется."),
    "calibration_done": _rule(
        EventKind.CALIBRATION_DONE, Channel.SYSTEM, 0.0, 0.5, 0.0,
        "Калибровка завершена{stage_note}. Пороги взгляда настроены по этому "
        "студенту, дополнительное действие не требуется."),
}

# ===========================================================================
# Режим развёртывания: какие правила вообще регистрируются.
#
# Р-10. В аудитории анализ звука выключен: `voice_other` срабатывает на соседа
# и преподавателя, `speech_without_lip_motion` — на любую чужую речь при
# закрытом рте студента, то есть постоянно. Эти правила в classroom не просто
# молчат — их НЕТ в реестре движка, поэтому наблюдение по ним не может стать
# инцидентом даже через push_external. Угроза наушника закрыта правилом
# `audio_device_connected`, которое работает в обоих режимах.
# ===========================================================================
REMOTE_ONLY_RULES: frozenset[str] = frozenset({
    "voice_other",
    "speech_without_lip_motion",
})

#: Объяснение для HUD, `hello` и отчёта: правила не «молчат», их НЕТ в реестре.
#: Строка нужна, чтобы факт работы Р-10 был виден человеку, а не только в
#: покадровом счётчике `suppressed_by_mode`.
CLASSROOM_AUDIO_OFF_REASON = (
    "Анализ звука выключен режимом развёртывания «аудитория»: в помещении на "
    "30 человек он измеряет шум класса, а не студента. Угроза личного "
    "аудио-канала закрыта проверкой активных аудио-устройств "
    "(AUDIO_DEVICE_CONNECTED), она не зависит от шума."
)

# ===========================================================================
# Внешние находки, которые по природе являются СОСТОЯНИЕМ, а не моментом.
#
# env-проверки повторяются каждый цикл, пока условие держится: подключённая
# гарнитура попадает в результат и на первом прогоне, и на шестидесятом. Если
# такую находку проводить через обычный `push_external`, в доказательной базе
# окажется череда точечных записей с `duration = 0`, `open_incidents()` по
# этому виду всегда пуст, а отчёт не может сказать «гарнитура была подключена
# с 14:03 по 14:21» — именно то, что нужно преподавателю для решения. Плюс
# каждая запись добавляла новый вклад в risk-score и держала его на плато.
#
# Поэтому такие находки идут через тот же конечный автомат, что и покадровые
# наблюдения (`_advance`): активность = находка пришла в этом прогоне, снятие —
# по времени (`_expire`), потому что env-проверка не присылает «условие снято»,
# находка просто пропадает из результата очередного прогона.
# ===========================================================================
STATEFUL_EXTERNAL: frozenset[str] = frozenset({
    "audio_device_connected",
})

#: Известные режимы. Неизвестное значение трактуется как classroom: более
#: строгий к ложным срабатываниям режим — безопасный выбор по умолчанию.
EXAM_MODES: tuple[str, ...] = ("classroom", "remote")
DEFAULT_EXAM_MODE = "classroom"


def normalize_exam_mode(value: Any) -> str:
    """Значение конфига -> 'classroom' | 'remote'."""
    mode = str(value or "").strip().lower()
    return mode if mode in EXAM_MODES else DEFAULT_EXAM_MODE


def rules_for_mode(exam_mode: Any = DEFAULT_EXAM_MODE) -> dict[str, Rule]:
    """Реестр правил для режима развёртывания."""
    if normalize_exam_mode(exam_mode) == "remote":
        return dict(RULES)
    return {name: rule for name, rule in RULES.items()
            if name not in REMOTE_ONLY_RULES}

#: Синонимы имён наблюдений: разные модули называют одно и то же по-разному.
ALIASES: dict[str, str] = {
    "speech_no_lips": "speech_without_lip_motion",
    "speech_without_lips": "speech_without_lip_motion",
    "no_lips": "speech_without_lip_motion",
    "gaze_away": "gaze_side",          # EventKind.GAZE_AWAY не существует
    "gaze_aside": "gaze_side",
    "head_away": "head_turned",
    "face_lost": "no_face",
    "face_absent": "no_face",
    "second_person": "second_face",
    "multiple_faces": "second_face",
    "phone": "phone_in_frame",
    "other_voice": "voice_other",
    "foreign_voice": "voice_other",
    "camera_lost": "sensor_lost",
    "paste": "clipboard_paste",
    # проверка аудио-устройств: env_checks называет находку по-разному
    "audio_device": "audio_device_connected",
    "headphones": "audio_device_connected",
    "headset": "audio_device_connected",
    "bluetooth_headset": "audio_device_connected",
}

#: Входы составного правила: имя наблюдения -> слот внутреннего состояния.
COMPOSITE_INPUTS: dict[str, str] = {
    "audio.speech": "speech",
    "audio_speech": "speech",
    "face.mouth_open_ratio": "mouth",
    "face_mouth_open_ratio": "mouth",
    "mouth_open_ratio": "mouth",
    "face.mouth": "mouth",
}

#: Во сколько раз превышен порог, если детектор не отдал нормированную девиацию:
#: (ключи detail, из которых берём max|·|; ключ конфига с лимитом; дефолт лимита).
DEV_FALLBACK: dict[str, tuple[tuple[str, ...], str, float]] = {
    "gaze_down": (("gaze_pitch",), "gaze_pitch_down_limit", 18.0),
    "gaze_side": (("gaze_yaw",), "gaze_yaw_limit", 20.0),
    "gaze_off_screen": (("gaze_yaw", "gaze_pitch"), "gaze_off_screen_limit", 32.0),
    "head_turned": (("head_yaw", "head_pitch"), "head_yaw_limit", 25.0),
}

#: Нормированные девиации, если детектор их отдаёт (|dev| > 1 — за персональным порогом).
DEV_KEYS = ("dev", "deviation", "gaze_dev_v", "gaze_dev_h",
            "head_dev_yaw", "head_dev_pitch")

#: Зоны взгляда -> по-русски, для подстановки в message.
ZONE_RU: dict[str, str] = {
    "left": "влево", "right": "вправо", "down": "вниз", "bottom": "вниз",
    "up": "вверх", "top": "вверх", "center": "в центр экрана",
    "side": "в сторону", "off": "мимо экрана", "off_screen": "мимо экрана",
    "away": "мимо экрана", "outside": "мимо экрана", "unknown": "в сторону",
}

#: Метки YOLO -> по-русски.
LABEL_RU: dict[str, str] = {
    "cell phone": "телефон", "mobile phone": "телефон", "phone": "телефон",
    "book": "книга", "laptop": "ноутбук", "tv": "монитор или телевизор",
    "monitor": "второй монитор", "tvmonitor": "монитор", "remote": "пульт",
    "keyboard": "посторонняя клавиатура", "person": "посторонний человек",
}


class _SafeVars(dict):
    """Подстановка без падений: отсутствующий ключ шаблона даёт пустую строку."""

    def __missing__(self, key: str) -> str:  # pragma: no cover - тривиально
        return ""


@dataclass
class _State:
    """Состояние одного наблюдаемого условия."""
    name: str
    since: float | None = None          # когда условие стало активным
    last_active: float = 0.0            # последний активный ts
    last_seen: float = 0.0              # монотонность входного времени
    fired: bool = False                 # инцидент по этому проходу уже выпущен
    last_fire: float = 0.0              # когда выпускали в последний раз
    open_event: ProctorEvent | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    fired_total: int = 0
    peak_duration: float = 0.0
    held_total: float = 0.0             # суммарное время под условием, сек


#: Префикс ключа прогона лестницы.
#:
#: Ключ уходит в `detail["escalation"]["run"]`, то есть в журнал, в цепочку и
#: в отчёт, поэтому он обязан быть печатным: служебный \x00 (как во внутренних
#: ключах `engine/risk.py`) в SQLite-TEXT и в HTML-отчёте — мина.
#: Столкнуться с ключом наблюдения по вопросу он всё равно не может: метку
#: вопроса пропускает только `protocol.safe_label`, а она не допускает ни «/»,
#: ни «:», ни «#» и обрезает длину до LABEL_MAX_LEN.
LADDER_RUN_PREFIX = "ladder/"


@dataclass
class _LadderRun:
    """Один прогон лестницы: эскалация показаний об ОДНОМ объекте.

    `level` — высшая достигнутая ступень прогона, `rows` — выпущенные ступени
    для отчёта, `objs` — ссылки на ещё живые события младших ступеней: им
    ставится `detail["refined_by"]`, когда приходит старшая.

    `key` фиксируется при создании и больше не меняется, даже если признак
    субъекта станет известен позже: по этому ключу `engine/risk.py` держит
    группу вклада, и смена ключа посреди прогона вернула бы двойной счёт.
    """
    ladder: str
    seq: int
    key: str
    subject: str = ""
    level: int = -1
    peak_kind: str = ""
    first_ts: float = 0.0
    last_ts: float = 0.0
    ended: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)
    objs: list[tuple[int, ProctorEvent]] = field(default_factory=list)


class EventEngine:
    """Единственное место, где наблюдения превращаются в ProctorEvent.

    Использование:
        eng = EventEngine(config)
        events = eng.push_observation("GAZE_DOWN", True, ts, gaze_pitch=-43.0)
        events = eng.push_external(EventKind.WINDOW_BLUR, {"source": "shell"})

    Все методы возвращают список готовых `ProctorEvent` (обычно пустой).
    Потокобезопасность не обеспечивается специально: вызывающий (main.py)
    держит собственный lock вокруг движка.
    """

    def __init__(self, config: dict | None = None) -> None:
        cfg = config or {}
        self.cfg = cfg
        self._confirm_cfg = dict(cfg.get("confirm_windows") or {})
        self._cooldown_cfg = dict(cfg.get("cooldowns") or {})
        self._release_cfg = float(cfg.get("release_window", 0.0) or 0.0)
        self._identity_threshold = float(cfg.get("identity_threshold", 0.35) or 0.35)
        self._mouth_threshold = float(cfg.get("mouth_open_speech", 0.22) or 0.22)
        #: Реестр правил этого движка зависит от режима развёртывания (Р-10):
        #: в classroom аудио-правил в нём нет вообще.
        self.exam_mode = normalize_exam_mode(_cfg_exam_mode(cfg))
        self.rules: dict[str, Rule] = rules_for_mode(self.exam_mode)
        self._suppressed: frozenset[str] = frozenset(set(RULES) - set(self.rules))
        #: Сколько наблюдений отброшено из-за режима — видно в stats().
        self._mode_suppressed: dict[str, int] = {}
        self._states: dict[str, _State] = {}
        self._external_fire: dict[str, float] = {}
        #: Текущий прогон каждой лестницы наблюдения и счётчик прогонов.
        self._ladder_runs: dict[str, _LadderRun] = {}
        self._ladder_seq: dict[str, int] = {}
        #: Сколько ступеней пришло не выше уже достигнутой — видно в stats().
        self._ladder_folded: dict[str, int] = {}
        #: Срезы закрытых инцидентов — отчёт берёт отсюда итоговые длительности.
        self._closed: list[dict[str, Any]] = []
        #: Имена наблюдений, для которых нет правила — видно в stats(), не падаем.
        self._unknown: dict[str, int] = {}
        # состояние составного правила
        self._speech = False
        self._speech_detail: dict[str, Any] = {}
        self._mouth_ratio = 0.0
        self._face_present = True
        self._fired_total = 0

    # ------------------------------------------------------------------ сброс
    def reset(self) -> None:
        """Полный сброс между сессиями."""
        self._states.clear()
        self._external_fire.clear()
        self._closed.clear()
        self._unknown.clear()
        self._mode_suppressed.clear()
        self._ladder_runs.clear()
        self._ladder_seq.clear()
        self._ladder_folded.clear()
        self._speech = False
        self._speech_detail = {}
        self._mouth_ratio = 0.0
        self._face_present = True
        self._fired_total = 0

    def available(self) -> bool:
        """Движок чистый Python, зависимостей нет — доступен всегда."""
        return True

    # ------------------------------------------------------------- параметры
    @staticmethod
    def normalize(name: Any) -> str:
        """Имя наблюдения -> канонический ключ правила."""
        if isinstance(name, EventKind):
            raw = name.value
        else:
            raw = str(getattr(name, "value", name) or "")
        key = raw.strip().lower().replace(" ", "_").replace("-", "_")
        return ALIASES.get(key, key)

    def rule_for(self, name: Any) -> Rule | None:
        return self.rules.get(self.normalize(name))

    def suppressed_by_mode(self, name: Any) -> bool:
        """Правило существует, но в этом режиме развёртывания не зарегистрировано."""
        return self.normalize(name) in self._suppressed

    def confirm_for(self, name: Any) -> float:
        """Окно подтверждения: конфиг (по EventKind.value) важнее таблицы."""
        key = self.normalize(name)
        rule = self.rules.get(key)
        if rule is None:
            return float(self.cfg.get("confirm_window", 1.5) or 1.5)
        value = self._confirm_cfg.get(rule.kind.value, self._confirm_cfg.get(key))
        return float(value) if value is not None else rule.confirm_sec

    def cooldown_for(self, name: Any) -> float:
        key = self.normalize(name)
        rule = self.rules.get(key)
        if rule is None:
            return float(self.cfg.get("cooldown_sec", 10.0) or 10.0)
        value = self._cooldown_cfg.get(rule.kind.value, self._cooldown_cfg.get(key))
        return float(value) if value is not None else rule.cooldown_sec

    def release_for(self, name: Any) -> float:
        """Окно отпускания. Конфиг задаёт один общий `release_window`."""
        key = self.normalize(name)
        rule = self.rules.get(key)
        if rule is None:
            return self._release_cfg or 1.0
        if key in STATEFUL_EXTERNAL:
            # Общий `release_window` (1 с) рассчитан на покадровые наблюдения
            # при 15 fps. У env-прогона окно отпускания обязано быть больше
            # интервала проверки, иначе инцидент закрывается между двумя
            # прогонами и запись снова распадается на череду точек. Поэтому
            # здесь действует только табличное значение правила.
            return rule.release_sec
        return self._release_cfg or rule.release_sec

    # --------------------------------------------------------- основной вход
    def push_observation(self, name: str, active: bool, ts: float, /,
                         **detail: Any) -> list[ProctorEvent]:
        """Принять покадровое наблюдение. Вернуть рождённые инциденты (обычно []).

        `name` — имя условия: EventKind.value ('GAZE_DOWN'), каноническое имя
        ('gaze_down'), синоним из ALIASES или вход составного правила
        ('audio.speech', 'face.mouth_open_ratio').

        Первые три параметра — positional-only: detail приходит как **kwargs, и
        ключ с именем `name`/`active`/`ts` иначе ломал бы вызов целиком.
        """
        key = self.normalize(name)
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            ts = time.time()

        # Прогон env-проверки закрывается по времени, а когда находок не стало
        # вообще, env-тикер не делает ни одного push_external — иначе инцидент
        # «гарнитура подключена» остался бы открытым до конца сессии. Часы те
        # же (`time.time()` и в capture, и в env-тикере), поэтому сравнение
        # корректно; расхождение часов даст отрицательный разрыв и просто
        # отложит закрытие до следующего прогона проверок.
        self._expire(ts)

        # вход составного правила: сам по себе события не даёт
        if key in COMPOSITE_INPUTS:
            return self._note_composite(COMPOSITE_INPUTS[key], bool(active), ts, detail)

        # побочный учёт присутствия лица — нужен составному правилу
        if key == "no_face":
            self._face_present = not bool(active)

        rule = self.rules.get(key)
        if rule is None:
            if key in self._suppressed:
                # Правило есть в таблице, но в этом режиме развёртывания не
                # зарегистрировано (Р-10). Это не «неизвестное имя»: источник
                # работает штатно, инцидента быть не должно.
                self._mode_suppressed[key] = self._mode_suppressed.get(key, 0) + 1
            else:
                self._unknown[str(name)] = self._unknown.get(str(name), 0) + 1
            return []
        return self._advance(key, rule, bool(active), ts, detail)

    def push_external(self, kind: EventKind, detail: dict | None = None) -> list[ProctorEvent]:
        """Готовое событие от оболочки, env-проверок или fusion.

        Окно подтверждения не применяется (условие уже установлено источником),
        но cooldown по виду нужен: оболочка умеет щёлкать фокусом десять раз в
        секунду, и отчёт не должен состоять из тридцати WINDOW_BLUR.

        Исключение — виды из `STATEFUL_EXTERNAL` (подключённое аудио-устройство).
        Это состояние, а не момент: env-проверка присылает находку каждый цикл,
        пока устройство подключено. Такие находки идут через тот же конечный
        автомат, что и покадровые наблюдения, поэтому одно подключение = один
        инцидент с растущей длительностью, а не одиннадцать записей с
        `duration = 0`. Снятие — по времени, см. `_expire`.
        """
        detail = dict(detail or {})
        key = self.normalize(kind)
        if key in self._suppressed:
            # Режим развёртывания сильнее источника: в classroom аудио-инцидента
            # не существует, даже если его прислали готовым (Р-10).
            self._mode_suppressed[key] = self._mode_suppressed.get(key, 0) + 1
            return []
        rule = self.rules.get(key)
        real_kind = rule.kind if rule is not None else _as_kind(kind)
        if real_kind is None:
            self._unknown[str(kind)] = self._unknown.get(str(kind), 0) + 1
            return []

        ts = _as_float(detail.get("ts"), time.time())
        severity = _as_severity(detail.pop("severity", None))
        detail.pop("ts", None)

        # Прогоны, по которым находка перестала приходить, закрываем здесь:
        # env-тикер — единственный источник, который гарантированно тикает
        # и в headless-режиме, без камеры и без кадров.
        self._expire(ts)

        if rule is not None and key in STATEFUL_EXTERNAL:
            return self._advance(key, rule, True, ts, detail,
                                 severity=severity, external=True)

        cooldown = self.cooldown_for(real_kind)
        last = self._external_fire.get(real_kind.value, 0.0)
        if cooldown > 0 and last and (ts - last) + _EPS < cooldown:
            return []
        self._external_fire[real_kind.value] = ts

        duration = _as_float(detail.get("duration"), 0.0)
        event = self._build(rule, real_kind, key, detail, duration, ts,
                            severity=severity, external=True)
        self._fired_total += 1
        return [event]

    # ---------------------------------------------------- составное правило
    def _note_composite(self, slot: str, active: bool, ts: float,
                        detail: dict[str, Any]) -> list[ProctorEvent]:
        """Собрать `speech_without_lip_motion` из речи и раскрытия рта.

        Речь даёт `active`, рот — числовое значение (`value`/`mouth_open_ratio`/
        `ratio`); если числа нет, `active` трактуется как «рот открыт».
        Условие активно, когда есть речь, рот закрыт и лицо в кадре: речь без
        лица — это уже NO_FACE, смешивать не надо.

        В classroom правила в реестре нет (Р-10): состояние слотов обновляем —
        оно дешёвое и пригодится при переключении режима, — но инцидент не
        рождается, а наблюдение уходит в счётчик подавленных.
        """
        if slot == "speech":
            self._speech = bool(active)
            self._speech_detail = {k: v for k, v in detail.items() if k != "ts"}
        else:
            value = detail.get("value", detail.get("mouth_open_ratio", detail.get("ratio")))
            if value is None:
                self._mouth_ratio = 1.0 if active else 0.0
            else:
                self._mouth_ratio = _as_float(value, 0.0)
            if "mouth_open_speech" in detail:
                self._mouth_threshold = _as_float(detail["mouth_open_speech"],
                                                  self._mouth_threshold)

        rule = self.rules.get("speech_without_lip_motion")
        if rule is None:
            name = "speech_without_lip_motion"
            self._mode_suppressed[name] = self._mode_suppressed.get(name, 0) + 1
            return []

        mouth_closed = self._mouth_ratio < self._mouth_threshold
        condition = bool(self._speech and mouth_closed and self._face_present)
        merged: dict[str, Any] = dict(self._speech_detail)
        merged.update({
            "mouth_open_ratio": round(self._mouth_ratio, 3),
            "mouth_threshold": round(self._mouth_threshold, 3),
            "speech": self._speech,
            "face_present": self._face_present,
        })
        return self._advance("speech_without_lip_motion", rule, condition, ts, merged)

    # -------------------------------------------------- конечный автомат
    def _advance(self, key: str, rule: Rule, active: bool, ts: float,
                 detail: dict[str, Any], *, severity: Severity | None = None,
                 external: bool = False) -> list[ProctorEvent]:
        st = self._states.get(key)
        if st is None:
            st = self._states[key] = _State(name=key)
        # время может прийти назад (разные потоки, разные часы) — не даём откатиться
        ts = max(ts, st.last_seen)
        st.last_seen = ts

        if active:
            if detail:
                st.detail.update({k: v for k, v in detail.items() if k != "ts"})
            if st.since is None:
                st.since = ts
            prev_active = st.last_active or ts
            # Шаг между наблюдениями ограничиваем окном отпускания: для кадров
            # это прежняя 1 с, а для прогона env-проверки (шаг 4-20 с) без
            # этого `held_sec` врал бы в четыре-двадцать раз.
            step_cap = max(1.0, self.release_for(key))
            st.held_total += max(0.0, min(ts - prev_active, step_cap))
            st.last_active = ts
            held = ts - st.since
            st.peak_duration = max(st.peak_duration, held)

            if st.fired:
                # условие продолжается: обновляем уже выпущенный инцидент
                self._refresh(rule, key, st, held)
                return []

            if held + _EPS < self.confirm_for(key):
                return []
            cooldown = self.cooldown_for(key)
            if cooldown > 0 and st.last_fire and (ts - st.last_fire) + _EPS < cooldown:
                # инцидент заслужен, но слишком рано — выпустим, когда cooldown истечёт
                return []

            event = self._build(rule, rule.kind, key, dict(st.detail), held, ts,
                                severity=severity, external=external)
            st.fired = True
            st.last_fire = ts
            st.open_event = event
            st.fired_total += 1
            self._fired_total += 1
            return [event]

        # условие не наблюдается
        if st.since is None and not st.fired:
            return []
        gap = ts - (st.last_active or ts)
        if gap + _EPS >= self.release_for(key):
            self._close(st)
        return []

    def _expire(self, now: float) -> int:
        """Закрыть прогоны env-проверок, находка по которым больше не приходит.

        env-проверка не присылает «условие снято»: находка просто пропадает из
        результата очередного прогона. Поэтому состояние снимается по времени —
        по тому же release-окну правила, что и у покадровых наблюдений, только
        окно берётся табличное (см. `release_for`). Возвращает число закрытых.
        """
        closed = 0
        for key in STATEFUL_EXTERNAL:
            st = self._states.get(key)
            if st is None or st.open_event is None:
                continue
            last = st.last_active or st.last_seen
            if not last:
                continue
            if (now - last) + _EPS >= self.release_for(key):
                self._close(st)
                closed += 1
        return closed

    def _observed_now(self) -> float:
        """Последнее время, которое движок ВИДЕЛ во входных наблюдениях.

        Намеренно не `time.time()`: часы задаёт источник наблюдений, и у
        мок-сайдкара, тестов и воспроизведения записи они свои. Сравнение
        собственного состояния с системными часами закрывало бы прогон сразу
        же, как только времена разъехались.
        """
        return max((st.last_seen for st in self._states.values()), default=0.0)

    def expire(self, now: float | None = None) -> int:
        """Публичная форма `_expire`: снять прогоны, по которым нечего продлевать.

        Вызывается сама из `push_external`, `push_observation` и аксессоров
        инцидентов; отдельный вход нужен тому, кто хочет закрыть инциденты
        перед формированием отчёта, не дожидаясь очередного наблюдения.
        Без аргумента берётся время последнего наблюдения (см. `_observed_now`).
        """
        return self._expire(self._observed_now() if now is None
                            else _as_float(now, self._observed_now()))

    def _refresh(self, rule: Rule, key: str, st: _State, held: float) -> None:
        """Продлить уже выпущенный инцидент: duration и текст, тот же объект."""
        ev = st.open_event
        if ev is None:
            return
        ev.duration = round(held, 2)
        ev.detail["duration_total"] = round(held, 2)
        ev.message = self._message(rule, key, ev.detail, held)

    def _close(self, st: _State) -> None:
        """Снять условие по гистерезису и зафиксировать итог для отчёта."""
        if st.open_event is not None:
            self._closed.append({
                "kind": st.open_event.kind.value,
                "code": code_for(st.open_event.kind),
                "id": st.open_event.id,
                "ts": st.open_event.ts,
                "duration": st.open_event.duration,
                "message": st.open_event.message,
            })
            if len(self._closed) > 500:
                del self._closed[:-500]
        st.since = None
        st.fired = False
        st.open_event = None
        st.detail.clear()
        # Условие снято — возможно, это конец прогона лестницы наблюдения.
        # Проверять надо после очистки состояния: `_ladder_release` смотрит
        # именно на то, держится ли ещё хоть одна ступень.
        self._ladder_release(st.name)

    # ------------------------------------------------------- сборка события
    def _build(self, rule: Rule | None, kind: EventKind, key: str,
               detail: dict[str, Any], duration: float, ts: float,
               severity: Severity | None = None,
               external: bool = False) -> ProctorEvent:
        weight = RISK_WEIGHTS.get(kind, 10.0)
        channel = rule.channel if rule is not None else Channel.SYSTEM
        sev = severity or (rule.severity if rule is not None else severity_for_weight(weight))
        confidence = self._confidence(key, detail)
        detail = dict(detail)
        # Код выводится из вида события, а не из того, что прислал источник:
        # это идентификатор для апелляции, подменять его детектор не вправе.
        detail["code"] = code_for(kind)
        detail.setdefault("confirm_sec", round(self.confirm_for(key), 2))
        if external:
            detail.setdefault("confirmed_by", "external")
        # источник мог прислать готовый русский текст (fusion, env-проверки) —
        # он подробнее шаблона, поэтому имеет приоритет
        ready = detail.get("message")
        message = (str(ready) if isinstance(ready, str) and ready.strip()
                   else self._message(rule, key, detail, duration))
        event = ProctorEvent(
            kind=kind,
            severity=sev,
            channel=channel,
            confidence=confidence,
            ts=ts,
            duration=round(float(duration), 2),
            message=message,
            detail=detail,
        )
        # Разметка лестницы ставится ПОСЛЕ сборки текста: `escalation` — это
        # служебная связь между событиями, а не подстановка в формулировку.
        self._ladder_annotate(kind, event, ts)
        return event

    # ------------------------------------------------- лестницы наблюдения
    def _ladder_annotate(self, kind: EventKind, event: ProctorEvent,
                         ts: float) -> None:
        """Отметить ступень лестницы в `detail["escalation"]`.

        Без этой разметки три вида про один телефон выглядят для risk-score
        как три независимых обвинения. Разметка не меняет ни вес, ни severity
        события — она говорит счёту, какие вклады описывают ОДНО наблюдение, а
        отчёту — какая запись какую уточняет.
        """
        spec = ladder_of(kind)
        if spec is None:
            return
        ladder, level = spec
        subject = ladder_subject(ladder, event.detail)
        run = self._ladder_run(ladder, subject, ts)
        weight = RISK_WEIGHTS.get(kind, 10.0)

        ann: dict[str, Any] = {
            "ladder": ladder,
            "ladder_ru": LADDER_LABELS.get(ladder, ladder),
            "run": run.key,
            "step": level + 1,
            "steps": ladder_steps(ladder),
            "weight": weight,
            "top_weight": ladder_top_weight(ladder),
        }
        if subject:
            ann["subject"] = subject

        lower = [row for row in run.rows if int(row.get("level", -1)) < level]
        if level > run.level:
            # Настоящая эскалация: старшее показание уточняет младшие. В risk
            # она даёт разницу ступеней, а не полный вес сверху.
            if lower:
                ann["refines"] = lower
                for ev_level, ev in run.objs:
                    if ev_level >= level:
                        continue
                    # Ключ ИМЕННО `refined_by`, а не `replaced_by`: у того в
                    # этой же системе другое значение — `mode_report()` ставит
                    # его там, где аудио-правила ЗАМЕНЕНЫ проверкой устройств.
                    # Ступень лестницы не заменена, а уточнена, и смешивать
                    # два смысла в одном ключе значит печатать в отчёте
                    # «Заменено записью» там, где запись никуда не делась.
                    ev.detail["refined_by"] = kind.value
                    ev.detail["refined_by_code"] = code_for(kind)
            run.level = level
            run.peak_kind = kind.value
            # Разрыв прогона (`ladder_gap`) отсчитывается от последней НОВОЙ
            # ступени. Повтор уже достигнутой ступени прогон не продлевает:
            # иначе поток повторов с интервалом короче `ladder_gap` (20 с) и
            # длиннее cooldown вида (12 с) держал бы прогон бессмертным и
            # сворачивал в ноль всё, что в него попадёт. Для покадровых
            # лестниц прогон и так закрывает `_ladder_release` по снятию
            # показания, но у мгновенных внешних ступеней состояния нет, и
            # время — единственный признак конца прогона.
            run.last_ts = max(run.last_ts, ts)
        else:
            # Ступень не выше уже достигнутой: то же наблюдение, показанное
            # повторно (или шаг назад). Нового вклада в risk не даёт, но в
            # журнале остаётся — по ней видно, как эпизод развивался.
            ann["superseded_by"] = run.peak_kind
            ann["peak_step"] = run.level + 1
            self._ladder_folded[ladder] = self._ladder_folded.get(ladder, 0) + 1

        run.rows.append({
            "kind": kind.value,
            "code": code_for(kind),
            "id": event.id,
            "ts": round(float(ts), 3),
            "level": level,
            "weight": weight,
        })
        if len(run.rows) > 16:
            del run.rows[:-16]
        run.objs.append((level, event))
        if len(run.objs) > 8:
            del run.objs[:-8]
        event.detail["escalation"] = ann

    def _ladder_run(self, ladder: str, subject: str, ts: float) -> _LadderRun:
        """Текущий прогон лестницы или новый, если прежний закончился.

        Прогон продолжается, пока он не закрыт по снятию условий
        (`_ladder_release`), пока с последней ступени прошло не больше
        `ladder_gap`, и пока речь идёт о том же объекте. Молчание источника о
        субъекте (пустая строка) трактуется в пользу продолжения: сейчас
        PHONE_IN_FRAME приходит без `track_id`, и если считать его «другим
        объектом», двойной счёт вернётся сам собой.
        """
        run = self._ladder_runs.get(ladder)
        if run is not None and not run.ended:
            fresh = (ts - run.last_ts) <= ladder_gap(ladder) + _EPS
            if fresh and ladder_same_subject(run.subject, subject, ladder):
                # Признаки накапливаются: находки об одной программе называют
                # разные подмножества её процессов.
                run.subject = ladder_merge_subject(run.subject, subject)
                return run
        seq = self._ladder_seq.get(ladder, 0) + 1
        self._ladder_seq[ladder] = seq
        run = _LadderRun(
            ladder=ladder,
            seq=seq,
            key=f"{LADDER_RUN_PREFIX}{ladder}/{subject or '-'}#{seq}",
            subject=subject,
            first_ts=ts,
            last_ts=ts,
        )
        self._ladder_runs[ladder] = run
        return run

    def _ladder_release(self, name: str) -> None:
        """Закрыть прогон, когда снято ТО САМОЕ показание, которым он оценён.

        Вызывается из `_close`: условие снято по гистерезису.

        Прогон оценён в risk-score по высшей достигнутой ступени (`peak_kind`).
        Пока это показание наблюдается, всё остальное про тот же объект —
        уточнение уже начисленного, и начислять его второй раз нельзя. Как
        только оно СНЯТО, требование прогона исчерпано: следующее такое же
        наблюдение — новый эпизод, и он обязан быть начислен заново.

        РАНЬШЕ прогон закрывался только когда снята КАЖДАЯ ступень лестницы, и
        это давало рабочий обход на обеих покадровых лестницах:

        * телефон, который не выносят из кадра. `PHONE_IN_FRAME` держится —
          прогон бессмертен. Разрыв прогона (`ladder_gap` = 20 с) больше, чем
          cooldown старших ступеней (12 с), поэтому каждое следующее «поднял →
          навёл» попадало внутрь живого прогона и давало НОЛЬ. За 5 минут
          20 циклов: пик 38.85, финал 4.18, вердикт `none` — счёт ПАДАЛ с
          каждым следующим использованием телефона;
        * взгляд. `GAZE_DOWN` и `GAZE_SIDE` стоят на одной ступени, и пока
          студент переводит взгляд между ними, не возвращая его на экран,
          вторая ступень всегда активна — прогон не закрывался никогда.
          Шесть минут взгляда мимо экрана давали пик 8.00, тогда как честный
          студент, который периодически смотрит на экран, получал `lock`.
          Правило инвертировало стимул: непрерывное нарушение дешевле
          прерывистого.

        Теперь снятие `peak_kind` закрывает прогон независимо от того, что ещё
        наблюдается. Свёртка ОДНОГО эпизода не страдает: пока телефон наведён
        на экран, показание держится, и младшие ступени про тот же телефон
        по-прежнему уточняют начисленное, а не добавляют к нему. Повтор того же
        показания внутри живого прогона (взгляд вниз при уже отведённом
        взгляде) тоже сворачивается как прежде — это буквально одновременное
        наблюдение.

        Для мгновенных внешних ступеней состояния нет, и прогон закрывается по
        времени (`ladder_gap`) — эта ветка не менялась.
        """
        rule = self.rules.get(name)
        spec = ladder_of(rule.kind if rule is not None else name)
        if spec is None:
            return
        ladder = spec[0]
        run = self._ladder_runs.get(ladder)
        if run is None or run.ended:
            return
        # Прогон без выпущенных ступеней закрывать нечего и не от чего.
        if not run.peak_kind:
            return
        st = self._states.get(self.normalize(run.peak_kind))
        if st is not None and (st.since is not None or st.open_event is not None):
            # Показание, которым прогон оценён, ещё наблюдается.
            return
        run.ended = True
        run.objs.clear()

    def escalation_report(self) -> dict[str, Any]:
        """Лестницы наблюдения: объявленное правило и что происходило.

        Нужен отчёту и защите: число «риск 45 вместо 105» надо уметь объяснить
        не словами, а таблицей — какие виды объявлены ступенями одного
        наблюдения и сколько раз правило сработало.
        """
        return {
            "rule": ("в risk-score идёт вклад только высшей достигнутой ступени "
                     "прогона лестницы; младшие ступени остаются в журнале с "
                     "пометкой refined_by"),
            "ladders": {
                ladder: {
                    "label": LADDER_LABELS.get(ladder, ladder),
                    "steps": [[kind.value for kind in level_kinds]
                              for level_kinds in levels],
                    "top_weight": ladder_top_weight(ladder),
                    "gap_sec": ladder_gap(ladder),
                }
                for ladder, levels in ESCALATION_LADDERS.items()
            },
            "runs_total": dict(self._ladder_seq),
            "folded_steps": dict(self._ladder_folded),
            "active": [
                {
                    "ladder": run.ladder,
                    "run": run.key,
                    "subject": run.subject,
                    "peak_kind": run.peak_kind,
                    "peak_step": run.level + 1,
                    "steps_fired": [row["kind"] for row in run.rows],
                }
                for run in self._ladder_runs.values() if not run.ended
            ],
        }

    def _confidence(self, key: str, detail: dict[str, Any]) -> float:
        """Уверенность инцидента.

        Приоритет — число от детектора (`conf`/`confidence`). Если его нет, но
        известно, во сколько раз превышен персональный порог, уверенность
        растёт от 0.55 на самой границе до 1.0 при двукратном превышении:
        пограничный случай не должен весить столько же, сколько очевидный.
        """
        for field_name in ("conf", "confidence", "score"):
            if field_name in detail:
                return _clamp(_as_float(detail[field_name], 1.0), 0.05, 1.0)
        ratio = self._dev_ratio(key, detail)
        if ratio is not None:
            return _clamp(0.55 + 0.45 * (ratio - 1.0), 0.55, 1.0)
        return 1.0

    def _dev_ratio(self, key: str, detail: dict[str, Any]) -> float | None:
        """Во сколько раз превышен порог. None — посчитать не из чего."""
        best: float | None = None
        for dev_key in DEV_KEYS:
            if dev_key in detail:
                value = abs(_as_float(detail[dev_key], 0.0))
                best = value if best is None else max(best, value)
        if best is not None and best > 0:
            return best
        spec = DEV_FALLBACK.get(key)
        if spec is None:
            return None
        keys, cfg_key, default_limit = spec
        limit = abs(_as_float(self.cfg.get(cfg_key), default_limit)) or default_limit
        raw: float | None = None
        for k in keys:
            if k in detail:
                value = abs(_as_float(detail[k], 0.0))
                raw = value if raw is None else max(raw, value)
        if raw is None or limit <= 0:
            return None
        return raw / limit

    def _message(self, rule: Rule | None, key: str, detail: dict[str, Any],
                 duration: float) -> str:
        if rule is None:
            return key.upper()
        try:
            return rule.message_template.format_map(
                self._message_vars(key, detail, duration))
        except Exception:  # шаблон кривой — отчёт всё равно должен собраться
            return rule.message_template

    def _message_vars(self, key: str, detail: dict[str, Any],
                      duration: float) -> _SafeVars:
        """Значения для подстановки в message_template.

        Все значения — уже строки: шаблоны пишутся без форматных спецификаторов,
        поэтому отсутствующий ключ безопасно превращается в пустую строку.

        Необязательные уточнения собираются как ЦЕЛЫЕ фразы вместе со своими
        знаками препинания (`{conf_area}` -> « (уверенность 82%, площадь кадра
        2.1%)»). Иначе внешнее событие без длительности и уверенности — а
        именно такие шлёт оболочка и env-проверки — дало бы в отчёте «на 0.0 с»
        и пустые скобки.
        """
        out = _SafeVars()
        for name, value in detail.items():
            out[name] = _fmt(value)

        dur = max(duration, 0.0)
        has_dur = dur >= 0.05
        out["duration"] = f"{dur:.1f}"
        out["duration_int"] = str(int(round(dur)))
        out["held_na"] = f" на {dur:.1f} с" if has_dur else ""
        out["held"] = f" {dur:.1f} с" if has_dur else ""
        out["held_dash"] = f" — {dur:.1f} с" if has_dur else ""
        out["held_par"] = f" ({dur:.1f} с)" if has_dur else ""
        out["held_for"] = f" и держится {dur:.1f} с" if has_dur else ""
        #: Основная форма для текстов по формуле гайда: «...телефон, 3.2 с».
        out["held_comma"] = f", {dur:.1f} с" if has_dur else ""

        conf = self._confidence(key, detail)
        out["conf_pct"] = str(int(round(conf * 100)))

        ratio = self._dev_ratio(key, detail)
        if ratio is not None and ratio >= 1.05:
            out["dev_note"] = f" (базовое отклонение превышено в {ratio:.1f} раза)"
            # Контекст-число для формулы гайда: насколько эпизод выходит за
            # ПЕРСОНАЛЬНУЮ норму этого студента, а не за абстрактный порог.
            out["dev_phrase"] = f", отклонение {_times_phrase(ratio)} выше базового"
            out["dev_ratio"] = f"{ratio:.1f}"
        else:
            out["dev_note"] = ""
            out["dev_phrase"] = ""
            out["dev_ratio"] = "" if ratio is None else f"{ratio:.1f}"

        # Задержка между сигналом и ответом — главное число fusion-событий.
        lag_sec: float | None = None
        for lag_key, scale in (("lag_sec", 1.0), ("lag_ms", 0.001),
                               ("delay_ms", 0.001), ("gap_sec", 1.0)):
            if lag_key in detail:
                lag_sec = abs(_as_float(detail[lag_key], 0.0)) * scale
                break
        out["lag_note"] = (f" (ответ через {lag_sec:.1f} с)"
                           if lag_sec is not None and lag_sec > 0 else "")

        zone = str(detail.get("zone", detail.get("gaze_zone", "")) or "").lower()
        out["zone_ru"] = "вниз" if key == "gaze_down" else ZONE_RU.get(zone, "в сторону")

        label = str(detail.get("label", "") or "").lower()
        out["label_ru"] = LABEL_RU.get(label, label or "неопознанный предмет")

        # уверенность детектора и площадь — только если их реально прислали
        pieces: list[str] = []
        for field_name in ("conf", "confidence", "score"):
            if field_name in detail:
                pieces.append(f"уверенность {int(round(_as_float(detail[field_name], 0.0) * 100))}%")
                break
        if "area_ratio" in detail:
            area = _as_float(detail["area_ratio"], 0.0) * 100
            out["area_pct"] = f"{area:.1f}"
            pieces.append(f"площадь кадра {area:.1f}%")
        out["conf_area"] = f" ({', '.join(pieces)})" if pieces else ""

        out["threshold_pct"] = str(int(round(self._identity_threshold * 100)))
        if "similarity" in detail:
            sim = int(round(_as_float(detail["similarity"], 0.0) * 100))
            out["similarity_pct"] = str(sim)
            out["sim_note"] = (f": близость {sim}% при пороге "
                               f"{int(round(self._identity_threshold * 100))}%")
        if "streak" in detail:
            streak = int(_as_float(detail["streak"], 0.0))
            if streak > 0:
                word = _plural(streak, ("проверка", "проверки", "проверок"))
                out["streak_note"] = f", {streak} {word} подряд"
        # SECOND_FACE по смыслу — минимум два лица. Если источник события не
        # передал число (например, push_external из оболочки с пустым detail),
        # шаблон без этой подстановки даёт «В кадре  лица одновременно» —
        # дыру в тексте, который уйдёт в отчёт. Подставляем осмысленный минимум.
        faces_n = int(_as_float(detail.get("faces"), 0.0))
        faces_n = faces_n if faces_n >= 2 else 2
        out["faces"] = str(faces_n)
        out["faces_word"] = _plural(faces_n, ("лицо", "лица", "лиц"))
        if "no_blink_sec" in detail:
            out["no_blink_note"] = f" {_as_float(detail['no_blink_sec'], 0.0):.1f} с"

        angles = []
        if "head_yaw" in detail:
            angles.append(f"поворот {_as_float(detail['head_yaw'], 0.0):.0f}°")
        if "head_pitch" in detail:
            angles.append(f"наклон {_as_float(detail['head_pitch'], 0.0):.0f}°")
        out["angles_note"] = (", " + ", ".join(angles)) if angles else ""

        audio = []
        if "rms" in detail:
            audio.append(f"громкость {_as_float(detail['rms'], 0.0):.3f}")
        if "conf" in detail or "confidence" in detail:
            audio.append(f"уверенность {out['conf_pct']}%")
        out["rms_conf"] = f" ({', '.join(audio)})" if audio else ""

        out["mouth_threshold"] = f"{self._mouth_threshold:.2f}"
        if "mouth_open_ratio" in detail:
            out["mouth_note"] = (
                f" (раскрытие рта {_as_float(detail['mouth_open_ratio'], 0.0):.2f} "
                f"при пороге {self._mouth_threshold:.2f})")

        if "length" in detail:
            length = int(_as_float(detail["length"], 0.0))
            out["length_note"] = (f", {length} "
                                  f"{_plural(length, ('символ', 'символа', 'символов'))}")
        if "displays" in detail:
            out["displays_note"] = f" ({_fmt(detail['displays'])})"
        if detail.get("combo"):
            out["combo_note"] = f" {_fmt(detail['combo'])}"
        if detail.get("stage"):
            out["stage_note"] = f" ({_fmt(detail['stage'])})"
        if detail.get("sensor"):
            out["sensor_note"] = f" ({_fmt(detail['sensor'])})"

        # Градуированные контекст/обстоятельство/действие от env-проверок.
        # Собираются как ЦЕЛЫЕ предложения со своей точкой: без них текст был бы
        # одинаковым для гарнитуры, виртуального вывода и монитора по HDMI,
        # а «понятное действие» из формулы гайда оказалось бы неисполнимым.
        for src, var, fallback in (
            ("reason", "reason_sentence", ""),
            ("when", "when_sentence", ""),
            ("advice", "advice_sentence", "Откройте запись для контекста."),
        ):
            text = str(detail.get(src) or "").strip()
            if text:
                out[var] = " " + text[0].upper() + text[1:].rstrip(".") + "."
            else:
                out[var] = f" {fallback}" if fallback else ""

        # Для env-проверок имя процесса/устройства лежит под разными ключами.
        #
        # "summary" обязан стоять перед "reason". У находок, где совпадений
        # несколько (запрещённые процессы, аудио-устройства), имена лежат
        # ВЛОЖЕННО — в ai_clients[].name, messengers[].name и т.п., — а на верхнем
        # уровне есть только обезличенное reason вроде «Запущено ПО, через которое
        # можно получить подсказку». Без summary сообщение не называет ни одной
        # программы: студент не знает, что закрывать, преподаватель не может
        # проверить. Именно так и вышло на живом прогоне 07.10.
        # summary собирается в env_checks и уже содержит имена, pid и число процессов.
        for candidate in ("name", "process", "device", "software", "app", "summary", "reason"):
            value = detail.get(candidate)
            if value:
                out["name_or_detail"] = _fmt(value)
                out["name_par"] = f" ({_fmt(value)})"
                out["name_colon"] = f": {_fmt(value)}"
                break
        else:
            out["name_or_detail"] = "подробности в отчёте"
        return out

    # ------------------------------------------------------------- телеметрия
    def open_incidents(self) -> list[dict[str, Any]]:
        """Инциденты, условие которых держится прямо сейчас.

        Прогоны env-проверок снимаются здесь же: иначе на машине, где находок
        больше нет вообще (а значит нет и вызовов push_*), «гарнитура
        подключена» висела бы открытой до конца сессии.
        """
        self.expire()
        rows = []
        for st in self._states.values():
            if st.open_event is None:
                continue
            rows.append({
                "kind": st.open_event.kind.value,
                "code": code_for(st.open_event.kind),
                "id": st.open_event.id,
                "ts": st.open_event.ts,
                "duration": st.open_event.duration,
                "message": st.open_event.message,
            })
        return rows

    def closed_incidents(self) -> list[dict[str, Any]]:
        """Итоговые интервалы снятых инцидентов — отчёт берёт длительности отсюда."""
        self.expire()
        return list(self._closed)

    def mode_report(self) -> dict[str, Any]:
        """ФАКТ «режим выключил правила» — не покадровый счётчик.

        `suppressed_by_mode` растёт на каждом кадре (15 fps x 2 ч = 108 000) и
        поэтому читается как поломка, а не как штатная работа Р-10. Здесь тот
        же факт изложен один раз и пригоден для вставки в `hello`, `status` и
        отчёт: правила не «молчат», их НЕТ в реестре движка, а закрываемая ими
        угроза закрыта детерминированной проверкой устройств.
        """
        suppressed = sorted(self._suppressed)
        return {
            "exam_mode": self.exam_mode,
            "suppressed_rules": suppressed,
            "reason": CLASSROOM_AUDIO_OFF_REASON if suppressed else "",
            "replaced_by": "AUDIO_DEVICE_CONNECTED" if suppressed else "",
            "rules_registered": len(self.rules),
        }

    def stats(self) -> dict[str, Any]:
        """Диагностика движка: сколько чего выпущено и что не опознано."""
        return {
            "fired_total": self._fired_total,
            "by_name": {name: st.fired_total for name, st in self._states.items()
                        if st.fired_total},
            "held_sec": {name: round(st.held_total, 1) for name, st in self._states.items()
                         if st.held_total >= 0.5},
            "open": self.open_incidents(),
            "closed": len(self._closed),
            "unknown_names": dict(self._unknown),
            "mouth_threshold": self._mouth_threshold,
            "exam_mode": self.exam_mode,
            "rules_registered": len(self.rules),
            # Факт в человекочитаемом виде — его и показывать на защите.
            "mode": self.mode_report(),
            # Лестницы наблюдения: что объявлено эскалацией и как часто
            # младшая ступень оказывалась уточнена старшей.
            "escalation": self.escalation_report(),
            # не ошибка, а штатная работа режима: источник шлёт, движок не ведёт
            "suppressed_by_mode": dict(self._mode_suppressed),
        }


# ===========================================================================
# мелкие помощники
# ===========================================================================
def _cfg_exam_mode(cfg: dict) -> Any:
    """Найти `exam_mode` в конфиге.

    Конфиг приходит и плоским (`ProctorConfig.to_dict()`), и секциями
    (`{"events": {...}}`, `{"audio": {...}}`), поэтому смотрим оба уровня.
    Ничего не нашли — классная аудитория, режим по умолчанию.
    """
    for key in ("exam_mode", "mode"):
        if cfg.get(key):
            return cfg[key]
    for section in ("events", "audio", "env"):
        block = cfg.get(section)
        if isinstance(block, dict) and block.get("exam_mode"):
            return block["exam_mode"]
    return DEFAULT_EXAM_MODE


def _times_phrase(ratio: float) -> str:
    """«в 2.4 раза», «в 3 раза», «в 5 раз» — согласование для текста отчёта."""
    if abs(ratio - round(ratio)) < 0.05:
        n = int(round(ratio))
        return f"в {n} {_plural(n, ('раз', 'раза', 'раз'))}"
    return f"в {ratio:.1f} раза"


def _as_kind(value: Any) -> EventKind | None:
    if isinstance(value, EventKind):
        return value
    try:
        return EventKind(str(getattr(value, "value", value)).strip().upper())
    except (ValueError, TypeError):
        return None


def _as_severity(value: Any) -> Severity | None:
    if isinstance(value, Severity):
        return value
    if value is None:
        return None
    try:
        return Severity(str(value).strip().lower())
    except (ValueError, TypeError):
        return None


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    """Русское согласование числительного: 1 проверка, 3 проверки, 5 проверок."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _fmt(value: Any) -> str:
    """Человекочитаемое представление значения detail для message."""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float):
        return f"{value:.0f}" if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple)):
        return ", ".join(_fmt(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={_fmt(v)}" for k, v in value.items())
    return str(value)


def rule_names(exam_mode: Any = None) -> Iterable[str]:
    """Имена известных наблюдений — удобно для тестов и отладки.

    Без аргумента — вся таблица; с режимом — только то, что в нём регистрируется.
    """
    if exam_mode is None:
        return RULES.keys()
    return rules_for_mode(exam_mode).keys()


__all__ = ["EventEngine", "Rule", "RULES", "ALIASES", "COMPOSITE_INPUTS",
           "EVENT_CODES", "UNKNOWN_CODE", "code_for", "LADDER_RUN_PREFIX",
           "REMOTE_ONLY_RULES", "STATEFUL_EXTERNAL", "EXAM_MODES",
           "CLASSROOM_AUDIO_OFF_REASON", "DEFAULT_EXAM_MODE",
           "normalize_exam_mode", "rules_for_mode",
           "severity_for_weight", "rule_names"]

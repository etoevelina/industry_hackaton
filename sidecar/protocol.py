"""
Единый контракт обмена между Python-сайдкаром (CV/аудио) и Electron-оболочкой.

ЭТОТ ФАЙЛ — ИСТОЧНИК ИСТИНЫ. Любой модуль, который порождает или потребляет
события, импортирует типы отсюда и не изобретает свои строки.

Транспорт: WebSocket на 127.0.0.1, JSON-сообщения, по одному на фрейм.
Направление указано в докстринге каждого типа сообщения.
"""
from __future__ import annotations

import hashlib
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
    EVENT = "event"              # зафиксированный инцидент (+ screen: bool, см. event_message)
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
    COMMAND = "command"            # {"name": <COMMAND_NAMES>, ...}
    #: Снимок ОКНА ЭКЗАМЕНА к событию, разосланному с `screen: true`:
    #: {"event_id", "mime", "data_b64", "width", "height", "captured_at",
    #: "source"} или, если снять не удалось, то же без `data_b64` и с `error`.
    #: Снимается только представление экзамена, не рабочий стол.
    SCREEN_EVIDENCE = "screen_evidence"


#: Полный словарь команд оболочки. `proctor_lock` / `proctor_release` — решение
#: человека над приостановленным экзаменом, оба требуют `actor` (кто решил) и
#: оба оставляют неизгладимую запись в хеш-цепочке. `deliver_package` —
#: повторить копирование последнего пакета в папку проктора (`--deliver-to`);
#: допустима только после завершения сессии, итог приходит в `status.package.delivery`.
COMMAND_NAMES: tuple[str, ...] = (
    "snapshot", "reset_risk", "export_report", "proctor_lock", "proctor_release",
    "deliver_package",
)


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
    #: Фактический режим запуска оболочки: какие защиты машины реально
    #: применены, а какие сняты флагами (`--no-lockdown`, `--no-kiosk`,
    #: `--allow-multi-display`). Не обвинение студента, а УСЛОВИЯ наблюдения,
    #: поэтому вес 0. Без этой записи выключенные блокировки видны только по
    #: отсутствию SHORTCUT_BLOCKED и WINDOW_BLUR, что неотличимо от честной
    #: сессии, в которой студент просто ничего не нажимал.
    SHELL_CONFIG = "SHELL_CONFIG"

    # --- профиль экзамена: белый список источников (У-13, правка .seb) ---
    #: Попытка ОТКРЫТЬ источник вне белого списка: навигация, которую оболочка
    #: не пропустила. В `detail` — ПОЛНЫЙ адрес: ценность записи не в запрете
    #: (профиль обходим, он улика), а в доказательстве попытки.
    NAVIGATION_OFF_PROFILE = "NAVIGATION_OFF_PROFILE"
    #: Подзапрос вне белого списка: шрифт, картинка, скрипт, аналитика со
    #: стороннего адреса. Чаще техническая мелочь страницы, а не действие
    #: студента, поэтому вес малый — см. RISK_WEIGHTS.
    SUBRESOURCE_OFF_PROFILE = "SUBRESOURCE_OFF_PROFILE"
    #: Профиль применён: в `detail` хеш, источник, состояние подписи.
    #: Информационное событие, вес 0 — это УСЛОВИЯ экзамена, не поведение.
    EXAM_PROFILE_APPLIED = "EXAM_PROFILE_APPLIED"
    #: Профиль не применён: правила экзамена проктором не задавались. Тоже
    #: факт, и тоже обязан быть в журнале и в цепочке: именно необязательность
    #: проверки сломала Safe Exam Browser.
    EXAM_PROFILE_ABSENT = "EXAM_PROFILE_ABSENT"
    #: Профиль применён С РАЗРЕШЁННЫМИ поисковиками (или ИИ-ассистентами).
    #: Отдельный вид, а не поле в предыдущем: разрешённый поиск означает, что
    #: готовый ответ доступен студенту прямо в выдаче, и в шапке отчёта это
    #: должно стоять строкой, а не прятаться во вложенном словаре.
    EXAM_PROFILE_SEARCH_ALLOWED = "EXAM_PROFILE_SEARCH_ALLOWED"

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

    # --- решения над экзаменом (их принимает человек, а не детектор) ---
    #: Автоматика дошла до порога блокировки и ОСТАНОВИЛАСЬ, запросив проктора.
    LOCK_REVIEW_REQUESTED = "LOCK_REVIEW_REQUESTED"
    #: Проктор решил: подтвердил блокировку или снял её. В detail — кто и когда.
    PROCTOR_DECISION = "PROCTOR_DECISION"
    #: Накопленный риск обнулён. Запись неизгладима: кто сбросил и когда.
    RISK_RESET = "RISK_RESET"


#: Служебные записи — описание условий наблюдения, а не наблюдение за
#: студентом. Каноническая копия набора, который отчёт держит как
#: `SERVICE_KINDS` (storage/report.py): в таблицу инцидентов, в таймлайн и в
#: integrity score они не идут, кадр камеры и снимок окна экзамена к ним не
#: снимаются (`evidence_skipped = "service"`). `EXAM_PROFILE` — имя из
#: журналов прежних версий, вида с таким значением в EventKind больше нет.
#:
#: Решения над экзаменом (LOCK_REVIEW_REQUESTED, PROCTOR_DECISION, RISK_RESET)
#: сюда НЕ входят: вес у них нулевой, но кадр момента решения — доказательство.
SERVICE_EVENT_KINDS: frozenset[str] = frozenset({
    EventKind.SESSION_STARTED.value,
    EventKind.SESSION_ENDED.value,
    EventKind.CALIBRATION_DONE.value,
    EventKind.SHELL_CONFIG.value,
    EventKind.EXAM_PROFILE_APPLIED.value,
    EventKind.EXAM_PROFILE_ABSENT.value,
    EventKind.EXAM_PROFILE_SEARCH_ALLOWED.value,
    "EXAM_PROFILE",
})


def is_service_kind(kind: Any) -> bool:
    """Служебная ли запись (см. SERVICE_EVENT_KINDS). Принимает EventKind и строку."""
    return str(getattr(kind, "value", kind) or "").strip().upper() in SERVICE_EVENT_KINDS


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
    # Режим запуска — описание условий, а не поведение студента: в risk-score
    # он не вкладывается (иначе ослабляющий флаг сам приостанавливал бы
    # экзамен), но в журнале, в цепочке и в отчёте стоит обязательно.
    EventKind.SHELL_CONFIG: 0.0,

    # --- профиль экзамена -------------------------------------------------
    # НАВИГАЦИЯ ВНЕ БЕЛОГО СПИСКА — 20. Вес выбран по тому, что событие
    # означает: оболочка переход НЕ ПРОПУСТИЛА, содержимого студент не
    # получил, и ценность записи в доказательстве попытки. Рядом стоят
    # DEVTOOLS_ATTEMPT (25) и WINDOW_BLUR (15): это тот же класс «намеренное
    # действие в обход правил, остановленное оболочкой». 20 даёт severity
    # medium, то есть эпизод попадает и в отчёт, и в запись клипа
    # (`evidence_min_severity = "medium"`; кадр камеры снимается к любому
    # неслужебному событию). Арифметика осознанная и проверена:
    # три отдельные попытки подряд дают 52-58 при полураспаде 90 с и пороге
    # приостановки 60 — экзамен подходит к краю, но переводит его в ожидание
    # проктора ЧЕТВЁРТАЯ попытка, а не третья. Больше ставить нельзя: внешняя ссылка в тексте
    # задания — обычное дело в Moodle, и один щелчок по ней не должен
    # приостанавливать экзамен.
    EventKind.NAVIGATION_OFF_PROFILE: 20.0,
    # ПОДЗАПРОС ВНЕ БЕЛОГО СПИСКА — 2. Это почти всегда техника страницы:
    # шрифт с CDN, счётчик, картинка. Вес обязан быть настолько мал, чтобы
    # канал НЕ МОГ сам довести экзамен даже до предупреждения: при cooldown
    # 30 с (см. config._default_cooldowns) установившийся вклад потока таких
    # находок — около 10 из 30 до WARN. Иначе обвинение приходило бы из
    # устройства чужой страницы, а не из действия студента — ровно та
    # ошибка, против которой сделаны лестницы наблюдения ниже.
    EventKind.SUBRESOURCE_OFF_PROFILE: 2.0,
    # Состояние профиля — описание УСЛОВИЙ экзамена, как и SHELL_CONFIG:
    # в risk-score не вкладывается (иначе отсутствие профиля само
    # приостанавливало бы экзамен), но в журнале, в цепочке и в ШАПКЕ отчёта
    # стоит обязательно.
    EventKind.EXAM_PROFILE_APPLIED: 0.0,
    EventKind.EXAM_PROFILE_ABSENT: 0.0,
    EventKind.EXAM_PROFILE_SEARCH_ALLOWED: 0.0,

    EventKind.PASTE_BURST: 30.0,
    EventKind.TYPING_ANOMALY: 15.0,

    EventKind.FUSION_GAZE_THEN_ANSWER: 55.0,
    EventKind.FUSION_BLUR_THEN_ANSWER: 55.0,
    EventKind.FUSION_PHONE_THEN_ANSWER: 65.0,

    EventKind.SESSION_STARTED: 0.0,
    EventKind.SESSION_ENDED: 0.0,
    EventKind.CALIBRATION_DONE: 0.0,
    EventKind.SENSOR_LOST: 20.0,

    # Решения над экзаменом не наблюдение, а действие над ним: в risk-score и в
    # integrity они не вкладываются (вес 0), но в журнале и в отчёте стоят.
    EventKind.LOCK_REVIEW_REQUESTED: 0.0,
    EventKind.PROCTOR_DECISION: 0.0,
    EventKind.RISK_RESET: 0.0,
}

#: Пороги реакции по накопленному risk-score.
RISK_WARN = 30.0
RISK_PAUSE = 60.0
RISK_LOCK = 90.0


# --------------------------------------------------------------------------
# Лестницы наблюдения: эскалация ОДНОГО наблюдения, а не новое обвинение
# --------------------------------------------------------------------------
# Разбор худшего сценария показал дыру не в весах, а в самом способе их
# складывать. Телефон, который попал в кадр, был поднят к лицу и наведён на
# экран за полторы секунды, давал PHONE_IN_FRAME (25) + PHONE_RAISED (35) +
# PHONE_AIMED_AT_SCREEN (45) = 105 — то есть порог блокировки с ОДНОГО
# наблюдаемого объекта. Между этими тремя видами нет независимости: это три
# последовательных показания про один и тот же телефон, и каждое следующее
# УТОЧНЯЕТ предыдущее, а не добавляет к нему новый факт. Складывать их — то же
# самоподтверждение, которое уже устранено для связок (см. engine/fusion.py,
# «ОДНО НАБЛЮДЕНИЕ — ОДНО ОБВИНЕНИЕ») и для вставки с ритмом набора
# (см. потолок наблюдения в engine/risk.py).
#
# Правило: виды, стоящие на одной лестнице, дают в risk-score вклад ТОЛЬКО
# высшей достигнутой ступени. 25 -> 35 -> 45 вместо 25 -> 60 -> 105. Настоящая
# эскалация по-прежнему повышает тревогу (высшая ступень весит больше низшей) и
# ровно на разницу ступеней, а возврат на ступень ниже её не обнуляет: снятых
# начислений не бывает, бывает только отсутствие новых.
#
# Доказательная база при этом не беднеет. Младшее событие остаётся в журнале и
# в хеш-цепочке со своим кодом, временем и длительностью; оно получает пометку
# `refined_by` (чем уточнено), а старшее — список `refines` (что именно оно
# уточняет). Апелляция по коду VIS_101 по-прежнему поднимает тот же эпизод.
#
# Ключи называются «уточнено», а не «заменено», намеренно: `replaced_by` в этой
# же системе занят другим смыслом — им `EventEngine.mode_report()` помечает
# аудио-правила, ЗАМЕНЁННЫЕ в аудитории проверкой устройств (Р-10). Ступень
# лестницы ничем не заменена: она осталась в журнале со своим кодом и временем.
#
# Ступени перечислены от младшей к старшей. На ОДНОЙ ступени стоят виды,
# которые не усиливают друг друга: взгляд вниз и взгляд в сторону — разные
# направления одного уровня «взгляд ушёл с экрана».
ESCALATION_LADDERS: dict[str, tuple[tuple[EventKind, ...], ...]] = {
    # Один телефон: виден -> поднят к лицу -> держится вертикально и
    # приближается. Ступени считает одна эвристика в detectors/objects.py, и
    # старшая внутри себя ПЕРЕПРОВЕРЯЕТ младшую (_eval_aimed вызывает
    # _eval_raised), то есть буквально то же наблюдение, но строже.
    "phone": (
        (EventKind.PHONE_IN_FRAME,),
        (EventKind.PHONE_RAISED,),
        (EventKind.PHONE_AIMED_AT_SCREEN,),
    ),
    # Один взгляд: ушёл с экрана -> ушёл за пределы карты монитора. Обе ступени
    # считаются из одних и тех же нормированных девиаций, разными порогами
    # (`gaze_dev_limit` и `gaze_off_dev_limit`), поэтому «вне экрана» — это
    # «в сторону», только дальше.
    "gaze_away": (
        (EventKind.GAZE_DOWN, EventKind.GAZE_SIDE),
        (EventKind.GAZE_OFF_SCREEN,),
    ),
    # Одна вставка: факт вставки из буфера (видит оболочка) -> вставленный блок
    # оказался крупным (видит телеметрия ответа). Второе — уточнение первого,
    # а не второе нажатие.
    "paste": (
        (EventKind.CLIPBOARD_PASTE,),
        (EventKind.PASTE_BURST,),
    ),
    # Одна программа захвата: запущена (пишет экран) -> к ней найдено ещё и
    # устройство виртуальной камеры. Проверки в env_checks.py уже выстроены
    # слоями — check_virtual_camera отдаёт универсальные стримеры (OBS,
    # Streamlabs) в check_screen_recording, пока нет устройства или плагина, —
    # но когда устройство есть, ОДИН процесс OBS попадает в обе находки и
    # приносит 70 + 35 = 105. Лестница срабатывает только при совпадении
    # субъекта (см. LADDER_SUBJECT_KEYS): ffmpeg, пишущий экран, и отдельная
    # ManyCam — это два разных наблюдения, и они складываются как прежде.
    "screen_capture": (
        (EventKind.SCREEN_RECORDING,),
        (EventKind.VIRTUAL_CAMERA,),
    ),
}

# ПОЧЕМУ `NAVIGATION_OFF_PROFILE` И `SUBRESOURCE_OFF_PROFILE` ЛЕСТНИЦЫ НЕ
# ОБРАЗУЮТ. Соблазн есть: один источник вне белого списка даёт и подзапросы, и
# попытку перехода, то есть внешне это тот же «один объект — один вклад». Но:
#
#   1. это НЕ уточнение одного наблюдения. Подзапрос делает страница, переход
#      инициирует человек. Старшая ступень лестницы обязана ПЕРЕПРОВЕРЯТЬ
#      младшую строже (см. телефон: `_eval_aimed` вызывает `_eval_raised`), а
#      здесь перепроверять нечего — это два разных факта об одном адресе;
#   2. определение «что считать одним наблюдением» в этой системе объявлено
#      дважды — лестницей здесь и кластером поведения в `storage/report.py`, и
#      их согласованность проверяет `make test`
#      (`ladder_cluster_coherence`). Заводить лестницу, не заводя парный
#      кластер, значит оставить расхождение между тем, что мерит счёт, и тем,
#      что печатает отчёт.
#
# Двойной счёт закрыт вместо лестницы ВЕСАМИ: подзапрос стоит 2 при cooldown
# 30 с, то есть поток технических находок не доводит экзамен даже до
# предупреждения (разбор чисел — в RISK_WEIGHTS). Если практика покажет, что
# лестница здесь всё-таки нужна, её надо вводить ВМЕСТЕ с кластером в
# report.py и одним коммитом, иначе `make test` честно покажет расхождение.

#: Название лестницы для отчёта и HUD.
LADDER_LABELS: dict[str, str] = {
    "phone": "телефон: в кадре → поднят → наведён на экран",
    "gaze_away": "взгляд: отведён → вне экрана",
    "paste": "вставка: из буфера → крупный блок",
    "screen_capture": "захват экрана: запись → виртуальная камера",
}

#: Ключи `detail`, по которым опознаётся ОДИН наблюдаемый объект лестницы.
#:
#: Пустой кортеж — у лестницы объект заведомо единственный: глаза у студента
#: одни, буфер обмена один. Непустой — ступени обязаны говорить об одном и том
#: же объекте, иначе это разные наблюдения и сворачивать их нельзя.
#:
#: Если ступень пришла БЕЗ признака субъекта, она присоединяется к текущему
#: прогону лестницы: промолчавший источник не должен превращаться в «другой
#: объект» и возвращать двойной счёт. Так сейчас и приходит PHONE_IN_FRAME —
#: `track_id` в его detail не доходит, см. комментарий у LADDER_SUBJECT_KEYS
#: в docs и отчёт по задаче.
LADDER_SUBJECT_KEYS: dict[str, tuple[str, ...]] = {
    "phone": ("track_id",),
    "gaze_away": (),
    "paste": (),
    "screen_capture": ("pid", "pids"),
}

#: Лестницы, на которых МОЛЧАНИЕ о субъекте НЕ склеивает ступени.
#:
#: Общее правило выше (молчание трактуется в пользу склейки) написано под
#: телефон и обязано там остаться: `PHONE_IN_FRAME` приходит без `track_id`, и
#: считать его «другим объектом» значило бы вернуть двойной счёт по главному
#: сценарию демо. Но на `screen_capture` то же правило работало как обход.
#:
#: Разбор `env_checks.check_virtual_camera`: список `processes` там пуст РОВНО
#: в одном случае — когда находка держится на зарегистрированном устройстве
#: или установленном плагине и ни одного процесса-хозяина не найдено
#: (`relevant = procs` при `device_hits or plugin_hits`, то есть запущенную
#: программу проверка без pid не называет никогда). Значит пустой список здесь
#: не «не знаем, какая программа», а «работающей программы в этой находке нет».
#: Склеивать такую находку с ffmpeg, который пишет экран, не с чем: общей
#: программы у них нет по построению. Проверено: `lock` превращался в `pause`,
#: и два независимых факта давали ровно тот же результат, что один OBS.
#:
#: Случай, для которого лестница и строилась, — ОДИН OBS, попавший и в запись
#: экрана, и в виртуальную камеру, — этой строкой не ломается: там pid есть на
#: обеих сторонах (находка с устройством перечисляет процессы-хозяева), и
#: пересечение множеств по-прежнему сворачивает её в одно наблюдение.
LADDER_SUBJECT_REQUIRED: frozenset[str] = frozenset({"screen_capture"})

#: Сколько секунд прогон лестницы живёт без новых ступеней.
#:
#: Для покадровых ступеней (телефон, взгляд) прогон обычно закрывается раньше —
#: по снятию условия в engine/events.py, — а это страховка на случай, когда
#: снятия не видно. Для мгновенных внешних событий (вставка, проверки
#: окружения) она и есть единственный признак конца прогона, поэтому значения
#: подобраны по смыслу: вставка — одно нажатие (3 с), находки окружения —
#: короче cooldown соответствующих правил (60 и 120 с), иначе повторные
#: прогоны проверки склеились бы в один бесконечный прогон.
LADDER_GAP_SEC: dict[str, float] = {
    "phone": 20.0,
    "gaze_away": 20.0,
    "paste": 3.0,
    "screen_capture": 45.0,
}
DEFAULT_LADDER_GAP_SEC = 20.0

#: Вид события -> (лестница, номер ступени). Строится один раз из таблицы выше.
LADDER_OF_KIND: dict[EventKind, tuple[str, int]] = {
    kind: (ladder, level)
    for ladder, levels in ESCALATION_LADDERS.items()
    for level, kinds in enumerate(levels)
    for kind in kinds
}


def ladder_of(kind: Any) -> tuple[str, int] | None:
    """(лестница, ступень) для вида события. None — вид сам по себе."""
    if not isinstance(kind, EventKind):
        try:
            kind = EventKind(str(getattr(kind, "value", kind)).strip().upper())
        except (ValueError, TypeError):
            return None
    return LADDER_OF_KIND.get(kind)


def ladder_steps(ladder: str) -> int:
    """Сколько ступеней у лестницы (для текста «ступень 3 из 3»)."""
    return len(ESCALATION_LADDERS.get(ladder, ()))


def ladder_gap(ladder: str) -> float:
    """Сколько секунд прогон лестницы ждёт следующую ступень."""
    return float(LADDER_GAP_SEC.get(ladder, DEFAULT_LADDER_GAP_SEC))


def ladder_top_weight(ladder: str) -> float:
    """Вес высшей ступени — потолок вклада всей лестницы за один прогон."""
    levels = ESCALATION_LADDERS.get(ladder, ())
    if not levels:
        return 0.0
    return max((RISK_WEIGHTS.get(kind, 10.0) for kind in levels[-1]), default=0.0)


def ladder_subject(ladder: str, detail: Any) -> str:
    """Признак наблюдаемого объекта ступени: «track:7», «pid:812,814» или "".

    Пустая строка означает «источник не сказал, про какой объект речь» — это
    НЕ «другой объект». Решение, склеивать ли ступени, принимает вызывающий:
    молчание источника трактуется в пользу склейки (иначе двойной счёт
    возвращается сам собой), а разные непустые признаки — против.
    """
    keys = LADDER_SUBJECT_KEYS.get(ladder, ())
    if not keys or not isinstance(detail, dict):
        return ""
    found: set[str] = set()
    _collect_subject(detail, frozenset(keys), found, depth=0)
    if not found:
        return ""
    prefix = "track" if "track_id" in keys else "id"
    return f"{prefix}:" + ",".join(sorted(found))


def ladder_same_subject(left: str, right: str,
                        ladder: str | None = None) -> bool:
    """Про один и тот же объект говорят две ступени или про разные.

    Пустой признак — «источник не сказал, про какой объект речь». Это НЕ
    другой объект: PHONE_IN_FRAME сейчас приходит без `track_id`, и если
    считать его чужим, двойной счёт вернётся сам собой.

    ИСКЛЮЧЕНИЕ — лестницы из `LADDER_SUBJECT_REQUIRED` (см. комментарий там).
    На них молчание о субъекте означает не «не знаем», а «общей программы в
    этой находке нет», и склейка по молчанию была рабочим обходом. `ladder`
    не передан — остаётся общее правило: вызывающий, который не знает, о какой
    лестнице речь, не должен ужесточать счёт.

    Непустые признаки сравниваются ПЕРЕСЕЧЕНИЕМ, а не равенством. Одна и та
    же программа попадает в разные находки разным числом процессов: запись
    экрана видит `pids [812, 813]`, виртуальная камера — только `pid 812`.
    Требовать совпадения множеств значило бы снова считать один OBS двумя
    независимыми наблюдениями.
    """
    if not left or not right:
        if ladder is not None and ladder in LADDER_SUBJECT_REQUIRED:
            return False
        return True
    left_prefix, _, left_values = left.partition(":")
    right_prefix, _, right_values = right.partition(":")
    if left_prefix != right_prefix:
        # Признаки разной природы (трек и pid) сравнивать нечем: считаем, что
        # об одном объекте, иначе молчание одного источника снова раздвоит счёт.
        return True
    return bool(set(left_values.split(",")) & set(right_values.split(",")))


def ladder_merge_subject(left: str, right: str) -> str:
    """Объединить признаки субъекта, накопленные прогоном лестницы.

    Нужно, чтобы следующая ступень сравнивалась со ВСЕМ, что про объект уже
    известно: находка с `pid 813` относится к тому же OBS, что и предыдущая с
    `pids [812, 813]`, даже если первая ступень назвала только 812.
    """
    if not left:
        return right
    if not right:
        return left
    left_prefix, _, left_values = left.partition(":")
    right_prefix, _, right_values = right.partition(":")
    if left_prefix != right_prefix:
        return left
    merged = sorted(set(left_values.split(",")) | set(right_values.split(",")))
    return f"{left_prefix}:" + ",".join(merged[:32])


def _collect_subject(node: Any, keys: frozenset[str], out: set[str],
                     depth: int) -> None:
    """Собрать значения ключей субъекта, в том числе из вложенных находок.

    Проверки окружения кладут процессы ВЛОЖЕННО (`processes[].pids`,
    `confirmed[].pid`), поэтому плоского просмотра `detail` не хватает.
    Глубина ограничена: detail приходит извне, и обход не имеет права
    превращаться в разбор произвольного дерева.
    """
    if depth > 3 or len(out) > 64:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys:
                _add_subject_value(value, out)
            elif isinstance(value, (dict, list, tuple)):
                _collect_subject(value, keys, out, depth + 1)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect_subject(item, keys, out, depth + 1)


def _add_subject_value(value: Any, out: set[str]) -> None:
    if isinstance(value, (list, tuple)):
        for item in value:
            _add_subject_value(item, out)
        return
    if value is None or isinstance(value, bool):
        return
    text = str(value).strip()
    # -1 у детектора объектов означает «трека нет», это не идентификатор.
    if not text or text in {"-1", "0", "None"}:
        return
    out.add(text[:32])


class VerdictAction(str, Enum):
    NONE = "none"
    WARN = "warn"      # мягкое предупреждение в HUD
    PAUSE = "pause"    # тест приостановлен, нужен возврат в рамку
    LOCK = "lock"      # сессия закрыта, формируется отчёт


# --------------------------------------------------------------------------
# Политика блокировки: ЧТО система делает сама, а что остаётся человеку
# --------------------------------------------------------------------------
# Разбор опубликованного кейса Duolingo English Test показал, что «человек в
# цикле» сам по себе не защищает: прокторы подтверждали от 29% до 50% заведомо
# ложных сигналов, и вероятность подтверждения зависела от национальности
# тестируемого (p < .001). Вывод для нас не «убрать человека», а «не давать
# автоматике делать то, что человек потом вынужден лишь задним числом
# оправдывать». Блокировка посреди экзамена необратима для студента: отменить
# её можно, а потерянное время и состояние — нет.
#
# Поэтому САМОЕ СТРОГОЕ, что система делает сама, — приостановка (PAUSE).
# Порог RISK_LOCK не закрывает сессию, а переводит её в ожидание решения
# проктора: экзамен приостановлен, на экране объяснение и код сессии, снятие
# или подтверждение — действие человека.
#
#: Максимум, который автоматика выносит без человека.
AUTO_ACTION_CAP = VerdictAction.PAUSE

#: Политика блокировки. Пишется в цепочку записью `control`/`policy` и в отчёт.
LOCK_POLICY_PROCTOR = "proctor"   # по умолчанию: LOCK только решением человека
LOCK_POLICY_AUTO = "auto"         # прежнее поведение, включается --auto-lock

LOCK_POLICY_LABELS: dict[str, str] = {
    LOCK_POLICY_PROCTOR: "блокировку подтверждает проктор",
    LOCK_POLICY_AUTO: "блокировка автоматическая по порогу",
}

#: Решения, которые проктор присылает командой `command`.
PROCTOR_DECISION_LOCK = "lock"        # подтвердить блокировку
PROCTOR_DECISION_RELEASE = "release"  # снять приостановку, экзамен продолжается


def cap_auto_action(action: VerdictAction, auto_lock: bool = False) -> VerdictAction:
    """Ограничить АВТОМАТИЧЕСКОЕ действие потолком политики.

    `auto_lock=False` (по умолчанию): LOCK превращается в `AUTO_ACTION_CAP`,
    то есть в приостановку. Сама блокировка остаётся возможной, но только как
    решение человека — см. `PROCTOR_DECISION_*`.
    """
    if auto_lock:
        return action
    if action is VerdictAction.LOCK:
        return AUTO_ACTION_CAP
    return action


#: Почему к событию НЕТ кадра камеры: значение `detail["evidence_skipped"]`.
#: Ставит только ядро (из `shell_event` это поле снимается на границе сокета),
#: и ставит ДО записи в хеш-цепочку — причина отсутствия кадра входит в
#: подписанное тело события так же, как путь к кадру.
EVIDENCE_SKIP_SERVICE = "service"            # служебная запись, кадр не нужен
#: Камеры нет: --headless, --mock, нет opencv — или устройство в этом процессе
#: не отдало ни одного кадра (не подключено, занято, доступ не выдан).
EVIDENCE_SKIP_NO_CAMERA = "no_camera"
#: Камера кадры отдавала, но последний старше EVIDENCE_FRAME_MAX_AGE: пропала.
EVIDENCE_SKIP_STALE = "stale_frame"
EVIDENCE_SKIP_WRITE_FAILED = "write_failed"  # файл кадра не записался
EVIDENCE_SKIP_NOT_RECORDING = "not_recording"  # сессия не пишется
#: Кадр выключен КОНФИГУРАЦИЕЙ: `save_evidence = false` или severity ниже
#: `evidence_frame_min_severity`. При настройках по умолчанию не возникает.
EVIDENCE_SKIP_DISABLED = "disabled"
EVIDENCE_SKIP_REASONS: tuple[str, ...] = (
    EVIDENCE_SKIP_SERVICE, EVIDENCE_SKIP_NO_CAMERA, EVIDENCE_SKIP_STALE,
    EVIDENCE_SKIP_WRITE_FAILED, EVIDENCE_SKIP_NOT_RECORDING, EVIDENCE_SKIP_DISABLED,
)
#: Кадр камеры старше этого (секунд) к событию не прикладывается: он показал
#: бы не момент события, а то, что было до потери камеры.
EVIDENCE_FRAME_MAX_AGE = 2.0

#: `evidence.extra.clip_skipped` — почему к событию, которому клип положен по
#: severity и виду, клипа нет. Размывать видео рекордер не умеет, поэтому клип
#: пишется только тогда, когда в кадре заведомо одно лицо.
CLIP_SKIP_MULTIPLE_FACES = "multiple_faces"  # второе лицо в кадре или в последние clip_seconds
CLIP_SKIP_FACES_UNKNOWN = "faces_unknown"    # FaceMesh недоступен: лица считать нечем
#: Запись `control` в хеш-цепочке о клипе, снятом до записи файла:
#: {event_id, kind, reason, at}. Клип уже назван в `evidence.clip_path`
#: события, но файла нет и не будет.
CLIP_CANCELLED_CONTROL = "clip_cancelled"
#: Причина в `clip_cancelled`: посторонний вошёл в кадр, пока набиралось «после».
CLIP_CANCEL_MULTIPLE_FACES_AFTER = "multiple_faces_after"
#: `evidence.extra.blur` — как искались лица сверх рамок FaceMesh на кадре,
#: ушедшем на диск. "haar" — кадр прошёл запасной каскад Хаара OpenCV, его
#: лица (кроме основного) размыты вместе с «прочими» лицами FaceMesh.
#: "unavailable" — каскада нет: кадр записан, лица, которых не увидел FaceMesh
#: (а без mediapipe — все), НЕ размыты. Поля нет — кадр записан до этого
#: правила.
BLUR_HAAR = "haar"
BLUR_UNAVAILABLE = "unavailable"


@dataclass
class Evidence:
    """Доказательство инцидента. Пути — относительно каталога сессии.

    `extra["frame_ts"]` — время кадра камеры (epoch, с), когда кадр приложен:
    у событий оболочки и окружения оно не совпадает с `ts` события.
    """
    frame_path: str | None = None          # jpeg-кадр момента
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


def event_message(event: ProctorEvent, screen: bool = False) -> dict[str, Any]:
    """Сообщение `event`. `screen=True` — оболочка снимает окно экзамена и
    отвечает `screen_evidence` с `event_id` этого события. Ядро ставит его
    каждому НЕслужебному событию, пока сессия пишется."""
    return envelope(MsgType.EVENT, event=event.to_dict(), screen=bool(screen))


#: Пределы приёма `screen_evidence` (проверяет ядро, а не оболочка).
SCREEN_EVIDENCE_MAX_BYTES = 3 * 1024 * 1024   # после декодирования base64
SCREEN_EVIDENCE_MAX_AGE = 60.0                # с момента рассылки события, с
SCREEN_EVIDENCE_SOURCES: tuple[str, ...] = ("exam_view", "main_window")
#: Имя записи `control` в хеш-цепочке со снимком окна экзамена.
SCREEN_EVIDENCE_CONTROL = "evidence_screen"
#: Потолок одного WS-кадра на транспорте: снимок предельного размера в base64
#: плюс конверт. Сообщение больше max_size библиотека не отклоняет, а рвёт
#: соединение (1009), поэтому потолок выше проверки ядра. Предел для всех
#: ОСТАЛЬНЫХ сообщений — `ws_max_message` конфига (1 МиБ): больше ядро их не
#: обрабатывает, а пишет отказ в лог.
WS_TRANSPORT_MAX_BYTES = (SCREEN_EVIDENCE_MAX_BYTES * 4) // 3 + (64 << 10)


# ---------------------------------------------------------------------------
# Приватность журнала: метки, приходящие извне
# ---------------------------------------------------------------------------
#: Максимальная длина метки вопроса в журнале. Это номер в билете, а не текст.
#: Шестнадцать символов хватает любому идентификатору (`q3`, `вопрос-12`,
#: `ticket.07`) и не хватает ни одной читаемой фразе.
LABEL_MAX_LEN = 16

#: Разрешённые символы метки: буквы (включая кириллицу), цифры и `-_.`.
#: Пробелов нет намеренно — связный текст без них не записывается, а значит
#: метка физически не может быть вместилищем ответа студента.
_LABEL_OK = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "әғқңөұүһіӘҒҚҢӨҰҮҺІ"
    "0123456789-_."
)


def safe_label(value: Any) -> str:
    """Привести внешнюю метку (id вопроса) к виду, не несущему текста.

    Обещание приватности в этом продукте абсолютное: «по записи журнала нельзя
    восстановить ответ студента». Обрезка по длине его не выполняет — тридцать
    два символа ответа это всё ещё ответ, — поэтому метка либо ПОХОЖА на
    идентификатор, либо не попадает в журнал вовсе.

    Похожая метка (короткая, без пробелов, из `_LABEL_OK`) проходит как есть.
    Всё остальное заменяется суррогатом `qid-<8 hex>`: он устойчив (одинаковый
    вход даёт одинаковый выход), поэтому дедупликация в `engine/fusion.py`,
    потолок наблюдения в `engine/risk.py` и группировка в отчёте продолжают
    работать, а содержание из журнала при этом уходит.

    Суррогат — не отказ, а честная подстановка: факт замены видно по префиксу,
    и отчёт рядом пишет, что метка пришла не в форме идентификатора. Молча
    подставлять было бы хуже, чем не подставлять.
    """
    raw = "" if value is None else str(value)
    raw = raw.strip()
    if not raw:
        return ""
    if len(raw) <= LABEL_MAX_LEN and all(ch in _LABEL_OK for ch in raw):
        return raw
    digest = hashlib.blake2s(raw.encode("utf-8", "replace"), digest_size=4).hexdigest()
    return f"qid-{digest}"


def is_surrogate_label(value: Any) -> bool:
    """Метка была подставлена вместо непригодной. Для пояснения в отчёте."""
    return str(value or "").startswith("qid-")

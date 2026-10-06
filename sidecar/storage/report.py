"""
Сборка автономного HTML-отчёта по сессии прокторинга + подпись Ed25519.

Отчёт — не скриншот демки, а артефакт, который преподаватель открывает с
флешки без интернета и может проверить:

  * все картинки вшиты в HTML как base64, CSS инлайном, внешних запросов нет;
  * числа считаются по тем же весам (`protocol.RISK_WEIGHTS`), что и в рантайме;
  * данные берутся из `events.payload` — ровно из тех байтов, по которым считан
    hash-chain, поэтому отчёт и цепочка говорят об одном и том же;
  * результат `verify_chain()` и голова цепочки печатаются в самом отчёте;
  * рядом с отчётом кладутся `report.html.sig` (подпись Ed25519) и
    `report.html.pub` (публичный ключ) — проверка одной командой через
    `scripts/verify_report.py`.

ВИЗУАЛЬНЫЙ ЯЗЫК. Отчёт собран в той же системе NEON/PROCTOR, что и оболочка
(см. `shell/renderer/tokens.css` и Р-13), но с обратным распределением
поверхностей: ведущий тон здесь белый, потому что отчёт — среда чтения и
анализа. Тёмное оставлено ровно за наблюдением: таймлайн, график риска,
таблица инцидентов с кадрами, схемы таймингов связок. Кислотный `#c6ff00`
несёт только подтверждение и одну ключевую метрику и ни разу не служит
цветом текста на белом. Значения токенов продублированы в `_CSS` и в
константах палитры: отчёт обязан быть ОДНИМ автономным файлом и сослаться
на внешний css не может.

Шрифты: вшиты base64 Onest (весь русский и казахский текст, Р-14) и
JetBrains Mono (телеметрия, хеши, коды инцидентов, где подмена шрифта ломает
смысл). Space Grotesk не вшивается: он нужен только латинскому вордмарку.

ПОЧЕМУ ONEST ВШИТ, ХОТЯ ЭТО +60 КБ. Subset'ы JetBrains Mono в репозитории
НЕ содержат шести казахских букв (ә ғ қ ң ұ һ), хотя `unicode-range` их
объявляет: `document.fonts.check()` возвращает true, а в cmap глифов нет, и
браузер молча уходит в Menlo/Consolas или в тофу — ровно в моноширинных полях
(exam_id, student_id, session_id, коды инцидентов). Onest покрытие имеет
полное, поэтому он вшивается и стоит в `--font-mono` ВТОРЫМ: для букв, которых
у JetBrains Mono нет, браузер берёт следующее семейство из списка, и это
детерминированно вшитый Onest, а не шрифт чужой ОС. Цена — эти буквы в
моноширинных полях пропорциональной ширины; тофу на месте имени университета
имени Ахмет Байтұрсынұлы дороже. Покрытие проверяется по cmap функцией
`check_font_coverage()`, а не по объявленному unicode-range.

ФОРМУЛИРОВКИ. Формула гайда: наблюдаемый факт -> контекст -> понятное
действие. Обвинительных слов в отчёте нет: система сообщает наблюдение,
решение принимает преподаватель.

INTEGRITY SCORE. Итоговая цифра — не вероятность чего-либо, а мера того,
насколько можно опираться на результат. Считается по НАБЛЮДАЕМОМУ ПОВЕДЕНИЮ,
а не по видам событий:

    кластер        = группа видов, описывающих одно поведение
                     (GAZE_DOWN + GAZE_SIDE + GAZE_OFF_SCREEN + HEAD_TURNED —
                      это один «взгляд ушёл от экрана», а не четыре повода)
    масса_вида     = сумма (вес * уверенность) по срабатываниям вида
    база_кластера  = самое тяжёлое ОДИНОЧНОЕ срабатывание кластера
    штраф_кластера = база * min(1 + G*(sqrt(масса/база) - 1), CAP)   # G=0.6 CAP=4
    штраф          = сумма штрафов кластеров
    integrity      = 100 * SCALE / (SCALE + штраф)                   # SCALE=100

Почему так, а не «сумма по видам с sqrt внутри вида»:

  * sqrt внутри вида при линейном сложении видов означал, что раскладка одного
    и того же поведения по четырём видам даёт штраф в 2 раза больше, чем те же
    срабатывания в одном виде, — полоса вердикта зависела от номенклатуры
    детектора, а не от поведения студента. Кластер это закрывает;
  * CAP ограничивает кластер четырьмя «самыми тяжёлыми одиночными»: сколько бы
    раз студент ни посмотрел на клавиатуру, лёгкое поведение не опускает оценку
    ниже полосы «выборочная проверка». Это прямое следствие модели угроз: отвод
    взгляда без связки — предупреждение, а не доказательство (У-01);
  * гипербола SCALE/(SCALE+штраф) вместо exp(-штраф/SCALE) сохраняет разрешение
    в нижней трети: 200 и 400 штрафа это 33 и 20 баллов, а не 0.0 и 0.0. Для
    апелляции важно именно различать тяжёлые сессии между собой.

Калибровка (проверена на профилях 1/4/11/20/120/200 срабатываний):
одна IDENTITY_MISMATCH -> 62 («нужен разбор»); 4 «взгляд вниз» -> 90 («норма»);
11 «взгляд вниз» -> 87; 120 отводов взгляда -> 77—81 независимо от раскладки
по видам; демо-сессия 16.10 (13 срабатываний, одна критичная связка) -> 25;
сессия на 200 срабатываний -> единицы, но не нуль.

Разложение по каналам точное: штраф кластера делится между его видами
пропорционально массе вида, потерянные баллы — пропорционально штрафу, поэтому
сумма вкладов равна `100 - integrity` — именно это показывают горизонтальные
полосы под кольцом.

Отдельно показывается `risk-score` из рантайма: он мгновенный и затухающий
(half-life), поэтому к концу спокойной сессии почти нулевой. Эти две метрики
дополняют друг друга, и в отчёте это написано прямо.
"""
from __future__ import annotations

import base64
import html
import importlib
import importlib.util
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent          # sidecar/storage
_SIDECAR = _HERE.parent                          # sidecar
_ROOT = _SIDECAR.parent                          # корень репозитория
for _p in (str(_SIDECAR), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: Масштаб гиперболы integrity score: integrity = 100 * SCALE / (SCALE + штраф).
#: Подобран так, чтобы одна подмена личности роняла доверие примерно до 62,
#: а четыре «взгляд вниз» — до 90. Проверено на профилях до 200 срабатываний.
INTEGRITY_SCALE = 100.0

#: Насколько повторы внутри одного наблюдаемого поведения добавляют к штрафу.
#: 1.0 — честный sqrt (десять повторов = x3.16), 0.6 — повтор учитывается, но
#: привычка смотреть на клавиатуру не весит как подмена личности.
REPEAT_GAIN = 0.6

#: Потолок кластера в «самых тяжёлых одиночных срабатываниях». Сколько бы раз
#: условие ни повторилось, одно поведение не может весить больше четырёх своих
#: худших эпизодов: иначе шкала измеряет длительность сессии, а не поведение.
REPEAT_CAP = 4.0

#: Половина времени жизни вклада события в risk-score, сек (как в рантайме).
DEFAULT_HALF_LIFE = 90.0

#: Лимит на суммарный объём вшитых картинок, чтобы отчёт оставался открываемым.
EMBED_BUDGET_BYTES = 24 * 1024 * 1024
THUMB_WIDTH = 260
CLIP_EMBED_LIMIT = 3 * 1024 * 1024

TEMPLATE_NAME = "report.html.j2"

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_LABELS = {
    "info": "информация", "low": "низкая", "medium": "средняя",
    "high": "высокая", "critical": "критическая",
}
# ---------------------------------------------------------------------------
# Палитра и шкала — NEON/PROCTOR
#
# Значения ЗЕРКАЛЯТ shell/renderer/tokens.css. Отчёт не может подключить
# tokens.css: он обязан открываться с флешки одним файлом, поэтому токены
# продублированы здесь (для SVG, который рисуется строками) и в `_CSS`
# (для разметки). Меняется значение — меняется в обоих местах.
#
# Роли поверхностей, по правилу гайда:
#   белый     — среда анализа: шапка, разбор score, связки, целостность, границы;
#   чёрный    — среда наблюдения: таймлайн, график риска, таблица инцидентов
#               с кадрами-доказательствами, служебные полосы отчёта;
#   кислотный — ТОЛЬКО действие, live-статус, подтверждение и ОДНА ключевая
#               метрика. Крупной заливкой не используется и никогда не служит
#               цветом текста на белом (контраст 1.4:1): для акцентного текста
#               на светлом есть токен --signal-text-on-light (#4a6100) в `_CSS`,
#               а в SVG акцент на белом делается ЗАЛИВКОЙ под чёрным текстом.
# ---------------------------------------------------------------------------
ACID = "#c6ff00"
INK = "#0a0a0a"
INK_2 = "#141414"
GRAPHITE = "#262626"
GRAY_500 = "#747474"
GRAY_300 = "#b9b9b9"
GRAY_100 = "#ececec"
WHITE = "#ffffff"
DANGER = "#ff334e"
WARNING = "#ffb800"
INFO_BLUE = "#35b7ff"
PAUSE_ORANGE = "#ff8a1f"
#: Цвета severity — ровно --sev-* из tokens.css.
SEVERITY_COLORS = {
    "info": INFO_BLUE, "low": GRAY_500, "medium": WARNING,
    "high": PAUSE_ORANGE, "critical": DANGER,
}
#: ФОРМА метки дублирует цвет. Требование a11y из гайда: цвет никогда не
#: единственный носитель смысла — метку на таймлайне и в таблице можно
#: различить при полной цветовой слепоте и на чёрно-белой печати.
SEVERITY_SHAPES = {
    "info": "ring", "low": "circle", "medium": "diamond",
    "high": "triangle", "critical": "square",
}
SHAPE_NAMES = {
    "ring": "контур", "circle": "круг", "diamond": "ромб",
    "triangle": "треугольник", "square": "квадрат",
}
# Цвет текста severity на ЧЁРНОЙ поверхности живёт в `_CSS` (`.sev` + `--tone`):
# --sev-low #747474 на #141414 даёт около 3:1, поэтому у «низкой» в тёмной
# таблице подпись берёт --gray-300. В SVG этой проблемы нет: там severity
# рисуется фигурой, а не текстом.

CHANNEL_LABELS = {
    "vision": "Объекты в кадре", "gaze": "Взгляд и голова", "identity": "Личность",
    "audio": "Аудио", "environment": "Окружение (ОС)", "shell": "Оболочка экзамена",
    "fusion": "Связки сигналов", "system": "Служебное",
}
#: Канал обозначается МОНОШИРИННЫМ КОДОМ, а не цветным кружком. Восьми
#: различимых цветов в палитре из трёх базовых нет и быть не должно, а код
#: читается и в печати, и при цветовой слепоте, и совпадает с protocol.Channel.
CHANNEL_CODES = {
    "vision": "VIS", "gaze": "GAZE", "identity": "ID", "audio": "AUD",
    "environment": "ENV", "shell": "SHELL", "fusion": "FUS", "system": "SYS",
}

#: Уровни итогового вердикта: границы, подпись уровня и цвет кольца score.
#: Кислотный достаётся только верхнему уровню — он означает подтверждение.
VERDICT_LEVELS = (
    (85.0, "ok", 4, ACID),
    (65.0, "attention", 3, WARNING),
    (40.0, "doubt", 2, PAUSE_ORANGE),
    (0.0, "fail", 1, DANGER),
)

#: Техническая подпись вида инцидента — вторая строка под наблюдением движка
#: и основная строка только там, где у события нет `message`.
#:
#: ВАЖНО О ТОНЕ. Эти подписи НЕ описывают намерение: детектор наблюдает
#: обращение к хоткею, а не «попытку» (хоткей может быть мышечной памятью), и
#: знает только, что процесс в ЕГО контрольном списке, а не что он был запрещён
#: студенту правилами экзамена. Крупным кеглем в таблице печатается `message`
#: из `engine/events.py` — он построен по формуле гайда «факт -> контекст ->
#: действие»; подпись вида остаётся мелкой технической строкой, иначе
#: документ подменяет наблюдение движка собственной формулировкой.
#: Новых видов событий здесь не вводится.
KIND_LABELS = {
    "PHONE_IN_FRAME": "Телефон в кадре",
    "PHONE_RAISED": "Телефон поднят к лицу",
    "PHONE_AIMED_AT_SCREEN": "Телефон направлен на экран",
    "FORBIDDEN_OBJECT": "Посторонний предмет в кадре",
    "NO_FACE": "Лицо не в кадре",
    "SECOND_FACE": "Второе лицо в кадре",
    "IDENTITY_MISMATCH": "Личность не совпадает с эталоном",
    "LIVENESS_FAIL": "Признаки статичного изображения вместо живого кадра",
    "GAZE_DOWN": "Взгляд направлен вниз",
    "GAZE_SIDE": "Взгляд направлен в сторону",
    "GAZE_OFF_SCREEN": "Взгляд вне области экрана",
    "HEAD_TURNED": "Голова отвернута от экрана",
    "VOICE_OTHER": "Посторонний голос",
    "SPEECH_WITHOUT_LIP_MOTION": "Речь без движения губ",
    # Р-10: в режиме аудитории аудио-анализ выключен, и угрозу наушника
    # закрывает именно эта проверка окружения — подпись ей обязательна.
    "AUDIO_DEVICE_CONNECTED": "Подключена гарнитура или наушники",
    "VIRTUAL_CAMERA": "Виртуальная камера",
    "REMOTE_ACCESS_SOFTWARE": "Софт удалённого доступа",
    "VIRTUAL_MACHINE": "Виртуальная машина",
    "SCREEN_RECORDING": "Запись экрана",
    "MULTIPLE_DISPLAYS": "Подключено несколько экранов",
    "BLACKLISTED_PROCESS": "Процесс из контрольного списка",
    "WINDOW_BLUR": "Потеря фокуса окна экзамена",
    "FULLSCREEN_EXIT": "Выход из полноэкранного режима",
    "SHORTCUT_BLOCKED": "Заблокированное сочетание клавиш",
    "CLIPBOARD_PASTE": "Вставка из буфера обмена",
    "DEVTOOLS_ATTEMPT": "Обращение к инструментам разработчика",
    "PASTE_BURST": "Вставка большого блока текста",
    "TYPING_ANOMALY": "Аномалия ритма набора",
    "FUSION_GAZE_THEN_ANSWER": "Связка: взгляд ушёл — сразу ответ",
    "FUSION_BLUR_THEN_ANSWER": "Связка: потеря фокуса — сразу ответ",
    "FUSION_PHONE_THEN_ANSWER": "Связка: телефон — сразу ответ",
    "SESSION_STARTED": "Сессия начата",
    "SESSION_ENDED": "Сессия завершена",
    "CALIBRATION_DONE": "Калибровка завершена",
    "SENSOR_LOST": "Потерян датчик (камера/микрофон)",
}
#: Расшифровка полей detail для разбора связок.
#:
#: Первые три ключа движок ставит КАЖДОМУ событию (`engine/events.py`), и без
#: подписи они печатались английскими именами посреди русского документа.
#: `code` — тот самый идентификатор, по которому подаётся апелляция, поэтому он
#: вынесен в колонку «Код» таблицы, а не спрятан в раскрытие.
DETAIL_LABELS = {
    "code": "Код инцидента", "confirm_sec": "Окно подтверждения, с",
    "confirmed_by": "Подтверждено", "held_sec": "Условие держалось, с",
    "duration_total": "Суммарно за сессию, с", "streak": "Срабатываний подряд",
    "combo": "Составное условие", "displays": "Экраны", "sensor": "Датчик",
    "head_yaw": "Поворот головы, град", "head_pitch": "Наклон головы, град",
    "no_blink_sec": "Без моргания, с", "mouth_open_ratio": "Раскрытие рта",
    "mouth_threshold": "Порог раскрытия рта", "mouth_open_speech": "Порог рта при речи",
    "face_present": "Лицо в кадре", "speech": "Речь слышна",
    "replaced_by": "Заменено записью", "by_name": "Опознано по имени",
    "unknown_names": "Неопознанные имена", "mode": "Режим развёртывания",
    "exam_mode": "Режим экзамена", "suppressed_by_mode": "Подавлено режимом",
    "question_id": "Вопрос", "zone": "Зона взгляда", "gaze_zone": "Зона взгляда",
    "duration": "Длительность, с", "gaze_duration": "Взгляд отведён, с",
    "delay": "Задержка до ответа, с", "delay_ms": "Задержка до ответа, мс",
    "gap_sec": "Пауза, с", "chars": "Символов введено", "length": "Длина, символов",
    "mean_ms": "Средний интервал набора, мс", "std_ms": "Разброс интервала, мс",
    "baseline_ms": "Базовый интервал студента, мс", "ratio": "Отношение к базовой линии",
    "speed_ratio": "Во сколько раз быстрее обычного", "conf": "Уверенность",
    "confidence": "Уверенность", "label": "Метка детектора", "yaw": "Поворот головы, град",
    "pitch": "Наклон головы, град", "gaze_yaw": "Взгляд по горизонтали, град",
    "gaze_pitch": "Взгляд по вертикали, град", "similarity": "Близость к эталону",
    "threshold": "Порог", "area_ratio": "Доля кадра", "bbox": "Рамка (x,y,w,h)",
    "process": "Процесс", "device": "Устройство", "count": "Количество",
    "face_count": "Лиц в кадре", "rms": "Громкость (RMS)", "is_owner": "Голос владельца",
    "source": "Источник", "stage": "Этап", "reason": "Причина",
    "time_to_answer_ms": "Время на ответ, мс", "trigger": "Триггер",
    "linked_event": "Связанное событие", "window_sec": "Окно связки, с",
    "paste": "Вставка", "typing_stats": "Статистика набора", "student_id": "Студент",
    "exam_id": "Экзамен", "capabilities": "Активные каналы", "difficulty": "Сложность",
}
SERVICE_KINDS = {"SESSION_STARTED", "SESSION_ENDED", "CALIBRATION_DONE"}

# ---------------------------------------------------------------------------
# Кластеры наблюдаемого поведения — основа integrity score
#
# Шкала обязана мерить ПОВЕДЕНИЕ, а не номенклатуру детектора. Четыре вида
# GAZE_* плюс HEAD_TURNED описывают одно и то же: взгляд ушёл от экрана.
# Если складывать их линейно, то одни и те же 120 отводов взгляда дают разный
# штраф в зависимости от того, как детектор их разложил, — и привычка смотреть
# на клавиатуру обгоняет зафиксированную подмену личности.
#
# Внутри кластера повторы затухают, кластеры складываются. Кластеры НЕ
# пересекают границу канала: связка сигналов разных каналов — это и есть
# ценность продукта, её гасить нельзя. Каждая FUSION_* — свой кластер:
# «взгляд -> ответ» и «телефон -> ответ» это разные наблюдения, а не повтор.
# Вид, которого нет в таблице, образует кластер сам по себе.
# ---------------------------------------------------------------------------
BEHAVIOR_CLUSTERS: dict[str, str] = {
    "GAZE_DOWN": "gaze", "GAZE_SIDE": "gaze", "GAZE_OFF_SCREEN": "gaze",
    "HEAD_TURNED": "gaze",
    "PHONE_IN_FRAME": "phone", "PHONE_RAISED": "phone",
    "PHONE_AIMED_AT_SCREEN": "phone",
    "IDENTITY_MISMATCH": "identity", "LIVENESS_FAIL": "identity",
    "VOICE_OTHER": "voice", "SPEECH_WITHOUT_LIP_MOTION": "voice",
    "VIRTUAL_CAMERA": "env_runtime", "VIRTUAL_MACHINE": "env_runtime",
    "REMOTE_ACCESS_SOFTWARE": "env_runtime", "SCREEN_RECORDING": "env_runtime",
    "AUDIO_DEVICE_CONNECTED": "env_device", "MULTIPLE_DISPLAYS": "env_device",
    "WINDOW_BLUR": "focus", "FULLSCREEN_EXIT": "focus",
    "SHORTCUT_BLOCKED": "shell_keys", "DEVTOOLS_ATTEMPT": "shell_keys",
    "CLIPBOARD_PASTE": "paste", "PASTE_BURST": "paste",
}
#: Название поведения для разбора под кольцом score.
CLUSTER_LABELS: dict[str, str] = {
    "gaze": "Взгляд и поза головы",
    "phone": "Телефон в кадре",
    "identity": "Сверка личности",
    "voice": "Посторонняя речь",
    "env_runtime": "Среда исполнения (ОС)",
    "env_device": "Подключённые устройства",
    "focus": "Окно экзамена вне фокуса",
    "shell_keys": "Служебные сочетания клавиш",
    "paste": "Вставка текста",
}


def _cluster_of(kind: str) -> str:
    """Ключ кластера наблюдаемого поведения. Неизвестный вид — сам себе кластер."""
    return BEHAVIOR_CLUSTERS.get(kind, kind)


def _cluster_label(key: str) -> str:
    return CLUSTER_LABELS.get(key, KIND_LABELS.get(key, key))


# ---------------------------------------------------------------------------
# Загрузка соседних модулей максимально терпимо к способу импорта
# ---------------------------------------------------------------------------
def _import_first(*names: str) -> Any:
    for name in names:
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None


def _load_db_module() -> Any:
    """Модуль хранилища: как пакет, как плоский модуль или прямо по файлу."""
    mod = _import_first("storage.db", "sidecar.storage.db", "db")
    if mod is not None and hasattr(mod, "EvidenceStore"):
        return mod
    try:
        spec = importlib.util.spec_from_file_location("_evidence_db", _HERE / "db.py")
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    except Exception:
        pass
    return None


_PROTO = _import_first("protocol", "sidecar.protocol")


def _risk_weights() -> dict[str, float]:
    """Веса из protocol.py. Нет протокола — нейтральный вес, отчёт всё равно соберётся."""
    weights: dict[str, float] = {}
    table = getattr(_PROTO, "RISK_WEIGHTS", None) if _PROTO else None
    if isinstance(table, dict):
        for kind, value in table.items():
            key = getattr(kind, "value", kind)
            try:
                weights[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    return weights


def _thresholds() -> tuple[float, float, float]:
    warn = float(getattr(_PROTO, "RISK_WARN", 30.0) or 30.0) if _PROTO else 30.0
    pause = float(getattr(_PROTO, "RISK_PAUSE", 60.0) or 60.0) if _PROTO else 60.0
    lock = float(getattr(_PROTO, "RISK_LOCK", 90.0) or 90.0) if _PROTO else 90.0
    return warn, pause, lock


def _fusion_kinds() -> set[str]:
    kinds = set()
    enum = getattr(_PROTO, "EventKind", None) if _PROTO else None
    if enum is not None:
        for item in enum:
            value = str(getattr(item, "value", item))
            if value.startswith("FUSION_"):
                kinds.add(value)
    if not kinds:
        kinds = {k for k in KIND_LABELS if k.startswith("FUSION_")}
    return kinds


# ---------------------------------------------------------------------------
# Утилиты форматирования
# ---------------------------------------------------------------------------
def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clock(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    except Exception:
        return "--:--:--"


def _full_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return "—"


def _offset(ts: float, start: float) -> str:
    delta = max(_f(ts) - _f(start), 0.0)
    return f"+{int(delta // 60):02d}:{int(delta % 60):02d}"


def _dur(seconds: float) -> str:
    total = int(max(_f(seconds), 0.0))
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h} ч {m:02d} мин {s:02d} с" if h else f"{m} мин {s:02d} с"


def _num(value: float, digits: int = 1) -> str:
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def _channel_label(channel: str) -> str:
    return CHANNEL_LABELS.get(channel, channel or "—")


def _channel_code(channel: str) -> str:
    """Короткий моноширинный код канала — совпадает с protocol.Channel."""
    return CHANNEL_CODES.get(channel, (channel or "—")[:5].upper())


def _verdict_level(score: float) -> tuple[str, int, str]:
    """(ключ уровня, номер 1..4, цвет кольца) по integrity score."""
    for threshold, key, index, color in VERDICT_LEVELS:
        if score >= threshold:
            return key, index, color
    return "fail", 1, DANGER


# ---------------------------------------------------------------------------
# Чтение данных сессии
# ---------------------------------------------------------------------------
@dataclass
class ReportData:
    session_dir: Path
    db_path: Path | None
    meta: dict[str, Any]
    summary: dict[str, Any]
    events: list[dict[str, Any]]
    chain: dict[str, Any]
    sources: list[str]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_jsonl_events(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any],
                                            dict[str, Any], dict[str, Any]]:
    """Фолбэк-формат сайдкара (events.jsonl), если SQLite недоступен.

    Возвращает ещё и счётчики цепочки: записей, из них инцидентов, первый и
    последний hash. Р-05 называет деградированный режим штатным, значит и
    футер отчёта обязан печатать в нём реальное число записей, а не нуль
    на документе, который тут же перечисляет инциденты.
    """
    events: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    counts: dict[str, Any] = {"records": 0, "events_checked": 0,
                              "genesis": "", "last_hash": ""}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return events, meta, summary, counts
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            continue
        counts["records"] += 1
        record_hash = str(record.get("hash") or "")
        if record_hash:
            if not counts["genesis"]:
                counts["genesis"] = record_hash
            counts["last_hash"] = record_hash
        rtype = payload.get("type")
        if rtype == "event" and isinstance(payload.get("event"), dict):
            event = dict(payload["event"])
            event["_seq"] = record.get("seq")
            event["_hash"] = record.get("hash", "")
            events.append(event)
            counts["events_checked"] += 1
        elif rtype == "session_open" and isinstance(payload.get("meta"), dict):
            meta = dict(payload["meta"])
        elif rtype == "session_close" and isinstance(payload.get("summary"), dict):
            summary = dict(payload["summary"])
    return events, meta, summary, counts


def load_report_data(session_dir: str | Path, db_path: str | Path | None = None) -> ReportData:
    """Собрать всё, что нужно отчёту: мета, сводка, события, состояние цепочки."""
    sdir = Path(str(session_dir)).resolve()
    sources: list[str] = []
    meta = _read_json(sdir / "meta.json")
    summary = _read_json(sdir / "summary.json")
    if meta:
        sources.append("meta.json")
    if summary:
        sources.append("summary.json")

    db = Path(str(db_path)).resolve() if db_path else None
    if db is None:
        candidate = sdir / "evidence.sqlite"
        db = candidate if candidate.is_file() else None

    events: list[dict[str, Any]] = []
    chain: dict[str, Any] = {"ok": False, "reason": "проверка не выполнялась",
                             "checked": 0, "events_checked": 0, "broken_at": -1,
                             "last_hash": "", "genesis": "", "available": False}

    db_mod = _load_db_module()
    if db is not None and db.is_file() and db_mod is not None:
        store = None
        try:
            store = db_mod.EvidenceStore(None, db_path=str(db))
            sessions = store.list_sessions()
            session_id = str(meta.get("session_id") or (sessions[0]["session_id"] if sessions else ""))
            row = next((s for s in sessions if str(s.get("session_id")) == session_id), None)
            if row is None and sessions:
                row = sessions[0]
                session_id = str(row.get("session_id") or "")
            if row is not None:
                # приоритет у данных из цепочки: meta.json/summary.json лежат рядом
                # с отчётом и не защищены хешем, поэтому они только дополняют.
                meta = {**meta, **(row.get("meta_parsed") or {})}
                stored_summary = row.get("summary_parsed") or {}
                summary = {**summary, **stored_summary} if stored_summary else summary
                chain["genesis"] = str(row.get("genesis_hash") or "")
            events = store.load_events(session_id or None)
            verified = store.verify_chain_detailed()
            chain.update(verified)
            chain["available"] = True
            chain.update(store.stats(session_id or None))
            sources.append(f"SQLite: {db.name}")
        except Exception as exc:
            chain["reason"] = f"не удалось прочитать базу: {exc}"
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

    if not events:
        jsonl = sdir / "events.jsonl"
        if jsonl.is_file():
            events, jmeta, jsummary, counts = _read_jsonl_events(jsonl)
            meta = {**jmeta, **meta}
            summary = {**jsummary, **summary} if summary else dict(jsummary)
            if events:
                sources.append("events.jsonl (резервный журнал)")
                chain.setdefault("reason", "")
                if not chain.get("available"):
                    # Цепочка из JSONL: hash и seq в записях есть, но пересчёт
                    # SQLite не выполнялся. Это ДРУГОЕ состояние, чем «пересчёт
                    # не сошёлся», и числа в футере должны быть настоящими.
                    chain["reason"] = ("журнал прочитан из резервного файла events.jsonl, "
                                       "пересчёт SQLite-цепочки не выполнялся")
                    chain["source"] = "events.jsonl"
                    chain["records"] = counts["records"]
                    chain["events_checked"] = counts["events_checked"]
                    if counts["genesis"] and not chain.get("genesis"):
                        chain["genesis"] = counts["genesis"]
                    if counts["last_hash"]:
                        chain["last_hash"] = counts["last_hash"]

    events.sort(key=lambda e: _f(e.get("ts")))
    return ReportData(session_dir=sdir, db_path=db, meta=meta, summary=summary,
                      events=events, chain=chain, sources=sources)


# ---------------------------------------------------------------------------
# Расчёты
# ---------------------------------------------------------------------------
def compute_integrity(events: list[dict[str, Any]],
                      weights: dict[str, float] | None = None,
                      scale: float = INTEGRITY_SCALE) -> dict[str, Any]:
    """Integrity score и его точное разложение по поведению, каналам и видам.

    Порядок расчёта (подробно — в докстринге модуля):

    1. срабатывания группируются по видам, у вида считается масса
       `сумма(вес * уверенность)` и самое тяжёлое одиночное срабатывание;
    2. виды собираются в КЛАСТЕРЫ наблюдаемого поведения (`BEHAVIOR_CLUSTERS`):
       все GAZE_* и HEAD_TURNED — один кластер, а не четыре повода;
    3. штраф кластера = база * min(1 + G*(sqrt(масса/база) - 1), CAP), то есть
       повторы учитываются с убывающей отдачей и упираются в потолок;
    4. штраф сессии = сумма по кластерам, integrity = 100*SCALE/(SCALE+штраф);
    5. штраф кластера делится между видами пропорционально массе вида, поэтому
       разложение по каналам остаётся точным при любой раскладке видов.
    """
    weights = weights if weights is not None else _risk_weights()
    kinds: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = str(event.get("kind") or "")
        if not kind or kind in SERVICE_KINDS:
            continue
        weight = float(weights.get(kind, 10.0))
        if weight <= 0:
            continue
        conf = min(max(_f(event.get("confidence"), 1.0), 0.2), 1.0)
        row = kinds.setdefault(kind, {
            "kind": kind, "label": _kind_label(kind),
            "channel": str(event.get("channel") or "system"),
            "cluster": _cluster_of(kind), "cluster_label": _cluster_label(_cluster_of(kind)),
            "weight": weight, "count": 0, "conf_sum": 0.0, "mass": 0.0, "peak": 0.0,
            "severity": str(event.get("severity") or "info"),
        })
        row["count"] += 1
        row["conf_sum"] += conf
        row["mass"] += weight * conf
        row["peak"] = max(row["peak"], weight * conf)
        if SEVERITY_ORDER.get(str(event.get("severity")), 0) > SEVERITY_ORDER.get(row["severity"], 0):
            row["severity"] = str(event.get("severity"))

    for row in kinds.values():
        row["mean_conf"] = round(row["conf_sum"] / max(row["count"], 1), 3)

    # --- кластеры наблюдаемого поведения ---
    clusters: dict[str, dict[str, Any]] = {}
    for row in kinds.values():
        node = clusters.setdefault(row["cluster"], {
            "cluster": row["cluster"], "label": row["cluster_label"],
            "mass": 0.0, "base": 0.0, "count": 0, "kinds": [],
        })
        node["mass"] += row["mass"]
        node["base"] = max(node["base"], row["peak"])
        node["count"] += row["count"]
        node["kinds"].append(row)

    penalty = 0.0
    for node in clusters.values():
        base = node["base"]
        if base <= 0 or node["mass"] <= 0:
            node["penalty"] = 0.0
            node["repeat_factor"] = 1.0
            node["capped"] = False
            continue
        raw_factor = 1.0 + REPEAT_GAIN * (math.sqrt(node["mass"] / base) - 1.0)
        factor = min(max(raw_factor, 1.0), REPEAT_CAP)
        node["repeat_factor"] = round(factor, 3)
        node["capped"] = raw_factor > REPEAT_CAP
        node["penalty"] = base * factor
        penalty += node["penalty"]

    # Штраф кластера делится между его видами по массе: сумма по видам равна
    # штрафу кластера, сумма по кластерам — штрафу сессии. Разложение точное.
    for node in clusters.values():
        for row in node["kinds"]:
            share = row["mass"] / node["mass"] if node["mass"] > 0 else 0.0
            row["penalty"] = node["penalty"] * share

    integrity = 100.0 * scale / (max(scale, 1.0) + penalty) if penalty > 0 else 100.0
    lost = 100.0 - integrity

    channels: dict[str, dict[str, Any]] = {}
    for row in kinds.values():
        ch = row["channel"] or "system"
        node = channels.setdefault(ch, {
            "channel": ch, "label": _channel_label(ch), "code": _channel_code(ch),
            "penalty": 0.0, "count": 0, "kinds": [],
        })
        node["penalty"] += row["penalty"]
        node["count"] += row["count"]
        node["kinds"].append(row)

    for node in channels.values():
        node["lost"] = round(lost * node["penalty"] / penalty, 2) if penalty > 0 else 0.0
        node["share"] = round(100.0 * node["penalty"] / penalty, 1) if penalty > 0 else 0.0
        node["kinds"].sort(key=lambda r: r["penalty"], reverse=True)
        for row in node["kinds"]:
            row["lost"] = round(lost * row["penalty"] / penalty, 2) if penalty > 0 else 0.0

    ordered = sorted(channels.values(), key=lambda n: n["penalty"], reverse=True)
    for node in clusters.values():
        node["lost"] = round(lost * node["penalty"] / penalty, 2) if penalty > 0 else 0.0
        node["penalty"] = round(node["penalty"], 2)
        node["mass"] = round(node["mass"], 2)
        node["base"] = round(node["base"], 2)
        node["kinds"] = sorted(node["kinds"], key=lambda r: r["penalty"], reverse=True)
    return {
        "score": round(integrity, 1),
        "lost": round(lost, 1),
        "penalty": round(penalty, 2),
        "scale": scale,
        "gain": REPEAT_GAIN,
        "cap": REPEAT_CAP,
        "channels": ordered,
        "clusters": sorted(clusters.values(), key=lambda n: n["penalty"], reverse=True),
        "kinds": sorted(kinds.values(), key=lambda r: r["penalty"], reverse=True),
    }


#: Вердикт по уровням. Формулировки по формуле гайда (раздел Alerts):
#: НАБЛЮДАЕМЫЙ ФАКТ -> КОНТЕКСТ -> ПОНЯТНОЕ ДЕЙСТВИЕ. Система сообщает
#: наблюдение; слов «нарушитель», «обман», «списывание» в отчёте нет,
#: потому что решение принимает преподаватель, а не детектор.
VERDICT_TEXTS: dict[str, dict[str, str]] = {
    "ok": {
        "label": "НОРМА",
        "title": "Отклонений от условий экзамена не зафиксировано",
        "text": "Отдельные срабатывания объясняются обычными движениями за столом "
                "и не образуют связок между каналами.",
        "action": "Результат можно принять без просмотра записи.",
    },
    "attention": {
        "label": "ВЫБОРОЧНАЯ ПРОВЕРКА",
        "title": "Зафиксированы единичные отклонения",
        "text": "Срабатывания не повторяются и не складываются в связки, но часть "
                "из них подкреплена кадрами и точным временем.",
        "action": "Откройте отмеченные моменты таймлайна и решите по существу.",
    },
    "doubt": {
        "label": "НУЖЕН РАЗБОР",
        "title": "Отклонения повторяются и подкреплены доказательствами",
        "text": "Срабатывания идут по нескольким каналам, их длительности и "
                "интервалы записаны в журнал и приведены ниже.",
        "action": "Разберите пункты таймлайна со студентом до выставления оценки.",
    },
    "fail": {
        "label": "РЕШЕНИЕ ПРЕПОДАВАТЕЛЯ",
        "title": "Зафиксированы продолжительные отклонения от условий экзамена",
        "text": "Срабатывания охватывают несколько каналов, часть подтверждена "
                "связками сигналов с точными интервалами.",
        "action": "Решение по работе принимает преподаватель на основании "
                  "приведённых ниже фактов.",
    },
}
#: Приписка, когда в журнале есть критичное срабатывание: оценка сама по себе
#: могла остаться высокой, а смотреть момент всё равно нужно.
CRITICAL_NOTE = ("В журнале есть срабатывание критичного уровня — его стоит "
                 "открыть независимо от итоговой оценки.")

#: Текст уровня «норма» для сессии, где не зафиксировано НИ ОДНОГО отклонения.
#: Без этой ветки документ объяснял бы срабатывания, которых не было: чип
#: состояния говорит «фиксаций не было», а абзац рядом — «отдельные
#: срабатывания объясняются обычными движениями за столом».
CLEAN_TEXT = ("За сессию не зафиксировано ни одного отклонения: в журнале "
              "только служебные записи о начале, калибровке и завершении.")


def integrity_verdict(score: float, has_critical: bool,
                      incidents: int = -1) -> dict[str, str]:
    """Вердикт по integrity score: факт, контекст, действие и уровень 1..4.

    Критичное срабатывание не даёт выдать верхний уровень даже при высокой
    оценке: одна подмена личности весит больше, чем десять спокойных минут.

    `incidents` — число содержательных записей журнала. Ноль означает чистую
    сессию, и объяснять в ней «отдельные срабатывания» нельзя: их не было.
    """
    key, index, color = _verdict_level(_f(score, 100.0))
    if has_critical and key == "ok":
        key, index, color = "attention", 3, WARNING
    base = VERDICT_TEXTS[key]
    text = CLEAN_TEXT if (key == "ok" and incidents == 0) else base["text"]
    if has_critical:
        text = f"{text} {CRITICAL_NOTE}"
    return {
        "level": key,
        "level_index": str(index),
        "level_label": base["label"],
        "title": base["title"],
        "text": text,
        "action": base["action"],
        "color": color,
    }


def risk_series(events: list[dict[str, Any]], t0: float, t1: float,
                weights: dict[str, float] | None = None,
                half_life: float = DEFAULT_HALF_LIFE,
                max_score: float | None = 100.0,
                points: int = 240) -> list[tuple[float, float]]:
    """Восстановить кривую risk-score: те же веса и то же затухание, что в рантайме.

    `max_score=None` — не обрезать накопленный счёт. Отчёт считает серию без
    обрезки и рисует потолок рантайма отдельной линией: на тяжёлой сессии
    обрезанная кривая вырождается в прямую поверх всех порогов и перестаёт
    показывать, где счёт к этим порогам подходил.
    """
    weights = weights if weights is not None else _risk_weights()
    items: list[tuple[float, float]] = []
    for event in events:
        kind = str(event.get("kind") or "")
        if kind in SERVICE_KINDS:
            continue
        weight = float(weights.get(kind, 10.0)) * min(max(_f(event.get("confidence"), 1.0), 0.2), 1.0)
        if weight > 0:
            items.append((_f(event.get("ts")), weight))
    span = max(t1 - t0, 1.0)
    step = span / max(points - 1, 1)
    hl = max(half_life, 1.0)
    out: list[tuple[float, float]] = []
    for i in range(points):
        t = t0 + i * step
        total = 0.0
        for ts, weight in items:
            if ts <= t:
                total += weight * math.pow(0.5, (t - ts) / hl)
        out.append((t, total if max_score is None else min(total, max_score)))
    return out


# ---------------------------------------------------------------------------
# Вшивание файлов-доказательств
# ---------------------------------------------------------------------------
class _Embedder:
    """base64-вшивание с бюджетом по объёму: отчёт обязан остаться открываемым."""

    def __init__(self, session_dir: Path, budget: int = EMBED_BUDGET_BYTES) -> None:
        self.session_dir = session_dir
        self.budget = budget
        self.used = 0
        self.skipped = 0
        self._cv: Any = None
        self._np: Any = None
        self._tried = False

    def _deps(self) -> tuple[Any, Any]:
        if not self._tried:
            self._tried = True
            try:
                import cv2  # type: ignore
                import numpy  # type: ignore
                self._cv, self._np = cv2, numpy
            except Exception:
                self._cv, self._np = None, None
        return self._cv, self._np

    def resolve(self, rel: str) -> Path | None:
        if not rel:
            return None
        path = Path(str(rel))
        if not path.is_absolute():
            path = self.session_dir / path
        return path if path.is_file() else None

    def image(self, rel: str, width: int = THUMB_WIDTH) -> str:
        """Миниатюра кадра-доказательства как data URI."""
        path = self.resolve(rel)
        if path is None:
            return ""
        cv, np = self._deps()
        blob: bytes | None = None
        if cv is not None and np is not None:
            try:
                image = cv.imread(str(path))
                if image is not None:
                    h, w = image.shape[:2]
                    if w > width:
                        scale = width / float(w)
                        image = cv.resize(image, (width, max(int(h * scale), 2)),
                                          interpolation=cv.INTER_AREA)
                    ok, buf = cv.imencode(".jpg", image, [int(cv.IMWRITE_JPEG_QUALITY), 72])
                    if ok:
                        blob = bytes(buf.tobytes())
            except Exception:
                blob = None
        if blob is None:
            try:
                raw = path.read_bytes()
                blob = raw if len(raw) <= 600 * 1024 else None
            except Exception:
                blob = None
        if blob is None or self.used + len(blob) > self.budget:
            self.skipped += 1
            return ""
        self.used += len(blob)
        return "data:image/jpeg;base64," + base64.b64encode(blob).decode("ascii")

    def clip(self, rel: str) -> tuple[str, str]:
        """(data URI клипа или пусто, имя файла). Крупные клипы не вшиваем."""
        path = self.resolve(rel)
        if path is None:
            return "", ""
        try:
            size = path.stat().st_size
        except Exception:
            return "", ""
        if size > CLIP_EMBED_LIMIT or self.used + size > self.budget:
            self.skipped += 1
            return "", path.name
        try:
            blob = path.read_bytes()
        except Exception:
            return "", path.name
        self.used += len(blob)
        mime = "video/mp4" if path.suffix.lower() == ".mp4" else "video/x-msvideo"
        return f"data:{mime};base64," + base64.b64encode(blob).decode("ascii"), path.name


# ---------------------------------------------------------------------------
# SVG — рисуем руками, без библиотек
#
# Атрибут xmlns у этих svg намеренно отсутствует: разметка инлайновая,
# парсер HTML и так помещает её в SVG-пространство имён. Зато в готовом
# отчёте не остаётся НИ ОДНОЙ строки "http://" — автономность файла
# проверяется обычным grep, а не на слово.
# ---------------------------------------------------------------------------
def _polar(cx: float, cy: float, r: float, value: float) -> tuple[float, float]:
    """Точка на шкале 0..100, отложенной по часовой стрелке от 12 часов."""
    rad = math.radians(3.6 * value)
    return cx + r * math.sin(rad), cy - r * math.cos(rad)


def _sev_marker(x: float, y: float, severity: str, size: float = 5.0,
                title: str = "", color: str | None = None) -> str:
    """Метка инцидента: ФОРМА по severity, цвет — вторым слоем.

    Форма обязательна: цвет не может быть единственным носителем смысла
    (требование a11y из гайда). Пять форм различимы и в ч/б печати.

    `color="currentColor"` нужен иконке внутри чипа: на светлой поверхности
    статусный цвет уходит в ЗАЛИВКУ чипа, а сама форма рисуется чёрным,
    иначе фигура сливается с собственной подложкой.
    """
    color = color or SEVERITY_COLORS.get(severity, GRAY_500)
    shape = SEVERITY_SHAPES.get(severity, "circle")
    tip = f"<title>{title}</title>" if title else ""
    if shape == "ring":
        body = (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{size:.1f}" fill="none" '
                f'stroke="{color}" stroke-width="2">{tip}</circle>')
    elif shape == "circle":
        body = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{size:.1f}" fill="{color}">{tip}</circle>'
    elif shape == "diamond":
        d = size * 1.25
        pts = f"{x:.1f},{y - d:.1f} {x + d:.1f},{y:.1f} {x:.1f},{y + d:.1f} {x - d:.1f},{y:.1f}"
        body = f'<polygon points="{pts}" fill="{color}">{tip}</polygon>'
    elif shape == "triangle":
        d = size * 1.35
        pts = (f"{x:.1f},{y - d:.1f} {x + d * 0.92:.1f},{y + d * 0.72:.1f} "
               f"{x - d * 0.92:.1f},{y + d * 0.72:.1f}")
        body = f'<polygon points="{pts}" fill="{color}">{tip}</polygon>'
    else:  # square
        d = size * 1.05
        body = (f'<rect x="{x - d:.1f}" y="{y - d:.1f}" width="{2 * d:.1f}" '
                f'height="{2 * d:.1f}" fill="{color}">{tip}</rect>')
    return body


def _sev_glyph(severity: str, size: int = 14) -> str:
    """Та же форма отдельной иконкой — для таблицы и карточек связок."""
    half = size / 2.0
    marker = _sev_marker(half, half, severity, size=size * 0.30, color="currentColor")
    return (f'<svg class="glyph" width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
            f'aria-hidden="true">{marker}</svg>')


def build_score_svg(integrity: dict[str, Any],
                    verdict: dict[str, str] | None = None) -> str:
    """Кольцо integrity score: крупное число внутри, пороги вердикта снаружи.

    Кислотный достаётся кольцу только на верхнем уровне — там он означает
    подтверждение. На остальных уровнях кольцо окрашено статусным цветом,
    и уровень продублирован номером и подписью, а не только цветом.
    """
    score = max(min(_f(integrity.get("score"), 100.0), 100.0), 0.0)
    key, index, color = _verdict_level(score)
    if verdict:
        key = str(verdict.get("level") or key)
        index = int(_f(verdict.get("level_index"), index))
        color = str(verdict.get("color") or color)
    label = VERDICT_TEXTS.get(key, VERDICT_TEXTS["fail"])["label"]

    cx, cy, r, w = 150.0, 130.0, 104.0, 24.0
    circumference = 2.0 * math.pi * r
    filled = circumference * score / 100.0

    # Подписи над кольцом нет намеренно: при оценке около 100 указатель
    # встаёт ровно на 12 часов и перечёркивает её, а заголовок секции и так
    # называет метрику крупно.
    parts: list[str] = [
        # дорожка кольца
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{GRAY_100}" '
        f'stroke-width="{w}"/>',
        # заполненная дуга: от 12 часов по часовой стрелке
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
        f'stroke-width="{w}" stroke-linecap="butt" '
        f'stroke-dasharray="{filled:.2f} {circumference - filled:.2f}" '
        f'transform="rotate(-90 {cx} {cy})"/>',
        # тонкий контур, чтобы кислотное кольцо на белом имело край
        f'<circle cx="{cx}" cy="{cy}" r="{r + w / 2:.1f}" fill="none" stroke="{GRAY_100}"/>',
        f'<circle cx="{cx}" cy="{cy}" r="{r - w / 2:.1f}" fill="none" stroke="{GRAY_100}"/>',
    ]

    # Пороги уровней вердикта снаружи кольца — видно, в какую зону попала оценка.
    for bound in (40.0, 65.0, 85.0):
        x1, y1 = _polar(cx, cy, r + w / 2 + 3, bound)
        x2, y2 = _polar(cx, cy, r + w / 2 + 10, bound)
        tx, ty = _polar(cx, cy, r + w / 2 + 22, bound)
        anchor = "start" if tx > cx + 2 else ("end" if tx < cx - 2 else "middle")
        parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                     f'stroke="{GRAY_500}" stroke-width="1"/>')
        parts.append(f'<text class="svg-mono" x="{tx:.1f}" y="{ty + 4:.1f}" '
                     f'text-anchor="{anchor}" font-size="11" fill="{GRAY_500}">'
                     f'{_num(bound, 0)}</text>')

    # Указатель на фактическое положение оценки.
    px, py = _polar(cx, cy, r + w / 2 + 2, score)
    qx, qy = _polar(cx, cy, r + w / 2 + 11, score)
    parts.append(f'<line x1="{px:.1f}" y1="{py:.1f}" x2="{qx:.1f}" y2="{qy:.1f}" '
                 f'stroke="{INK}" stroke-width="3"/>')

    parts.append(f'<text class="svg-num" x="{cx}" y="{cy + 24:.0f}" text-anchor="middle" '
                 f'font-size="76" fill="{INK}">{_num(score)}</text>')
    parts.append(f'<text class="svg-cap" x="{cx}" y="{cy + 50:.0f}" text-anchor="middle" '
                 f'font-size="11" fill="{GRAY_500}">ИЗ 100</text>')

    # Чип уровня: заливка статусным цветом, текст чёрный. На белом кислотный
    # допустим только так — заливкой под тёмным текстом, но не цветом текста.
    chip_w, chip_h, chip_y = 176.0, 30.0, 272.0
    parts.append(f'<rect x="{cx - chip_w / 2:.1f}" y="{chip_y}" width="{chip_w}" '
                 f'height="{chip_h}" rx="4" fill="{color}"/>')
    parts.append(f'<text class="svg-cap" x="{cx}" y="{chip_y + 20:.0f}" '
                 f'text-anchor="middle" font-size="12" fill="{INK}">'
                 f'УРОВЕНЬ {index} ИЗ 4</text>')
    parts.append(f'<text class="svg-cap" x="{cx}" y="{chip_y + 50:.0f}" '
                 f'text-anchor="middle" font-size="11" fill="{INK}">{_esc(label)}</text>')

    body = "\n".join(parts)
    return (f'<svg class="ring" viewBox="0 0 300 332" width="100%" role="img" '
            f'aria-label="Integrity score {_num(score)} из 100, уровень {index} из 4: '
            f'{_esc(label)}">{body}</svg>')


def _time_ticks(t0: float, t1: float, target: int = 8) -> list[float]:
    span = max(t1 - t0, 1.0)
    for step in (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200):
        if span / step <= target:
            break
    ticks = []
    t = t0
    while t <= t1 + 1e-6:
        ticks.append(t)
        t += step
    if len(ticks) < 2:
        ticks = [t0, t1]
    return ticks


#: Кегли подписей внутри SVG. Это НЕ CSS-пиксели: графики масштабируются по
#: ширине от viewBox 1000, поэтому вместе с ними съезжает и кегль. Чтобы
#: рабочая подпись не опускалась ниже 14 CSS px (прямое требование гайда),
#: SVG обёрнут в контейнер с `overflow-x:auto`, а `.plot` получает
#: `min-width`, равную ширине viewBox: при узком окне график ПРОКРУЧИВАЕТСЯ,
#: а не сжимается, и масштаб никогда не меньше 1:1.
SVG_FS_BODY = 14.0      # рабочие подписи: деления оси, подписи инцидентов, легенда
SVG_FS_SMALL = 14.0     # вторая строка подписи: время и код канала — тоже рабочая
SVG_FS_TINY = 13.0      # избыточный слой: НАЧАЛО/КОНЕЦ, названия форм в легенде
#: Ширина viewBox обоих графиков. Совпадает с `min-width` у `.plot` в `_CSS`.
PLOT_VB_WIDTH = 1000.0

#: Выше этого числа инцидентов таймлайн переходит на агрегацию по корзинам:
#: 200 меток на 912 единицах плота — это шаг 4.6 при размере метки 10, то есть
#: сплошной смаз, который не несёт ни «когда», ни «что».
TIMELINE_BUCKET_LIMIT = 60
#: Сколько подписей инцидентов таймлайн вообще может разложить по трём рядам.
TIMELINE_LABEL_ROWS = 3


def _plot_wrap(svg: str) -> str:
    """Обёртка с горизонтальной прокруткой — см. комментарий у SVG_FS_BODY."""
    return (f'<div class="plot-scroll" tabindex="0" role="group" '
            f'aria-label="График, прокручивается по горизонтали">{svg}</div>')


def _time_bucket_step(span: float, buckets: int) -> float:
    """Шаг корзины, округлённый до читаемого значения."""
    raw = span / max(buckets, 1)
    for step in (5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300, 600, 900, 1800, 3600):
        if step >= raw:
            return float(step)
    return float(raw)


def build_timeline_svg(events: list[dict[str, Any]], t0: float, t1: float) -> str:
    """Полоса сессии с метками инцидентов. Тёмная поверхность — это наблюдение.

    Два режима, потому что на объёме реальной сессии (cooldown gaze-правил 10 с
    даёт до ~540 срабатываний одного вида за 90 минут) поштучные метки
    перекрывают друг друга втрое и полоса превращается в смаз:

      * до `TIMELINE_BUCKET_LIMIT` инцидентов — поштучно: метка на каждое
        срабатывание, заливка в полосе = сколько держалось условие;
      * больше — агрегация по корзинам времени: столбик = сколько срабатываний
        в корзине, форма и цвет = самая высокая критичность корзины.

    В обоих режимах график ПЕЧАТАЕТ, сколько подписей он смог разложить и
    сколько их всего: «подписано 12 из 81, полный список — в таблице 04».
    Молча отбрасывать 85% подписей документ не имеет права — лид секции
    обещает показать, когда и что было зафиксировано.
    """
    width = PLOT_VB_WIDTH
    left, right = 64.0, 28.0
    plot = width - left - right
    span = max(t1 - t0, 1.0)

    def x_of(ts: float) -> float:
        return left + plot * min(max((_f(ts) - t0) / span, 0.0), 1.0)

    incidents = [e for e in events if str(e.get("kind") or "") not in SERVICE_KINDS]
    bucketed = len(incidents) > TIMELINE_BUCKET_LIMIT

    # Вертикальная раскладка. Под счётчик показанного зарезервированы две
    # строки: он обязан поместиться в viewBox целиком — svg молча обрезает всё,
    # что за его пределами. Полоса опускается ниже в режиме корзин: там над ней
    # стоят столбики высотой до BUCKET_COLUMN_MAX.
    bound_y = 146.0            # подписи НАЧАЛО / КОНЕЦ, ниже второй строки счётчика
    column_max = 62.0          # предельная высота столбика корзины
    band_h = 30.0
    band_y = (bound_y + 22.0 + column_max + 14.0) if bucketed else (bound_y + 42.0)
    row_y = (band_y + band_h + 64.0, band_y + band_h + 100.0, band_y + band_h + 136.0)
    legend_y = row_y[2] + 46.0
    height = legend_y + 34.0

    lead_1 = ("каждый столбик — корзина времени: высота = сколько срабатываний в ней, "
              "форма и цвет = самая высокая критичность корзины"
              if bucketed else
              "каждая метка — зафиксированный инцидент; форма и цвет метки = критичность,")
    lead_2 = ("поштучные метки на таком объёме накладывались бы друг на друга — "
              "точное время каждого срабатывания в таблице 04"
              if bucketed else
              "ширина заливки в полосе = сколько держалось условие")

    parts: list[str] = [
        f'<text class="svg-cap" x="{left}" y="24" font-size="{SVG_FS_BODY}" '
        f'fill="{WHITE}">ТАЙМЛАЙН СЕССИИ</text>',
        f'<text x="{left}" y="52" font-size="{SVG_FS_BODY}" fill="{GRAY_300}">'
        f'{_esc(lead_1)}</text>',
        f'<text x="{left}" y="74" font-size="{SVG_FS_BODY}" fill="{GRAY_300}">'
        f'{_esc(lead_2)}</text>',
        f'<rect x="{left}" y="{band_y}" width="{plot}" height="{band_h}" rx="4" '
        f'fill="{GRAPHITE}"/>',
    ]
    # Границы записи: сессия началась и закончилась ровно здесь.
    for x, text, anchor in ((left, "НАЧАЛО", "start"),
                            (left + plot, "КОНЕЦ", "end")):
        parts.append(f'<line x1="{x:.1f}" y1="{bound_y + 8}" x2="{x:.1f}" '
                     f'y2="{band_y + band_h + 8}" stroke="{WHITE}" stroke-width="2"/>')
        parts.append(f'<text class="svg-cap" x="{x:.1f}" y="{bound_y}" '
                     f'text-anchor="{anchor}" font-size="{SVG_FS_TINY}" '
                     f'fill="{GRAY_300}">{text}</text>')

    for tick in _time_ticks(t0, t1):
        x = x_of(tick)
        parts.append(f'<line x1="{x:.1f}" y1="{band_y + band_h}" x2="{x:.1f}" '
                     f'y2="{band_y + band_h + 7}" stroke="{GRAY_500}"/>')
        parts.append(f'<text class="svg-mono" x="{x:.1f}" y="{band_y + band_h + 26}" '
                     f'text-anchor="middle" font-size="{SVG_FS_BODY}" fill="{GRAY_500}">'
                     f'{_offset(tick, t0)}</text>')

    peak_bucket = 0
    bucket_step = 0.0
    if bucketed:
        # --- режим корзин ---
        bucket_step = _time_bucket_step(span, TIMELINE_BUCKET_LIMIT)
        buckets: dict[int, dict[str, Any]] = {}
        for event in incidents:
            index = int(max(_f(event.get("ts")) - t0, 0.0) // bucket_step)
            node = buckets.setdefault(index, {"count": 0, "severity": "info"})
            node["count"] += 1
            severity = str(event.get("severity") or "info")
            if SEVERITY_ORDER.get(severity, 0) > SEVERITY_ORDER.get(node["severity"], 0):
                node["severity"] = severity
        peak_bucket = max((n["count"] for n in buckets.values()), default=0)
        column_w = max(plot * bucket_step / span - 1.0, 2.0)
        top_h = column_max
        for index, node in sorted(buckets.items()):
            bucket_t0 = t0 + index * bucket_step
            x = x_of(bucket_t0)
            share = node["count"] / max(peak_bucket, 1)
            h = 8.0 + (top_h - 8.0) * share
            color = SEVERITY_COLORS.get(node["severity"], GRAY_500)
            tip = (f'{_offset(bucket_t0, t0)}—{_offset(bucket_t0 + bucket_step, t0)} · '
                   f'{node["count"]} фикс. · до '
                   f'{_esc(SEVERITY_LABELS.get(node["severity"], node["severity"]))}')
            parts.append(f'<rect x="{x:.1f}" y="{band_y - h:.1f}" '
                         f'width="{column_w:.1f}" height="{h:.1f}" fill="{color}" '
                         f'fill-opacity="0.55" stroke="{color}" stroke-width="1">'
                         f'<title>{tip}</title></rect>')
            parts.append(_sev_marker(x + column_w / 2.0, band_y - h - 9.0,
                                     node["severity"], size=4.5, title=tip))
            parts.append(f'<rect x="{x:.1f}" y="{band_y + 3}" width="{column_w:.1f}" '
                         f'height="{band_h - 6}" fill="{color}" fill-opacity="0.45"/>')
    else:
        for event in incidents:
            severity = str(event.get("severity") or "info")
            color = SEVERITY_COLORS.get(severity, GRAY_500)
            kind = str(event.get("kind") or "")
            x = x_of(event.get("ts"))
            duration = _f(event.get("duration"))
            if duration > 0.5:
                w = max(plot * (duration / span), 2.0)
                parts.append(f'<rect x="{x:.1f}" y="{band_y + 3}" width="{w:.1f}" '
                             f'height="{band_h - 6}" fill="{color}" fill-opacity="0.45"/>')
            parts.append(f'<line x1="{x:.1f}" y1="{band_y - 4}" x2="{x:.1f}" '
                         f'y2="{band_y + band_h}" stroke="{color}" stroke-width="2"/>')
            tooltip = (f'{_clock(event.get("ts"))} · {_esc(kind)} · '
                       f'{_esc(_kind_label(kind))} · '
                       f'{_esc(SEVERITY_LABELS.get(severity, severity))}')
            parts.append(_sev_marker(x, band_y - 15, severity, size=5.5, title=tooltip))

    # Подписи крупных инцидентов раскладываются по трём рядам. Ряд выбирается
    # не по «минимальной дистанции», а по реальному занятому интервалу: подпись
    # длиной 24 знака шире 190 единиц, и фиксированный зазор их всё равно
    # наложит. Не поместилась ни в один ряд — подписи нет, но метка на полосе
    # и строка в таблице инцидентов остаются, и ниже напечатано, сколько
    # подписей не поместилось.
    rows: list[list[tuple[float, float]]] = [[] for _ in range(TIMELINE_LABEL_ROWS)]
    label_parts: list[str] = []
    candidates = [e for e in incidents if SEVERITY_ORDER.get(str(e.get("severity")), 0) >= 3]
    labelled = 0
    for event in candidates:
        severity = str(event.get("severity") or "info")
        color = SEVERITY_COLORS.get(severity, GRAY_500)
        kind = str(event.get("kind") or "")
        x = x_of(event.get("ts"))
        label = _kind_label(kind)
        if len(label) > 24:
            label = label[:23] + "…"
        anchor = "start" if x < width - 340 else "end"
        tx = x + (9 if anchor == "start" else -9)
        # Ширина: подпись кеглем 14 плюс строка времени и кода канала моноширинным.
        text_w = max(len(label) * 7.8, 130.0) + 14.0
        span_from, span_to = ((tx, tx + text_w) if anchor == "start"
                              else (tx - text_w, tx))
        row = next((i for i in range(TIMELINE_LABEL_ROWS)
                    if all(span_to < start or span_from > end
                           for start, end in rows[i])), None)
        if row is None:
            continue
        rows[row].append((span_from, span_to))
        labelled += 1
        y = row_y[row]
        label_parts.append(
            f'<line x1="{x:.1f}" y1="{band_y + band_h + 30}" x2="{x:.1f}" '
            f'y2="{y - 13:.1f}" stroke="{color}" stroke-width="1" stroke-dasharray="2 3"/>'
            f'<text x="{tx:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
            f'font-size="{SVG_FS_BODY}" fill="{color}" font-weight="600">'
            f'{_esc(label)}</text>'
            f'<text class="svg-mono" x="{tx:.1f}" y="{y + 18:.1f}" text-anchor="{anchor}" '
            f'font-size="{SVG_FS_SMALL}" fill="{GRAY_300}">{_clock(event.get("ts"))} · '
            f'{_esc(_channel_code(str(event.get("channel") or "")))}</text>')
    parts.extend(label_parts)

    # Счётчик: что именно показано и где искать остальное. Без этой строки
    # график молча теряет подписи и при этом выглядит полным.
    if not incidents:
        counters = ["за сессию не зафиксировано ни одного срабатывания"]
    else:
        counters = [f"срабатываний {len(incidents)}"]
    if bucketed:
        counters.append(f"корзина {_num(bucket_step, 0)} с, "
                        f"в самой высокой {peak_bucket}")
    if candidates:
        counters.append(f"подписано {labelled} из {len(candidates)} крупных")
    if incidents:
        counters.append("полный список — в таблице 04")
    # Моноширинный: advance 0.6em, значит в строку плота влезает plot/(0.6*кегль)
    # знаков. Переносим по чанкам, не по символам, и печатаем максимум две
    # строки — место под них зарезервировано над полосой.
    per_line = max(int(plot / (0.6 * SVG_FS_SMALL)), 20)
    lines: list[str] = []
    for chunk in counters:
        if lines and len(lines[-1]) + 3 + len(chunk) <= per_line:
            lines[-1] = f"{lines[-1]} · {chunk}"
        else:
            lines.append(chunk)
    if len(lines) > 2:
        tail = " · ".join(lines[1:])
        lines = [lines[0], tail if len(tail) <= per_line else tail[:per_line - 1] + "…"]
    for index, line in enumerate(lines[:2]):
        parts.append(f'<text class="svg-mono" x="{left}" y="{100 + index * 20}" '
                     f'font-size="{SVG_FS_SMALL}" fill="{ACID}">'
                     f'{_esc(line)}</text>')

    # Легенда: форма + подпись. Цвет здесь — третий, избыточный слой.
    legend_x = left
    parts.append(f'<text class="svg-cap" x="{legend_x}" y="{legend_y}" '
                 f'font-size="{SVG_FS_TINY}" fill="{GRAY_500}">КРИТИЧНОСТЬ</text>')
    legend_x += 118
    for key in ("info", "low", "medium", "high", "critical"):
        parts.append(_sev_marker(legend_x, legend_y - 4, key, size=5.5))
        parts.append(f'<text x="{legend_x + 14}" y="{legend_y}" '
                     f'font-size="{SVG_FS_BODY}" fill="{GRAY_300}">'
                     f'{SEVERITY_LABELS[key]}</text>')
        parts.append(f'<text class="svg-mono" x="{legend_x + 14}" y="{legend_y + 17}" '
                     f'font-size="{SVG_FS_TINY}" fill="{GRAY_500}">'
                     f'{SHAPE_NAMES[SEVERITY_SHAPES[key]]}</text>')
        # Моноширинный даёт advance 0.6em: при кегле 13 это 7.9 на знак.
        legend_x += 36 + max(7.8 * len(SEVERITY_LABELS[key]),
                             7.9 * len(SHAPE_NAMES[SEVERITY_SHAPES[key]]))

    body = "\n".join(parts)
    mode = ("агрегировано по корзинам времени" if bucketed else "по срабатываниям")
    svg = (f'<svg class="plot" viewBox="0 0 {width:.0f} {height:.0f}" width="100%" '
           f'role="img" aria-label="Таймлайн инцидентов сессии, {mode}: '
           f'{len(incidents)} срабатываний">{body}</svg>')
    return _plot_wrap(svg)

def build_risk_svg(series: list[tuple[float, float]], t0: float, t1: float,
                   thresholds: tuple[float, float, float],
                   half_life: float = DEFAULT_HALF_LIFE,
                   cap: float = 100.0) -> str:
    """График восстановленного risk-score с порогами реакции. Поверхность тёмная.

    Ось НЕ обрезается на 100. На сессии с двумя сотнями срабатываний
    накопленный счёт уходит далеко за потолок рантайма, и жёсткая обрезка
    превращала кривую в прямую линию поверх всех трёх порогов: 95% точек лежали
    на потолке, и график перестал показывать то, что обещает лид секции, —
    где счёт подходил к порогам предупреждения, паузы и блокировки.

    Поэтому: ось растягивается до фактического пика, рантаймовый потолок
    (`cap`, `risk_max` из конфига) рисуется отдельной линией с подписью, а пик
    подписывается настоящим числом. Выше линии потолка рантайм счёт обрезал —
    об этом сказано прямо, а не спрятано формой кривой.
    """
    width = PLOT_VB_WIDTH
    left, right, bottom = 78.0, 28.0, 52.0
    plot_w = width - left - right
    plot_h = 146.0
    span = max(t1 - t0, 1.0)

    peak_value = max((value for _ts, value in series), default=0.0)
    # Верх оси: круглое число не ниже потолка рантайма и не ниже пика.
    axis_top = max(cap, 100.0)
    if peak_value > axis_top:
        axis_top = math.ceil(peak_value / 50.0) * 50.0
    clipped = peak_value > cap + 0.5
    # Объяснение обрезки занимает две строки: одной строкой оно шире viewBox,
    # а svg обрезает всё, что за его пределами, молча.
    top = 108.0 if clipped else 86.0
    height = top + plot_h + bottom

    def x_of(ts: float) -> float:
        return left + plot_w * min(max((ts - t0) / span, 0.0), 1.0)

    def y_of(value: float) -> float:
        return top + plot_h * (1.0 - min(max(value / axis_top, 0.0), 1.0))

    lead = (f'восстановлен по журналу теми же весами, что в рантайме; затухание '
            f'half-life {int(max(half_life, 1.0))} с')
    parts: list[str] = [
        f'<text class="svg-cap" x="{left}" y="24" font-size="{SVG_FS_BODY}" '
        f'fill="{WHITE}">RISK-SCORE ВО ВРЕМЕНИ</text>',
        f'<text x="{left}" y="52" font-size="{SVG_FS_BODY}" fill="{GRAY_300}">'
        f'{_esc(lead)}</text>',
        f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="{INK_2}" '
        f'stroke="{GRAPHITE}"/>',
    ]
    if clipped:
        parts.append(
            f'<text class="svg-mono" x="{left}" y="76" font-size="{SVG_FS_SMALL}" '
            f'fill="{ACID}">накопленный счёт доходил до {_num(peak_value, 0)}, '
            f'рантайм обрезает его на {_num(cap, 0)}</text>')
        parts.append(
            f'<text class="svg-mono" x="{left}" y="96" font-size="{SVG_FS_SMALL}" '
            f'fill="{ACID}">ось растянута до пика, иначе кривая легла бы прямой '
            f'поверх всех трёх порогов</text>')

    # Сетка значений: шаг круглый и не чаще пяти линий, иначе подписи сливаются.
    grid_step = 25.0
    while axis_top / grid_step > 6.0:
        grid_step *= 2.0
    value = 0.0
    while value <= axis_top + 1e-6:
        y = y_of(value)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
                     f'stroke="{GRAPHITE}"/>')
        parts.append(f'<text class="svg-mono" x="{left - 12}" y="{y + 5:.1f}" '
                     f'text-anchor="end" font-size="{SVG_FS_BODY}" fill="{GRAY_500}">'
                     f'{_num(value, 0)}</text>')
        value += grid_step

    # Подписи порогов. Когда ось растянута до пика, три линии сходятся в нижнюю
    # четверть и подписи наезжают друг на друга — тогда они расходятся ПО
    # ГОРИЗОНТАЛИ, а не исчезают: порог без подписи не читается.
    placed: list[tuple[int, float]] = []
    for value, label, color in zip(thresholds, ("предупреждение", "пауза", "блокировка"),
                                   (WARNING, PAUSE_ORANGE, DANGER)):
        y = y_of(value)
        slot = next(k for k in range(4)
                    if all(abs(py - y) >= 20.0 for ps, py in placed if ps == k))
        shift = 210.0 * slot
        placed.append((slot, y))
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
                     f'stroke="{color}" stroke-width="1" stroke-dasharray="6 4"/>')
        parts.append(f'<text class="svg-mono" x="{left + plot_w - 8 - shift:.1f}" '
                     f'y="{y - 8:.1f}" text-anchor="end" font-size="{SVG_FS_SMALL}" '
                     f'fill="{color}">{label} · {_num(value, 0)}</text>')
    if clipped:
        y = y_of(cap)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
                     f'stroke="{GRAY_300}" stroke-width="1" stroke-dasharray="2 4"/>')
        parts.append(f'<text class="svg-mono" x="{left + 8}" y="{y - 8:.1f}" '
                     f'font-size="{SVG_FS_SMALL}" fill="{GRAY_300}">'
                     f'потолок рантайма · {_num(cap, 0)}</text>')

    for tick in _time_ticks(t0, t1):
        x = x_of(tick)
        parts.append(f'<line x1="{x:.1f}" y1="{top + plot_h}" x2="{x:.1f}" '
                     f'y2="{top + plot_h + 7}" stroke="{GRAY_500}"/>')
        parts.append(f'<text class="svg-mono" x="{x:.1f}" y="{top + plot_h + 26}" '
                     f'text-anchor="middle" font-size="{SVG_FS_BODY}" fill="{GRAY_500}">'
                     f'{_offset(tick, t0)}</text>')

    if series:
        points = " ".join(f"{x_of(ts):.1f},{y_of(value):.1f}" for ts, value in series)
        first_x = x_of(series[0][0])
        last_x = x_of(series[-1][0])
        base_y = y_of(0)
        parts.append(f'<polygon points="{first_x:.1f},{base_y:.1f} {points} '
                     f'{last_x:.1f},{base_y:.1f}" fill="{ACID}" fill-opacity="0.14"/>')
        parts.append(f'<polyline points="{points}" fill="none" stroke="{ACID}" '
                     f'stroke-width="2"/>')
        peak_ts, peak = max(series, key=lambda item: item[1])
        if peak > 1.0:
            px, py = x_of(peak_ts), y_of(peak)
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4.5" fill="{ACID}"/>')
            anchor = "start" if px < width - 230 else "end"
            dx = 12 if anchor == "start" else -12
            # Высокий пик приходится на ту же строку, где справа подписан порог
            # блокировки. Подпись пика в этом случае уходит ПОД точку, иначе две
            # мелкие строки накладываются именно в самом важном месте графика.
            label_y = (py + 24) if py < top + 50 else (py - 14)
            peak_text = f"пик {_num(peak)} · {_clock(peak_ts)}"
            # Подложка: подпись пика почти всегда ложится на штриховую линию
            # порога, и без плашки две мелкие строки читаются как одна.
            plate_w = len(peak_text) * 8.4 + 12.0
            plate_x = (px + dx - 6) if anchor == "start" else (px + dx - plate_w + 6)
            parts.append(f'<rect x="{plate_x:.1f}" y="{label_y - 15:.1f}" '
                         f'width="{plate_w:.1f}" height="20" fill="{INK_2}"/>')
            parts.append(f'<text class="svg-mono" x="{px + dx:.1f}" '
                         f'y="{label_y:.1f}" text-anchor="{anchor}" '
                         f'font-size="{SVG_FS_BODY}" fill="{ACID}">{peak_text}</text>')

    body = "\n".join(parts)
    svg = (f'<svg class="plot" viewBox="0 0 {width:.0f} {height:.0f}" width="100%" '
           f'role="img" aria-label="График risk-score за сессию, пик '
           f'{_num(peak_value)} при потолке рантайма {_num(cap, 0)}">{body}</svg>')
    return _plot_wrap(svg)
# ---------------------------------------------------------------------------
# HTML-фрагменты
# ---------------------------------------------------------------------------
def _detail_rows(detail: dict[str, Any], limit: int = 14) -> str:
    rows: list[str] = []
    for key, value in list(detail.items())[:limit]:
        label = DETAIL_LABELS.get(str(key), str(key))
        if isinstance(value, float):
            shown = _num(value, 3)
        elif isinstance(value, bool):
            shown = "да" if value else "нет"
        elif isinstance(value, (dict, list)):
            shown = json.dumps(value, ensure_ascii=False)
        else:
            shown = str(value)
        if len(shown) > 220:
            shown = shown[:217] + "…"
        rows.append(f'<div class="kv"><span class="k">{_esc(label)}</span>'
                    f'<span class="v">{_esc(shown)}</span></div>')
    return "".join(rows) or '<div class="muted">подробности не записаны</div>'


#: Инлайновые SVG-иконки. Ни одного внешнего ресурса и ни одного эмодзи —
#: эмодзи рендерятся шрифтом ОС и ломают и печать, и тон документа.
_ICON_OK = ('<svg class="icon" viewBox="0 0 20 20" width="20" height="20" '
            'aria-hidden="true">'
            '<path d="M3 10.5 L7.5 15 L17 5" fill="none" stroke="currentColor" '
            'stroke-width="2.4" stroke-linecap="square"/></svg>')
_ICON_ALERT = ('<svg class="icon" viewBox="0 0 20 20" width="20" height="20" '
               'aria-hidden="true">'
               '<path d="M10 2 L19 18 H1 Z" fill="none" stroke="currentColor" '
               'stroke-width="2"/><path d="M10 7.5 V12" stroke="currentColor" '
               'stroke-width="2"/><circle cx="10" cy="15" r="1.2" fill="currentColor"/></svg>')
_ICON_MARK = ('<svg class="mark" viewBox="0 0 34 34" width="34" height="34" '
              'aria-hidden="true">'
              f'<rect x="1" y="1" width="32" height="32" rx="7" fill="none" '
              f'stroke="{ACID}" stroke-width="2"/>'
              f'<circle cx="17" cy="17" r="4.5" fill="{ACID}"/></svg>')


def _evidence_cell(event: dict[str, Any], embed: _Embedder,
                   empty: str = '<span class="muted mono">нет кадра</span>') -> str:
    evidence = event.get("evidence") or {}
    if not isinstance(evidence, dict):
        return empty
    frame = str(evidence.get("frame_path") or "")
    clip = str(evidence.get("clip_path") or "")
    bits: list[str] = []
    uri = embed.image(frame) if frame else ""
    if uri:
        bits.append(f'<figure class="ev"><img class="thumb" src="{uri}" '
                    f'alt="кадр момента фиксации" title="{_esc(frame)}">'
                    f'<figcaption class="mono">{_esc(Path(frame).name)}</figcaption></figure>')
    elif frame:
        bits.append(f'<span class="muted mono">кадр {_esc(Path(frame).name)} '
                    f'не вшит</span>')
    if clip:
        bits.append(f'<span class="muted mono">клип {_esc(Path(clip).name)}</span>')
    if not bits:
        return empty
    return "".join(bits)


def build_masthead_html(meta: dict[str, Any], summary: dict[str, Any]) -> str:
    """Служебная полоса продукта. Тёмная — это хром системы, не среда чтения."""
    session = str(meta.get("session_id") or summary.get("session_id") or "—")
    return (
        f'<div class="masthead"><div class="masthead-inner">'
        f'<div class="masthead-brand">{_ICON_MARK}'
        f'<span class="brand-text"><strong class="wordmark">NEON/PROCTOR</strong>'
        f'<span class="mono caps brand-sub">Local proctoring</span></span></div>'
        f'<div class="masthead-side">'
        f'<span class="badge mono caps">архивная запись</span>'
        f'<span class="mono masthead-id">{_esc(session)}</span>'
        f'</div></div></div>'
    )


def build_header_html(meta: dict[str, Any], summary: dict[str, Any],
                      integrity: dict[str, Any], verdict: dict[str, str],
                      stats: dict[str, Any]) -> str:
    """Вердикт крупно + паспорт сессии. Светлая поверхность: это чтение и анализ."""
    started = _f(summary.get("started_at") or meta.get("started_at"))
    ended = _f(summary.get("ended_at"))
    duration = _f(summary.get("duration_sec")) or max(ended - started, 0.0)
    rows = [
        ("Студент", (meta.get("student_name") or summary.get("student_name") or "—"), False),
        ("Идентификатор", (meta.get("student_id") or summary.get("student_id") or "—"), True),
        ("Экзамен", (meta.get("exam_id") or summary.get("exam_id") or "—"), True),
        ("Сессия", (meta.get("session_id") or summary.get("session_id") or "—"), True),
        ("Начало", _full_time(started) if started else "—", True),
        ("Окончание", _full_time(ended) if ended else "сессия не закрыта штатно", True),
        ("Длительность", _dur(duration) if duration else "—", True),
        ("Причина завершения", (summary.get("end_reason") or "—"), False),
    ]
    cells = "".join(
        f'<div class="meta-item"><div class="meta-k mono caps">{_esc(k)}</div>'
        f'<div class="meta-v{" mono" if mono else ""}">{_esc(v)}</div></div>'
        for k, v, mono in rows)
    chips = "".join(
        f'<span class="chip chip-{_esc(key)}">{_sev_glyph(key)}'
        f'<span class="chip-text">{_esc(SEVERITY_LABELS.get(key, key))}</span>'
        f'<span class="chip-num mono">{_esc(value)}</span></span>'
        for key, value in (stats.get("by_severity") or {}).items() if value)
    if not chips:
        chips = ('<span class="chip chip-info"><span class="chip-text">'
                 'фиксаций не было</span></span>')
    return (
        f'<div class="verdict" style="--vc:{verdict.get("color", INK)}">'
        f'<div class="verdict-head">'
        f'<span class="verdict-level mono caps">уровень '
        f'{_esc(verdict.get("level_index", "—"))} из 4</span>'
        f'<span class="verdict-badge mono caps">{_esc(verdict.get("level_label", ""))}</span>'
        f'</div>'
        f'<h2 class="verdict-title">{_esc(verdict.get("title", ""))}</h2>'
        f'<p class="verdict-text">{_esc(verdict.get("text", ""))}</p>'
        f'<p class="verdict-action"><span class="mono caps">что делать</span>'
        f'{_esc(verdict.get("action", ""))}</p>'
        f'<div class="chips">{chips}</div>'
        f'</div>'
        f'<div class="meta-grid">{cells}</div>'
    )


def build_breakdown_html(integrity: dict[str, Any], summary: dict[str, Any]) -> str:
    """Разложение потерянных баллов по каналам — горизонтальные полосы.

    Полосы монохромные: каждая снабжена подписью, кодом канала и числом,
    поэтому различать каналы цветом не нужно (и восьми различимых цветов в
    палитре из трёх базовых нет). Кислотным помечен только ведущий канал —
    тонкой линией, а не заливкой, и дополнительно словами.
    """
    channels = integrity.get("channels") or []
    lost = _f(integrity.get("lost"))
    score = _f(integrity.get("score"), 100.0)
    current, _index, _color = _verdict_level(score)

    level_items: list[str] = []
    for (lo, key, _idx, color), hi in zip(VERDICT_LEVELS, (100.0, 85.0, 65.0, 40.0)):
        is_current = key == current
        tag = '<span class="mono caps level-tag">текущий</span>' if is_current else ""
        label = VERDICT_TEXTS[key]["label"].lower()
        level_items.append(
            f'<li class="level{" is-current" if is_current else ""}">'
            f'<span class="level-swatch" style="--c:{color}"></span>'
            f'<span class="mono level-range">{_num(lo, 0)}&#8201;—&#8201;{_num(hi, 0)}</span>'
            f'<span class="level-name">{_esc(label)}</span>{tag}</li>')
    levels = "".join(level_items)

    top = max((_f(n.get("lost")) for n in channels), default=0.0)
    bars: list[str] = []
    for position, node in enumerate(channels):
        node_lost = _f(node.get("lost"))
        width = (node_lost / top * 100.0) if top > 0 else 0.0
        kinds = "".join(
            f'<li><span class="code">{_esc(row.get("kind", ""))}</span>'
            f'<span class="bar-kind-label">{_esc(row.get("label", ""))}</span>'
            f'<span class="mono bar-kind-num">{_esc(row.get("count", 0))} фикс. · вес '
            f'{_num(_f(row.get("weight")), 0)} · &#8722;{_num(_f(row.get("lost")), 2)}</span>'
            f'</li>'
            for row in node.get("kinds") or [])
        lead = position == 0 and node_lost > 0
        lead_tag = '<span class="mono caps bar-tag">основной вклад</span>' if lead else ""
        bars.append(
            f'<div class="bar-row{" is-lead" if lead else ""}">'
            f'<div class="bar-top">'
            f'<span class="code">{_esc(node.get("code", "—"))}</span>'
            f'<span class="bar-name">{_esc(node.get("label", ""))}</span>'
            f'{lead_tag}'
            f'<span class="mono bar-value">&#8722;{_num(node_lost, 2)}</span>'
            f'</div>'
            f'<div class="bar-track"><div class="bar-fill" '
            f'style="width:{max(width, 1.0):.1f}%"></div></div>'
            f'<div class="mono bar-foot">{_esc(node.get("count", 0))} фиксаций · '
            f'{_num(_f(node.get("share")), 1)}% штрафа сессии</div>'
            f'<ul class="bar-kinds">{kinds}</ul>'
            f'</div>')

    body = ("".join(bars) if bars else
            '<p class="muted">Штрафов нет: оценка доверия за сессию не снижалась.</p>')

    final_risk = _f(summary.get("final_risk"))
    final_action = str(summary.get("final_action") or "none")
    actions = {"none": "действий не требовалось", "warn": "выдано предупреждение",
               "pause": "тест приостанавливался", "lock": "сессия заблокирована"}
    clusters = integrity.get("clusters") or []
    cluster_items = "".join(
        f'<li><span class="cluster-name">{_esc(node.get("label", ""))}</span>'
        f'<span class="mono cluster-num">{_esc(node.get("count", 0))} фикс. · '
        f'тяжелейшее одиночное {_num(_f(node.get("base")), 1)} · '
        f'повтор &#215;{_num(_f(node.get("repeat_factor"), 1.0), 2)}'
        f'{" (потолок)" if node.get("capped") else ""} · '
        f'штраф {_num(_f(node.get("penalty")), 1)}</span></li>'
        for node in clusters)
    clusters_block = (
        f'<h3 class="sub-h">Наблюдаемое поведение и его штраф</h3>'
        f'<ul class="clusters">{cluster_items}</ul>'
        if cluster_items else "")

    return (
        f'<ul class="levels">{levels}</ul>'
        f'<p class="lead">Integrity score = 100 · {int(INTEGRITY_SCALE)} / '
        f'({int(INTEGRITY_SCALE)} + штраф). Штраф считается по наблюдаемому '
        f'поведению, а не по видам событий: все отводы взгляда и повороты головы — '
        f'это одно поведение, и повторы внутри него учитываются с убывающей отдачей '
        # Знака корня здесь нет намеренно: U+221A не содержится ни в одном из
        # вшитых шрифтов и ушёл бы в системный фолбэк — ровно тот дефект, из-за
        # которого казахские буквы рвали моноширинную сетку. Пишем словом.
        f'(корень из массы с коэффициентом {_num(REPEAT_GAIN, 2)}) и не выше '
        f'{_num(REPEAT_CAP, 0)} тяжелейших одиночных срабатываний. Поэтому привычка '
        f'смотреть на клавиатуру не весит как сигнал другого канала, а раскладка '
        f'одного и того же поведения по нескольким видам не меняет оценку. '
        f'Суммарный штраф сессии — {_num(_f(integrity.get("penalty")), 2)}, '
        f'потеряно баллов — {_num(lost)}.</p>'
        f'{clusters_block}'
        f'<h3 class="sub-h">Вклад каналов в потерянные баллы</h3>'
        f'<div class="bars">{body}</div>'
        f'<p class="note">Мгновенный risk-score на момент завершения — '
        f'{_num(final_risk)} ({_esc(actions.get(final_action, final_action))}). Он затухает '
        f'со временем и описывает ситуацию «здесь и сейчас»; integrity score оценивает '
        f'сессию целиком и не затухает. Обе величины — мера зафиксированных отклонений '
        f'от условий экзамена, а не вероятность чего-либо.</p>'
    )


def _disclosure(label_closed: str, label_open: str, body: str,
                extra_class: str = "") -> str:
    """<details> с аффордансом, который не держится на одном цвете.

    Правило `summary{display:flex}` убирает нативный треугольник раскрытия,
    поэтому состояние несут три слоя: инлайновый шеврон (поворот по
    `details[open]`), ПОДПИСЬ, которая меняется со «показать» на «свернуть»,
    и только потом цвет. Требование a11y из гайда: цвет никогда не
    единственный носитель смысла — при ч/б печати и при цветовой слепоте
    раскрытие обязано читаться как раскрытие, а не как обычная строка.
    Переключение подписи сделано на CSS, без скриптов: в отчёте их нет.
    """
    chevron = ('<svg class="chev" viewBox="0 0 12 12" width="12" height="12" '
               'aria-hidden="true"><path d="M3 1.5 L8.5 6 L3 10.5" fill="none" '
               'stroke="currentColor" stroke-width="2" stroke-linecap="square"/></svg>')
    cls = f' class="{extra_class}"' if extra_class else ""
    return (f'<details{cls}><summary>{chevron}'
            f'<span class="sum-closed">{_esc(label_closed)}</span>'
            f'<span class="sum-open">{_esc(label_open)}</span></summary>'
            f'{body}</details>')


def _incident_code(event: dict[str, Any]) -> str:
    """Код инцидента из `detail['code']` — то, по чему подаётся апелляция.

    Движок ставит его каждому событию (`engine/events.py`), и подменять его
    отчёт не вправе. Кода нет (старый журнал, внешнее событие без детали) —
    колонка показывает вид события, как раньше.
    """
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    code = str(detail.get("code") or "").strip()
    return code


def build_incidents_html(events: list[dict[str, Any]], t0: float,
                         embed: _Embedder) -> str:
    """Таблица инцидентов по паттерну .data-table: тёмная, с кодами и кадрами.

    Иерархия ячейки «Наблюдение»: КРУПНО — `message` из движка, он построен по
    формуле гайда «факт -> контекст -> действие»; мелко под ним — техническая
    подпись вида. Обратный порядок означал бы, что документ подменяет
    наблюдение движка собственной короткой формулировкой, а она по природе
    своей ближе к ярлыку, чем к наблюдению.
    """
    rows: list[str] = []
    for event in events:
        kind = str(event.get("kind") or "")
        if kind in SERVICE_KINDS:
            continue
        severity = str(event.get("severity") or "info")
        channel = str(event.get("channel") or "")
        message = str(event.get("message") or "").strip()
        label = _kind_label(kind)
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        extra = ""
        if detail:
            extra = _disclosure("показать показания детектора",
                                "свернуть показания детектора",
                                f'<div class="kv-list">{_detail_rows(detail)}</div>')
        duration = _f(event.get("duration"))
        code = _incident_code(event)
        if code:
            code_cell = (f'<span class="code code-kind">{_esc(code)}</span>'
                         f'<div class="cell-sub mono">{_esc(kind)}</div>')
        else:
            code_cell = f'<span class="code code-kind">{_esc(kind)}</span>'
        if message:
            observation = (f'<div class="obs">{_esc(message)}</div>'
                           f'<div class="obs-kind mono">{_esc(label)}</div>')
        else:
            observation = f'<div class="obs">{_esc(label)}</div>'
        rows.append(
            f'<tr>'
            f'<td class="nowrap"><div class="mono t-main">{_clock(event.get("ts"))}</div>'
            f'<div class="mono t-sub">{_offset(event.get("ts"), t0)}</div></td>'
            f'<td class="nowrap"><span class="code">{_esc(_channel_code(channel))}</span>'
            f'<div class="cell-sub">{_esc(_channel_label(channel))}</div></td>'
            f'<td class="nowrap">{code_cell}</td>'
            f'<td class="cell-wide">{observation}{extra}</td>'
            f'<td class="nowrap"><span class="sev sev-{_esc(severity)}">'
            f'{_sev_glyph(severity)}'
            f'{_esc(SEVERITY_LABELS.get(severity, severity))}</span></td>'
            f'<td class="num mono">{_num(_f(event.get("confidence"), 1.0) * 100, 0)}%</td>'
            f'<td class="num mono">{(_num(duration) + " с") if duration else "—"}</td>'
            f'<td>{_evidence_cell(event, embed)}</td>'
            f'</tr>')
    if not rows:
        return ('<p class="muted">За сессию не зафиксировано ни одного инцидента. '
                'В журнале только служебные записи о начале и завершении сессии.</p>')
    # Длинная таблица прокручивается внутри своего контейнера — иначе липкая
    # шапка не к чему прилипать (см. комментарий у .table-wrap.is-long в _CSS).
    long_cls = " is-long" if len(rows) > 25 else ""
    return (
        f'<div class="table-wrap{long_cls}" tabindex="0" role="group" '
        f'aria-label="Таблица инцидентов, {len(rows)} строк">'
        '<table class="data-table"><thead><tr>'
        '<th>Время</th><th>Канал</th><th>Код</th><th>Наблюдение</th>'
        '<th>Критичность</th><th>Уверен&shy;ность</th><th>Длит.</th>'
        '<th>Доказательство</th>'
        '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
    )


#: Как называется первый сигнал связки — для разбора таймингов.
FUSION_SIGNAL_NAMES = {
    "FUSION_GAZE_THEN_ANSWER": "взгляд отведён от экрана",
    "FUSION_BLUR_THEN_ANSWER": "окно экзамена вне фокуса",
    "FUSION_PHONE_THEN_ANSWER": "телефон в кадре",
}


def _timing_numbers(event: dict[str, Any]) -> tuple[float, float, Any]:
    """(длительность первого сигнала, пауза до ответа, объём ответа) из detail."""
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    signal = _f(detail.get("gaze_duration")) or _f(detail.get("duration")) \
        or _f(detail.get("gap_sec")) or _f(event.get("duration"))
    delay = _f(detail.get("delay"))
    if not delay:
        delay = _f(detail.get("delay_ms")) / 1000.0
    if not delay:
        delay = _f(detail.get("time_to_answer_ms")) / 1000.0
    volume = detail.get("chars", detail.get("length"))
    return signal, delay, volume


def build_fusion_timing_svg(event: dict[str, Any]) -> str:
    """Разбор таймингов одной связки: сигнал, пауза, ответ на одной шкале.

    Тёмная поверхность внутри светлой секции: это телеметрия, а не текст.
    Пустая строка, если в журнале нет ни длительности сигнала, ни паузы —
    рисовать нечего, и выдумывать числа отчёт не имеет права.
    """
    signal, delay, volume = _timing_numbers(event)
    if signal <= 0 and delay <= 0:
        return ""
    kind = str(event.get("kind") or "")
    severity = str(event.get("severity") or "high")
    color = SEVERITY_COLORS.get(severity, GRAY_500)
    name = FUSION_SIGNAL_NAMES.get(kind, "сигнал канала")

    width, height = 760.0, 134.0
    left, right = 54.0, 26.0
    plot = width - left - right
    total = max(signal + delay, 0.001)
    # 12% справа оставлено под метку ответа, иначе она уедет за край.
    scale = (plot * 0.88) / total
    x_signal_end = left + max(signal * scale, 8.0 if signal > 0 else 0.0)
    x_answer = x_signal_end + max(delay * scale, 10.0 if delay > 0 else 0.0)
    axis_y = 98.0

    parts: list[str] = [
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="8" fill="{INK}"/>',
        f'<text class="svg-cap" x="{left}" y="24" font-size="{SVG_FS_TINY}" '
        f'fill="{GRAY_500}">РАЗБОР ТАЙМИНГОВ СВЯЗКИ</text>',
        f'<line x1="{left}" y1="{axis_y}" x2="{width - right}" y2="{axis_y}" '
        f'stroke="{GRAPHITE}" stroke-width="1"/>',
    ]
    if signal > 0:
        parts.append(f'<rect x="{left}" y="{axis_y - 22}" '
                     f'width="{x_signal_end - left:.1f}" height="22" fill="{color}" '
                     f'fill-opacity="0.38" stroke="{color}"/>')
        parts.append(f'<text class="svg-mono" x="{left + 6}" y="{axis_y - 30}" '
                     f'font-size="{SVG_FS_BODY}" fill="{GRAY_300}">{_esc(name)} · '
                     f'{_num(signal)} с</text>')
    if delay > 0:
        parts.append(f'<line x1="{x_signal_end:.1f}" y1="{axis_y - 11}" '
                     f'x2="{x_answer:.1f}" y2="{axis_y - 11}" stroke="{GRAY_300}" '
                     f'stroke-width="1" stroke-dasharray="4 3"/>')
        mid = (x_signal_end + x_answer) / 2.0
        parts.append(f'<text class="svg-mono" x="{mid:.1f}" y="{axis_y + 22}" '
                     f'text-anchor="middle" font-size="{SVG_FS_BODY}" fill="{GRAY_300}">'
                     f'пауза {_num(delay)} с</text>')
    # Ответ — ключевая точка связки, единственный кислотный элемент схемы.
    parts.append(f'<line x1="{x_answer:.1f}" y1="{axis_y - 34}" x2="{x_answer:.1f}" '
                 f'y2="{axis_y + 6}" stroke="{ACID}" stroke-width="2"/>')
    parts.append(f'<circle cx="{x_answer:.1f}" cy="{axis_y}" r="4" fill="{ACID}"/>')
    answer_label = "ответ записан"
    if volume not in (None, ""):
        answer_label = f"ответ записан · {volume} знаков"
    anchor = "start" if x_answer < width - 240 else "end"
    dx = 8 if anchor == "start" else -8
    parts.append(f'<text class="svg-mono" x="{x_answer + dx:.1f}" y="{axis_y - 44}" '
                 f'text-anchor="{anchor}" font-size="{SVG_FS_BODY}" fill="{ACID}">'
                 f'{_esc(answer_label)}</text>')
    parts.append(f'<text class="svg-mono" x="{left}" y="{axis_y + 22}" '
                 f'font-size="{SVG_FS_TINY}" fill="{GRAY_500}">0 с</text>')

    body = "\n".join(parts)
    # Та же защита подписей, что у графиков раздела 02: схема не сжимается
    # ниже 1:1, а прокручивается — иначе на узком окне тайминги связки,
    # главное доказательство в документе, уходят в 4 px.
    svg = (f'<svg class="timing" viewBox="0 0 {width:.0f} {height:.0f}" width="100%" '
           f'role="img" aria-label="Тайминги связки: {_esc(name)} {_num(signal)} с, '
           f'пауза {_num(delay)} с, затем ответ">{body}</svg>')
    return _plot_wrap(svg)


def build_fusion_html(events: list[dict[str, Any]], t0: float, embed: _Embedder) -> str:
    """Раздел связок: главное отличие системы, поэтому с подробным разбором."""
    fusion_kinds = _fusion_kinds()
    items = [e for e in events if str(e.get("kind") or "") in fusion_kinds]
    intro = (
        '<p class="lead">Связка — не одно срабатывание, а совпадение сигналов разной '
        'природы на одной временной шкале. По отдельности «взгляд ушёл в сторону» и '
        '«быстро введён длинный ответ» не значат ничего; вместе, с точными интервалами, '
        'они образуют проверяемый факт. Ниже разобрана каждая связка в том виде, в '
        'котором она была зафиксирована во время экзамена.</p>'
    )
    if not items:
        return intro + ('<p class="muted">Связок за сессию не зафиксировано: совпадений '
                        'поведения ввода и сигналов камеры в окне корреляции не было.</p>')
    cards: list[str] = []
    for event in items:
        kind = str(event.get("kind") or "")
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        severity = str(event.get("severity") or "high")
        thumb = _evidence_cell(event, embed, empty="")
        timing = build_fusion_timing_svg(event)
        raw = json.dumps(event.get("detail") or {}, ensure_ascii=False, indent=2,
                         sort_keys=True)
        evidence_block = (f'<div class="fusion-ev">{thumb}</div>') if thumb else ""
        cards.append(
            f'<article class="fusion-card">'
            f'<header class="fusion-head">'
            f'<span class="code code-kind">{_esc(_incident_code(event) or kind)}</span>'
            f'<h3>{_esc(_kind_label(kind))}</h3>'
            f'<span class="sev sev-{_esc(severity)}">{_sev_glyph(severity)}'
            f'{_esc(SEVERITY_LABELS.get(severity, severity))}</span>'
            f'<span class="mono fusion-time">{_clock(event.get("ts"))} · '
            f'{_offset(event.get("ts"), t0)}</span></header>'
            f'<p class="fusion-msg">{_esc(str(event.get("message") or _kind_label(kind)))}</p>'
            f'{timing}'
            f'<div class="fusion-body">'
            f'<div class="kv-list">{_detail_rows(detail, limit=20)}</div>'
            f'{evidence_block}</div>'
            f'<div class="mono fusion-foot">уверенность связки '
            f'{_num(_f(event.get("confidence"), 1.0) * 100, 0)}% · запись цепочки '
            f'#{_esc(event.get("_seq", "—"))} · hash '
            f'{_esc(str(event.get("_hash") or "—")[:16])}</div>'
            + _disclosure("показать сырые данные связки, как записаны в журнал",
                          "свернуть сырые данные связки",
                          f'<pre>{_esc(raw)}</pre>')
            + '</article>')
    return intro + "".join(cards)


def build_integrity_html(chain: dict[str, Any], signature: dict[str, Any],
                         db_path: Path | None, report_name: str) -> str:
    ok = bool(chain.get("ok"))
    if ok:
        status = (
            f'<div class="chain-status is-ok">{_ICON_OK}'
            f'<div><div class="mono caps chain-head">запись целостна</div>'
            f'<div class="chain-text">Пересчитано {_esc(chain.get("checked", 0))} '
            f'записей, расхождений нет. Журнал можно использовать как доказательную '
            f'базу при разборе.</div></div></div>')
    elif not chain.get("available"):
        # Деградированный режим Р-05: журнал есть, пересчёта не было. Это не
        # то же самое, что «пересчёт не сошёлся», и писать второе про первое
        # означало бы обвинить запись в подделке на ровном месте.
        status = (
            f'<div class="chain-status is-partial">{_ICON_ALERT}'
            f'<div><div class="mono caps chain-head">пересчёт не выполнялся</div>'
            f'<div class="chain-text">'
            f'{_esc(chain.get("reason") or "SQLite-цепочка недоступна")}. '
            f'В записях резервного журнала есть номер и hash, и ниже они '
            f'перечислены, но подтвердить связность цепочки без базы нельзя. '
            f'Для полной проверки откройте отчёт рядом с '
            f'<span class="mono">evidence.sqlite</span> этой сессии.'
            f'</div></div></div>')
    else:
        broken = chain.get("broken_at", -1)
        where = (f' Первое расхождение — запись №{_esc(broken)}.'
                 if _f(broken, -1) >= 0 else "")
        status = (
            f'<div class="chain-status is-bad">{_ICON_ALERT}'
            f'<div><div class="mono caps chain-head">пересчёт цепочки не сошёлся</div>'
            f'<div class="chain-text">{_esc(chain.get("reason") or "причина не определена")}.'
            f'{where} Сверьте файл журнала с резервной копией до того, как опираться '
            f'на приведённые ниже числа.</div></div></div>')
    rows = [
        ("Файл журнала", db_path.name if db_path else
         (chain.get("source") or "—")),
        ("Записей в цепочке", chain.get("records", chain.get("checked", 0))),
        ("Из них инцидентов", chain.get("events_checked", chain.get("events", 0))),
        ("Genesis-хеш сессии", chain.get("genesis") or "—"),
        ("Хеш последней записи", chain.get("last_hash") or chain.get("chain_head") or "—"),
    ]
    if signature.get("available"):
        rows.extend([
            ("Алгоритм подписи", signature.get("algorithm", "Ed25519")),
            ("Публичный ключ", signature.get("public_key", "—")),
            ("Подпись головы цепочки", signature.get("chain_signature", "—")),
            ("Файл подписи отчёта", signature.get("sig_name", f"{report_name}.sig")),
            ("Файл публичного ключа", signature.get("pub_name", f"{report_name}.pub")),
        ])
    sign_note = (
        '<p class="note">Подпись отчёта лежит рядом с ним отдельным файлом: подписать '
        'можно только уже готовый файл. Публичный ключ приложен, приватный остаётся на '
        'машине, где проходил экзамен, в комплект отчёта не входит и для проверки не '
        'нужен. Подпись головы цепочки вшита в сам отчёт — её можно сверить с хешем '
        'последней записи журнала.</p>'
        if signature.get("available") else
        f'<p class="warn">Подпись не сформирована: '
        f'{_esc(signature.get("reason") or "нет пакета cryptography")}. Hash-chain при '
        f'этом работает и проверяется — содержимое журнала подменить незаметно нельзя, '
        f'но подтвердить авторство самого файла отчёта нечем.</p>'
    )
    table = "".join(
        f'<div class="kv"><span class="k">{_esc(k)}</span>'
        f'<span class="v mono">{_esc(v)}</span></div>' for k, v in rows)
    return (
        f'{status}'
        f'<p class="lead">Каждая запись журнала содержит <span class="mono">'
        f'hash = sha256(prev_hash + canonical_json(запись))</span>, а начало цепочки '
        f'выведено из метаданных сессии. Изменение любого байта, удаление, вставка или '
        f'перестановка записей ломают пересчёт и указывают номер первой испорченной '
        f'записи.</p>'
        f'<div class="kv-list kv-wide">{table}</div>'
        f'{sign_note}'
        f'<p class="lead">Проверка одной командой — из каталога системы '
        f'прокторинга, рядом с этим отчётом должны лежать '
        f'<span class="mono">{_esc(report_name)}.sig</span>, '
        f'<span class="mono">{_esc(report_name)}.pub</span> и журнал сессии '
        f'<span class="mono">evidence.sqlite</span>. Без журнала подпись '
        f'проверится, а цепочка — нет, и проверка честно скажет, что она '
        f'неполная:</p>'
        f'<pre>python3 scripts/verify_report.py {_esc(report_name)}</pre>'
    )


def build_limits_html(meta: dict[str, Any], events: list[dict[str, Any]],
                      chain: dict[str, Any]) -> str:
    """Честная граница: что система НЕ контролировала в этой сессии."""
    capabilities: dict[str, Any] = {}
    for event in events:
        if str(event.get("kind")) == "SESSION_STARTED":
            detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
            caps = detail.get("capabilities")
            if isinstance(caps, dict):
                capabilities = caps
            break
    names = {"vision": "видеоканал (объекты в кадре)", "gaze": "взгляд и поза головы",
             "identity": "непрерывная сверка личности", "audio": "аудиоканал",
             "env": "проверки окружения ОС"}
    off = [names[key] for key, value in capabilities.items()
           if key in names and not value]
    on = [names[key] for key, value in capabilities.items() if key in names and value]

    platform = sys.platform
    if platform == "darwin":
        plat_items = [
            "на macOS из пользовательского пространства не блокируются Cmd+Tab, "
            "Mission Control и системный снимок экрана — такие попытки только фиксируются;",
            "защита окна от записи экрана на macOS слабее, чем на Windows: "
            "возможна съёмка сторонним софтом с разрешением на запись экрана.",
        ]
    elif platform == "win32":
        plat_items = [
            "клавиши Win, Ctrl+Alt+Del и аппаратный PrtScn не перехватываются без "
            "драйвера уровня ядра — такие события только фиксируются;",
            "окно экзамена защищено от снимков средствами ОС, но не от съёмки внешней камерой.",
        ]
    else:
        plat_items = [
            "на этой платформе перехват системных сочетаний клавиш не реализован — "
            "подобные действия только фиксируются в журнале.",
        ]

    common = [
        "бумажная шпаргалка и любые записи вне поля зрения веб-камеры не детектируются;",
        "второй человек, находящийся вне кадра и не издающий звуков, не детектируется;",
        "телефон физически вне поля зрения камеры (под столом, за монитором) виден "
        "только косвенно — по направлению взгляда;",
        "реалистичный deepfake в реальном времени выходит за рамки модели угроз;",
        "студент с правами администратора может воздействовать на сам клиент: такие "
        "попытки фиксируются (потеря датчика, обрыв связи), но не предотвращаются;",
        "аппаратный KVM и HDMI-сплиттер не отличимы от одного монитора;",
        "risk-score и integrity score — мера того, сколько зафиксировано отклонений "
        "от условий экзамена, а не вероятность чего-либо. Решение принимает "
        "преподаватель, а не система.",
    ]
    off_html = ""
    if off:
        off_html = ('<p class="warn">В этой сессии были отключены или недоступны каналы: '
                    + _esc(", ".join(off)) + '. Соответствующие события зафиксированы '
                    'быть не могли — учитывайте это при разборе.</p>')
    on_html = ""
    if on:
        on_html = (f'<p class="note">Активные каналы сессии: '
                   f'<span class="mono">{_esc(", ".join(on))}</span>.</p>')
    if not chain.get("available"):
        on_html += ('<p class="warn">Журнал прочитан не из SQLite-цепочки: '
                    f'{_esc(chain.get("reason") or "база недоступна")}.</p>')
    items = "".join(f"<li>{_esc(text)}</li>" for text in plat_items + common)
    return (f'{off_html}{on_html}'
            f'<p class="lead">Что система не контролировала — граница модели угроз, '
            f'заявленная разработчиком, а не обнаруженная проверяющим:</p>'
            f'<ul class="limits">{items}</ul>')


# ---------------------------------------------------------------------------
# Подпись Ed25519
# ---------------------------------------------------------------------------
def _ed25519() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519  # type: ignore
        return ed25519
    except Exception:
        return None


def _default_key_path() -> Path:
    return _ROOT / "keys" / "report_ed25519.key"


def ensure_keys(key_path: str | Path | None = None) -> tuple[Path, Path, Any, Any]:
    """Вернуть (приватный путь, публичный путь, приватный ключ, публичный ключ).

    Ключи генерируются при первом запуске в `keys/` (вне git). Приватный файл
    получает права 0600. Нет `cryptography` -> (пути, None, None).
    """
    ed = _ed25519()
    path = Path(str(key_path)) if key_path else _default_key_path()
    if path.is_dir():
        path = path / "report_ed25519.key"
    pub_path = path.with_suffix(path.suffix + ".pub") if path.suffix else Path(str(path) + ".pub")
    if ed is None:
        return path, pub_path, None, None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            private = ed.Ed25519PrivateKey.from_private_bytes(
                bytes.fromhex(path.read_text(encoding="utf-8").strip()))
        else:
            private = ed.Ed25519PrivateKey.generate()
            from cryptography.hazmat.primitives import serialization  # type: ignore
            raw = private.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption())
            path.write_text(raw.hex() + "\n", encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        public = private.public_key()
        from cryptography.hazmat.primitives import serialization  # type: ignore
        pub_hex = public.public_bytes(encoding=serialization.Encoding.Raw,
                                      format=serialization.PublicFormat.Raw).hex()
        pub_path.write_text(pub_hex + "\n", encoding="utf-8")
        return path, pub_path, private, public
    except Exception:
        return path, pub_path, None, None


def _public_hex(public: Any) -> str:
    try:
        from cryptography.hazmat.primitives import serialization  # type: ignore
        return public.public_bytes(encoding=serialization.Encoding.Raw,
                                   format=serialization.PublicFormat.Raw).hex()
    except Exception:
        return ""


def sign_report(path: str | Path, key_path: str | Path | None = None) -> str:
    """Подписать файл отчёта Ed25519. Возвращает путь к файлу подписи ('' если нечем).

    Рядом с отчётом появляются `<отчёт>.sig` (JSON: алгоритм, sha256, подпись)
    и `<отчёт>.pub` (публичный ключ в hex) — этого достаточно для проверки
    на любой машине без доступа к приватному ключу.
    """
    report = Path(str(path))
    if not report.is_file():
        return ""
    _priv_path, _pub_path, private, public = ensure_keys(key_path)
    if private is None:
        return ""
    import hashlib
    blob = report.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    signature = private.sign(blob)
    sig_path = report.with_name(report.name + ".sig")
    payload = {
        "algorithm": "Ed25519",
        "file": report.name,
        "sha256": digest,
        "signature": signature.hex(),
        "public_key": _public_hex(public),
        "signed_at": datetime.now().isoformat(timespec="seconds"),
    }
    sig_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report.with_name(report.name + ".pub").write_text(
        _public_hex(public) + "\n", encoding="utf-8")
    return str(sig_path)


def verify_report(path: str | Path, sig_path: str | Path | None = None,
                  pub_key_path: str | Path | None = None) -> bool:
    """Проверить подпись отчёта. Любая проблема -> False (подробности в verify_report_detailed)."""
    return bool(verify_report_detailed(path, sig_path, pub_key_path)["ok"])


def verify_report_detailed(path: str | Path, sig_path: str | Path | None = None,
                           pub_key_path: str | Path | None = None) -> dict[str, Any]:
    """Проверка подписи с человекочитаемой причиной отказа."""
    import hashlib
    out: dict[str, Any] = {"ok": False, "reason": "", "sha256": "", "public_key": "",
                           "signed_at": "", "sig_path": "", "pub_path": ""}
    report = Path(str(path))
    if not report.is_file():
        out["reason"] = f"файл отчёта не найден: {report}"
        return out
    sig = Path(str(sig_path)) if sig_path else report.with_name(report.name + ".sig")
    out["sig_path"] = str(sig)
    if not sig.is_file():
        out["reason"] = f"файл подписи не найден: {sig.name}"
        return out
    ed = _ed25519()
    if ed is None:
        out["reason"] = "пакет cryptography не установлен, подпись проверить нечем"
        return out

    raw = sig.read_text(encoding="utf-8").strip()
    data: dict[str, Any]
    try:
        parsed = json.loads(raw)
        data = parsed if isinstance(parsed, dict) else {}
    except Exception:
        data = {"signature": raw}
    sig_hex = str(data.get("signature") or "").strip()
    out["sha256"] = str(data.get("sha256") or "")
    out["signed_at"] = str(data.get("signed_at") or "")

    pub = Path(str(pub_key_path)) if pub_key_path else report.with_name(report.name + ".pub")
    out["pub_path"] = str(pub)
    pub_hex = ""
    if pub.is_file():
        pub_hex = pub.read_text(encoding="utf-8").strip()
    elif data.get("public_key"):
        pub_hex = str(data["public_key"]).strip()
        out["pub_path"] = f"{sig.name} (ключ внутри подписи)"
    if not pub_hex:
        out["reason"] = "публичный ключ не найден"
        return out
    out["public_key"] = pub_hex

    blob = report.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    if out["sha256"] and out["sha256"] != digest:
        out["reason"] = "файл отчёта изменён: sha256 не совпадает с записанным в подписи"
        out["sha256_actual"] = digest
        return out
    try:
        public = ed.Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        public.verify(bytes.fromhex(sig_hex), blob)
    except Exception as exc:
        out["reason"] = f"подпись недействительна: {exc}"
        return out
    out["ok"] = True
    out["reason"] = "подпись действительна"
    return out


def _sign_chain_head(chain: dict[str, Any], key_path: str | Path | None) -> dict[str, Any]:
    """Подписать голову цепочки до рендера — подпись попадает внутрь HTML."""
    info: dict[str, Any] = {"available": False, "reason": "", "algorithm": "Ed25519"}
    head = str(chain.get("last_hash") or chain.get("chain_head") or "")
    if _ed25519() is None:
        info["reason"] = "пакет cryptography не установлен"
        return info
    _priv, _pub, private, public = ensure_keys(key_path)
    if private is None:
        info["reason"] = "не удалось подготовить ключи в каталоге keys/"
        return info
    try:
        signature = private.sign(head.encode("utf-8")) if head else b""
    except Exception as exc:
        info["reason"] = f"подписать голову цепочки не удалось: {exc}"
        return info
    info.update({
        "available": True,
        "public_key": _public_hex(public),
        "chain_signature": signature.hex(),
        "chain_head": head,
    })
    return info


# ---------------------------------------------------------------------------
# Рендер
# ---------------------------------------------------------------------------
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*(?:\|\s*safe\s*)?\}\}")

_BUILTIN_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; media-src data:; font-src data:; base-uri 'none'; form-action 'none'">
<title>{{ doc_title }}</title><style>{{ css }}</style></head>
<body>
{{ masthead_html }}
<main class="page">
<header class="head"><p class="eyebrow mono caps">отчёт о сессии прокторинга</p>
<h1>{{ page_title }}</h1><p class="head-sub">{{ generated_at }}</p></header>
{{ header_html }}
<section id="score" class="sheet"><h2>Integrity score</h2>
<div class="score-grid"><figure class="score-ring">{{ score_svg }}</figure>
<div class="score-breakdown">{{ breakdown_html }}</div></div></section>
<section id="timeline" class="monitor"><h2>Таймлайн сессии</h2>
{{ timeline_svg }}{{ risk_svg }}</section>
<section id="fusion" class="sheet is-key"><h2>Связки сигналов</h2>{{ fusion_html }}</section>
<section id="incidents" class="sheet"><h2>Инциденты сессии</h2>{{ incidents_html }}</section>
<section id="chain" class="sheet"><h2>Целостность записи</h2>{{ integrity_html }}</section>
<section id="limits" class="sheet"><h2>Границы системы</h2>{{ limits_html }}</section>
</main>
<footer class="foot"><div class="foot-inner">{{ footer_html }}</div></footer>
</body></html>
"""

# ---------------------------------------------------------------------------
# Шрифты
#
# Вшиваются ДВА семейства: Onest (весь русский и казахский текст, Р-14) и
# JetBrains Mono (телеметрия, таймкоды, хеши, КОДЫ инцидентов — ровно то, где
# подмена шрифта ломает смысл: цифры перестают быть одной ширины, хеш
# перестаёт выравниваться). Space Grotesk не вшивается: он нужен только
# латинскому вордмарку, и там системный гротеск допустим.
#
# ПОЧЕМУ ONEST ВШИТ, ХОТЯ ЭТО +60 КБ НА ОТЧЁТ. Subset'ы JetBrains Mono в
# репозитории НЕ содержат шести казахских букв: по cmap в них нет
# ә Ә ғ Ғ қ Қ ң Ң һ Һ (cyrillic-ext — всего 10 глифов) и ұ Ұ (cyrillic
# объявляет U+04B0-04B1, но глифов нет). `unicode-range` при этом их
# объявляет, поэтому `document.fonts.check()` возвращает true, а браузер молча
# уходит в Menlo / Consolas / Liberation Mono или в тофу — и именно в
# моноширинных полях: exam_id, student_id, session_id, коды инцидентов,
# подписи графиков, <pre> с сырыми данными связки. Университет кейса — имени
# Ахмет Байтұрсынұлы, так что это не теоретический случай.
#
# Onest покрытие имеет полное (проверено по cmap, см. `check_font_coverage`),
# поэтому он вшивается и ставится в `--font-mono` ВТОРЫМ семейством: для
# символа, которого у JetBrains Mono нет, браузер берёт следующее семейство
# из списка — и это детерминированно вшитый Onest, а не шрифт чужой ОС.
# Цена: эти буквы в моноширинных полях идут пропорциональной ширины. Тофу или
# Menlo посреди слова дороже.
#
# Пересубсетить сам JetBrains Mono под казахский набор нельзя в этом файле —
# это правка каталога шрифтов; как только в `shell/renderer/fonts` появятся
# subset'ы с нужными глифами, `check_font_coverage()` это увидит, и второе
# семейство в `--font-mono` просто перестанет срабатывать.
# ---------------------------------------------------------------------------
#: (файл, unicode-range) — значения один в один из shell/renderer/fonts.css.
_MONO_SUBSETS: tuple[tuple[str, str], ...] = (
    ("jetbrainsmono-latin.woff2",
     "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, "
     "U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, "
     "U+2212, U+2215, U+FEFF, U+FFFD"),
    ("jetbrainsmono-cyrillic.woff2",
     "U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116"),
    ("jetbrainsmono-cyrillic-ext.woff2",
     "U+0460-052F, U+1C80-1C8A, U+20B4, U+2DE0-2DFF, U+A640-A69F, U+FE2E-FE2F"),
)
#: То же для Onest — основного текста интерфейса и отчёта (Р-14).
_SANS_SUBSETS: tuple[tuple[str, str], ...] = (
    ("onest-latin.woff2",
     "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, "
     "U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, "
     "U+2212, U+2215, U+FEFF, U+FFFD"),
    ("onest-cyrillic.woff2",
     "U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116"),
    ("onest-cyrillic-ext.woff2",
     "U+0460-052F, U+1C80-1C8A, U+20B4, U+2DE0-2DFF, U+A640-A69F, U+FE2E-FE2F"),
)
#: Шрифты не должны раздувать отчёт: превысили — отчёт идёт на системном шрифте.
#: Фактически вшивается 102 КБ исходных байт (JetBrains Mono 41 КБ + Onest
#: 60 КБ), около 136 КБ в base64; бюджет оставлен с запасом на досубсеченные
#: файлы с казахскими глифами.
FONT_EMBED_BUDGET_BYTES = 260 * 1024

#: Набор, на котором проверяется покрытие: казахские буквы плюс украинская «і».
#: Проверка идёт по cmap, а НЕ по объявленному unicode-range — именно на этом
#: расхождении дефект и прожил: `document.fonts.check()` возвращает true для
#: всех восьми букв, потому что range объявлен, а глифов в файле нет, и ни
#: один автотест на fonts.check такое не поймает.
FONT_COVERAGE_PROBE = "ӘҒҚҢӨҮҰҺәғқңөүұһіІ"


def _font_dir() -> Path:
    return _ROOT / "shell" / "renderer" / "fonts"


def _face_css(family: str, data: str, ranges: str) -> str:
    return ("@font-face{font-family:\"" + family + "\";font-style:normal;"
            "font-weight:400 700;font-display:swap;"
            f"src:url(data:font/woff2;base64,{data}) format(\"woff2\");"
            f"unicode-range:{ranges}}}")


def embedded_fonts_css(font_dir: str | Path | None = None) -> str:
    """@font-face с Onest и JetBrains Mono в base64. Нет файлов — пустая строка.

    Порядок важен: сначала Onest (основной текст), потом JetBrains Mono. Бюджет
    считается общим, и моноширинный идёт ПЕРВЫМ в очереди на вшивание — если
    места хватит только на одно семейство, важнее сохранить моноширинную сетку
    телеметрии: в ней цифры и хеши, где подмена шрифта ломает смысл.

    Отсутствие шрифта не ошибка: в `--font-sans` и `--font-mono` следом стоят
    системные, и отчёт остаётся читаемым и автономным.
    """
    directory = Path(str(font_dir)) if font_dir else _font_dir()
    total = 0
    faces: list[str] = []
    for family, subsets in (("JetBrains Mono", _MONO_SUBSETS), ("Onest", _SANS_SUBSETS)):
        for name, ranges in subsets:
            try:
                blob = (directory / name).read_bytes()
            except Exception:
                continue
            if total + len(blob) > FONT_EMBED_BUDGET_BYTES:
                continue
            total += len(blob)
            faces.append(_face_css(family, base64.b64encode(blob).decode("ascii"),
                                   ranges))
    return "".join(faces)


#: Старое имя функции: вшивался только моноширинный. Оставлено, чтобы не
#: ломать вызов из соседнего кода, если он где-то остался.
_embedded_mono_css = embedded_fonts_css


def check_font_coverage(font_dir: str | Path | None = None,
                        probe: str = FONT_COVERAGE_PROBE) -> dict[str, Any]:
    """Проверить ПО CMAP, что каждый символ `probe` есть хотя бы в одном лице.

    Это и есть тот тест, которого не хватало: `unicode-range` в @font-face —
    объявление, а не покрытие, и проверка через `document.fonts.check()`
    проходит даже тогда, когда глифа в файле нет. Здесь берётся cmap.

    Возвращает `{"checked": bool, "ok": bool, "missing": {...}, "families": {...}}`.
    `checked=False` означает, что в системе нет fontTools — тогда это не
    провал проверки, а её отсутствие, и отчёт собирается как обычно.
    """
    out: dict[str, Any] = {"checked": False, "ok": True, "missing": {},
                           "families": {}, "reason": ""}
    try:
        from fontTools.ttLib import TTFont  # type: ignore
    except Exception as exc:
        out["reason"] = f"fontTools недоступен: {exc}"
        return out
    directory = Path(str(font_dir)) if font_dir else _font_dir()
    out["checked"] = True
    needed = {ord(ch) for ch in probe}
    for family, subsets in (("JetBrains Mono", _MONO_SUBSETS), ("Onest", _SANS_SUBSETS)):
        covered: set[int] = set()
        for name, _ranges in subsets:
            path = directory / name
            if not path.is_file():
                continue
            try:
                covered |= set(TTFont(str(path)).getBestCmap().keys())
            except Exception:
                continue
        gap = sorted(needed - covered)
        out["families"][family] = {
            "covered": sorted(needed & covered),
            "missing": gap,
            "missing_text": "".join(chr(c) for c in gap),
        }
    union: set[int] = set()
    for family in out["families"].values():
        union |= set(family["covered"])
    gap = sorted(needed - union)
    out["missing"] = {"codepoints": gap, "text": "".join(chr(c) for c in gap)}
    out["ok"] = not gap
    if gap:
        out["reason"] = ("ни одно вшитое лицо не содержит: "
                         + " ".join(f"U+{c:04X} {chr(c)}" for c in gap))
    return out


# ---------------------------------------------------------------------------
# Стили
#
# Токены продублированы из shell/renderer/tokens.css — отчёт обязан быть одним
# автономным файлом и не может сослаться на внешний css. Значения совпадают
# один в один; хардкода цветов и размеров вне блока :root в правилах нет.
#
# Распределение поверхностей здесь ОБРАТНО экранному: на экране ведущий
# тон чёрный (наблюдение), в отчёте ведущий тон белый, потому что отчёт —
# среда чтения и анализа. Тёмное оставлено ровно за наблюдением: таймлайн,
# график риска, таблица инцидентов с кадрами, схемы таймингов связок и
# служебные полосы продукта.
# ---------------------------------------------------------------------------
_CSS = """
:root{
--acid:#c6ff00;--acid-hover:#d7ff52;--acid-pressed:#9ed000;
--ink:#0a0a0a;--ink-2:#141414;--graphite:#262626;
--gray-500:#747474;--gray-300:#b9b9b9;--gray-100:#ececec;--white:#ffffff;
--danger:#ff334e;--warning:#ffb800;--info:#35b7ff;
--bg-canvas:var(--white);--bg-monitor:var(--ink);--bg-panel:var(--ink-2);
--border-hair:var(--gray-100);--border-hair-dark:var(--graphite);
--text-primary:var(--ink);--text-muted:var(--gray-500);
--text-on-dark:var(--white);--text-muted-on-dark:var(--gray-300);
--signal:var(--acid);--signal-text-on-light:#4a6100;
--signal-glow:0 0 0 1px rgba(198,255,0,.55),0 0 32px rgba(198,255,0,.16);
--shadow-hard:6px 6px 0 var(--ink);
--sev-info:var(--info);--sev-low:var(--gray-500);--sev-medium:var(--warning);
--sev-high:#ff8a1f;--sev-critical:var(--danger);
--font-sans:"Onest",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
/* Onest стоит в моноширинном стеке ВТОРЫМ не для красоты: subset'ы JetBrains
   Mono не содержат ә ғ қ ң ұ һ, и без этого шага браузер уходил в Menlo или
   в тофу прямо в exam_id и кодах инцидентов. Подробно — у _MONO_SUBSETS. */
--font-mono:"JetBrains Mono","Onest",ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
--fs-h1:clamp(28px,4vw,44px);--fs-h2:clamp(22px,3vw,32px);--fs-h3:20px;
--fs-body:16px;--fs-small:14px;--fs-label:13px;--fs-tiny:11px;
--lh-head:1.05;--lh-body:1.55;
--tracking-display:-.055em;--tracking-label:.12em;
--space-1:4px;--space-2:8px;--space-3:12px;--space-4:16px;--space-6:24px;
--space-8:32px;--space-12:48px;--space-16:64px;
--radius-xs:4px;--radius-sm:8px;--radius-md:12px;--radius-lg:18px;
/* Обводка фокуса зависит от поверхности — зеркалим tokens.css: --info
   (#35b7ff) даёт на белом всего 2.24:1, то есть на листе отчёта фокус
   физически не виден, а WCAG 2.2 требует >=3:1. Тёмная среда наблюдения
   возвращает --info в правиле section.monitor ниже. Это важно именно здесь:
   прокручиваемые графики и таблица получили tabindex и доступны с клавиатуры. */
--focus-ring-color:#0b6ea8;
--tap-min:44px;--focus-ring:3px solid var(--focus-ring-color);--focus-offset:3px;
--transition:.16s ease;--layout-max:1120px;
}
*,*::before,*::after{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:var(--space-6)}
body{margin:0;background:var(--bg-canvas);color:var(--text-primary);
font-family:var(--font-sans);font-size:var(--fs-body);line-height:var(--lh-body);
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
img,svg{max-width:100%}
:focus-visible{outline:var(--focus-ring);outline-offset:var(--focus-offset)}
::selection{color:var(--ink);background:var(--acid)}
.mono{font-family:var(--font-mono);font-variant-numeric:tabular-nums}
.caps{text-transform:uppercase;letter-spacing:var(--tracking-label)}
.muted{color:var(--text-muted);font-size:var(--fs-small)}
.wordmark{font-weight:700;letter-spacing:-.04em}

/* ---- каркас ---- */
.page{max-width:var(--layout-max);margin:0 auto;padding:var(--space-12) var(--space-6)
var(--space-16)}
.masthead{background:var(--bg-monitor);color:var(--text-on-dark);
border-bottom:1px solid var(--border-hair-dark)}
.masthead-inner{max-width:var(--layout-max);margin:0 auto;display:flex;flex-wrap:wrap;
align-items:center;gap:var(--space-4);padding:var(--space-4) var(--space-6)}
.masthead-brand{display:flex;align-items:center;gap:var(--space-3)}
.mark{flex:0 0 auto}
.brand-text{display:flex;flex-direction:column;line-height:1.25}
.brand-sub{font-size:var(--fs-small);color:var(--text-muted-on-dark)}
.masthead-side{margin-left:auto;display:flex;flex-wrap:wrap;align-items:center;
gap:var(--space-4)}
.badge{display:inline-flex;align-items:center;gap:var(--space-2);
padding:var(--space-1) var(--space-3);border:1px solid var(--border-hair-dark);
border-radius:999px;font-size:var(--fs-small);color:var(--text-muted-on-dark)}
.badge::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--gray-500)}
.masthead-id{font-size:var(--fs-small);color:var(--text-muted-on-dark);word-break:break-all}
.foot{background:var(--bg-monitor);color:var(--text-muted-on-dark);
border-top:1px solid var(--border-hair-dark)}
.foot-inner{max-width:var(--layout-max);margin:0 auto;padding:var(--space-8) var(--space-6);
font-family:var(--font-mono);font-size:var(--fs-small);text-align:center}

/* ---- шапка документа ---- */
.head{margin-bottom:var(--space-8)}
.eyebrow{margin:0;font-family:var(--font-mono);font-size:var(--fs-small);
color:var(--text-muted)}
h1{margin:var(--space-3) 0 var(--space-2);font-size:var(--fs-h1);
line-height:var(--lh-head);letter-spacing:var(--tracking-display)}
.head-sub{margin:0;font-family:var(--font-mono);font-size:var(--fs-small);
color:var(--text-muted)}
.toc{display:flex;flex-wrap:wrap;gap:var(--space-2);margin-top:var(--space-6)}
.toc a{display:inline-flex;align-items:center;min-height:var(--tap-min);
padding:0 var(--space-4);border:1px solid var(--border-hair);border-radius:var(--radius-sm);
color:var(--text-primary);text-decoration:none;font-size:var(--fs-small);
transition:background var(--transition),color var(--transition),border-color var(--transition)}
.toc a:hover{color:var(--ink);background:var(--acid);border-color:var(--acid)}

/* ---- вердикт и паспорт сессии ---- */
.verdict{--vc:var(--ink);margin:0 0 var(--space-6);padding:var(--space-8);
background:var(--white);border:1px solid var(--border-hair);
border-top:6px solid var(--vc);border-radius:var(--radius-lg)}
.verdict-head{display:flex;flex-wrap:wrap;align-items:center;gap:var(--space-3);
margin-bottom:var(--space-4)}
.verdict-level{font-size:var(--fs-small);color:var(--text-muted)}
.verdict-badge{padding:var(--space-1) var(--space-3);border-radius:var(--radius-xs);
background:var(--vc);color:var(--ink);font-size:var(--fs-small);font-weight:600}
.verdict-title{margin:0 0 var(--space-3);font-size:var(--fs-h2);line-height:var(--lh-head);
letter-spacing:var(--tracking-display);text-transform:none}
.verdict-text{margin:0 0 var(--space-4);font-size:var(--fs-body);max-width:78ch}
.verdict-action{display:flex;flex-wrap:wrap;align-items:baseline;gap:var(--space-3);
margin:0;padding:var(--space-3) var(--space-4);background:var(--gray-100);
border-radius:var(--radius-sm);font-size:var(--fs-body)}
.verdict-action>.mono{font-size:var(--fs-small);color:var(--signal-text-on-light)}
.chips{display:flex;flex-wrap:wrap;gap:var(--space-2);margin-top:var(--space-4)}
.chip{--tone:var(--gray-300);display:inline-flex;align-items:center;gap:var(--space-2);
padding:var(--space-2) var(--space-3);background:var(--white);
border:1px solid var(--border-hair);border-left:4px solid var(--tone);
border-radius:var(--radius-xs);font-size:var(--fs-small)}
.chip-info{--tone:var(--sev-info)}
.chip-low{--tone:var(--gray-300)}
.chip-medium{--tone:var(--sev-medium)}
.chip-high{--tone:var(--sev-high)}
.chip-critical{--tone:var(--sev-critical)}
.chip-num{font-family:var(--font-mono);font-weight:700}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:1px;
margin:0 0 var(--space-8);background:var(--border-hair);border:1px solid var(--border-hair);
border-radius:var(--radius-md);overflow:hidden}
.meta-item{padding:var(--space-3) var(--space-4);background:var(--white)}
.meta-k{font-family:var(--font-mono);font-size:var(--fs-small);color:var(--text-muted);
text-transform:uppercase;letter-spacing:var(--tracking-label)}
.meta-v{margin-top:var(--space-1);font-size:var(--fs-body);word-break:break-word}

/* ---- секции ---- */
section{margin-bottom:var(--space-8);padding:var(--space-8);
border:1px solid var(--border-hair);border-radius:var(--radius-lg)}
section.sheet{background:var(--bg-canvas);color:var(--text-primary)}
section.monitor{background:var(--bg-monitor);color:var(--text-on-dark);
border-color:var(--border-hair-dark);--focus-ring-color:var(--info)}
.masthead,.foot{--focus-ring-color:var(--info)}
section.is-key{border:1px solid var(--ink);border-top:6px solid var(--acid);
box-shadow:var(--shadow-hard)}
.section-head{display:grid;grid-template-columns:64px minmax(0,1fr);gap:var(--space-4);
align-items:start;margin-bottom:var(--space-6);padding-bottom:var(--space-4);
border-bottom:1px solid var(--border-hair)}
.monitor .section-head{border-bottom-color:var(--border-hair-dark)}
.section-index{font-size:var(--fs-label);letter-spacing:var(--tracking-label);
color:var(--text-muted)}
.monitor .section-index{color:var(--acid)}
h2{margin:0;font-size:var(--fs-h2);line-height:var(--lh-head);
letter-spacing:var(--tracking-display);text-transform:uppercase}
h3{margin:0;font-size:var(--fs-h3);letter-spacing:-.025em}
.sub-h{margin:var(--space-6) 0 var(--space-4);font-size:var(--fs-h3)}
.lead{margin:0 0 var(--space-4);font-size:var(--fs-body);max-width:78ch}
.monitor .lead{color:var(--text-muted-on-dark)}
.note{margin:var(--space-4) 0;font-size:var(--fs-small);color:var(--text-muted);
max-width:78ch}
.note:last-child{margin-bottom:0}
.warn{margin:0 0 var(--space-4);padding:var(--space-3) var(--space-4);
background:var(--gray-100);border:1px solid var(--border-hair);
border-left:4px solid var(--warning);border-radius:var(--radius-sm);
font-size:var(--fs-small)}

/* ---- integrity score ---- */
.score-grid{display:grid;grid-template-columns:300px minmax(0,1fr);gap:var(--space-8);
align-items:start}
.score-ring{margin:0}
.ring{display:block;width:100%;max-width:300px;margin:0 auto}
svg text{font-family:var(--font-sans)}
.svg-mono{font-family:var(--font-mono);font-variant-numeric:tabular-nums}
.svg-cap{font-family:var(--font-mono);text-transform:uppercase;
letter-spacing:var(--tracking-label)}
.svg-num{font-weight:700;letter-spacing:var(--tracking-display);
font-variant-numeric:tabular-nums}
.levels{display:flex;flex-wrap:wrap;gap:var(--space-2);margin:0 0 var(--space-6);
padding:0;list-style:none}
.level{display:flex;align-items:center;gap:var(--space-2);
padding:var(--space-2) var(--space-3);border:1px solid var(--border-hair);
border-radius:var(--radius-sm);font-size:var(--fs-small)}
.level.is-current{border-color:var(--ink);font-weight:600}
.level-swatch{width:12px;height:12px;border-radius:3px;background:var(--c);
border:1px solid var(--border-hair-dark)}
.level-tag{font-size:var(--fs-small);color:var(--signal-text-on-light)}
.bars{display:grid;gap:var(--space-4)}
.bar-row{padding:var(--space-3) 0 var(--space-3) var(--space-4);
border-left:3px solid var(--border-hair)}
.bar-row.is-lead{border-left-color:var(--acid)}
.bar-top{display:flex;flex-wrap:wrap;align-items:center;gap:var(--space-2)}
.bar-name{font-size:var(--fs-body);font-weight:600}
.bar-tag{font-size:var(--fs-small);color:var(--signal-text-on-light)}
.bar-value{margin-left:auto;font-size:var(--fs-body);font-weight:700}
.bar-track{height:14px;margin-top:var(--space-2);background:var(--gray-100);
border-radius:var(--radius-xs);overflow:hidden}
.bar-fill{height:100%;background:var(--ink);border-radius:var(--radius-xs)}
.bar-foot{margin-top:var(--space-1);font-size:var(--fs-small);color:var(--text-muted)}
.bar-kinds{display:grid;gap:var(--space-1);margin:var(--space-3) 0 0;padding:0;
list-style:none}
.bar-kinds li{display:flex;flex-wrap:wrap;align-items:baseline;gap:var(--space-2);
font-size:var(--fs-small)}
.bar-kind-num{color:var(--text-muted)}
.clusters{display:grid;gap:var(--space-1);margin:0 0 var(--space-6);padding:0;
list-style:none}
.clusters li{display:flex;flex-wrap:wrap;align-items:baseline;gap:var(--space-3);
padding:var(--space-2) 0;border-bottom:1px dotted var(--border-hair);
font-size:var(--fs-small)}
.cluster-name{font-weight:600;font-size:var(--fs-body)}
.cluster-num{margin-left:auto;color:var(--text-muted)}

/* ---- графики ----
   Подписи внутри SVG заданы в единицах viewBox, то есть масштабируются вместе
   с картинкой: при ширине окна 375 px они падали до 3 px. Поэтому график не
   сжимается ниже 1:1, а ПРОКРУЧИВАЕТСЯ внутри своего контейнера — рабочая
   подпись в 14 единиц viewBox никогда не меньше 14 CSS px. Прокрутка
   доступна с клавиатуры: у контейнера есть tabindex и видимый focus ring. */
.plot-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;
margin-bottom:var(--space-6)}
.plot-scroll:last-child{margin-bottom:0}
.plot{display:block;width:100%;min-width:1000px;height:auto}
.timing{display:block;width:100%;min-width:760px;height:auto;
border-radius:var(--radius-sm)}
.fusion-card .plot-scroll{margin-bottom:var(--space-4)}

/* ---- таблица инцидентов (паттерн .data-table: среда наблюдения) ---- */
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;
border:1px solid var(--border-hair-dark);border-radius:var(--radius-md)}
.data-table{width:100%;min-width:1040px;border-collapse:collapse;
background:var(--bg-panel);color:var(--text-on-dark);font-size:var(--fs-small)}
/* Шапка липкая: на длинной сессии таблица уходит на десятки экранов, и без
   этого восемь колонок (две из них числовые подряд) читаются вслепую.
   Тонкость: `position:sticky` прилипает к ближайшему СКРОЛЛПОРТУ, а
   `.table-wrap` им уже является (overflow-x:auto делает auto и по вертикали),
   поэтому одного `sticky` недостаточно — у длинной таблицы контейнер получает
   ограничение по высоте и прокручивается сам. Короткие таблицы остаются
   обычным блоком: вложенная прокрутка на пять строк только мешает. */
.table-wrap.is-long{max-height:78vh}
.data-table thead th{position:sticky;top:0;z-index:2}
.data-table th{padding:var(--space-3) var(--space-4);background:var(--ink);
color:var(--text-muted-on-dark);font-family:var(--font-mono);font-size:var(--fs-small);
font-weight:400;text-align:left;text-transform:uppercase;
letter-spacing:var(--tracking-label);white-space:nowrap}
.data-table td{padding:var(--space-4);border-top:1px solid var(--border-hair-dark);
vertical-align:top}
.data-table tr:hover td{background:rgba(198,255,0,.04)}
.cell-wide{min-width:280px}
.nowrap{white-space:nowrap}
.num{text-align:right}
.t-main{font-size:var(--fs-body)}
.t-sub{margin-top:2px;font-size:var(--fs-small);color:var(--text-muted-on-dark)}
.cell-sub{margin-top:var(--space-1);font-size:var(--fs-small);
color:var(--text-muted-on-dark)}
/* Крупно — наблюдение движка (формула «факт -> контекст -> действие»),
   мелко под ним — техническая подпись вида. Обратная иерархия отдавала глазу
   короткий ярлык вместо наблюдения. */
.obs{font-size:var(--fs-body);max-width:52ch}
.obs-kind{margin-top:var(--space-1);font-size:var(--fs-small);
color:var(--text-muted-on-dark)}
.code{display:inline-block;padding:2px var(--space-2);background:var(--gray-100);
border:1px solid var(--border-hair);border-radius:var(--radius-xs);
font-family:var(--font-mono);font-size:var(--fs-small);color:var(--text-primary);
white-space:nowrap}
.code-kind{letter-spacing:.02em}
.monitor .code,.data-table .code{background:var(--graphite);
border-color:var(--border-hair-dark);color:var(--text-on-dark)}
.glyph{flex:0 0 auto}
.sev{--tone:var(--gray-300);display:inline-flex;align-items:center;gap:var(--space-2);
padding:var(--space-1) var(--space-3);border:1px solid transparent;border-radius:999px;
font-size:var(--fs-small);white-space:nowrap}
.sev-info{--tone:var(--sev-info)}
.sev-low{--tone:var(--gray-300)}
.sev-medium{--tone:var(--sev-medium)}
.sev-high{--tone:var(--sev-high)}
.sev-critical{--tone:var(--sev-critical)}
.sheet .sev{background:var(--tone);color:var(--ink);border-color:var(--border-hair-dark)}
.monitor .sev,.data-table .sev{background:transparent;color:var(--tone);
border-color:var(--tone)}
.ev{margin:0}
.thumb{display:block;width:140px;border:1px solid var(--border-hair-dark);
border-radius:var(--radius-xs)}
.ev figcaption{max-width:140px;margin-top:var(--space-1);font-size:var(--fs-small);
color:var(--text-muted-on-dark);word-break:break-all}
.fusion-ev .ev figcaption{color:var(--text-muted)}

/* ---- связки ---- */
.fusion-card{margin-bottom:var(--space-6);padding:var(--space-6);background:var(--white);
border:1px solid var(--ink);border-radius:var(--radius-md)}
.fusion-card:last-child{margin-bottom:0}
.fusion-head{display:flex;flex-wrap:wrap;align-items:center;gap:var(--space-3);
margin-bottom:var(--space-4)}
.fusion-time{margin-left:auto;font-size:var(--fs-small);color:var(--text-muted)}
.fusion-msg{margin:0 0 var(--space-4);font-size:var(--fs-body);max-width:78ch}
.fusion-body{display:flex;flex-wrap:wrap;gap:var(--space-6)}
.fusion-body .kv-list{flex:1 1 320px;margin:0}
.fusion-ev{flex:0 0 140px}
.fusion-foot{margin-top:var(--space-4);padding-top:var(--space-3);
border-top:1px solid var(--border-hair);font-size:var(--fs-small);
color:var(--text-muted);word-break:break-all}

/* ---- пары ключ-значение, раскрытия, код ---- */
.kv-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
gap:var(--space-1) var(--space-6);margin:var(--space-3) 0}
.kv-wide{grid-template-columns:1fr}
.kv{display:flex;justify-content:space-between;align-items:baseline;gap:var(--space-3);
padding:var(--space-2) 0;border-bottom:1px dotted var(--border-hair);
font-size:var(--fs-small)}
.kv .k{color:var(--text-muted)}
.kv .v{text-align:right;word-break:break-all}
.data-table .kv,.monitor .kv{border-bottom-color:var(--border-hair-dark)}
.data-table .kv .k,.monitor .kv .k{color:var(--text-muted-on-dark)}
details{margin-top:var(--space-2)}
/* display:flex убирает нативный треугольник раскрытия, поэтому состояние
   несут шеврон (форма), подпись («показать» / «свернуть») и только потом
   цвет: требование a11y из гайда — цвет не единственный носитель смысла.
   Именно через эти раскрытия преподаватель добирается до показаний детектора
   и сырого JSON связки, и при ч/б печати они обязаны читаться как кнопка. */
summary{display:flex;align-items:center;gap:var(--space-2);
min-height:var(--tap-min);cursor:pointer;
font-family:var(--font-mono);font-size:var(--fs-small);
color:var(--signal-text-on-light)}
summary::-webkit-details-marker{display:none}
.chev{flex:0 0 auto;transform:rotate(0deg);transition:transform var(--transition)}
details[open]>summary .chev{transform:rotate(90deg)}
details>summary .sum-open{display:none}
details[open]>summary .sum-open{display:inline}
details[open]>summary .sum-closed{display:none}
.data-table summary,.monitor summary{color:var(--acid)}
pre{margin:var(--space-3) 0 0;padding:var(--space-4);background:var(--ink);
color:var(--text-muted-on-dark);border-radius:var(--radius-sm);overflow-x:auto;
font-family:var(--font-mono);font-size:var(--fs-small);line-height:1.5}

/* ---- целостность записи ---- */
.chain-status{display:flex;align-items:flex-start;gap:var(--space-4);
margin-bottom:var(--space-6);padding:var(--space-4) var(--space-6);
background:var(--bg-monitor);color:var(--text-on-dark);
border:1px solid var(--border-hair-dark);border-radius:var(--radius-sm)}
.chain-status.is-ok{border-color:var(--acid);box-shadow:var(--signal-glow)}
.chain-status.is-ok .icon,.chain-status.is-ok .chain-head{color:var(--acid)}
.chain-status.is-partial{border-color:var(--warning)}
.chain-status.is-partial .icon,.chain-status.is-partial .chain-head{
color:var(--warning)}
.chain-status.is-bad{border-color:var(--danger)}
.chain-status.is-bad .icon,.chain-status.is-bad .chain-head{color:var(--danger)}
.icon{flex:0 0 auto;margin-top:2px}
.chain-head{margin-bottom:var(--space-1);font-size:var(--fs-small)}
.chain-text{font-size:var(--fs-small);color:var(--text-muted-on-dark);max-width:78ch}

/* ---- границы системы ---- */
ul.limits{margin:0;padding:0;list-style:none}
ul.limits li{position:relative;padding:var(--space-3) 0 var(--space-3) var(--space-8);
border-top:1px solid var(--border-hair);font-size:var(--fs-body)}
ul.limits li::before{content:"+";position:absolute;left:0;top:var(--space-3);
font-family:var(--font-mono);font-weight:700;color:var(--signal-text-on-light)}

/* ---- адаптив ---- */
@media (max-width:900px){
.score-grid{grid-template-columns:minmax(0,1fr)}
.section-head{grid-template-columns:minmax(0,1fr);gap:var(--space-2)}
section{padding:var(--space-6)}
.verdict{padding:var(--space-6)}
.page{padding:var(--space-8) var(--space-4) var(--space-12)}
.masthead-inner,.foot-inner{padding-left:var(--space-4);padding-right:var(--space-4)}
.fusion-body{gap:var(--space-4)}
}

/* ---- системные предпочтения: те же правила, что в tokens.css ---- */
@media (prefers-contrast:more){
:root{--gray-500:#5e5e5e;--gray-300:#d1d1d1;--border-hair:#d4d4d4;
--focus-ring-color:#004e7c}
section.monitor{--focus-ring-color:var(--info)}
section,.verdict,.meta-grid,.table-wrap,.level,.chip{border-color:var(--ink)}
}
@media (prefers-reduced-motion:reduce){
html{scroll-behavior:auto}
*,*::before,*::after{transition-duration:.01ms !important;
animation-duration:.01ms !important}
}

/* ---- печать: тёмные блоки обязаны остаться тёмными ---- */
@media print{
body{background:var(--white)}
.page{max-width:none;padding:0}
.toc{display:none}
section{break-inside:avoid;box-shadow:none;border-color:var(--gray-300)}
.fusion-card,.verdict{break-inside:avoid}
.table-wrap,.table-wrap.is-long{max-height:none;overflow:visible}
.data-table{min-width:0}
.data-table thead th{position:static}
.data-table thead{display:table-header-group}
.plot-scroll{overflow:visible}
.plot,.timing{min-width:0}
/* На печати раскрытия не нажимаются: закрытое остаётся закрытым, поэтому
   подпись берётся «закрытая», а не «свернуть» — иначе бумага предлагает
   свернуть то, что и так не раскрыто. */
details{break-inside:avoid}
details[open]>summary .sum-open{display:none}
details[open]>summary .sum-closed{display:inline}
.masthead,.foot,section.monitor,.data-table,.chain-status,pre,.timing,.plot,
.ring,.verdict-badge,.bar-fill,.level-swatch{-webkit-print-color-adjust:exact;
print-color-adjust:exact}
}
"""

def _render(fragments: dict[str, str], template_dir: Path) -> str:
    """Jinja2, если есть; иначе string.Template по тому же шаблону; иначе встроенный."""
    template_path = template_dir / TEMPLATE_NAME
    source = ""
    if template_path.is_file():
        try:
            source = template_path.read_text(encoding="utf-8")
        except Exception:
            source = ""
    if not source:
        source = _BUILTIN_TEMPLATE

    try:
        import jinja2  # type: ignore
        env = jinja2.Environment(autoescape=False, keep_trailing_newline=True)
        return env.from_string(source).render(**fragments)
    except Exception:
        pass
    from string import Template
    converted = _PLACEHOLDER_RE.sub(lambda m: "${" + m.group(1) + "}", source)
    return Template(converted).safe_substitute(**fragments)


def build_report(session_dir: str | Path, db_path: str | Path | None = None,
                 out_html: str | Path | None = None,
                 config: dict[str, Any] | None = None,
                 sign: bool | None = None) -> str:
    """Собрать автономный HTML-отчёт по сессии. Возвращает путь к файлу.

    `sign=None` — подписать, если установлен `cryptography` (ключи создаются
    в `keys/` при первом запуске). `sign=False` — только HTML.
    """
    data = load_report_data(session_dir, db_path)
    cfg = config or {}
    report_cfg = cfg.get("report") if isinstance(cfg.get("report"), dict) else {}
    key_path = (report_cfg.get("key_path") or cfg.get("report_key_path") or None)

    out = Path(str(out_html)) if out_html else (data.session_dir / "report.html")
    out.parent.mkdir(parents=True, exist_ok=True)

    weights = _risk_weights()
    events = data.events
    meaningful = [e for e in events if str(e.get("kind")) not in SERVICE_KINDS]

    started = _f(data.summary.get("started_at") or data.meta.get("started_at"))
    if not started:
        started = _f(events[0].get("ts")) if events else time.time()
    ended = _f(data.summary.get("ended_at"))
    if not ended:
        ended = _f(events[-1].get("ts")) if events else started + 1.0
    if ended <= started:
        ended = started + max(_f(data.summary.get("duration_sec")), 60.0)

    integrity = compute_integrity(meaningful, weights)
    has_critical = any(str(e.get("severity")) == "critical" for e in meaningful)
    verdict = integrity_verdict(_f(integrity.get("score"), 100.0), has_critical,
                                incidents=len(meaningful))

    risk_cfg = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
    half_life = _f(risk_cfg.get("half_life") or cfg.get("risk_half_life"), DEFAULT_HALF_LIFE)
    # Потолок рантайма (`risk_max` в конфиге RiskScorer) рисуется на графике
    # отдельной линией, а сама серия считается БЕЗ обрезки: иначе на тяжёлой
    # сессии кривая вырождается в прямую поверх всех трёх порогов.
    risk_cap = _f(risk_cfg.get("risk_max") or risk_cfg.get("max")
                  or cfg.get("risk_max"), 100.0) or 100.0
    series = risk_series(meaningful, started, ended, weights, half_life,
                         max_score=None)

    by_severity: dict[str, int] = {}
    for event in meaningful:
        key = str(event.get("severity") or "info")
        by_severity[key] = by_severity.get(key, 0) + 1
    ordered_sev = {k: by_severity[k] for k in
                   sorted(by_severity, key=lambda s: -SEVERITY_ORDER.get(s, 0))}

    embed = _Embedder(data.session_dir)
    signature = _sign_chain_head(data.chain, key_path) if sign is not False else {
        "available": False, "reason": "подпись отключена конфигурацией"}
    signature.setdefault("sig_name", out.name + ".sig")
    signature.setdefault("pub_name", out.name + ".pub")

    student = str(data.meta.get("student_name") or data.meta.get("student_id") or "студент")
    exam = str(data.meta.get("exam_id") or "")
    doc_title = f"{student}" + (f" — {exam}" if exam else "") + " — отчёт о прокторинге"
    page_title = student

    fragments = {
        "doc_title": _esc(doc_title),
        # В h1 стоит имя студента: это документ о человеке, а не о детекторе.
        "page_title": _esc(page_title),
        "css": embedded_fonts_css() + _CSS,
        "generated_at": _esc(
            (f"{exam} · " if exam else "")
            + f"отчёт сформирован {_full_time(time.time())} · "
            + f"источники данных: {', '.join(data.sources) or 'нет'}"),
        "masthead_html": build_masthead_html(data.meta, data.summary),
        "header_html": build_header_html(data.meta, data.summary, integrity, verdict,
                                         {"by_severity": ordered_sev}),
        "score_svg": build_score_svg(integrity, verdict),
        "breakdown_html": build_breakdown_html(integrity, data.summary),
        "timeline_svg": build_timeline_svg(meaningful, started, ended),
        "risk_svg": build_risk_svg(series, started, ended, _thresholds(), half_life,
                                   cap=risk_cap),
        "fusion_html": build_fusion_html(events, started, embed),
        "incidents_html": build_incidents_html(events, started, embed),
        "integrity_html": build_integrity_html(data.chain, signature, data.db_path, out.name),
        "limits_html": build_limits_html(data.meta, events, data.chain),
    }
    fragments["footer_html"] = _esc(
        f"инцидентов {len(meaningful)} · записей в цепочке "
        f"{data.chain.get('records', data.chain.get('checked', 0))} · "
        f"вшито доказательств {round(embed.used / 1024)} КБ"
        + (f" · пропущено крупных {embed.skipped}" if embed.skipped else "")
        + " · локальная система прокторинга · отчёт автономен, "
          "внешних запросов не делает"
    )

    # Покрытие вшитых шрифтов проверяется по cmap, а не по unicode-range:
    # дефект с казахскими буквами в моноширинных полях прожил именно потому,
    # что `document.fonts.check()` объявленный range подтверждает, а глифов в
    # файле нет. Проверка не обязательная (нужен fontTools) и не фатальная:
    # отчёт собирается, но расхождение попадает в stderr, а не замолчано.
    coverage = check_font_coverage()
    if coverage.get("checked") and not coverage.get("ok"):
        print(f"[report] вшитые шрифты не покрывают набор: "
              f"{coverage.get('reason')}", file=sys.stderr)

    html_text = _render(fragments, _HERE / "templates")
    out.write_text(html_text, encoding="utf-8")

    if sign is not False:
        try:
            sign_report(out, key_path)
        except Exception:
            pass
    return str(out)


__all__ = [
    "build_report", "sign_report", "verify_report", "verify_report_detailed",
    "load_report_data", "compute_integrity", "integrity_verdict", "risk_series",
    "ensure_keys", "build_masthead_html", "build_header_html", "build_score_svg",
    "build_breakdown_html", "build_timeline_svg", "build_risk_svg",
    "build_fusion_timing_svg", "build_fusion_html", "build_incidents_html",
    "build_integrity_html", "build_limits_html", "INTEGRITY_SCALE",
    "REPEAT_GAIN", "REPEAT_CAP", "BEHAVIOR_CLUSTERS", "CLUSTER_LABELS",
    "embedded_fonts_css", "check_font_coverage", "FONT_COVERAGE_PROBE",
]

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
import base64
import binascii
import contextlib
import hashlib
import hmac
import http
import importlib
import inspect
import json
import logging
import math
import os
import secrets
import signal
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:  # передача доказательств: каталог проктора, пакет, код сверки
    from storage import handover as handover_mod  # noqa: E402
except Exception:  # pragma: no cover — Р-05: без модуля система всё равно работает
    handover_mod = None  # type: ignore[assignment]

try:  # нужен `prepare_handover` до старта: проверить ключ учреждения
    from storage import report as report_startup_mod  # noqa: E402
except Exception:  # pragma: no cover — Р-05
    report_startup_mod = None  # type: ignore[assignment]

from protocol import (  # noqa: E402
    AUTO_ACTION_CAP,
    COMMAND_NAMES,
    LOCK_POLICY_AUTO,
    LOCK_POLICY_PROCTOR,
    LOCK_POLICY_LABELS,
    PROCTOR_DECISION_LOCK,
    PROCTOR_DECISION_RELEASE,
    PROTOCOL_VERSION,
    RISK_WEIGHTS,
    safe_label,
    Channel,
    Evidence,
    EventKind,
    MsgType,
    ProctorEvent,
    Severity,
    VerdictAction,
    cap_auto_action,
    encode,
    decode,
    envelope,
    event_message,
    is_service_kind,
    EVIDENCE_FRAME_MAX_AGE,
    EVIDENCE_SKIP_DISABLED,
    EVIDENCE_SKIP_NO_CAMERA,
    EVIDENCE_SKIP_NOT_RECORDING,
    EVIDENCE_SKIP_SERVICE,
    EVIDENCE_SKIP_STALE,
    EVIDENCE_SKIP_WRITE_FAILED,
    SCREEN_EVIDENCE_CONTROL,
    SCREEN_EVIDENCE_MAX_AGE,
    SCREEN_EVIDENCE_MAX_BYTES,
    SCREEN_EVIDENCE_SOURCES,
    BLUR_HAAR,
    BLUR_UNAVAILABLE,
    CLIP_CANCELLED_CONTROL,
    CLIP_CANCEL_MULTIPLE_FACES_AFTER,
    CLIP_SKIP_FACES_UNKNOWN,
    CLIP_SKIP_MULTIPLE_FACES,
    WS_TRANSPORT_MAX_BYTES,
)
from capture import CameraCapture  # noqa: E402
from config import (  # noqa: E402
    ALLOW_SEARCH_ENV_VAR,
    DEFAULT_DELIVER_TIMEOUT_SEC,
    DELIVER_DIR_ENV_VAR,
    EXAM_PROFILE_ENV_VAR,
    EXAM_PROFILE_FILENAME,
    EXAM_PROFILE_PRESETS,
    EXAM_PROFILE_PUBKEY_ENV_VAR,
    PROFILE_SIG_LABELS,
    PROFILE_SOURCE_CLI,
    ProctorConfig,
    exam_profile_example,
    is_control_channel_host,
    url_host,
)
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
    EventKind.SHELL_CONFIG: Channel.SHELL,
    # Профиль экзамена — канал оболочки: белый список применяет она, и коды
    # этих видов стоят в её блоке (SHL_6xx).
    EventKind.NAVIGATION_OFF_PROFILE: Channel.SHELL,
    EventKind.SUBRESOURCE_OFF_PROFILE: Channel.SHELL,
    EventKind.EXAM_PROFILE_APPLIED: Channel.SHELL,
    EventKind.EXAM_PROFILE_ABSENT: Channel.SHELL,
    EventKind.EXAM_PROFILE_SEARCH_ALLOWED: Channel.SHELL,
    EventKind.PASTE_BURST: Channel.FUSION,
    EventKind.TYPING_ANOMALY: Channel.FUSION,
    EventKind.FUSION_GAZE_THEN_ANSWER: Channel.FUSION,
    EventKind.FUSION_BLUR_THEN_ANSWER: Channel.FUSION,
    EventKind.FUSION_PHONE_THEN_ANSWER: Channel.FUSION,
    EventKind.SESSION_STARTED: Channel.SYSTEM,
    EventKind.SESSION_ENDED: Channel.SYSTEM,
    EventKind.CALIBRATION_DONE: Channel.SYSTEM,
    EventKind.SENSOR_LOST: Channel.SYSTEM,
    EventKind.LOCK_REVIEW_REQUESTED: Channel.SYSTEM,
    EventKind.PROCTOR_DECISION: Channel.SYSTEM,
    EventKind.RISK_RESET: Channel.SYSTEM,
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
    EventKind.SHELL_CONFIG: "Зафиксирован фактический режим запуска оболочки",
    EventKind.NAVIGATION_OFF_PROFILE: "Попытка открыть источник вне правил экзамена",
    EventKind.SUBRESOURCE_OFF_PROFILE: "Запрос к источнику вне правил экзамена",
    EventKind.EXAM_PROFILE_APPLIED: "Применён профиль экзамена",
    EventKind.EXAM_PROFILE_ABSENT: "Правила экзамена проктором не задавались",
    EventKind.EXAM_PROFILE_SEARCH_ALLOWED: "Профиль экзамена разрешает поисковики",
    EventKind.PASTE_BURST: "Вставлен большой блок текста",
    EventKind.TYPING_ANOMALY: "Ритм набора не похож на базовый профиль студента",
    EventKind.FUSION_GAZE_THEN_ANSWER: "Взгляд в сторону, сразу после — ответ на вопрос",
    EventKind.FUSION_BLUR_THEN_ANSWER: "Переключение окна, сразу после — ответ на вопрос",
    EventKind.FUSION_PHONE_THEN_ANSWER: "Телефон в кадре, сразу после — ответ на вопрос",
    EventKind.SESSION_STARTED: "Сессия прокторинга начата",
    EventKind.SESSION_ENDED: "Сессия прокторинга завершена",
    EventKind.CALIBRATION_DONE: "Калибровка завершена",
    EventKind.SENSOR_LOST: "Потерян сигнал с камеры или микрофона",
    EventKind.LOCK_REVIEW_REQUESTED: "Экзамен приостановлен, требуется решение проктора",
    EventKind.PROCTOR_DECISION: "Решение проктора по приостановленному экзамену",
    EventKind.RISK_RESET: "Накопленный risk-score обнулён",
}

# ===========================================================================
# РЕЖИМ ЗАПУСКА КАК ЧАСТЬ ДОКАЗАТЕЛЬСТВА
#
# Разбор поверхности запуска (LAUNCH-01, -02, -04) показал три места, где
# условия экзамена задаются снаружи и НЕ попадают в подписанную цепочку:
#
#   1. флаги оболочки (`--no-lockdown`, `--no-kiosk`, `--allow-multi-display`)
#      снимают защиту машины, а в журнале это видно только по отсутствию
#      SHORTCUT_BLOCKED и WINDOW_BLUR — то есть не отличимо от честной сессии,
#      в которой студент ничего не нажимал;
#   2. флаги ядра (`--no-yolo`, `--no-identity`, `--no-audio`, `--no-env`,
#      `--headless`) дают чистый на вид подписанный отчёт, потому что в
#      `capabilities` «выключено флагом» и «недоступно» сливались в один
#      булев: честная машина без модели выглядела как саботаж, и наоборот;
#   3. переменные окружения подменяют интерпретатор сайдкара, ключ подписи и
#      каталог доказательств.
#
# Ни одно из трёх здесь не БЛОКИРУЕТСЯ: на машине студента запрет бесполезен
# (запускает он, и ядро у него в руках). Всё три оставляют улику в цепочке.
# ===========================================================================

#: Переменные окружения, которые меняют доказательную базу. Их значения идут в
#: цепочку записью `control`/`launch_env`. Секретов среди них нет: это пути.
#: Токен канала (`PROCTOR_WS_TOKEN`) сюда намеренно НЕ входит — он секрет, и
#: его место не в пакете доказательств, который забирает проктор.
EVIDENCE_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("PROCTOR_PYTHON", "интерпретатор, которым оболочка запускает сайдкар"),
    ("PROCTOR_SIGNING_KEY", "ключ подписи отчёта"),
    ("PROCTOR_SESSIONS_DIR", "каталог, куда собираются доказательства"),
)

#: Код инцидента для SHELL_CONFIG. Канонический реестр кодов живёт в
#: `engine/events.py` (EVENT_CODES), но этот вид не проходит через движок
#: (см. `_cmd_shell_config`), поэтому код проставляется здесь. Номер взят из
#: свободного начала блока оболочки (SHL_601…605 заняты). Владельцу
#: `engine/events.py` остаётся внести ту же строку в EVENT_CODES.
SHELL_CONFIG_CODE = "SHL_600"

#: Коды видов, относящихся к профилю экзамена. Причина, по которой они здесь, а
#: не только в `engine/events.py`, та же, что у SHELL_CONFIG: эти события не
#: проходят через движок правил (см. `_cmd_off_profile` и
#: `_emit_exam_profile_state`), и без кода отчёт печатал бы UNK_000 — то есть
#: апелляция по названному с экрана коду не нашла бы в журнале ничего.
#: Номера взяты из свободной части блока оболочки (SHL_600…605 заняты).
#: Владельцу `engine/events.py` остаётся внести ТЕ ЖЕ строки в EVENT_CODES —
#: канонический реестр по-прежнему там, и расхождение кодов проверяет
#: `make test` (сверка с таблицей ADVICE в hud.js).
EXAM_PROFILE_CODES: dict[EventKind, str] = {
    EventKind.NAVIGATION_OFF_PROFILE: "SHL_606",
    EventKind.SUBRESOURCE_OFF_PROFILE: "SHL_607",
    EventKind.EXAM_PROFILE_APPLIED: "SHL_610",
    EventKind.EXAM_PROFILE_ABSENT: "SHL_611",
    EventKind.EXAM_PROFILE_SEARCH_ALLOWED: "SHL_612",
}

#: Виды, которые ядро принимает от оболочки как «источник вне белого списка».
OFF_PROFILE_KINDS: tuple[EventKind, ...] = (
    EventKind.NAVIGATION_OFF_PROFILE,
    EventKind.SUBRESOURCE_OFF_PROFILE,
)

#: Пауза между записями о ПОДЗАПРОСАХ к одному и тому же источнику, секунды.
#:
#: Почему ограничение живёт здесь, а не в таблице `cooldowns` конфига: таблица
#: действует только на виды, у которых в `engine/events.py` есть `Rule`
#: (`EventEngine.cooldown_for` для неизвестного вида возвращает ОБЩИЙ
#: `cooldown_sec` = 10 с, а не значение из таблицы). У этих двух видов правил
#: нет, и события выпускает само ядро, поэтому ограничение одно и ровно в том
#: месте, где оно применяется.
#:
#: Ограничение ПО ИСТОЧНИКУ, а не по виду: одна страница с чужого адреса
#: тянет десятки шрифтов и счётчиков, и это ОДНО наблюдение «страница
#: обращается наружу», а не десятки обвинений. Разные чужие адреса — разные
#: наблюдения, и они не глушат друг друга.
SUBRESOURCE_OFF_PROFILE_GAP_SEC = 30.0

#: Попытки НАВИГАЦИИ не глушатся вообще (пауза 0): ценность профиля не в
#: запрете — профиль обходим, он улика, — а в том, что каждая попытка выйти за
#: белый список лежит в журнале с полным адресом. Переход при этом оболочкой
#: не выполняется, поэтому поток таких событий не может быть вызван
#: перезагрузками страницы: заблокированная навигация ничего не загружает.
NAVIGATION_OFF_PROFILE_GAP_SEC = 0.0

#: Сколько символов адреса попадает в журнал.
#:
#: ПОЛНЫЙ адрес здесь — требование заказчика и осознанное исключение из
#: обещания «по записи журнала нельзя восстановить ответ студента»
#: (`protocol.safe_label`). Исключение узкое и объяснимое: адрес, который
#: студент пытался открыть, — это его собственное действие в сторону внешнего
#: материала, а не его ответ на вопрос. Без адреса запись бесполезна:
#: «попытка выхода за список» без указания куда ничего не доказывает.
#: Обрезка существует только против адреса на килобайт (data-URL, длинная
#: подпись в query) и факт обрезки отмечается в `detail`.
OFF_PROFILE_URL_MAX_LEN = 1000

#: Сколько событий выхода за белый список ядро кладёт в цепочку за сессию.
#:
#: Зачем предел вообще. Дедупликация подзапросов работает ПО ХОСТУ, а навигации
#: не сворачиваются вообще — и то и другое правильно, но против заливки
#: бессильно: заблокированный запрос отменяется ДО разрешения имени, поэтому
#: хосты можно печатать из воздуха (`fetch('https://h'+i+'.invalid/')`) и
#: получить тысячи разных «источников» с одной страницы. Две тысячи записей в
#: подписанной цепочке и две тысячи групп в отчёте — это не доказательство, а
#: способ утопить настоящую попытку в шуме, который студент создаёт сам.
#:
#: Почему предел, а не более хитрая склейка. Сворачивать разные хосты в один
#: факт нельзя: адрес и есть улика. Поэтому первые N попыток записываются
#: целиком и дословно, а дальше ядро перестаёт выпускать события и считает
#: остаток (`over_cap`). Счётчик уезжает в итоговую запись и в отчёт, то есть
#: масштаб заливки не теряется — теряется только дословный адрес попытки
#: номер 201, и это сознательный обмен.
OFF_PROFILE_MAX_EVENTS = 200

#: Поля `detail`, которыми владеет движок правил и счёт риска. Приходящие от
#: оболочки их задавать не вправе: `escalation.run` склеивает событие с чужим
#: прогоном лестницы и сворачивает его вклад в ноль, а `code` — это
#: идентификатор для апелляции. Срез стоит на границе сокета — там же, где
#: `engine/risk.py` его и предполагает (см. комментарий к `_ladder_obs_key`).
#: `evidence_skipped` — причина отсутствия кадра камеры; её ставит только ядро
#: (`_emit`), иначе оболочка могла бы сама объявить «камеры не было».
SHELL_DETAIL_RESERVED: tuple[str, ...] = (
    "escalation", "code", "confirmed_by", "confirm_sec", "duration_total",
    "refines", "refined_by", "refined_by_code", "superseded_by",
    "evidence_skipped",
)


def _profile_sig_label(state: Any) -> str:
    """Человекочитаемое состояние подписи профиля для текста события."""
    key = str(state or "")
    return PROFILE_SIG_LABELS.get(key, key or "неизвестно")


def _env_path_facts(raw: str) -> dict[str, Any]:
    """Что за путь пришёл в переменной окружения. Без чтения содержимого."""
    out: dict[str, Any] = {"value": raw[:500]}
    try:
        p = Path(raw).expanduser()
        out["resolved"] = str(p.resolve(strict=False))
        out["exists"] = p.exists()
    except Exception as exc:  # путь может быть синтаксически невозможным
        out["resolved"] = ""
        out["exists"] = False
        out["error"] = str(exc)[:200]
    return out


def launch_env_record(cfg: Any = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Улика по переменным окружения и фактическому запуску ядра.

    Уходит в цепочку записью `control`/`launch_env` при старте сессии, в
    `hello` и в `status`. Смысл записи: `PROCTOR_PYTHON` подменяет интерпретатор
    сайдкара (`spawnSidecar` берёт её первой), `PROCTOR_SIGNING_KEY` — ключ, под
    которым выйдет отчёт, `PROCTOR_SESSIONS_DIR` — место, куда уедут
    доказательства. Все три задаются снаружи процесса и до этой правки нигде не
    фиксировались: сессия, собранная подменённым интерпретатором в чужой
    каталог под чужим ключом, выглядела в точности как обычная.

    `python_executable` берётся из `sys.executable`, то есть описывает ЭТОТ
    процесс. Он не зависит от того, что оболочка сообщила о себе в SHELL_CONFIG,
    поэтому расхождение «оболочка заявила один интерпретатор, ядро запущено
    другим» видно без доверия к оболочке.
    """
    environ = os.environ if env is None else env
    rows: list[dict[str, Any]] = []
    for name, affects in EVIDENCE_ENV_VARS:
        raw = str(environ.get(name, "") or "")
        row: dict[str, Any] = {"name": name, "affects": affects, "set": bool(raw.strip())}
        if raw.strip():
            row.update(_env_path_facts(raw))
        rows.append(row)

    record: dict[str, Any] = {
        "vars": rows,
        "vars_set": [r["name"] for r in rows if r["set"]],
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "argv": list(sys.argv[1:]),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
    }
    if cfg is not None:
        # Чем всё это кончилось ФАКТИЧЕСКИ: переменная могла быть перебита
        # флагом CLI, а каталог — понижен до запасного (degraded_handover).
        record["sessions_dir_effective"] = str(getattr(cfg, "sessions_path", "") or "")
        record["sessions_dir_source"] = str(getattr(cfg, "sessions_dir_source", "") or "")
        # Куда после экзамена копируется пакет. Не меняет доказательную базу,
        # поэтому не в EVIDENCE_ENV_VARS, но разбору нужно знать, куда пакет
        # ДОЛЖЕН был уйти, — сверить с тем, что лежит в папке проктора.
        record["deliver_dir_effective"] = str(getattr(cfg, "deliver_path", "") or "")
        record["deliver_dir_source"] = str(getattr(cfg, "deliver_dir_source", "") or "")
        record["signing_key_effective"] = str(getattr(cfg, "signing_key_abs", "") or "")
        record["signature_authority"] = str(getattr(cfg, "signature_authority", "") or "")
        record["launch_flags"] = dict(getattr(cfg, "launch_flags", {}) or {})
    return record


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
# Аутентификация локального канала
#
# Канал на 127.0.0.1 раньше не проверял, КТО подключился: любой локальный
# процесс представлялся оболочкой. Отсюда растут сразу четыре вектора, и все
# четыре не требуют прав администратора:
#
#   1. подмена ядра — свой сервер на том же порту, пока сайдкар не поднялся,
#      и оболочка получает «чистую» сессию;
#   2. обнуление риска чужой командой — `{"type":"command","name":"reset_risk"}`
#      отправить мог кто угодно;
#   3. отравление базовой линии fusion — поток `telemetry` с выдуманным ритмом
#      набора делает последующее списывание «нормой»;
#   4. прослушивание потока — сторонний клиент получал status/event/risk,
#      то есть видел всё, что видит проктор.
#
# Лечится одноразовым токеном: сайдкар печатает его при старте и кладёт в файл
# с правами 0600, оболочка предъявляет при рукопожатии. Соединение без токена
# рвётся в `process_request`, то есть ДО апгрейда до WebSocket: непрошеный
# клиент не получает ни одного байта полезной нагрузки.
# ===========================================================================
#: Заголовок рукопожатия. Предпочтительный способ: значение не попадает ни в
#: лог сервера, ни в историю, ни в строку URL.
AUTH_HEADER = "X-Proctor-Token"

#: Переменная окружения, которой можно задать токен заранее — так launcher
#: передаёт один и тот же секрет сайдкару и оболочке, не читая файл.
AUTH_TOKEN_ENV_VAR = "PROCTOR_WS_TOKEN"


def _default_token_file(port: int) -> Path:
    """Файл с токеном: временный каталог ОС, имя по порту.

    Не в каталоге сессий: туда уходят доказательства, которые забирает проктор,
    и секрет канала там оказался бы в пакете. Не в репозитории: его копируют и
    публикуют. Временный каталог ОС чистится сам и не уезжает вместе с данными.
    """
    return Path(tempfile.gettempdir()) / f"qih-proctor-{int(port)}.token"


def _new_token() -> str:
    """Одноразовый токен сессии процесса. 32 байта энтропии из `secrets`."""
    return secrets.token_urlsafe(32)


def _token_from_request(path: str, headers: Any) -> str:
    """Достать предъявленный токен из рукопожатия.

    Понимает заголовок (основной способ) и query-параметр `?token=`
    (запасной: не всякий WS-клиент умеет ставить заголовки). Query-вариант
    принимается только потому, что адрес — петлевой loopback и в чужие логи
    не уезжает; предпочтительным он не становится.
    """
    for name in (AUTH_HEADER, "Authorization", "Sec-WebSocket-Protocol"):
        raw = ""
        with contextlib.suppress(Exception):
            getter = getattr(headers, "get", None)
            raw = str(getter(name) or "") if callable(getter) else ""
        if not raw:
            continue
        value = raw.strip()
        for prefix in ("Bearer ", "bearer ", "token ", "proctor-token."):
            if value.startswith(prefix):
                value = value[len(prefix):].strip()
        if value:
            return value
    with contextlib.suppress(Exception):
        query = urllib.parse.urlsplit(str(path or "")).query
        values = urllib.parse.parse_qs(query).get("token") or []
        if values and str(values[0]).strip():
            return str(values[0]).strip()
    return ""


def _write_token_file(path: Path, token: str, port: int, host: str) -> bool:
    """Положить токен в файл, читаемый только владельцем (0600)."""
    payload = {
        "token": token, "host": host, "port": int(port),
        "header": AUTH_HEADER, "pid": os.getpid(), "created_at": time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Права выставляются ДО записи: иначе между созданием файла и chmod
        # есть окно, в котором секрет читает кто угодно.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.chmod(str(path), stat.S_IRUSR | stat.S_IWUSR)
        return True
    except OSError as exc:
        log.warning("не удалось записать файл токена %s: %s", path, exc)
        return False


# ===========================================================================
# Сырой слой: наблюдения и телеметрия ввода в журнал
#
# Почему это вообще нужно. В журнал попадали только ГОТОВЫЕ события — то, что
# движок уже счёл инцидентом. Телеметрия ввода жила в памяти процесса и умирала
# вместе с ним. Значит, на апелляции нельзя было ответить на вопрос «что система
# видела», а можно только на «что она решила»; и нельзя пересчитать оценку,
# не доверяя разбору движка.
#
# Почему агрегация, а не поток один-в-один. CV-поток даёт десятки наблюдений в
# секунду на каждый канал. Запись каждого в хеш-цепочку — это сотни тысяч
# записей за экзамен, то есть база на гигабайты и проверка цепочки на минуты.
# Поэтому наблюдения складываются в окна фиксированной длины (по умолчанию 1 с)
# с сохранением ГРАНИЦ: момент включения и выключения условия пишется всегда,
# даже если окно ещё не закрылось. Границы — это именно то, по чему движок
# принимал решение, их терять нельзя.
#
# Приватность — часть контракта, а не настройка. Из телеметрии берутся только
# интервалы, КЛАССЫ клавиш и длины блоков. Символы, имена клавиш и любой текст
# отбрасываются на входе по БЕЛОМУ списку полей: неизвестное поле не проходит.
# Чёрный список здесь не годился бы — он пропускает всё, о чём не подумали.
# ===========================================================================
#: Белый список числовых полей телеметрии. Всё остальное не попадает в журнал.
TELEMETRY_NUMERIC_FIELDS: tuple[str, ...] = (
    "interval_ms", "length", "time_to_answer_ms", "mean_ms", "std_ms", "chars",
    "gap_sec", "delay_ms", "difficulty",
)
#: Числа, которые движок правил читает вложенными (`typing_stats`). Белый
#: список их уплощает, а `sanitize_telemetry` собирает обратно: иначе ворота
#: санитизации заодно ослепили бы правило 5 и отбор правой половины связки.
TELEMETRY_NESTED_FIELDS: tuple[str, ...] = ("mean_ms", "std_ms", "chars")
#: Белый список полей-категорий и допустимые значения каждого.
TELEMETRY_ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "kind": ("keystroke", "paste", "answer_submit", "question_shown"),
    "key_class": ("char", "nav", "ctrl", "digit", "letter", "other"),
    "source": ("clipboard", "keyboard", "shell", "mock"),
}
#: Поля-метки: пропускаются как короткие строки без текста ответа.
TELEMETRY_LABEL_FIELDS: tuple[str, ...] = ("question_id",)
#: Числовые поля наблюдений детекторов, которые имеет смысл агрегировать.
OBSERVATION_NUMERIC_FIELDS: tuple[str, ...] = (
    "conf", "confidence", "yaw", "pitch", "gaze_yaw", "gaze_pitch", "similarity",
    "area_ratio", "rms", "mismatch_ratio", "face_count", "count", "no_blink_sec",
    "mouth_open_ratio", "threshold",
)


def sanitize_telemetry(msg: dict[str, Any]) -> dict[str, Any]:
    """Привести сообщение телеметрии к приватному виду для журнала.

    Возвращает ТОЛЬКО то, что прошло белый список. Символы, набранный текст и
    имена клавиш не проходят ни при какой форме сообщения: по такой записи
    нельзя восстановить ответ студента, а именно это было бы слежкой за
    содержанием работы вместо контроля условий экзамена.

    Это ЕДИНСТВЕННЫЕ ворота потока телеметрии, и стоят они на стороне сайдкара
    намеренно. Раньше тот же белый список применялся только к сырому слою, а в
    движок правил сообщение уходило как пришло — и свободное поле `question_id`
    вместе с недокументированным `origin` уносили текст ответа в сообщение
    события, в payload, в хеш-цепочку, в пакет проктору и в лог. Санитизация на
    стороне оболочки от этого не защищает: доверять содержанию сообщения из
    сети сайдкар не вправе, кем бы ни был клиент.

    Метки не обрезаются по длине, а проходят `safe_label()`: обрезанный ответ
    это всё ещё ответ.
    """
    out: dict[str, Any] = {}
    nested = msg.get("typing_stats")
    merged: dict[str, Any] = dict(msg)
    if isinstance(nested, dict):
        for key, value in nested.items():
            merged.setdefault(str(key), value)

    for field_name in TELEMETRY_NUMERIC_FIELDS:
        if field_name in merged:
            try:
                out[field_name] = round(float(merged[field_name]), 3)
            except (TypeError, ValueError):
                continue
    for field_name, allowed in TELEMETRY_ENUM_FIELDS.items():
        value = str(merged.get(field_name) or "")
        if value in allowed:
            out[field_name] = value
    for field_name in TELEMETRY_LABEL_FIELDS:
        raw = str(merged.get(field_name) or "").strip()
        if not raw:
            continue
        # Идентификатор вопроса — это номер в билете, а не содержание ответа.
        # Непохожее на идентификатор заменяется устойчивым суррогатом: метка
        # нужна только чтобы связать события одного эпизода, и для этого
        # достаточно, чтобы она была одинаковой, а не читаемой.
        value = safe_label(raw)
        if value:
            out[field_name] = value

    # `typing_stats` возвращается в исходную форму: движок читает его вложенным,
    # и без этого ворота санитизации молча обнулили бы ритм набора.
    if isinstance(nested, dict) or any(f in out for f in TELEMETRY_NESTED_FIELDS):
        out["typing_stats"] = {f: out[f] for f in TELEMETRY_NESTED_FIELDS if f in out}
    return out


class RawObservationLog:
    """Окна сырых наблюдений: копит, агрегирует, отдаёт готовые записи журнала.

    Потокобезопасен: `note()` зовут из CV-потока, `note_telemetry()` — из
    event loop. Записи не пишутся отсюда: класс только считает, а решение
    записать принимает сайдкар — так проще не писать в закрытую сессию.
    """

    def __init__(self, window_sec: float = 1.0, enabled: bool = True,
                 max_records: int = 20000) -> None:
        self.window_sec = max(float(window_sec), 0.1)
        self.enabled = bool(enabled)
        self.max_records = int(max_records)
        self._lock = threading.Lock()
        self._open: dict[str, dict[str, Any]] = {}
        self._written = 0
        self.dropped = 0

    @property
    def written(self) -> int:
        return self._written

    def mark_written(self, count: int = 1) -> None:
        with self._lock:
            self._written += int(count)

    def reset(self) -> None:
        with self._lock:
            self._open.clear()
            self._written = 0
            self.dropped = 0

    def _budget_left(self) -> bool:
        """Потолок на сессию: сырой слой не имеет права съесть диск.

        Упёрлись — перестаём копить и считаем пропущенное. Факт обрезки
        попадает в журнал отдельной записью и в отчёт: молчаливо потерянный
        слой хуже, чем честно обрезанный.
        """
        if self.max_records <= 0:
            return True
        return self._written < self.max_records

    def note(self, source: str, name: str, active: bool, ts: float,
             detail: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Учесть одно наблюдение детектора. Возвращает ЗАКРЫТЫЕ окна."""
        if not self.enabled:
            return []
        with self._lock:
            if not self._budget_left():
                self.dropped += 1
                return []
            ready: list[dict[str, Any]] = []
            key = f"{source}/{name}"
            node = self._open.get(key)
            if node is not None and (ts - float(node["ts"])) >= self.window_sec:
                ready.append(self._close(key, node))
                node = None
            if node is None:
                node = self._new(source, name, ts)
                self._open[key] = node

            node["samples"] += 1
            node["last_ts"] = float(ts)
            if active:
                node["active"] += 1
            prev = node.get("_state")
            if prev is None or bool(prev) != bool(active):
                # Граница условия — это момент, по которому движок принимал
                # решение. Храним до 12 переключений на окно: больше означает
                # дребезг, и для него важно само число, а не каждый фронт.
                if len(node["edges"]) < 12:
                    node["edges"].append({"ts": round(float(ts), 3),
                                          "active": bool(active)})
                else:
                    node["edges_dropped"] += 1
                node["_state"] = bool(active)
            self._merge_numbers(node, detail or {}, OBSERVATION_NUMERIC_FIELDS)
            return ready

    def note_telemetry(self, payload: dict[str, Any], ts: float) -> list[dict[str, Any]]:
        """Учесть пачку телеметрии ввода. Возвращает ЗАКРЫТЫЕ окна."""
        if not self.enabled:
            return []
        clean = sanitize_telemetry(payload)
        if not clean:
            return []
        kind = str(clean.get("kind") or "input")
        with self._lock:
            if not self._budget_left():
                self.dropped += 1
                return []
            ready: list[dict[str, Any]] = []
            key = f"telemetry/{kind}"
            node = self._open.get(key)
            if node is not None and (ts - float(node["ts"])) >= self.window_sec:
                ready.append(self._close(key, node))
                node = None
            if node is None:
                node = self._new("telemetry", kind, ts)
                self._open[key] = node
            node["samples"] += 1
            node["active"] += 1
            node["last_ts"] = float(ts)
            self._merge_numbers(node, clean, TELEMETRY_NUMERIC_FIELDS)
            for field_name in ("key_class", "source", "question_id"):
                value = clean.get(field_name)
                if value is None:
                    continue
                bucket = node["classes"].setdefault(field_name, {})
                bucket[str(value)] = int(bucket.get(str(value), 0)) + 1
            return ready

    def flush(self) -> list[dict[str, Any]]:
        """Закрыть все открытые окна (конец сессии, смена состояния)."""
        with self._lock:
            ready = [self._close(key, node) for key, node in list(self._open.items())]
            self._open.clear()
            return ready

    def due(self, now: float) -> list[dict[str, Any]]:
        """Закрыть окна, у которых истёк срок. Зовётся тикером."""
        with self._lock:
            ready: list[dict[str, Any]] = []
            for key, node in list(self._open.items()):
                if (now - float(node["ts"])) >= self.window_sec:
                    ready.append(self._close(key, node))
            return ready

    # --------------------------------------------------------------- внутреннее
    def _new(self, source: str, name: str, ts: float) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex, "source": str(source), "name": str(name),
            "ts": round(float(ts), 3), "last_ts": round(float(ts), 3),
            "window_sec": self.window_sec, "samples": 0, "active": 0,
            "numbers": {}, "classes": {}, "edges": [], "edges_dropped": 0,
        }

    def _close(self, key: str, node: dict[str, Any]) -> dict[str, Any]:
        self._open.pop(key, None)
        numbers = {}
        for field_name, agg in node["numbers"].items():
            count = max(int(agg["n"]), 1)
            numbers[field_name] = {
                "min": round(float(agg["min"]), 4),
                "max": round(float(agg["max"]), 4),
                "mean": round(float(agg["sum"]) / count, 4),
                "n": int(agg["n"]),
            }
        out = {
            "id": node["id"], "source": node["source"], "name": node["name"],
            "ts": node["ts"], "last_ts": round(float(node["last_ts"]), 3),
            "window_sec": round(float(node["window_sec"]), 3),
            "samples": int(node["samples"]), "active": int(node["active"]),
            "numbers": numbers, "edges": list(node["edges"]),
        }
        if node["classes"]:
            out["classes"] = {k: dict(v) for k, v in node["classes"].items()}
        if node["edges_dropped"]:
            out["edges_dropped"] = int(node["edges_dropped"])
        return out

    @staticmethod
    def _merge_numbers(node: dict[str, Any], detail: dict[str, Any],
                       allowed: Iterable[str]) -> None:
        for field_name in allowed:
            if field_name not in detail:
                continue
            try:
                value = float(detail[field_name])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            agg = node["numbers"].setdefault(
                field_name, {"min": value, "max": value, "sum": 0.0, "n": 0})
            agg["min"] = min(agg["min"], value)
            agg["max"] = max(agg["max"], value)
            agg["sum"] += value
            agg["n"] += 1


# ===========================================================================
# Мелкие утилиты
# ===========================================================================
def _f_num(value: Any, default: float = 0.0) -> float:
    """float или значение по умолчанию. Данные приходят из WS, доверия нет."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Достать поле из наблюдения: и dataclass, и dict, и объект с атрибутами."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def _clamp_int(value: Any, lo: int, hi: int) -> int:
    """Целое из сообщения клиента в пределах [lo, hi]; мусор -> lo."""
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return lo
    return min(max(number, lo), hi)


def _norm_box(box: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) в пикселях или долях кадра -> целые пиксели внутри кадра."""
    try:
        x, y, w, h = (float(v) for v in list(box)[:4])
    except (TypeError, ValueError):
        return None
    if max(abs(x), abs(y), w, h) <= 1.5:  # нормированные координаты
        x, y, w, h = x * width, y * height, w * width, h * height
    x0, y0 = max(int(round(x)), 0), max(int(round(y)), 0)
    x1, y1 = min(int(round(x + w)), width), min(int(round(y + h)), height)
    if x1 - x0 <= 1 or y1 - y0 <= 1:
        return None
    return x0, y0, x1 - x0, y1 - y0


def _blurred(cv2: Any, frame: Any, regions: list[Any]) -> Any:
    """Копия кадра с размытыми регионами (лица посторонних). Без регионов — сам кадр."""
    if not regions:
        return frame
    image = frame.copy()
    fh, fw = int(image.shape[0]), int(image.shape[1])
    for region in regions:
        box = _norm_box(region, fw, fh)
        if box is None:
            continue
        x, y, w, h = box
        roi = image[y:y + h, x:x + w]
        if roi.size:
            k = max(3, (min(w, h) // 4) * 2 + 1)
            image[y:y + h, x:x + w] = cv2.GaussianBlur(roi, (k, k), 0)
    return image


def _overlap_share(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Какую долю площади рамки `b` закрывает рамка `a` (рамки в пикселях)."""
    iw = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    ih = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    area = b[2] * b[3]
    return (iw * ih) / float(area) if area > 0 else 0.0


def _finite_float(value: Any) -> float | None:
    """Конечное число из сообщения клиента или None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _raw_size(raw: Any, limit: int) -> int:
    """Размер сообщения WS в байтах UTF-8 (текст) или как есть (bytes).

    Точный подсчёт для текста копирует строку, поэтому делается, только когда
    предел вообще достижим: символ UTF-8 — не больше 4 байт.
    """
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return len(raw)
    text = str(raw)
    if len(text) * 4 <= limit:
        return len(text)
    return len(text.encode("utf-8", "surrogatepass"))


def _decode_screen_jpeg(data_b64: str) -> bytes:
    """base64 снимка окна экзамена -> байты JPEG. ValueError — снимок отклонён."""
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"data_b64 не разбирается как base64: {exc}") from None
    if not data:
        raise ValueError("пустой снимок")
    if len(data) > SCREEN_EVIDENCE_MAX_BYTES:
        raise ValueError(f"снимок {len(data)} байт больше предела "
                         f"{SCREEN_EVIDENCE_MAX_BYTES}")
    if not data.startswith(b"\xff\xd8\xff"):
        raise ValueError("не JPEG: нет сигнатуры FF D8 FF")
    return data


def _write_evidence_bytes(path: Path, data: bytes) -> str:
    """Записать файл доказательства как есть и вернуть sha256 записанных байт."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as fh:  # "x": чужой файл с тем же именем не затираем
        fh.write(data)
        fh.flush()
        with contextlib.suppress(OSError):
            os.fsync(fh.fileno())
    return hashlib.sha256(data).hexdigest()


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

    @property
    def genesis(self) -> str:
        """В резервном журнале genesis-хеша нет.

        Пустая строка здесь означает, что код сверки сессии не формируется, и
        это правильно: код обязан быть выводим из цепочки, а в деградированном
        режиме выводить его не из чего. Выдать случайный код значило бы выдать
        непроверяемое за проверяемое.
        """
        return ""

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
    finalize: bool = False    # gaze_grid c point=null: обход закончен, строим карту
    begun: bool = False       # begin_* уже выполнен CV-потоком
    #: Недоделанное задание другой стадии, с которой оболочка ушла по таймеру:
    #: CV-поток закроет его тем, что успело собраться, перед началом этого.
    supersedes: "_CalibrationJob | None" = None

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
        #: То же, но с РАЗДЕЛЁННОЙ причиной молчания канала: «выключен флагом»
        #: и «недоступен» — разные факты. Пока они сливались в один булев,
        #: честная машина без модели YOLO выглядела в отчёте так же, как
        #: машина, где детектор сняли флагом `--no-yolo` (LAUNCH-02).
        #: Заполняется в load_modules(), отдаётся в capabilities_info().
        self.caps_state: dict[str, dict[str, Any]] = {}
        #: Текст ошибки импорта/создания по человекочитаемому имени модуля —
        #: он и становится причиной в состоянии `unavailable`.
        self._module_errors: dict[str, str] = {}

        # Куда и как уходят доказательства. Каталог уже разрешён в main() —
        # сюда приходит ФАКТИЧЕСКИЙ путь вместе с признаком degraded_handover,
        # чтобы сессия не выясняла это заново и не разошлась с тем, о чём уже
        # сказали человеку в логе при старте.
        self.handover: dict[str, Any] = dict(getattr(cfg, "handover_resolved", None)
                                             or cfg.handover_info())
        self.session = Session(
            cfg.sessions_path, cfg.evidence_subdir, cfg.db_filename, cfg.report_filename,
            handover=self.handover,
        )
        #: Результат сборки последнего пакета доказательств (для статуса и отчёта).
        #: После доставки в папку проктора в нём же лежит `delivery`.
        self.last_package: dict[str, Any] = {}
        #: Каталог сессии последнего пакета: туда дописывается итог доставки
        #: (`handover.json`), в том числе при повторе из оболочки.
        self._package_dir: str = ""
        #: Папка проктора: результат стартовой проверки (`prepare_delivery`).
        #: Идёт в `hello` и в каждый `status` — предполётный экран показывает,
        #: уйдёт ли пакет с этой машины, ДО начала экзамена.
        self.delivery: dict[str, Any] = dict(getattr(cfg, "delivery_resolved", None)
                                             or _delivery_unprobed(cfg))
        #: Фоновая доставка (`_deliver`): завершение сессии её не ждёт. Пока
        #: задача не кончилась, копирование «идёт» (`delivery.in_progress`), и
        #: повторная команда не запускает вторую копию параллельно (иначе рядом
        #: легли бы `-2` и `-3`); следующая доставка встаёт за этой.
        self._delivery_task: asyncio.Task[Any] | None = None
        #: Выставляется на останове: поток копирования бросает копию между
        #: кусками и удаляет свою недописанную (`deliver_package(cancel=…)`).
        self._deliver_abort = threading.Event()
        #: Каталог сессии -> успешная доставка её пакета. Пересборка
        #: (`export_report`) после неё вторую копию не делает: лишний пакет на
        #: шаре ломает сверку числа пакетов со списком группы.
        self._delivered: dict[str, dict[str, Any]] = {}
        #: `handover.json` правят и сборка пакета, и фоновая доставка (в том
        #: числе поздний итог) — каждая чтением и перезаписью. Без замка одна
        #: затирала бы запись другой.
        self._note_lock = asyncio.Lock()
        #: Идёт завершение сессии: второй `session_end` (останов, повтор
        #: оболочки, `session_start` поверх) ждёт его, а не собирает второй
        #: отчёт и второй пакет, который ушёл бы в папку проктора как `-2`.
        self._ending: asyncio.Future[None] | None = None
        self.capture: CameraCapture | None = None

        self._engine_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._calib_lock = threading.Lock()

        self._last_frame: Any = None
        self._last_frame_ts: float = 0.0
        #: Лица на последнем разобранном кадре: (bbox основного, bbox прочих,
        #: сколько всего). Живут рядом с `_last_frame` под тем же локом: кадр к
        #: событию оболочки или окружения снимается НЕ в CV-потоке, а
        #: посторонних на нём размыть и решить про клип всё равно нужно.
        self._last_faces: tuple[Any, list[Any], int] = (None, [], 0)
        #: Когда (time.time) в кадре последний раз было больше одного лица.
        #: Клип берёт 10 с ДО события из буфера, поэтому смотреть только на
        #: текущий кадр мало: посторонний мог уйти секунду назад.
        self._multi_face_ts: float = 0.0
        #: Запасной детектор лиц для размытия (`storage.evidence.plan_fallback_blur`),
        #: загружается при первом кадре-доказательстве, см. `_fallback_planner`.
        self._fallback_plan: Any = None
        self._fallback_tried = False
        #: События, разосланные оболочке с `screen: true`, в порядке рассылки:
        #: id -> {kind, at, session, state}. По нему ядро принимает
        #: `screen_evidence`: только к своему событию этой сессии, не позже
        #: SCREEN_EVIDENCE_MAX_AGE и не больше одного раза. Ограничен по
        #: размеру и по возрасту, см. `_note_screen_request`.
        self._screen_requests: dict[str, dict[str, Any]] = {}
        self._calib: _CalibrationJob | None = None
        #: Новая сессия: GazeCalibration сбросит CV-поток на ближайшем кадре.
        self._calib_reset_pending = False
        #: До этого момента (time.time) взгляд и поворот головы не становятся
        #: инцидентами: на калибровке студент смотрит в края экрана по просьбе
        #: самой системы. Ограничено временем, а не состоянием сессии, чтобы
        #: оборванная калибровка не ослепила детектор на весь экзамен.
        self._gaze_quiet_until = 0.0
        self._gaze_quiet_spent = 0.0      # израсходовано из бюджета сессии, с

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

        # ---------------------------------------------------- политика решений
        # Потолок АВТОМАТИЧЕСКОГО действия. По умолчанию блокировку автоматика
        # не выносит: порог переводит экзамен в ожидание решения проктора.
        self.auto_lock: bool = bool(getattr(cfg, "auto_lock", False))
        self.lock_policy: str = LOCK_POLICY_AUTO if self.auto_lock else LOCK_POLICY_PROCTOR
        #: Открытый запрос решения: {ts, score, reason, breakdown, session_code}.
        #: Пока он есть, экзамен приостановлен и спад риска его НЕ снимает.
        self._lock_review: dict[str, Any] | None = None

        # ------------------------------------------------- аутентификация канала
        self.auth_required: bool = not bool(getattr(cfg, "no_auth", False))
        self.auth_token: str = str(getattr(cfg, "auth_token", "") or "")
        self.token_file: Path | None = None
        self.single_client: bool = not bool(getattr(cfg, "allow_multi_client", False))
        self._rejected_clients = 0

        # ----------------------------------------------------------- сырой слой
        self.raw_log = RawObservationLog(
            window_sec=float(getattr(cfg, "observation_window", 1.0) or 1.0),
            enabled=bool(getattr(cfg, "log_raw", True)),
            max_records=int(getattr(cfg, "observation_max_records", 20000) or 0),
        )
        self._raw_truncation_noted = False

        # ------------------------------------------------------- режим запуска
        # Переменные окружения и фактический интерпретатор — улика (LAUNCH-04).
        # Снимается один раз: os.environ после старта процесса не меняется, а
        # пересчёт на каждую сессию дал бы ложное впечатление, что запись
        # описывает что-то, способное измениться посреди экзамена.
        self.launch_env: dict[str, Any] = launch_env_record(cfg)
        #: Последний SHELL_CONFIG от оболочки — фактическое состояние защиты
        #: машины. Пустой словарь означает, что оболочка о себе НЕ сообщила, и
        #: это само по себе факт для отчёта (см. `_cmd_session_end`).
        self.shell_config: dict[str, Any] = {}
        #: ВСЕ записи SHELL_CONFIG этой сессии по порядку.
        #:
        #: Раньше была только последняя (`self.shell_config = record`), и этого
        #: хватало, чтобы честное признание в ослаблении исчезло из шапки: вторая
        #: запись затирала первую, а отчёт («_shell_assurance») тоже брал
        #: последнюю. В цепочке оба `control`-блока оставались, но ни шапка, ни
        #: `blocking_reasons` их больше не видели — то есть переобъявление
        #: состояния работало как отзыв признания. Признание отозвать нельзя:
        #: предполётный гейт и шапка считаются по ХУДШЕЙ записи сессии, а сам
        #: факт переобъявления идёт в отчёт отдельной строкой.
        self.shell_config_history: list[dict[str, Any]] = []
        #: Лёг ли SHELL_CONFIG в цепочку ТЕКУЩЕЙ сессии. Сбрасывается на старте.
        self._shell_config_in_chain: bool = False

        # ---------------------------------------------------- профиль экзамена
        # Правила экзамена: какой адрес открывать и какие источники разрешены.
        # Объект собран в main() до создания сайдкара и дальше только читается.
        # Профиля может не быть — это работающее состояние, см. ExamProfile.
        self.exam_profile = cfg.exam_profile
        #: Лёг ли хеш профиля в цепочку ТЕКУЩЕЙ сессии. Флага «не писать» нет и
        #: быть не должно: именно необязательность проверки сломала SEB (У-13).
        #: Поле нужно не для того, чтобы запись отключить, а чтобы доказать в
        #: итоговой записи сессии, что она там есть.
        self._exam_profile_in_chain: bool = False
        #: Когда последний раз писали подзапрос к этому источнику: источник ->
        #: отметка времени. Ограничение по ИСТОЧНИКУ, не по виду события.
        self._off_profile_last: dict[str, float] = {}
        #: Счётчики попыток выхода за белый список за сессию. Уходят в итоговую
        #: запись цепочки: «попыток не было» — такой же результат разбора, как
        #: и список попыток, и отличать его от «мы не смотрели» обязательно.
        self._off_profile_stats: dict[str, Any] = self._new_off_profile_stats()

        # Политика и режим канала должны быть видны отчёту. В `meta` сессии их
        # не добавляем намеренно: состав `meta` участвует в genesis, а из
        # genesis выводится код сверки — добавление поля сменило бы коды всем
        # будущим сессиям. Поэтому в цепочку они идут записью `control`.
        self.cfg_dict["decision_policy"] = self.policy_info()

    # ------------------------------------------------------- профиль экзамена
    @staticmethod
    def _new_off_profile_stats() -> dict[str, Any]:
        """Пустые счётчики попыток выхода за белый список."""
        return {
            "navigation": 0,          # попыток перехода, записанных в журнал
            "subresource": 0,         # подзапросов, записанных в журнал
            "subresource_folded": 0,  # подзапросов, свёрнутых в уже открытую запись
            "disputed": 0,            # оболочка назвала нарушением разрешённый адрес
            # Оболочка сообщает о нарушениях, хотя профиля ядро не выдавало.
            # Нарушить несуществующие правила нельзя, инцидента нет — но
            # расхождение «оболочка применяет фильтр, которого ядро не
            # выдавало» обязано остаться в итоговой записи сессии.
            "claimed_without_profile": 0,
            # Попытки СВЕРХ предела событий за сессию (OFF_PROFILE_MAX_EVENTS):
            # дословный адрес не записан, факт попытки — да. Нужно против
            # заливки журнала: заблокированный запрос отменяется до разрешения
            # имени, поэтому хосты можно печатать из воздуха.
            "over_cap": 0,
            # Из них те, что ядро опознало как абсолютный запрет САМО: адрес
            # без сетевого хоста (`file:`, `data:`) или петля на порт самого
            # ядра. Белым списком такие не разрешаются, перепроверять нечего.
            "absolute": 0,
            # Источники по порядку первого появления: короткий список для
            # шапки отчёта, чтобы не разбирать весь журнал ради одной строки.
            "origins": [],
        }

    def exam_profile_record(self, chain_state: bool = True) -> dict[str, Any]:
        """Запись `control`/`exam_profile` — то, что уходит в хеш-цепочку.

        Собирает `config.ExamProfile` (там же живёт канон и хеш), здесь к ней
        добавляется только то, что знает ядро: сколько попыток выхода за
        список уже записано и лежит ли хеш профиля в цепочке этой сессии.

        `chain_state=False` для ПЕРВОЙ записи сессии: запись не может честно
        сообщить о собственном наличии в цепочке до того, как её туда
        положили, а поле `in_chain: false` внутри самой цепочечной записи
        читалось бы прямо наоборот — «хеш не зафиксирован». Ответ на этот
        вопрос даёт итоговая запись `exam_profile_final`, статус и `hello`.
        """
        record = dict(self.exam_profile.record())
        record["off_profile"] = dict(self._off_profile_stats)
        if chain_state:
            record["in_chain"] = bool(self._exam_profile_in_chain)
        return record

    def exam_profile_summary(self) -> dict[str, Any]:
        """Короткая сводка профиля для КАЖДОГО статуса.

        В статус идёт ровно то, по чему HUD рисует состояние и по чему проктор
        у машины видит, какие правила действуют. Полный разбор уходит один раз
        в `hello` и в цепочку: статус не должен возить словарь на килобайт
        дважды в секунду.
        """
        profile = self.exam_profile
        return {
            "active": bool(profile.present),
            "profile_hash_short": profile.short_hash,
            "profile_source": profile.source,
            "profile_signed": bool(profile.signed),
            "signature_state": profile.signature_state,
            "allow_search": bool(profile.allow_search_effective),
            "exam_url": profile.exam_url,
            "origins": len(profile.effective_origins),
            # Строка для шапки отчёта и для HUD — одна и та же, чтобы экран и
            # документ не рассказывали про сессию разное.
            "header_line": profile.header_line(),
            "in_chain": bool(self._exam_profile_in_chain),
            "off_profile": dict(self._off_profile_stats),
        }

    # ------------------------------------------------------- политика решений
    def policy_info(self) -> dict[str, Any]:
        """Как эта сессия обращается с блокировкой и с каналом.

        Уходит в `hello`, в `status`, в запись `control`/`policy` хеш-цепочки и
        в отчёт. Один и тот же словарь во всех четырёх местах: расхождение
        документа и поведения — это именно то, на чём ловят на защите.
        """
        return {
            "lock_policy": self.lock_policy,
            "lock_policy_label": LOCK_POLICY_LABELS.get(self.lock_policy, self.lock_policy),
            "auto_lock": bool(self.auto_lock),
            # Что автоматика делает САМА. С --auto-lock потолок снят, и честное
            # значение здесь — «блокировка»: иначе отчёт по такой сессии писал
            # бы «максимум без человека — приостановка» под записью о сессии,
            # которую автоматика как раз и закрыла без человека.
            "auto_action_cap": (VerdictAction.LOCK.value if self.auto_lock
                                else AUTO_ACTION_CAP.value),
            "thresholds": {
                "warn": float(self.cfg.risk_warn),
                "pause": float(self.cfg.risk_pause),
                "lock": float(self.cfg.risk_lock),
            },
            "auth_required": bool(self.auth_required),
            "single_client": bool(self.single_client),
            "raw_log": {
                "enabled": bool(self.raw_log.enabled),
                "window_sec": self.raw_log.window_sec,
                "max_records": self.raw_log.max_records,
            },
        }

    # ------------------------------------------------- каналы наблюдения
    #: Канал -> (поле разрешения в конфиге, флаг CLI, нужна ли камера,
    #: человекочитаемое имя модуля — ровно то, с которым он идёт в `degraded`).
    CAPABILITY_SPECS: tuple[tuple[str, str, str, bool, str], ...] = (
        ("vision", "enable_vision", "--no-yolo", True, "объекты"),
        ("gaze", "enable_gaze", "", True, "лицо/взгляд"),
        ("identity", "enable_identity", "--no-identity", True, "личность"),
        ("audio", "enable_audio", "--no-audio", False, "аудио"),
        ("env", "enable_env", "--no-env", False, "проверки окружения"),
    )

    def _resolve_caps_state(self) -> None:
        """Разложить «канал молчит» на причины. Вызывается после load_modules().

        Состояния: `active`, `disabled_by_flag`, `disabled_by_mode`,
        `disabled_by_config`, `unavailable`. Первые четыре — РЕШЕНИЕ человека
        или режима развёртывания, последнее — отсутствие модели или библиотеки
        на этой машине. Смешивать их нельзя: «выключили детектор» и «детектора
        нет» ведут к противоположным выводам на разборе.

        Порядок разбора — от самого внешнего решения к самому внутреннему:
        флаг запуска сильнее режима, режим сильнее конфига, и только когда
        канал разрешён всеми тремя, молчание означает недоступность.
        """
        cfg = self.cfg
        flags = dict(getattr(cfg, "launch_flags", {}) or {})
        headless = bool(getattr(cfg, "headless", False))
        out: dict[str, dict[str, Any]] = {}
        for key, cfg_field, flag, needs_camera, human in self.CAPABILITY_SPECS:
            active = bool(self.caps.get(key))
            allowed = bool(getattr(cfg, cfg_field, False))
            flag_attr = flag.lstrip("-").replace("-", "_") if flag else ""
            row: dict[str, Any] = {
                "channel": key,
                "module": human,
                "active": active,
                "disabled_by_flag": False,
                "unavailable": False,
                "state": "active",
                # `source` — короткий токен из словаря storage/report.py
                # (`_off_reason`): отчёт по нему раскладывает каналы на
                # «решение человека» и «на машине нет». Подробная причина
                # лежит рядом в `reason`, но она — фраза, и сопоставлять её
                # со словарём нельзя.
                "source": "",
                "reason": "канал активен",
            }
            if not active:
                if flag_attr and flags.get(flag_attr):
                    row.update(state="disabled_by_flag", disabled_by_flag=True,
                               source="flag", flag=flag,
                               reason=f"выключен флагом {flag}")
                elif headless and needs_camera:
                    row.update(state="disabled_by_flag", disabled_by_flag=True,
                               source="flag", flag="--headless",
                               reason="выключен флагом --headless: каналу нужна камера")
                elif key == "audio" and getattr(cfg, "is_classroom", False):
                    # Режим развёртывания — тоже решение человека, а не
                    # отсутствие модуля: аудио-анализ в аудитории выключен
                    # намеренно (Р-10). Поэтому `config`, а не `unavailable`.
                    row.update(state="disabled_by_mode", source="config",
                               reason=str(getattr(cfg, "audio_off_reason", "")
                                          or "выключен режимом развёртывания"))
                elif not allowed:
                    row.update(state="disabled_by_config", source="config",
                               reason=f"выключен в конфигурации ({cfg_field} = false)")
                else:
                    err = self._module_errors.get(human, "")
                    row.update(state="unavailable", unavailable=True,
                               source="unavailable",
                               reason=("модуль недоступен на этой машине: " + err) if err
                               else ("модуль разрешён, но не работает: нет модели "
                                     "или библиотеки (available() вернул False)"))
            out[key] = row
        self.caps_state = out

    def capabilities_info(self) -> dict[str, Any]:
        """Каналы наблюдения с разделённой причиной молчания.

        Уходит в `hello`, в деталь события SESSION_STARTED (то есть в цепочку) и
        в сводный блок `launch` статуса. Старое поле `capabilities` остаётся
        булевым словарём: на него смотрят оболочка и отчёт, и ломать его ради
        подробностей незачем — подробности лежат рядом в `channels`.
        """
        if not self.caps_state:
            self._resolve_caps_state()
        channels = {k: dict(v) for k, v in self.caps_state.items()}
        return {
            "capabilities": dict(self.caps),
            "channels": channels,
            "disabled_by_flag": sorted(k for k, v in channels.items()
                                       if v.get("disabled_by_flag")),
            "unavailable": sorted(k for k, v in channels.items() if v.get("unavailable")),
            "headless": bool(self.cfg.headless),
            "mock": bool(self.cfg.mock),
            "launch_flags": dict(getattr(self.cfg, "launch_flags", {}) or {}),
            "degraded": sorted(set(self.degraded)),
        }

    # --------------------------------------------------------- режим запуска
    def launch_verdict(self) -> dict[str, Any]:
        """Пригодна ли эта сессия как доказательство на настоящем экзамене.

        Один ответ на три находки разбора поверхности запуска: ослабляющие
        флаги оболочки (LAUNCH-01), выключенные флагами каналы ядра (LAUNCH-02)
        и настройка доказательной базы снаружи процесса (LAUNCH-04).

        Ничего не запрещает и ничего не блокирует: на машине студента это
        бесполезно — запускает он. Задача ровно одна: назвать причины вслух и
        положить их в цепочку, чтобы «чистый отчёт» нельзя было получить, просто
        выключив наблюдение флагом.

        Переменные окружения в `blocking_reasons` НЕ попадают: указать каталог
        проктора через `PROCTOR_SESSIONS_DIR` — штатный способ развёртывания
        (README), а не подлог. Они идут в `notes`: улика есть, обвинения нет.
        """
        blocking: list[str] = []
        notes: list[str] = []
        if self.cfg.mock:
            blocking.append("ядро запущено с --mock: события берутся из сценария, "
                            "а не из наблюдения")
        if self.cfg.headless:
            blocking.append("ядро запущено с --headless: камеры нет")
        for key in sorted(k for k, v in self.caps_state.items() if v.get("disabled_by_flag")):
            blocking.append(f"канал «{key}» {self.caps_state[key]['reason']}")
        if not self.shell_config:
            blocking.append("оболочка не сообщила режим запуска (SHELL_CONFIG): "
                            "состояние защиты машины неизвестно")
        else:
            # По ВСЕМ записям сессии, а не по последней: иначе чистая запись
            # после честного признания в ослаблении снимала бы причину, то
            # есть переобъявление работало бы как отзыв признания.
            # Причины из SHELL_CONFIG уже сформулированы фразой целиком
            # («--no-kiosk: окно не удерживается…»), поэтому здесь только
            # помечаем источник, а не достраиваем предложение.
            seen: set[str] = set()
            for record in (self.shell_config_history or [self.shell_config]):
                for item in (record.get("weakened_by") or []):
                    if item in seen:
                        continue
                    seen.add(item)
                    blocking.append(f"оболочка: {item}")
            # Третье состояние: ослабления оболочка не признала, но и ни одной
            # защиты не подтвердила. Невиновность по умолчанию здесь не годится
            # — пустой `detail` давал верхний уровень вердикта.
            if not seen and all(not (r.get("confirmed") or [])
                                for r in (self.shell_config_history
                                          or [self.shell_config])):
                missing = "; ".join(self.shell_config.get("unconfirmed") or [])
                blocking.append(
                    "оболочка не подтвердила ни одной защиты машины"
                    + (f" (не сообщила: {missing})" if missing else "")
                    + ": по записи SHELL_CONFIG проверить состояние защиты нельзя")
            if len(self.shell_config_history) > 1:
                notes.append(
                    f"оболочка переобъявляла режим запуска ({len(self.shell_config_history)} "
                    "записи за сессию): в шапке учтена худшая, все остаются в цепочке")
        # Профиль экзамена идёт в `notes`, а НЕ в `blocking_reasons`, и это
        # сознательно. Отсутствие профиля — штатный способ работы (локальный
        # мок-тест, прежнее поведение), а не подлог; блокировать запуск из-за
        # него значило бы запретить демо без LMS. Но и молчать нельзя: «правила
        # не задавались» обязано быть видно в шапке, иначе получится ровно то
        # опциональное требование, на котором сломался SEB (У-13).
        if not self.exam_profile.present:
            notes.append("правила экзамена проктором не задавались: белого списка "
                         "источников нет, оболочка открывает локальный мок-тест")
        else:
            notes.append(f"действовал профиль экзамена {self.exam_profile.short_hash} "
                         f"({self.exam_profile.source}); профиль — улика о заявленных "
                         "правилах, обойти его на машине студента можно")
            # НЕУТВЕРЖДЁННЫЙ ЧЕРНОВИК — ЕДИНСТВЕННЫЙ случай, когда профиль
            # попадает в `blocking_reasons`, а не в `notes`. Отсутствие профиля
            # блокирующим не является (это штатный способ работы, демо без
            # LMS), а вот профиль, который НИКТО НЕ ЧИТАЛ, — является.
            #
            # Ревью показало, зачем: инструмент разведки собирает черновик с
            # заглушками `<ФИО и должность проктора>` в полях проктора, и этот
            # файл загружался как обычный профиль. Достаточно было подставить
            # `--exam-profile exam-profile.draft.json`, и экзамен ехал на
            # машинном белом списке, в котором лежали неопознанные источники.
            # Уставший админ ничего не копирует — он просто подставляет файл.
            # Поэтому барьер стоит здесь, в вердикте готовности, и снимается
            # он одним осознанным действием: проктор вписывает своё имя, дату
            # и убирает метку-страж из notes.
            if not self.exam_profile.approved_by_human:
                why = []
                if self.exam_profile.draft_marker:
                    why.append("в notes стоит метка машинного черновика "
                               "инструмента разведки")
                if self.exam_profile.unfilled_fields:
                    why.append("поля проктора остались заглушкой или пусты: "
                               + ", ".join(self.exam_profile.unfilled_fields))
                blocking.append(
                    "профиль экзамена НЕ УТВЕРЖДЁН ЧЕЛОВЕКОМ ("
                    + "; ".join(why)
                    + "). Белый список собран программой по фактическим запросам "
                      "страницы и содержит только то, что проходили руками. "
                      "Применять его как объявленные правила экзамена нельзя: "
                      "список обязан прочитать проктор, вычеркнуть лишнее, "
                      "вписать своё имя и дату выдачи")
            # Заметки проктора печатаются в разбор. Раньше `profile.notes` не
            # выводились нигде — ни на экран оболочки, ни в заметки отчёта, —
            # и всё, что проктор написал про правила своего экзамена, жило
            # только в JSON цепочки.
            if self.exam_profile.notes:
                notes.append("заметки проктора в профиле: "
                             + self.exam_profile.notes[:400])
            if self.exam_profile.allow_search_effective:
                notes.append("профиль разрешает поисковики: готовый ответ доступен "
                             "студенту прямо в выдаче, переходить никуда не нужно")
            for item in self.exam_profile.warnings:
                notes.append(f"профиль: {item}")
        for name in (self.launch_env.get("vars_set") or []):
            notes.append(f"задана переменная окружения {name}: доказательная база "
                         "настроена снаружи процесса")
        for key in sorted(k for k, v in self.caps_state.items() if v.get("unavailable")):
            notes.append(f"канал «{key}» {self.caps_state[key]['reason']}")
        return {
            "exam_ready": not blocking,
            "blocking_reasons": blocking,
            "notes": notes,
        }

    def _capabilities_wire(self) -> dict[str, Any]:
        """Поля о каналах для `hello` и для детали SESSION_STARTED.

        Плоские, с одинаковыми именами в обоих сообщениях: один и тот же
        `capabilities_detail` в hello и в журнале — это то, по чему отчёт
        сверяет заявленное с записанным.
        """
        info = self.capabilities_info()
        return {
            "capabilities_detail": info["channels"],
            "disabled_by_flag": info["disabled_by_flag"],
            "unavailable": info["unavailable"],
            "launch_flags": info["launch_flags"],
        }

    def review_info(self) -> dict[str, Any]:
        """Открытый запрос решения проктора — или пустой словарь."""
        if not self._lock_review:
            return {"pending": False}
        return {"pending": True, **dict(self._lock_review)}

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
                self._module_errors["проверки окружения"] = (
                    "модуль env_checks не импортировался или в нём нет run_all_checks")

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

        # Последним шагом: разложить «канал молчит» на «выключили» и «нет».
        # Именно здесь, а не в __init__: до загрузки модулей причина неизвестна.
        self._resolve_caps_state()
        for row in self.caps_state.values():
            if row["disabled_by_flag"]:
                log.warning("канал %s: %s — в отчёте это будет помечено как "
                            "решение запускающего, а не как отсутствие модуля",
                            row["channel"], row["reason"])
            elif row["unavailable"]:
                log.warning("канал %s: %s", row["channel"], row["reason"])

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
            self._module_errors[human] = f"модуль {path} не импортировался или в нём нет {attr}"
            return None
        try:
            try:
                obj = klass(cfg)
            except TypeError:  # конструктор без аргументов — тоже допустим
                obj = klass()
        except Exception as exc:
            log.warning("не удалось создать %s.%s (%s): %s", path, attr, human, exc)
            self.degraded.append(human)
            self._module_errors[human] = f"{path}.{attr} не создался: {exc}"[:400]
            return None
        if hasattr(obj, "available"):
            try:
                if not obj.available():
                    log.warning("%s: модуль есть, но недоступен (нет модели/библиотеки)", human)
                    self.degraded.append(f"{human} (недоступен)")
                    self._module_errors[human] = ("модуль загружен, но available() вернул "
                                                  "False: нет модели или библиотеки")
            except Exception as exc:
                log.warning("%s: available() упал: %s", human, exc)
                self._module_errors[human] = f"available() упал: {exc}"[:400]
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

    def prepare_auth(self) -> None:
        """Подготовить токен канала и сказать человеку, где его взять.

        Зовётся из `main()` ДО старта сервера: токен должен быть напечатан
        раньше, чем кто-либо сможет подключиться.
        """
        if not self.auth_required:
            log.warning("канал 127.0.0.1:%s открыт БЕЗ аутентификации (--no-auth): "
                        "любой локальный процесс может представиться оболочкой, "
                        "обнулить риск и читать поток событий. Режим допустим "
                        "только для стенда разработки.", self.cfg.ws_port)
            self.cfg_dict["decision_policy"] = self.policy_info()
            return

        env_token = str(os.environ.get(AUTH_TOKEN_ENV_VAR, "") or "").strip()
        if not self.auth_token and env_token:
            self.auth_token = env_token
            log.info("токен канала взят из %s", AUTH_TOKEN_ENV_VAR)
        if not self.auth_token:
            self.auth_token = _new_token()

        raw_file = str(getattr(self.cfg, "auth_token_file", "") or "")
        path = Path(raw_file) if raw_file else _default_token_file(self.cfg.ws_port)
        if _write_token_file(path, self.auth_token, self.cfg.ws_port, self.cfg.ws_host):
            self.token_file = path

        log.info("токен канала: %s", self.auth_token)
        log.info("оболочка предъявляет его заголовком %s при рукопожатии "
                 "(или ?token=... в адресе)", AUTH_HEADER)
        if self.token_file is not None:
            log.info("токен также лежит здесь (права 0600): %s", self.token_file)
        else:
            log.warning("файл токена не создан: передайте токен оболочке через %s",
                        AUTH_TOKEN_ENV_VAR)
        self.cfg_dict["decision_policy"] = self.policy_info()

    def _check_auth(self, path: str, headers: Any) -> str:
        """Пусто — токен верный. Иначе причина отказа по-русски."""
        if not self.auth_required:
            return ""
        presented = _token_from_request(path, headers)
        if not presented:
            return "токен не предъявлен"
        # Сравнение постоянного времени: иначе по длительности ответа токен
        # подбирается побайтно, а канал локальный и запросов можно сделать
        # сколько угодно.
        #
        # Сравниваются БАЙТЫ, а не строки: `compare_digest` на str требует
        # только-ASCII и падает TypeError на чём угодно другом. Предъявленное
        # значение приходит извне, поэтому на токене вида «подделка» отказ
        # превращался в 500 вместо 403 — то есть непрошеный клиент узнавал,
        # что попал в необработанную ветку, а в логе не оставалось причины.
        if not hmac.compare_digest(presented.encode("utf-8", "surrogatepass"),
                                   self.auth_token.encode("utf-8", "surrogatepass")):
            return "токен не совпадает"
        return ""

    def _process_request(self, *args: Any) -> Any:
        """Отказать до апгрейда до WebSocket.

        Вызывается библиотекой на каждое рукопожатие. Поддерживаются обе
        сигнатуры: `(connection, request)` у websockets >= 12 и
        `(path, headers)` у прежних версий.
        """
        connection: Any = None
        path, headers = "", None
        if len(args) >= 2 and hasattr(args[0], "respond"):
            connection, request = args[0], args[1]
            path = str(getattr(request, "path", "") or "")
            headers = getattr(request, "headers", None)
        elif len(args) >= 2:
            path, headers = str(args[0] or ""), args[1]

        # Проверка fail-closed: любая неожиданная ошибка разбора рукопожатия
        # означает ОТКАЗ, а не пропуск. Это единственная точка, где решается,
        # кто получит поток событий, и падать в ней «наружу» нельзя: исключение
        # отдало бы клиенту 500 вместо отказа, а в логе не осталось бы причины.
        try:
            reason = self._check_auth(path, headers)
        except Exception as exc:
            log.warning("рукопожатие не разобрано (%s) — отказываю", exc)
            reason = "рукопожатие не разобрано"
        if not reason and self.single_client and self.clients:
            # Ровно один активный клиент. Второе соединение — это либо вторая
            # оболочка (и тогда непонятно, которая из них ведёт экзамен), либо
            # сторонний процесс, читающий поток. Оба случая отказываем.
            reason = "канал уже занят другим клиентом"

        if not reason:
            return None

        self._rejected_clients += 1
        log.warning("соединение отклонено: %s (отклонено всего: %d)",
                    reason, self._rejected_clients)
        text = f"403 {reason}\n"
        if connection is not None:
            return connection.respond(http.HTTPStatus.FORBIDDEN, text)
        return (http.HTTPStatus.FORBIDDEN, [("Content-Type", "text/plain; charset=utf-8")],
                text.encode("utf-8"))

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
            # Не ниже, чем нужно снимку окна экзамена предельного размера в
            # base64: сообщение сверх max_size рвёт соединение (1009), а не
            # отклоняется, — см. комментарий к ws_max_message в config.py.
            # Прочие типы больше ws_max_message отсекает `_on_message`.
            max_size=max(int(self.cfg.ws_max_message or 0), WS_TRANSPORT_MAX_BYTES),
            process_request=self._process_request,
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
            # Тот же набор каналов, но с разделением «выключено флагом» и
            # «недоступно». Оболочка обязана показать студенту не только то,
            # что канал молчит, но и почему: предполётная проверка иначе
            # советует «проверьте камеру» машине, на которой камеру выключили
            # флагом запуска (LAUNCH-02).
            # `capabilities_detail` — карта «канал -> состояние и причина».
            # Именно картой, а не вложенным блоком: по этому ключу её читают и
            # оболочка, и storage/report.py (`_off_reason`), и обёртка вокруг
            # заставила бы оба места распаковывать лишний уровень.
            **self._capabilities_wire(),
            version=VERSION,
            protocol=PROTOCOL_VERSION,
            degraded=sorted(set(self.degraded)),
            headless=self.cfg.headless,
            mock=self.cfg.mock,
            # Переменные окружения и фактический интерпретатор ядра (LAUNCH-04).
            launch=dict(self.launch_env),
            # Оболочка должна знать о передаче доказательств с первого сообщения:
            # финальный экран показывает код сверки, а деградированную передачу
            # нужно показать студенту и проктору, а не спрятать в лог сайдкара.
            handover=dict(self.handover),
            # Папка проктора: задана ли и принимает ли запись (стартовая проба).
            # Предполётный экран показывает это ДО экзамена: «пакет будет
            # скопирован в …» или «пакет останется на этом компьютере».
            delivery=dict(self.delivery),
            # Профиль экзамена — при рукопожатии, потому что оболочке он нужен
            # ДО первой загрузки страницы: по `exam_url` она решает, открывать
            # внешний LMS или прежний локальный мок-тест, а по
            # `allowed_origins` ставит фильтр. `active: false` — профиля нет,
            # поведение прежнее. Хеш здесь тот же, что уходит в цепочку: HUD
            # показывает его студенту, и расхождения экрана с отчётом быть не
            # должно. Фильтр оболочки — НЕ защита: ядро всё равно
            # перепроверяет каждое сообщение о нарушении у себя
            # (`_cmd_off_profile`), а ценность записи — в улике, не в запрете.
            exam_profile=self.exam_profile.wire(),
            # Политика решений — с первого сообщения: оболочка обязана знать,
            # что порог блокировки НЕ закрывает экзамен сам, и нарисовать экран
            # ожидания решения, а не экран «сессия закрыта».
            policy=self.policy_info(),
            commands=list(COMMAND_NAMES),
        )

    def _status_message(self) -> dict[str, Any]:
        with self._status_lock:
            snap = dict(self._snap)
            snap["gaze"] = dict(self._snap["gaze"])
        if self.capture is not None:
            snap["fps"] = self.capture.fps
        elif self.cfg.mock:
            snap["fps"] = float(self.cfg.target_fps)

        # Состояние камеры отдаём ЯВНО. Раньше в статусе был только fps, и при
        # fps == 0 ни интерфейс, ни человек не могли узнать причину: нет opencv,
        # не выдано разрешение, занят чужой процесс или указан несуществующий
        # индекс — всё выглядело одинаково как «кадров нет». Это тот же дефект,
        # что был у BLACKLISTED_PROCESS: система знает причину и молчит о ней.
        if self.capture is not None:
            snap["camera"] = {
                "index": self.capture.index,
                "available": self.capture.available(),   # метод: есть ли opencv
                "opened": self.capture.opened,           # свойство: открыто ли устройство
                "frames_total": self.capture.frames_total,
                "error": self.capture.last_error,        # свойство: причина по-русски
            }
        elif self.cfg.mock:
            snap["camera"] = {
                "index": -1, "available": False, "opened": False,
                "frames_total": 0, "error": "режим имитации: камера не используется",
            }
        else:
            snap["camera"] = {
                "index": self.cfg.camera_index, "available": False, "opened": False,
                "frames_total": 0, "error": "захват кадров не запущен (режим без камеры)",
            }

        snap["audio_ok"] = bool(self._audio_ok)
        snap["risk"] = self._risk_score()
        snap["state"] = self.session.state.value
        # Код сверки и состояние передачи — в каждом статусе: финальный экран
        # оболочки показывает код, не запрашивая его отдельной командой.
        snap["session_code"] = self.session.session_code
        snap["session_code_display"] = self.session.code_display
        snap["degraded_handover"] = self.session.degraded_handover
        snap["handover"] = dict(self.handover)
        # Папка проктора: стартовая проба + идёт ли копирование прямо сейчас.
        # Итог копирования — в `package.delivery`, когда оно закончилось.
        snap["delivery"] = {**self.delivery, "in_progress": bool(self._delivering)}
        # Политика и открытый запрос решения идут в КАЖДОМ статусе: экран
        # ожидания проктора не должен зависеть от того, поймала ли оболочка
        # одно конкретное сообщение verdict.
        snap["policy"] = self.policy_info()
        snap["review"] = self.review_info()
        # Профиль — в каждом статусе: HUD показывает действующие правила и хеш
        # всё время, а не только в момент рукопожатия.
        snap["exam_profile"] = self.exam_profile_summary()
        snap["raw_log"] = {
            "enabled": bool(self.raw_log.enabled),
            "written": self.raw_log.written,
            "dropped": self.raw_log.dropped,
        }
        # Сводка режима запуска — в каждом статусе, но КОРОТКАЯ: полный разбор
        # каналов и переменных уходит один раз в `hello` и в цепочку, а здесь
        # нужно только то, по чему HUD и проктор видят, пригодна ли сессия.
        # `shell_reported = false` значит, что оболочка о своём режиме молчит, —
        # отдельный факт, а не «всё в порядке».
        snap["launch"] = {
            "python_executable": str(self.launch_env.get("python_executable") or ""),
            "env_vars_set": list(self.launch_env.get("vars_set") or []),
            "capabilities_disabled_by_flag": sorted(
                k for k, v in self.caps_state.items() if v.get("disabled_by_flag")),
            "capabilities_unavailable": sorted(
                k for k, v in self.caps_state.items() if v.get("unavailable")),
            "shell_reported": bool(self.shell_config),
            # По ВСЕЙ истории записей, а не по последней: чистая запись после
            # признания в ослаблении признание не отменяет (см.
            # `shell_config_history`).
            "shell_weakened": any(bool(r.get("weakened"))
                                  for r in (self.shell_config_history
                                            or [self.shell_config])),
            "shell_weakened_by": [
                item for r in (self.shell_config_history or [self.shell_config])
                for item in (r.get("weakened_by") or [])],
            # Трёхзначное состояние защиты: weakened / unconfirmed / confirmed.
            # Двух значений не хватало — молчание оболочки читалось как
            # «защита применена полностью».
            "shell_protection": str(self.shell_config.get("protection") or ""),
            "shell_unconfirmed": list(self.shell_config.get("unconfirmed") or []),
            "shell_config_in_chain": bool(self._shell_config_in_chain),
            "exam_ready": bool(self.launch_verdict().get("exam_ready")),
        }
        if self.last_package:
            snap["package"] = dict(self.last_package)
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
        # Транспортный потолок поднят до ~4 МиБ ради одного типа — снимка окна
        # экзамена. Всё остальное больше `ws_max_message` (1 МиБ) не
        # обрабатывается: оболочке такие сообщения не нужны, а журнал и
        # движок не должны принимать по 4 МиБ на сообщение от клиента.
        limit = int(self.cfg.ws_max_message or 0) or (1 << 20)
        if mtype != MsgType.SCREEN_EVIDENCE.value:
            size = _raw_size(raw, limit)
            if size > limit:
                log.warning("сообщение %r отклонено: %d байт больше предела %d",
                            mtype[:40], size, limit)
                await self._send(ws, envelope(
                    MsgType.ERROR, code="too_large",
                    message=f"Сообщение {mtype[:40]} больше {limit} байт — не обработано",
                    fatal=False))
                return
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
            elif mtype == MsgType.SCREEN_EVIDENCE.value:
                await self._cmd_screen_evidence(msg)
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
        # Завершение может ещё идти и после `session.end()` (отчёт, пакет):
        # новая сессия поверх него получила бы чужой `last_package`.
        if self.session.active or self._ending is not None:
            await self._cmd_session_end({"reason": "перезапуск сессии"})
        student_id = str(msg.get("student_id") or "anon")
        exam_id = str(msg.get("exam_id") or "")
        student_name = str(msg.get("student_name") or "")
        session_dir = self.session.start(student_id, exam_id, student_name)
        log.info("сессия начата: %s (%s)", self.session.session_id, student_name or student_id)

        self._reset_calibration()
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
        self.last_package = {}
        self._package_dir = ""
        if self.store is not None:
            try:
                await asyncio.to_thread(self.store.open_session, self.session.meta())
                self._store_open = True
            except Exception as exc:
                log.warning("хранилище не открылось: %s", exc)
                await self._error("store_open", f"Хранилище доказательств недоступно: {exc}")

        # Код сверки выводится из genesis-хеша цепочки, поэтому появляется
        # только после открытия хранилища. Он печатается в лог сразу: проктор
        # записывает код напротив фамилии в своём списке и потом сверяет, что
        # пакетов столько же, сколько студентов, и коды совпадают.
        code = self.session.set_genesis(self._store_genesis())
        if code:
            log.info("код сверки сессии: %s (сессия %s, студент %s)",
                     self.session.code_display, self.session.session_id,
                     student_name or student_id)
        else:
            log.warning("код сверки не сформирован: genesis-хеш цепочки недоступен "
                        "(хранилище в деградированном режиме) — сверяйте комплекты "
                        "по идентификатору сессии")
        if self.session.degraded_handover:
            log.warning("передача доказательств деградирована: %s",
                        self.handover.get("reason") or "каталог проктора недоступен")

        # Политика решений уходит в цепочку ДО первого события сессии. Иначе
        # отчёт не мог бы доказать, в каком режиме шёл экзамен: конфиг лежит
        # рядом и правится, а эта запись связана хешем с genesis.
        self._lock_review = None
        self.raw_log.reset()
        self._raw_truncation_noted = False
        await self._store_control("policy", self.policy_info())
        # Правила ОЦЕНКИ — туда же и по той же причине. Политика блокировки в
        # цепочку писалась, а веса наблюдений нет: профиль читался из конфига
        # в момент сборки отчёта, то есть ПОСЛЕ экзамена. Из одной целой
        # цепочки так собирались два по-разному подписанных отчёта с разными
        # integrity, и ни один верификатор не мог сказать, какие правила вуз
        # объявил заранее. Запись идёт до первого события сессии.
        await self._store_control("weight_profile", self._profile_record())
        # Режим запуска — туда же и по той же причине. Переменные окружения
        # задаются снаружи процесса (PROCTOR_PYTHON подменяет интерпретатор
        # сайдкара, PROCTOR_SIGNING_KEY — ключ отчёта, PROCTOR_SESSIONS_DIR —
        # место, куда уедут доказательства), и до этой записи ни одна из трёх
        # нигде не фиксировалась: подменённая сессия выглядела как обычная.
        await self._store_control("launch_env", self.launch_env)
        # ПРОФИЛЬ ЭКЗАМЕНА — ТУДА ЖЕ И БЕЗ ВОЗМОЖНОСТИ ОТКЛЮЧИТЬ.
        #
        # Это прямое следствие разбора провала Safe Exam Browser (У-13). Там
        # подпись конфигурации существовала, но Config Key, который LMS должна
        # сверять, включается в Moodle ОТДЕЛЬНО и по умолчанию выключен — вузы
        # его не включают. Проверка была, она была опциональной, и её не
        # включили. Поэтому здесь флага «не писать хеш профиля» НЕТ: запись
        # делается всегда, до первого события сессии, и её отсутствие
        # невозможно объяснить настройкой.
        #
        # Отсутствие профиля пишется ТЕМ ЖЕ способом: «правила экзамена не
        # задавались» — такой же факт для разбора, как и действовавший хеш, и
        # отличать его от «мы забыли записать» обязательно.
        self._off_profile_last.clear()
        self._off_profile_stats = self._new_off_profile_stats()
        self._exam_profile_in_chain = False
        profile_digest_in_chain = await self._store_control(
            "exam_profile", self.exam_profile_record(chain_state=False))
        self._exam_profile_in_chain = bool(profile_digest_in_chain)
        if not self._exam_profile_in_chain:
            # Записать не удалось — молчать об этом нельзя: отчёт по такой
            # сессии обязан сказать, что правила экзамена ничем не зафиксированы.
            log.error("хеш профиля экзамена НЕ попал в хеш-цепочку: правила этой "
                      "сессии ничем не зафиксированы (хранилище недоступно)")
        await self._emit_exam_profile_state(profile_digest_in_chain)
        self._shell_config_in_chain = False
        if self.shell_config:
            # SHELL_CONFIG пришёл раньше session_start (или остался от прошлой
            # сессии и будет перезаписан следующим сообщением оболочки) —
            # цепочка новой сессии всё равно обязана его содержать.
            digest = await self._store_control("shell_config", self.shell_config)
            self._shell_config_in_chain = bool(digest)

        await self._emit(self._system_event(EventKind.SESSION_STARTED, {
            "student_id": student_id, "exam_id": exam_id,
            "student_name": student_name, "session_dir": str(session_dir),
            "capabilities": dict(self.caps),
            # Рядом с булевым словарём — разбор причин: «выключен флагом» и
            # «недоступен» это разные факты, и в журнале они теперь различимы.
            # Поля те же, что в `hello`: отчёт сверяет одно с другим.
            **self._capabilities_wire(),
            # Код и признак деградации попадают в журнал первой же записью:
            # оболочка показывает их на экране, а отчёт — в шапке.
            "session_code": code,
            "session_code_display": self.session.code_display,
            "degraded_handover": self.session.degraded_handover,
            # Метаданные профиля — в первой же записи журнала.
            #
            # Почему именно здесь, а не в `Session.meta()`: состав `meta()`
            # участвует в вычислении genesis, а из genesis выводится код
            # сверки сессии (см. докстринг `session.py`). Добавление поля туда
            # сменило бы коды ВСЕМ будущим сессиям, то есть сломало бы уже
            # выданные комплекты. Деталь SESSION_STARTED защищена цепочкой
            # ровно так же, как и `meta`, и к ней эта оговорка не относится.
            "profile_hash": self.exam_profile.profile_hash,
            "profile_signed": bool(self.exam_profile.signed),
            "profile_source": self.exam_profile.source,
            "lock_policy": self.lock_policy,
            "auto_lock": bool(self.auto_lock),
            "auth_required": bool(self.auth_required),
        }))
        await self._broadcast(self._status_message())

    async def _cmd_reset_risk(self, msg: dict[str, Any]) -> None:
        """Сброс накопленного риска. Оставляет неизгладимую запись в цепочке.

        Раньше команда просто обнуляла счётчик, и в журнале не оставалось
        ничего. То есть самое сильное вмешательство в доказательную базу —
        «считаем, что этого не было» — было единственным, что база не
        фиксировала. Отчёт показывал чистую сессию, и проверить, сбрасывали ли
        риск и сколько раз, было нельзя ни по чему.
        """
        actor = str(msg.get("actor") or "").strip() or "неизвестный клиент канала"
        note = str(msg.get("note") or msg.get("reason") or "").strip()
        score_before = self._risk_score()
        breakdown = self._risk_breakdown()
        record = {
            "actor": actor[:120],
            "note": note[:400],
            "ts": time.time(),
            "score_before": score_before,
            "action_before": self._last_action.value,
            "session_state": self.session.state.value,
            "top_before": [str(row.get("label") or row.get("kind") or "")
                           for row in breakdown[:5]],
            "events_total": int(self.session.events_total),
        }
        digest = await self._store_control("risk_reset", record)

        # _last_action намеренно не трогаем: пусть _push_risk увидит смену
        # уровня и сам снимет pause (locked остаётся — это решение проктора)
        self._reset_engines()
        await self._emit(self._system_event(EventKind.RISK_RESET, {
            **record, "chain_hash": digest,
        }))
        await self._push_risk(force=True)
        log.warning("risk-score сброшен: было %.1f, инициатор «%s»%s",
                    score_before, actor, f", запись {digest[:12]}…" if digest else
                    " (в цепочку записать не удалось)")

    async def _cmd_proctor_decision(self, name: str, msg: dict[str, Any]) -> None:
        """Решение человека над приостановленным экзаменом.

        `proctor_lock` — подтвердить блокировку, `proctor_release` — продолжить
        экзамен. Только эти две команды переводят сессию в LOCKED по риску:
        сама автоматика до блокировки не доходит (см. `cap_auto_action`).
        """
        decision = (PROCTOR_DECISION_LOCK if name == "proctor_lock"
                    else PROCTOR_DECISION_RELEASE)
        actor = str(msg.get("actor") or "").strip()
        if not actor:
            # Решение без указания, кто его принял, бессмысленно: вся правка
            # политики затевалась ради того, чтобы в журнале стоял человек.
            await self._error("actor_required",
                              "Решение проктора требует поля actor: кто именно решил. "
                              "Без него запись в журнале не имеет смысла.")
            return
        if not self.session.active:
            await self._error("no_session", "Решение проктора: активной сессии нет")
            return

        reason = str(msg.get("reason") or msg.get("note") or "").strip()
        record = {
            "decision": decision,
            "actor": actor[:120],
            "reason": reason[:400],
            "ts": time.time(),
            "score": self._risk_score(),
            "session_state": self.session.state.value,
            "lock_policy": self.lock_policy,
            "review": self.review_info(),
        }
        digest = await self._store_control("proctor_decision", record)
        self._lock_review = None

        if decision == PROCTOR_DECISION_LOCK:
            with contextlib.suppress(SessionStateError):
                self.session.transition(SessionState.LOCKED,
                                        f"блокировку подтвердил {actor}")
            self._last_action = VerdictAction.LOCK
            text = f"Экзамен закрыт решением проктора ({actor})"
        else:
            with contextlib.suppress(SessionStateError):
                self.session.transition(SessionState.RUNNING,
                                        f"приостановку снял {actor}")
            # Риск обнуляется вместе со снятием: иначе экзамен продолжится и
            # через секунду снова упрётся в тот же порог, и человека позовут
            # опять по тем же самым наблюдениям. Сброс при этом тоже попадает
            # в цепочку — отдельной записью, как любой другой.
            await self._cmd_reset_risk({
                "actor": actor,
                "note": f"снятие приостановки решением проктора: {reason}" if reason
                        else "снятие приостановки решением проктора",
            })
            self._last_action = VerdictAction.NONE
            text = f"Экзамен продолжается, приостановку снял {actor}"

        await self._emit(self._system_event(EventKind.PROCTOR_DECISION, {
            **record, "chain_hash": digest,
        }))
        await self._broadcast(envelope(
            MsgType.VERDICT,
            action=(VerdictAction.LOCK.value if decision == PROCTOR_DECISION_LOCK
                    else VerdictAction.NONE.value),
            reason=text, score=self._risk_score(),
            requested_action=VerdictAction.LOCK.value,
            review_required=False,
            decided_by=actor[:120],
        ))
        await self._broadcast(self._status_message())
        log.warning("решение проктора: %s — %s%s", decision, text,
                    f" (запись {digest[:12]}…)" if digest else "")

    def _store_genesis(self) -> str:
        """genesis-хеш открытой цепочки. Пусто — хранилище его не даёт."""
        if self.store is None:
            return ""
        try:
            return str(getattr(self.store, "genesis", "") or "")
        except Exception:
            return ""

    async def _cmd_session_end(self, msg: dict[str, Any]) -> None:
        """Завершить сессию ровно один раз.

        Между проверкой `session.active` и `session.end()` десяток await: пока
        первое завершение пишет итоговые записи и собирает отчёт, сюда входили
        останов по сигналу, повторный `session_end` или `session_start` поверх.
        Второй проход собирал второй пакет и второй раз копировал его в папку
        проктора (`-2`), а останов, не дождавшись первого, гасил модули и
        цикл под незаконченной сборкой. Теперь второй вызов ждёт первый.
        """
        ending = self._ending
        if ending is not None:
            await asyncio.shield(ending)
            return
        if not self.session.active:
            return
        self._ending = asyncio.get_running_loop().create_future()
        try:
            await self._end_session(msg)
        finally:
            ending, self._ending = self._ending, None
            if ending is not None and not ending.done():
                ending.set_result(None)

    async def _end_session(self, msg: dict[str, Any]) -> None:
        if not self.session.active:
            return
        reason = str(msg.get("reason") or "завершение по команде оболочки")
        # Сырой слой закрывается ДО записи session_close: открытые окна должны
        # лечь в цепочку, пока она ещё принимает записи. После закрытия
        # дописать в неё что-либо означало бы правку журнала задним числом.
        with contextlib.suppress(Exception):
            await self._store_observations(self.raw_log.flush())
        # Итог политики — отдельной записью. Если экзамен кончился, так и не
        # дождавшись решения проктора, это ОБЯЗАНО быть видно в отчёте: иначе
        # приостановка без решения выглядела бы как нормально сданный экзамен.
        with contextlib.suppress(Exception):
            await self._store_control("policy_final", {
                **self.policy_info(),
                "review_pending_at_end": bool(self._lock_review),
                "review": self.review_info(),
                "final_state": self.session.state.value,
                "raw_written": self.raw_log.written,
                "raw_dropped": self.raw_log.dropped,
                "rejected_clients": int(self._rejected_clients),
                "end_reason": reason,
            })
        # Итог по правилам экзамена — такой же обязательной записью, как и
        # запись при старте. Главное в ней — счётчики: «попыток выхода за
        # белый список не было» это результат разбора, и отличать его от «мы
        # не смотрели» обязательно. Поле `in_chain` доказывает, что хеш
        # профиля в этой сессии действительно записан, а не потерян.
        with contextlib.suppress(Exception):
            await self._store_control("exam_profile_final", {
                **self.exam_profile_record(),
                "end_reason": reason,
            })
        # Итог режима запуска — последней записью наравне с политикой. Главное в
        # ней — случай `reported: false`: оболочка, которая о своём режиме
        # промолчала, не должна быть неотличима от оболочки, запущенной
        # правильно. Без этой записи «SHELL_CONFIG не прислали» выглядело бы
        # в точности как «прислали и всё в порядке».
        with contextlib.suppress(Exception):
            verdict = self.launch_verdict()
            await self._store_control("launch_final", {
                "shell_config": dict(self.shell_config) if self.shell_config
                else {"reported": False,
                      "reason": "оболочка не прислала SHELL_CONFIG: фактическое "
                                "состояние защиты машины за эту сессию неизвестно"},
                "shell_config_in_chain": bool(self._shell_config_in_chain),
                "capabilities": self.capabilities_info(),
                "launch_env": dict(self.launch_env),
                "exam_ready": bool(verdict["exam_ready"]),
                "blocking_reasons": list(verdict["blocking_reasons"]),
                "notes": list(verdict["notes"]),
                "end_reason": reason,
            })
            if not verdict["exam_ready"]:
                log.warning("сессия завершена в режиме, непригодном для настоящего "
                            "экзамена — причины записаны в цепочку:")
                for item in verdict["blocking_reasons"]:
                    log.warning("  - %s", item)
        # Режим запуска не наследуется следующей сессией: оболочка присылает его
        # на каждый session_start. Если новая сессия его не пришлёт, это обязано
        # дать `reported: false`, а не молча унаследовать прошлую запись.
        self.shell_config = {}
        self._shell_config_in_chain = False
        if self._lock_review:
            log.warning("сессия завершена, НЕ дождавшись решения проктора: "
                        "в отчёте это отмечено отдельно (код сессии %s)",
                        self.session.code_display or "—")
        # Код сверки едет в последней записи журнала и в последнем событии:
        # финальный экран оболочки показывает его студенту и проктору, не
        # запрашивая ничего отдельно, а проктор сверяет код с пакетом.
        await self._emit(self._system_event(EventKind.SESSION_ENDED, {
            "reason": reason,
            "session_code": self.session.session_code,
            "session_code_display": self.session.code_display,
            "degraded_handover": self.session.degraded_handover,
            "package_path": str(self.session.package_path or ""),
        }))
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
        self._lock_review = None
        await self._build_report(summary)
        # Пакет собирается ПОСЛЕ отчёта и после закрытия цепочки: в него должны
        # попасть и готовый report.html с подписью, и база с финальной записью
        # session_close (её же checkpoint сливает WAL в основной файл).
        await self._build_package(summary, session_dir=str(summary.get("session_dir") or ""))
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

        # Сетку обходит оболочка по своему таймеру: `gaze_grid` с точкой —
        # «сейчас показана эта точка», с point=null — «обход закончен, строй
        # карту по тому, что собрано». Итог этапа (done=true) — только на втором.
        grid_finalize = stage == "gaze_grid" and point is None

        module = self.face if stage.startswith("gaze") else self.identity
        if self._cv_thread is None or not self._module_ok(module):
            if stage == "gaze_grid" and not grid_finalize:
                await self._broadcast(envelope(MsgType.CALIBRATION, stage=stage, progress=0.0,
                                               done=False, result={"point": point,
                                                                   "degraded": True}))
                return
            await self._finish_calibration_async(stage, point, {
                "ok": True, "degraded": True,
                "reason": "камера или модуль недоступны, калибровка пропущена",
                **({"all_points_done": True, "screen_map_applied": False}
                   if grid_finalize else {}),
            })
            return

        if stage.startswith("gaze"):
            self._gaze_quiet(time.time())

        # GazeCalibration трогает только CV-поток: и начало стадии (begin_*),
        # и сэмплы, и расчёт карты — на ближайшем кадре (_handle_calibration).
        # Здесь только ставится задание, иначе кадр устаревшего задания мог
        # попасть в новую точку или переключить стадию посреди сетки.
        if grid_finalize:
            job = _CalibrationJob(stage=stage, point=None, needed=0, finalize=True)
        else:
            job = _CalibrationJob(stage=stage, point=point, needed=self.cfg.calibration_samples)
        with self._calib_lock:
            prev = self._calib
            if prev is not None and not prev.finished and prev.stage != stage \
                    and prev.stage in ("gaze_center", "identity"):
                job.supersedes = prev
            self._calib = job
        if not grid_finalize:
            await self._broadcast(envelope(MsgType.CALIBRATION, stage=stage, progress=0.0,
                                           done=False,
                                           result={"point": point} if point is not None else {}))

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
        """Телеметрия ввода: в FusionEngine (события) и в журнал (сырой слой).

        В журнал уходит только то, что прошло `sanitize_telemetry()`: интервалы,
        классы клавиш и длины. Раньше этот поток жил исключительно в памяти
        процесса и исчезал вместе с ним — то есть на апелляции по вопросу
        «на чём основана аномалия ритма набора» ответить было нечем.

        Санитизация стоит ЗДЕСЬ, до обоих потребителей, и это главное в методе.
        Движок правил получает ровно то же, что журнал: иначе поле, которое не
        прошло белый список, всё равно оказывалось бы в сообщении события и в
        хеш-цепочке — обещание «по журналу нельзя восстановить ответ» держится
        только если непрошедшего не видит НИКТО ниже этой строки.
        """
        ts = _f_num(msg.get("ts"), time.time())
        clean = sanitize_telemetry(dict(msg))
        if not clean:
            return
        clean["ts"] = ts
        ready = self.raw_log.note_telemetry(dict(clean), ts)
        if ready:
            await self._store_observations(ready)

        if self.fusion is None or not hasattr(self.fusion, "note_telemetry"):
            return
        try:
            events = self.fusion.note_telemetry(dict(clean))
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
        # Поля, которыми владеют движок правил и счёт риска, снимаются на
        # границе сокета. `engine/risk.py` в комментарии к `_ladder_obs_key`
        # прямо рассчитывает на этот заслон как на второй, независимый от
        # собственных проверок: присланный `escalation.run` приписывает
        # событие к чужому прогону лестницы и сворачивает его вклад в ноль, а
        # `code` — это идентификатор, по которому студент подаёт апелляцию.
        # До появления видов, которые шлёт оболочка и которые СТОЯТ на
        # лестницах, срез был безвреден; теперь он обязателен.
        stripped = [name for name in SHELL_DETAIL_RESERVED if name in detail]
        for name in stripped:
            detail.pop(name, None)
        if stripped:
            log.warning("оболочка прислала служебные поля detail (%s) в событии %s — "
                        "сняты: ими владеет движок правил",
                        ", ".join(stripped), kind.value)
        detail.setdefault("source", "shell")
        if kind in OFF_PROFILE_KINDS:
            # Источник вне белого списка: момент, а не удерживаемое условие, и
            # ядро перепроверяет адрес по своей копии профиля.
            await self._cmd_off_profile(kind, detail)
            return
        if kind is EventKind.SHELL_CONFIG:
            # Режим запуска — не инцидент, а условия наблюдения: окно
            # подтверждения, гистерезис и cooldown движка правил к нему
            # неприменимы (он приходит один раз за сессию и должен лечь в
            # цепочку целиком, а не быть подавлен как повтор).
            await self._cmd_shell_config(detail)
            return
        await self._emit(self._push_external(kind, detail))

    # ----------------------------------------------- режим запуска оболочки
    #: Что ядро принимает в SHELL_CONFIG. Сообщение приходит по сокету от
    #: клиента, поэтому в подписанную цепочку кладётся не присланный словарь, а
    #: собранный здесь по этому списку: лишние поля отбрасываются, строки
    #: обрезаются, списки ограничены по длине.
    SHELL_CONFIG_BOOLS: tuple[str, ...] = (
        "no_kiosk", "no_lockdown", "allow_multi_display",
        "locks_intended", "locks_active", "window_locked",
        "content_protection_requested",
        "clipboard_cleared", "admin_exit_held", "renderer_present",
        "sidecar_spawned_by_shell", "system_wide",
        # Оболочке подали флаги правил экзамена (--exam-url/--exam-origin/
        # --exam-preset/--allow-search), и она их ОТКЛОНИЛА. Правила задаёт
        # только файл профиля, который читает ядро; оболочка, применяющая свои
        # правила, давала бы два противоречащих документа об одной сессии.
        # Имена самих флагов и так лежат в argv_flags — это поле называет
        # случай словом, чтобы отчёт не разбирал argv глазами.
        "exam_profile_flags_refused",
    )
    #: Поля, у которых `null` значит «не знаем», а не «выключено». Electron не
    #: умеет спросить, применена ли защита содержимого, и до создания окна
    #: ответа просто нет. Свернуть такое в False — подлог в сторону обвинения.
    SHELL_CONFIG_TRISTATE: tuple[str, ...] = (
        "content_protection_applied",
        # ВСТАЛ ЛИ ФИЛЬТР ИСТОЧНИКОВ на страницу экзамена. Трёхзначное по той
        # же причине: `null` — страницу экзамена не открывали (профиль без
        # адреса, или экзамен до неё не дошёл), и спрашивать не о чем. Без
        # этого поля отчёт печатал бы «действовал профиль <хеш>» по сессии, в
        # которой фильтра не существовало, — и отличить одно от другого по
        # документу было бы нельзя.
        "exam_filter_installed",
        # БЫЛА ЛИ СТРАНИЦА LMS НА ЭКРАНЕ. Трёхзначное: `null` — профиль без
        # адреса экзамена, представление не открывали и спрашивать не о чем.
        # Без этого поля по документу нельзя отличить сессию, где студент
        # работал в настоящем LMS под наблюдением, от сессии, где
        # представление не встало и он весь экзамен смотрел в локальный
        # мок-тест, — и от обратного случая, где чужая страница накрыла собой
        # согласие и калибровку. Причина лежит рядом строкой
        # (`exam_view_reason`): «не прикреплено» без причины неотличимо от
        # «забыли прикрепить».
        "exam_view_attached",
    )
    SHELL_CONFIG_STRINGS: tuple[str, ...] = (
        "platform", "exam_state", "admin_exit", "shell_version",
        # Почему страница LMS в том положении, в каком она была, и откуда
        # взято отведённое ей место: отчёт renderer по фактической раскладке
        # или запасная константа. Запасная означает, что место под панель
        # прокторинга посчитано не по вёрстке и могло с ней не совпасть.
        "exam_view_reason",
        "exam_view_inset_source",
        "exam_view_inset_reason",
        # Хеш того, что оболочка ФАКТИЧЕСКИ применяла к запросам. Рядом с
        # хешем ЯДРА по объявленному файлу (`control/exam_profile`) это два
        # ответа на разные вопросы: «какие правила объявлены» и «какие
        # действовали». Сравнивать их между собой нельзя — каноны разные, —
        # но лежать они обязаны рядом, иначе второй вопрос в документе
        # вообще не задан.
        "exam_profile_applied_hash",
    )
    SHELL_CONFIG_INTS: tuple[str, ...] = (
        "display_count", "held_shortcuts_count", "auditable_shortcuts_count",
        # Масштаб заблокированного — по классам обращения. Инциденты ядро
        # считает само и этим полям не верит; здесь они нужны для сверки:
        # расхождение «оболочка отбила 40, в журнале 2» — само по себе факт.
        "exam_profile_blocked_navigation",
        "exam_profile_blocked_iframe",
        "exam_profile_blocked_subresource",
        # Отменённые скачивания с РАЗРЕШЁННОГО источника. Не инцидент:
        # студент правил не нарушал, вложение в Moodle — штатная работа LMS.
        # Но экзамен с материалами во вложении на этом не состоится, и
        # документ обязан показывать масштаб, а не молчать.
        "exam_downloads_cancelled",
    )

    @staticmethod
    def _clean_str(value: Any, limit: int = 200) -> str:
        return str("" if value is None else value)[:limit]

    @classmethod
    def _clean_str_list(cls, value: Any, limit: int = 64, each: int = 160) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        out = []
        for item in list(value)[:limit]:
            text = cls._clean_str(item, each).strip()
            if text:
                out.append(text)
        return out

    def _shell_config_record(self, detail: dict[str, Any]) -> dict[str, Any]:
        """Привести SHELL_CONFIG к записи для цепочки и посчитать вердикт.

        Вердикт `weakened` считает ЯДРО по отдельным фактам, а присланное
        оболочкой значение игнорируется. Иначе сборка, запущенная с
        `--no-lockdown`, могла бы прислать `weakened: false` и получить чистый
        отчёт — то есть ровно ту дыру, которую эта запись и закрывает.
        """
        src = detail if isinstance(detail, dict) else {}
        rec: dict[str, Any] = {"reported": True, "received_at": time.time()}
        for name in self.SHELL_CONFIG_BOOLS:
            if name in src:
                rec[name] = bool(src.get(name))
        for name in self.SHELL_CONFIG_TRISTATE:
            if name in src:
                value = src.get(name)
                rec[name] = None if value is None else bool(value)
        # Короткое имя для шапки отчёта: там читается ФАКТ («защита окна от
        # съёмки действовала»), а не намерение оболочки.
        if "content_protection_applied" in rec:
            rec["content_protection"] = rec["content_protection_applied"]
        for name in self.SHELL_CONFIG_STRINGS:
            if name in src:
                rec[name] = self._clean_str(src.get(name))
        for name in self.SHELL_CONFIG_INTS:
            if name in src:
                try:
                    rec[name] = max(0, int(src.get(name) or 0))
                except (TypeError, ValueError):
                    rec[name] = -1
        rec["held_shortcuts"] = self._clean_str_list(src.get("held_shortcuts"))
        rec["argv_flags"] = self._clean_str_list(src.get("argv_flags"))

        # Свойства окна приходят трёхзначными: Electron мог не ответить, и
        # `null` здесь значит «не знаем», а не «выключено». Сворачивать их в
        # False было бы подлогом в сторону обвинения.
        window = src.get("window")
        rec["window"] = ({self._clean_str(k, 32): (None if v is None else bool(v))
                          for k, v in list(window.items())[:16]}
                         if isinstance(window, dict) else {})

        py = src.get("python")
        if isinstance(py, dict):
            rec["python"] = {
                "requested": self._clean_str(py.get("requested"), 500),
                "resolved": self._clean_str(py.get("resolved"), 500),
                "source": self._clean_str(py.get("source"), 40),
                "used": bool(py.get("used")),
            }

        rows: list[dict[str, Any]] = []
        for item in (src.get("env") if isinstance(src.get("env"), (list, tuple)) else [])[:16]:
            if not isinstance(item, dict):
                continue
            name = self._clean_str(item.get("name"), 64)
            if not name:
                continue
            rows.append({
                "name": name,
                "set": bool(item.get("set")),
                "value": self._clean_str(item.get("value"), 500),
                "resolved": self._clean_str(item.get("resolved"), 500),
                "exists": bool(item.get("exists")),
            })
        rec["shell_env"] = rows
        rec["shell_env_set"] = [r["name"] for r in rows if r["set"]]

        # Интерпретатор: что оболочка собиралась запустить против того, чем
        # ядро запущено на самом деле. Расхождение — не обвинение (venv,
        # симлинк python3 -> python3.12 дают его штатно), поэтому оно идёт в
        # заметки, а не в список снятых защит.
        notes: list[str] = []
        rec["core_python_executable"] = sys.executable
        declared = str((rec.get("python") or {}).get("resolved") or "")
        if declared and (rec.get("python") or {}).get("used"):
            try:
                same = os.path.realpath(declared) == os.path.realpath(sys.executable)
            except Exception:
                same = declared == sys.executable
            rec["python_mismatch"] = not same
            if not same:
                notes.append("оболочка запускала сайдкар интерпретатором "
                             f"{declared}, а ядро работает под {sys.executable}")
        if (rec.get("python") or {}).get("source") == "env":
            notes.append("интерпретатор сайдкара задан переменной PROCTOR_PYTHON")
        for name in rec["shell_env_set"]:
            notes.append(f"в окружении оболочки задана {name}")

        # Второе независимо проверяемое поле записи, после интерпретатора.
        # `display_count` приходит СО СЛОВ оболочки (Electron
        # `screen.getAllDisplays()`), а систему ядро умеет опросить само.
        # Расхождение — не обвинение: монитор могли воткнуть между опросами,
        # зеркало на демо-дне считается по-разному разными API. Поэтому оно
        # идёт в заметки, а не в список снятых защит. Но оно идёт В ЦЕПОЧКУ:
        # запись «оболочка сказала 1, система видит 3» задним числом не
        # поправить, а без неё число мониторов было ничем не подкреплённым
        # утверждением той стороны, которую мы и проверяем.
        core_displays = self._core_display_count()
        if core_displays is not None:
            rec["core_display_count"] = core_displays
            if "display_count" in rec:
                shell_displays = rec["display_count"]
                rec["display_mismatch"] = bool(shell_displays != core_displays)
                if rec["display_mismatch"]:
                    notes.append(
                        f"оболочка сообщила мониторов: {shell_displays}, "
                        f"система видит: {core_displays}")
            else:
                rec["display_mismatch"] = None
        else:
            rec["core_display_count"] = None
            rec["display_mismatch"] = None
        rec["notes"] = notes

        # --- что из защиты машины фактически СНЯТО ---
        weakened: list[str] = []
        if rec.get("no_lockdown"):
            weakened.append("--no-lockdown: ни перехвата сочетаний, ни очистки "
                            "буфера обмена, ни захвата окна")
        if rec.get("no_kiosk"):
            weakened.append("--no-kiosk: окно не удерживается поверх других и закрываемо")
        if rec.get("allow_multi_display"):
            weakened.append("--allow-multi-display: второй экран не останавливает экзамен")
        # Строго `is False`: `None` значит «оболочка не знает» (окна ещё нет
        # или платформа не отвечает), и обвинять по незнанию нельзя.
        if rec.get("content_protection_applied") is False:
            weakened.append("защита содержимого окна не применена: экран снимается")
        if rec.get("locks_intended") and "locks_active" in rec and not rec["locks_active"]:
            weakened.append("блокировки должны были быть включены, но не включены")
        # Главный пункт LAUNCH-01: «блокировки включены» без списка фактически
        # удержанных сочетаний — утверждение на слово. Пустой список при
        # заявленной активности означает, что не перехвачено НИ ОДНО сочетание,
        # и отсутствие SHORTCUT_BLOCKED в журнале объясняется этим, а не
        # поведением студента.
        if rec.get("locks_active") and not rec["held_shortcuts"]:
            weakened.append("блокировки заявлены включёнными, но ни одно системное "
                            "сочетание фактически не перехвачено")
        # Честное «нет» — это тоже отсутствие защиты. Прежние две проверки
        # обвиняли только за ПРОТИВОРЕЧИЕ («собирались включить и не включили»,
        # «включили, но ничего не перехватили») и обе снимались честным ответом
        # «не собирались и не включили»: оболочка сообщала, что блокировок нет
        # ни в намерении, ни на деле, не перехвачено ни одного сочетания, — и
        # получала «защита машины применена полностью» и верхний уровень
        # вердикта. Проверено: это даже не подделка, достаточно сказать правду.
        if rec.get("locks_active") is False and not rec.get("no_lockdown"):
            weakened.append("перехват системных сочетаний не действовал: "
                            "оболочка сообщила, что блокировки не включены")
        if rec.get("window_locked") is False and not rec.get("no_kiosk"):
            weakened.append("окно экзамена не удерживалось поверх остальных")
        rec["weakened"] = bool(weakened)
        rec["weakened_by"] = weakened

        # --- что из защиты машины ПОДТВЕРЖДЕНО, и чего оболочка не сказала ---
        #
        # ТРЕТЬЕ СОСТОЯНИЕ. Вердикт считался по наличию ПРИЗНАНИЙ: пустой
        # `detail` не давал ни одного обвиняющего признака, `weakened`
        # оставался False, и запись означала «защита применена полностью».
        # Невиновность по умолчанию — не то свойство, которое нужно записи о
        # защите: отсутствие поля значит «оболочка об этом молчала», а не «всё
        # в порядке». Молчание теперь называется молчанием.
        confirmed: list[str] = []
        unconfirmed: list[str] = []
        #: (поле, что оно подтверждает). Порядок — как в шапке отчёта.
        checks: tuple[tuple[str, str], ...] = (
            ("locks_active", "перехват системных сочетаний"),
            ("window_locked", "удержание окна экзамена поверх остальных"),
            ("content_protection_applied", "защита окна от съёмки экрана"),
            ("renderer_present", "окно экзамена создано"),
            ("sidecar_spawned_by_shell", "ядро запущено этой оболочкой"),
        )
        for name, what in checks:
            if name not in rec:
                unconfirmed.append(what)
                continue
            value = rec.get(name)
            if value is True:
                confirmed.append(what)
            elif value is None:
                # Трёхзначное поле: «не знаем» — тоже не подтверждение.
                unconfirmed.append(what)
        # Заявленные блокировки без списка удержанных сочетаний подтверждением
        # не считаются: это тот же LAUNCH-01, только с другой стороны.
        if "перехват системных сочетаний" in confirmed and not rec["held_shortcuts"]:
            confirmed.remove("перехват системных сочетаний")
            unconfirmed.append("перехват системных сочетаний (список пуст)")
        rec["confirmed"] = confirmed
        rec["unconfirmed"] = unconfirmed

        if weakened:
            rec["protection"] = "weakened"
            rec["verdict"] = "сессия непригодна для настоящего экзамена"
        elif not confirmed:
            rec["protection"] = "unconfirmed"
            rec["verdict"] = ("оболочка не подтвердила ни одной защиты машины: "
                              "по этой записи проверить состояние защиты нельзя")
        else:
            rec["protection"] = "confirmed"
            # Формулировка — ровно про то, что запись доказывает. Она не
            # доказывает, что защита работала: оболочка свидетельствует о себе,
            # и на машине, где оба конца принадлежат студенту, подтвердить это
            # локально нечем (LIMITATIONS R1/R3). Доказано другое: оболочка
            # опросила систему и получила такие ответы, и утверждение
            # зафиксировано до экзамена и связано хешем с genesis.
            rec["verdict"] = ("оболочка сообщила о применённой защите машины "
                              "(со слов оболочки, независимо не подтверждено)")
        return rec

    @staticmethod
    def _core_display_count() -> int | None:
        """Сколько экранов видит САМО ядро. None — опросить нечем.

        Отдельный метод, чтобы проверка не ломала запись: модуль env-проверок
        может быть не установлен (деградация канала env — штатный режим), и
        отсутствие сверки не должно мешать SHELL_CONFIG лечь в цепочку.
        """
        try:
            import env_checks
        except Exception:
            return None
        fn = getattr(env_checks, "logical_display_count", None)
        if not callable(fn):
            return None
        try:
            value = fn()
        except Exception:
            return None
        return int(value) if isinstance(value, int) and value > 0 else None

    @staticmethod
    def _shell_config_message(record: dict[str, Any]) -> str:
        """Русский текст для журнала, HUD и отчёта. Три состояния, не два."""
        if record.get("weakened"):
            return ("Оболочка запущена с ослабленной защитой машины: "
                    + "; ".join(record.get("weakened_by") or [])
                    + ". Сессия непригодна для настоящего экзамена.")
        displays = f"мониторов: {record.get('display_count', '?')}"
        core = record.get("core_display_count")
        if core is not None and record.get("display_mismatch"):
            displays += f" (система видит {core})"
        if record.get("protection") == "unconfirmed":
            missing = "; ".join(record.get("unconfirmed") or []) or "ни одного поля"
            return ("Режим запуска оболочки зафиксирован, но ни одна защита "
                    "машины не подтверждена. Оболочка не сообщила: "
                    f"{missing}. По этой записи проверить состояние защиты "
                    f"нельзя. {displays[0].upper() + displays[1:]}.")
        return ("Режим запуска оболочки зафиксирован. Оболочка сообщила о "
                "применённой защите машины: "
                + "; ".join(record.get("confirmed") or []) + ". "
                f"Перехвачено сочетаний: {len(record.get('held_shortcuts') or [])}, "
                f"{displays}. Это утверждение оболочки о себе — ядро его "
                "независимо не подтверждает.")

    async def _cmd_shell_config(self, detail: dict[str, Any]) -> None:
        """Фактическое состояние защиты машины -> журнал и хеш-цепочка (LAUNCH-01).

        До этой записи состояние защиты жило только в HUD, в `protectionInfo()`:
        снятые блокировки были видны лишь по ОТСУТСТВИЮ SHORTCUT_BLOCKED и
        WINDOW_BLUR, то есть ничем не отличались от честной сессии, в которой
        студент просто ничего не нажимал. Теперь это запись в цепочке: её
        нельзя не прислать незаметно (см. `_cmd_session_end`) и нельзя
        переписать после экзамена.

        Запись НИЧЕГО не запрещает. Ослабляющий флаг не останавливает экзамен и
        не поднимает risk-score (вес вида — 0): он меняет только то, что будет
        написано в отчёте об условиях наблюдения.
        """
        record = self._shell_config_record(detail)
        self.shell_config = record
        self.shell_config_history.append(record)
        if len(self.shell_config_history) > 32:
            del self.shell_config_history[:-32]
        record["report_index"] = len(self.shell_config_history)
        weakened = bool(record.get("weakened"))
        # Переобъявление состояния — отдельный факт, и он обязан быть виден.
        # Чистая запись после признания в ослаблении признание не отменяет.
        if len(self.shell_config_history) > 1:
            record["restated"] = True
            record["notes"].append(
                "оболочка переобъявила режим запуска: это запись №"
                f"{len(self.shell_config_history)} за сессию, предыдущие остаются "
                "в цепочке и учитываются в шапке отчёта")

        if weakened:
            log.warning("%s", "=" * 72)
            log.warning("РЕЖИМ ЗАПУСКА ОСЛАБЛЕН: СЕССИЯ НЕПРИГОДНА ДЛЯ НАСТОЯЩЕГО ЭКЗАМЕНА")
            for item in record["weakened_by"]:
                log.warning("  - %s", item)
            log.warning("%s", "=" * 72)
        elif record.get("protection") == "unconfirmed":
            log.warning("режим запуска оболочки: НИ ОДНА ЗАЩИТА НЕ ПОДТВЕРЖДЕНА "
                        "(оболочка не сообщила: %s)",
                        "; ".join(record.get("unconfirmed") or []) or "ни одного поля")
        else:
            log.info("режим запуска оболочки: оболочка сообщила о применённой "
                     "защите (%s; перехвачено сочетаний: %d, мониторов: %s) — "
                     "независимо не подтверждено",
                     "; ".join(record.get("confirmed") or []),
                     len(record["held_shortcuts"]), record.get("display_count", "?"))
        for note in record["notes"]:
            log.warning("режим запуска: %s", note)

        digest = ""
        if self.session.active and self._store_open:
            digest = await self._store_control("shell_config", record)
            self._shell_config_in_chain = bool(digest)
        else:
            # Сессии ещё нет — цепочки тоже. Запись не теряем: её допишет
            # _cmd_session_start сразу после открытия хранилища.
            log.info("SHELL_CONFIG получен вне открытой сессии — запись в цепочку "
                     "отложена до начала сессии")

        ev = make_event(
            EventKind.SHELL_CONFIG,
            severity=Severity.HIGH if weakened else Severity.INFO,
            channel=Channel.SHELL,
            detail={**record, "code": SHELL_CONFIG_CODE, "chain_hash": digest},
        )
        # make_event ставит шаблонный текст по виду события; здесь нужен текст с
        # перечислением снятых защит, иначе в отчёте стоял бы один и тот же
        # заголовок и у честной сессии, и у ослабленной.
        ev.message = self._shell_config_message(record)
        await self._emit([ev])
        await self._broadcast(self._status_message())

    # ------------------------------------------------------- профиль экзамена
    async def _emit_exam_profile_state(self, chain_hash: str) -> None:
        """Состояние правил экзамена — событием журнала (У-13).

        Три вида вместо одного с полем внутри, потому что в шапке отчёта это
        три разные строки, и сворачивать их во вложенный словарь означает
        спрятать главное: «правила не задавались» и «поиск разрешён» должны
        читаться рядом с вердиктом, а не раскрываться по щелчку.

        Вес всех трёх — 0. Это УСЛОВИЯ экзамена, а не поведение студента:
        отсутствие профиля не имеет права само приостанавливать экзамен.
        """
        profile = self.exam_profile
        record = self.exam_profile_record()
        base = {**record, "chain_hash": chain_hash}

        if profile.present:
            kind = EventKind.EXAM_PROFILE_APPLIED
            message = (f"Действовал профиль экзамена {profile.short_hash}"
                       + (f", {profile.institution}" if profile.institution else "")
                       + f". Разрешённых источников: {len(profile.effective_origins)}"
                       + f". Подпись: {_profile_sig_label(profile.signature_state)}")
        else:
            kind = EventKind.EXAM_PROFILE_ABSENT
            message = ("Правила экзамена проктором не задавались: белого списка "
                       "источников нет, открывается локальный тест")
            if profile.load_error:
                message += f". Профиль не загружен: {profile.load_error}"

        events = [make_event(
            kind,
            severity=Severity.INFO,
            channel=Channel.SHELL,
            detail={**base, "code": EXAM_PROFILE_CODES[kind]},
        )]
        events[0].message = message

        if profile.present and profile.allow_search_effective:
            # Отдельная запись, а не поле: разрешённый поиск означает, что
            # готовый ответ доступен студенту прямо в выдаче, и на разборе это
            # первое, что нужно знать о сессии.
            by = ("флагом запуска --allow-search" if profile.allow_search_by_flag
                  else "самим профилем")
            search_ev = make_event(
                EventKind.EXAM_PROFILE_SEARCH_ALLOWED,
                severity=Severity.INFO,
                channel=Channel.SHELL,
                detail={**base, "allowed_by": by,
                        "code": EXAM_PROFILE_CODES[EventKind.EXAM_PROFILE_SEARCH_ALLOWED]},
            )
            search_ev.message = (
                f"Профиль экзамена разрешает поисковики ({by}): выдача содержит "
                "готовый ответ, переходить никуда не нужно")
            events.append(search_ev)

        await self._emit(events)

    async def _cmd_off_profile(self, kind: EventKind, detail: dict[str, Any]) -> None:
        """Источник вне белого списка -> журнал с ПОЛНЫМ адресом.

        Это и есть ценность профиля. Профиль не защита: оболочку можно
        подменить, белый список — поправить, и нигде в системе не утверждается
        обратного. Но каждая попытка выйти за список оставляет запись с полным
        адресом, временем и хешем действовавших правил, и эту запись нельзя
        удалить, не сломав хеш-цепочку.

        ЯДРО ПЕРЕПРОВЕРЯЕТ АДРЕС САМО. Оболочка присылает «вот это нарушение»,
        но верить ей на слово нельзя в обе стороны: подменённая оболочка может
        назвать нарушением разрешённый адрес (чтобы засорить журнал и утопить
        настоящую попытку) или, наоборот, прислать нарушение как подзапрос
        вместо навигации. Поэтому решение о классе принимает ядро по СВОЕЙ
        копии профиля, а расхождение с заявлением оболочки само записывается
        фактом (`disputed`).

        Мимо движка правил — по той же причине, что и SHELL_CONFIG: окно
        подтверждения и гистерезис тут неприменимы (это момент, а не
        удерживаемое условие), а общий `cooldown_sec` движка для вида без
        правила равен 10 с и съедал бы попытки.
        """
        raw_url = self._clean_str(detail.get("url") or detail.get("href")
                                  or detail.get("address") or "",
                                  limit=OFF_PROFILE_URL_MAX_LEN + 1).strip()
        # Управляющие символы из адреса убираем: запись едет в лог, в HTML-отчёт
        # и в сообщение события, и перевод строки внутри адреса разорвал бы
        # строку журнала на две — то есть адрес стал бы нечитаемым ровно там,
        # где он и нужен.
        url = "".join(ch for ch in raw_url if ch >= " " and ch != "\x7f")
        truncated = len(url) > OFF_PROFILE_URL_MAX_LEN
        url = url[:OFF_PROFILE_URL_MAX_LEN]
        host = url_host(url) or self._clean_str(detail.get("origin") or
                                               detail.get("host") or "", limit=300).lower()
        if not url and not host:
            log.warning("сообщение о выходе за белый список пришло без адреса — "
                        "записать нечего")
            return

        profile = self.exam_profile
        now = time.time()
        claimed = str(kind.value)

        # ЗАПРЕТ, КОТОРЫЙ НЕ СНИМАЕТСЯ НИКАКОЙ ЗАПИСЬЮ БЕЛОГО СПИСКА.
        #
        # Два случая, и ядро убеждается в обоих САМО, не веря оболочке:
        #
        #  * адреса без сетевого хоста (`file:`, `data:`, `blob:`, `about:`) —
        #    `url_host()` даёт "", и ни одна запись белого списка такой адрес
        #    разрешить не может: сравнивать не с чем;
        #  * петля на СОБСТВЕННЫЙ порт ядра — ядро знает свой порт, и страница
        #    экзамена туда не ходит ни при каких правилах.
        #
        # Без этой ветки `host_allowed("")` отвечал «разрешено» (и правильно:
        # для `url_allowed` это именно так), а здесь «разрешено» означало
        # `disputed` — то есть `file:///Users/stud/shpora.html`, самая вероятная
        # настоящая шпаргалка, становился расхождением вместо инцидента. Чтобы
        # улика не терялась, оболочка раньше присылала такие отказы видом
        # SHORTCUT_BLOCKED: адрес сохранялся, но под подписью «заблокированное
        # сочетание клавиш», в канале горячих клавиш, весом 5 вместо 20 и в
        # обход подраздела попыток в отчёте. Исправлено здесь, а не обходом там.
        absolute = False
        reason_absolute = ""
        if not host:
            absolute = True
            reason_absolute = ("адрес без сетевого хоста: ни одна запись белого "
                               "списка такой адрес разрешить не может")
        elif is_control_channel_host(host, getattr(self.cfg, "ws_port", 0)):
            absolute = True
            reason_absolute = ("обращение на порт канала оболочки к ядру: "
                               "страница экзамена туда не ходит ни при каких "
                               "правилах экзамена")

        allowed = False if absolute else (profile.host_allowed(host) if host else True)

        if not profile.present:
            # Правил нет — нарушить их нельзя. Сообщение всё равно ненормально:
            # оболочка применяет фильтр, которого ядро не выдавало. Пишем в лог
            # и не выпускаем инцидент: обвинение по несуществующим правилам
            # было бы хуже молчания. Но расхождение считаем: оно попадёт в
            # итоговую запись цепочки.
            self._off_profile_stats["claimed_without_profile"] = int(
                self._off_profile_stats.get("claimed_without_profile", 0)) + 1
            log.warning("оболочка сообщила о выходе за белый список, но профиль "
                        "экзамена не задан: %s", url or host)
            return

        if allowed:
            # Адрес РАЗРЕШЁН нашей копией профиля. Инцидента нет, но
            # расхождение — факт: либо оболочка применяет не тот профиль,
            # который выдало ядро, либо её поправили.
            self._off_profile_stats["disputed"] = int(
                self._off_profile_stats.get("disputed", 0)) + 1
            log.warning("оболочка назвала нарушением адрес, разрешённый профилем "
                        "%s: %s — расхождение записано в итог сессии",
                        profile.short_hash, url or host)
            return

        if absolute:
            self._off_profile_stats["absolute"] = int(
                self._off_profile_stats.get("absolute", 0)) + 1

        # ПРЕДЕЛ СОБЫТИЙ ЗА СЕССИЮ. Считается по уже записанным в журнал
        # попыткам, а не по присланным сообщениям: свёрнутые дедупликацией и
        # отвергнутые как расхождение в предел не входят — иначе подменённая
        # оболочка глушила бы настоящие попытки, присылая мусор.
        written = (int(self._off_profile_stats.get("navigation", 0))
                   + int(self._off_profile_stats.get("subresource", 0)))
        if written >= OFF_PROFILE_MAX_EVENTS:
            self._off_profile_stats["over_cap"] = int(
                self._off_profile_stats.get("over_cap", 0)) + 1
            if self._off_profile_stats["over_cap"] == 1:
                log.warning(
                    "попыток выхода за белый список уже %d — предел событий за "
                    "сессию достигнут. Дальше адреса в журнал не пишутся, "
                    "считается только их число (over_cap). Такой поток с одной "
                    "страницы сам является наблюдением: заблокированный запрос "
                    "отменяется до разрешения имени, поэтому хосты можно "
                    "печатать из воздуха", written)
            return

        # Склейка подзапросов — по источнику. Для адреса без сетевого хоста
        # (`file:`) источника нет, и склеивать по пустой строке нельзя: разные
        # файлы — разные наблюдения, а не повтор одного.
        fold_key = host or f"absolute:{url[:200]}"
        if kind is EventKind.SUBRESOURCE_OFF_PROFILE:
            last = float(self._off_profile_last.get(fold_key, 0.0))
            if last and (now - last) < SUBRESOURCE_OFF_PROFILE_GAP_SEC:
                # Одна страница тянет десятки шрифтов и счётчиков с одного
                # чужого адреса. Это ОДНО наблюдение «страница обращается
                # наружу», и записывать его десятками значило бы собрать
                # обвинение из устройства чужой страницы.
                self._off_profile_stats["subresource_folded"] = int(
                    self._off_profile_stats.get("subresource_folded", 0)) + 1
                return
            self._off_profile_last[fold_key] = now
            self._off_profile_stats["subresource"] = int(
                self._off_profile_stats.get("subresource", 0)) + 1
        else:
            self._off_profile_stats["navigation"] = int(
                self._off_profile_stats.get("navigation", 0)) + 1

        origins = self._off_profile_stats.setdefault("origins", [])
        if host and host not in origins and len(origins) < 64:
            origins.append(host)

        record = {
            # Полный адрес — главное в записи. Обоснование границы приватности
            # см. у OFF_PROFILE_URL_MAX_LEN.
            "url": url,
            "url_truncated": bool(truncated),
            "origin": host,
            "source": self._clean_str(detail.get("source") or "shell", limit=40),
            # Что оболочка заявила и что решило ядро. Совпадают почти всегда;
            # ценность поля в том редком случае, когда нет.
            "claimed_kind": claimed,
            "resolved_kind": kind.value,
            # Запрет, который ядро опознало САМО, без сверки с белым списком:
            # у адреса нет сетевого хоста, либо это петля на порт ядра. Поле
            # отвечает проверяющему на вопрос «чем это установлено».
            "absolute_denial": bool(absolute),
            "absolute_reason": reason_absolute,
            "profile_hash": profile.profile_hash,
            "profile_hash_short": profile.short_hash,
            "allowed_origins": list(profile.effective_origins),
            "allow_search": bool(profile.allow_search_effective),
            "blocked_by_shell": bool(detail.get("blocked", True)),
            "code": EXAM_PROFILE_CODES[kind],
            # Ни одна формулировка в записи не утверждает, что переход был
            # невозможен: оболочка его не пропустила, а обойти оболочку на
            # своей машине студент может. Запись доказывает ПОПЫТКУ.
            "evidence_of": "попытка обращения к источнику вне правил экзамена",
        }
        for extra in ("method", "resource_type", "frame", "reason", "redirect_from"):
            value = detail.get(extra)
            if value is not None:
                record[extra] = self._clean_str(value, limit=300)

        if kind is EventKind.NAVIGATION_OFF_PROFILE:
            message = (f"Попытка открыть {url or host} — источник вне правил "
                       f"экзамена (профиль {profile.short_hash}). "
                       "Переход оболочкой не выполнен")
            log.warning("ПОПЫТКА ВЫХОДА ЗА БЕЛЫЙ СПИСОК: %s (профиль %s)",
                        url or host, profile.short_hash)
        else:
            message = (f"Страница обратилась к {host} — источник вне правил "
                       f"экзамена (профиль {profile.short_hash}). Чаще это "
                       "техника страницы, а не действие студента")
            log.info("подзапрос вне белого списка: %s", url or host)

        ev = make_event(
            kind,
            severity=(Severity.MEDIUM if kind is EventKind.NAVIGATION_OFF_PROFILE
                      else Severity.INFO),
            channel=Channel.SHELL,
            detail=record,
        )
        ev.message = message
        await self._emit([ev])

    async def _cmd_command(self, msg: dict[str, Any]) -> None:
        name = str(msg.get("name") or "")
        if name == "snapshot":
            path = await asyncio.to_thread(self._snapshot)
            if path:
                log.info("снапшот сохранён: %s", path)
            else:
                await self._error("snapshot_failed", "Снимок не сохранён: нет кадра или сессии")
        elif name == "reset_risk":
            await self._cmd_reset_risk(msg)
        elif name in ("proctor_lock", "proctor_release"):
            await self._cmd_proctor_decision(name, msg)
        elif name == "export_report":
            # Пересборка комплекта по требованию проктора: отчёт И пакет.
            # Раньше команда пересобирала только HTML и там же, где он лежал, —
            # то есть забрать доказательства по ней было нельзя.
            summary = self.session.summary()
            await self._build_report(summary)
            await self._build_package(summary)
        elif name == "deliver_package":
            await self._cmd_deliver_package(msg)
        else:
            await self._error("unknown_command", f"Неизвестная команда: {name}")

    async def _cmd_deliver_package(self, msg: dict[str, Any]) -> None:
        """Повторить копирование последнего пакета в папку проктора.

        Та же функция, что и после сборки (`handover.deliver_package`): новое
        имя при занятом, сверка sha256, ничего чужого не трогаем. Допустима
        только после завершения сессии — во время экзамена с машины не уходит
        ничего, так сказано студенту на экране согласия.
        """
        if self.session.active:
            await self._error("bad_state", "Доставить пакет в папку проктора можно "
                                           "только после завершения сессии")
            return
        path = str(self.last_package.get("package") or "") if self.last_package.get("ok") else ""
        if not path or not Path(path).is_file():
            await self._error("no_package", (
                "Пакет не собирается (--no-package): копировать в папку проктора "
                "нечего — забирайте каталог сессии целиком"
                if not self.cfg.package_on_end else
                "Пакета для доставки нет: сессия ещё не завершалась или пакет не собран"))
            return
        if not self.delivery.get("configured"):
            await self._error("delivery_not_configured", (
                "Папка проктора не задана (--deliver-to или PROCTOR_DELIVER_DIR): "
                f"пакет остался на этом компьютере: {path}"))
            return
        if self._delivering:
            await self._error("delivery_busy", "Копирование в папку проктора уже идёт")
            return
        done = self.last_package.get("delivery") or {}
        if done.get("ok"):
            # Вторая копия того же пакета рядом с первой ломает единственную
            # надёжную проверку на стороне проктора — число пакетов на шаре
            # против списка группы. Повтор — только после неудачи.
            await self._error("already_delivered", (
                f"Пакет уже лежит в папке проктора: {done.get('dest') or '—'}. "
                f"Копия не делается" if done.get("same_dir") else
                f"Пакет уже скопирован в папку проктора: {done.get('dest') or '—'}. "
                f"Повторная копия не делается"))
            return
        # Фоном, как и после сборки: зависшая шара не должна держать канал
        # оболочки — итог придёт отдельным status.
        self._schedule_delivery(self.last_package, self._package_dir)
        await self._broadcast(self._status_message())

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

                obs: list[tuple[EventKind, bool, dict[str, Any]]] = []
                face_obs: Any = None
                face_bbox: Any = None

                # --- лицо и взгляд: каждый кадр (дешёвый FaceMesh)
                if self.face is not None and frame_idx % max(cfg.face_every_n_frames, 1) == 0:
                    face_obs = self._safe_call(self.face.analyze, frame, what="FaceMesh")
                    if face_obs is not None:
                        face_bbox = _get(face_obs, "face_bbox")
                        self._safe_call(self._note_faces, face_obs, ts, what="учёт лиц")
                        obs.extend(self._safe_call(self._face_observations, face_obs, ts,
                                                   what="разбор лица") or [])

                if self.recorder is not None:
                    # Кольцевой буфер: кадры «до» инцидента нужны для клипа.
                    # Кадр уходит в буфер ПОСЛЕ учёта лиц: если на нём второе
                    # лицо, `_note_faces` уже снял незакрытые клипы, и в их
                    # «после» этот кадр не попадёт.
                    self._safe_call(self.recorder.push, frame, ts, what="буфер кадров")

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
                    # Лица берутся из `_last_faces`: FaceMesh мог разбирать не
                    # этот кадр (face_every_n_frames), а посторонних размыть
                    # надо всё равно.
                    self._safe_call(self._attach_evidence, events, frame, face_bbox,
                                    None, ts, what="доказательства")
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

        if ts < self._gaze_quiet_until:
            # Идёт калибровка взгляда: студент смотрит в углы экрана по просьбе
            # самой системы, а карты и порогов этого человека ещё нет. Взгляд и
            # поворот головы инцидентами не становятся; лицо, второе лицо и
            # живость проверяются как обычно.
            down = side = off = turned = False

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

        # Идентификатор трека для лестницы эскалации (protocol.ESCALATION_LADDERS).
        # phone_in_frame в словаре детектора — это bool, поэтому flag() возвращает
        # пустой detail и track_id теряется ИМЕННО ЗДЕСЬ, хотя ObjectDetector его
        # исправно считает (Detection.track_id, phone_tracks). Без него движок
        # сворачивает два РАЗНЫХ телефона в кадре в один прогон эскалации,
        # то есть второй телефон не добавляет тревоги. Берём идентификатор
        # у старших ступеней — они приходят словарём и его сохраняют.
        _track = (raised_detail.get("track_id")
                  or aimed_detail.get("track_id")
                  or (data.get("phone_tracks") or [None])[0])
        if _track is not None:
            phone_detail.setdefault("track_id", _track)
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
        if not obs:
            return []
        # Сырой слой пишется ВНЕ зависимости от того, сложит ли движок из этих
        # наблюдений инцидент. Это и есть смысл слоя: отчёт должен отвечать на
        # вопрос «что система видела», а не только «что она решила».
        raw_ready: list[dict[str, Any]] = []
        for kind, active, detail in obs:
            name = kind.value if isinstance(kind, EventKind) else str(kind)
            source = self._observation_source(kind, name)
            raw_ready.extend(self.raw_log.note(source, name, bool(active), ts,
                                               detail or {}))
        if raw_ready:
            self._post(("observations", raw_ready))

        if self.engine is None:
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

    @staticmethod
    def _observation_source(kind: EventKind | str, name: str) -> str:
        """Слой наблюдения для таблицы `observations`.

        Берётся из той же карты `CHANNEL_BY_KIND`, что и канал события: один
        источник истины, чтобы сырой слой и инциденты раскладывались по
        каналам одинаково.
        """
        if isinstance(kind, EventKind):
            return CHANNEL_BY_KIND.get(kind, Channel.SYSTEM).value
        # Составные входы движка приходят как `audio.speech`, `face.blink`.
        head = name.split(".", 1)[0].strip().lower()
        return head or "system"

    def _push_external(self, kind: EventKind, detail: dict[str, Any]) -> list[ProctorEvent]:
        # Внешние наблюдения (оболочка, проверки окружения) — тоже сырьё, и в
        # сыром слое им место наравне с наблюдениями детекторов. Движок может
        # их подавить (cooldown, окно подтверждения, выключенный вид), и тогда
        # в журнале не осталось бы ни следа того, что сигнал вообще приходил.
        # Решения над экзаменом сюда не попадают: они и так ложатся отдельными
        # записями `control`, дублировать их в сыром слое нечего.
        if kind not in (EventKind.LOCK_REVIEW_REQUESTED, EventKind.PROCTOR_DECISION,
                        EventKind.RISK_RESET, EventKind.SESSION_STARTED,
                        EventKind.SESSION_ENDED):
            ready = self.raw_log.note(
                self._observation_source(kind, kind.value), kind.value, True,
                time.time(), detail or {})
            if ready:
                self._post(("observations", ready))

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
                elif kind == "observations":
                    await self._store_observations(payload)
                elif kind == "calibration":
                    await self._broadcast(envelope(MsgType.CALIBRATION, **payload))
                    if payload.get("done"):
                        self._after_calibration_stage(payload)
                elif kind == "error":
                    await self._broadcast(envelope(MsgType.ERROR, **payload))
                elif kind == "clip_cancelled":
                    await self._store_clip_cancelled(payload)
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
            recording = self.session.recording
            # Кадр камеры — ДО записи в цепочку: путь к нему (или причина,
            # почему его нет) входит в подписанное тело события. События
            # CV-потока приходят уже с кадром своего момента; остальным
            # (оболочка, окружение, связки, решения) достаётся последний кадр.
            await self._ensure_frame(ev)
            if self.store is not None and self._store_open and recording:
                try:
                    await asyncio.to_thread(self.store.append, ev)
                except Exception as exc:
                    log.warning("запись доказательства не удалась: %s", exc)
            log.info("[%s] %s %s", ev.severity.value, ev.kind.value, ev.message)
            # `screen: true` — просьба к оболочке снять окно экзамена к этому
            # событию и прислать `screen_evidence` с его id.
            screen = recording and not is_service_kind(ev.kind)
            if screen:
                self._note_screen_request(ev)
            await self._broadcast(event_message(ev, screen=screen))
        await self._push_risk()

    # ------------------------------------------- кадр камеры к любому событию
    async def _ensure_frame(self, ev: ProctorEvent) -> None:
        """Приложить к событию кадр камеры или записать, почему его нет.

        Итог всегда один из двух: `ev.evidence.frame_path` или
        `ev.detail["evidence_skipped"]` (коды — `EVIDENCE_SKIP_*` в протоколе).
        Молчаливого «кадра нет, и неизвестно почему» быть не должно: в отчёте
        отсутствие кадра — такой же факт, как сам кадр.
        """
        if ev.evidence is not None:
            if ev.evidence.frame_path:
                self._mark_evidence_skipped(ev, "")
            return
        if _get(ev.detail, "evidence_skipped"):
            # CV-поток уже пытался и записал причину (например, write_failed).
            return
        reason = self._frame_skip_reason(ev) or self._camera_skip_reason()
        if reason:
            self._mark_evidence_skipped(ev, reason)
            return
        try:
            await asyncio.to_thread(self._attach_latest_frame, ev)
        except Exception as exc:
            log.warning("кадр к событию %s не приложен: %s", ev.kind.value, exc)
            self._mark_evidence_skipped(ev, EVIDENCE_SKIP_WRITE_FAILED)
        if ev.evidence is None or not ev.evidence.frame_path:
            if not _get(ev.detail, "evidence_skipped"):
                self._mark_evidence_skipped(ev, EVIDENCE_SKIP_WRITE_FAILED)

    @staticmethod
    def _mark_evidence_skipped(ev: ProctorEvent, reason: str) -> None:
        """Записать причину отсутствия кадра (пустая строка — снять отметку)."""
        if not isinstance(ev.detail, dict):
            ev.detail = {}
        if reason:
            ev.detail["evidence_skipped"] = reason
        else:
            ev.detail.pop("evidence_skipped", None)

    def _frame_skip_reason(self, ev: ProctorEvent) -> str:
        """Причина НЕ снимать кадр по самому событию и политике. "" — снимать."""
        if is_service_kind(ev.kind):
            return EVIDENCE_SKIP_SERVICE
        if not self.session.recording:
            return EVIDENCE_SKIP_NOT_RECORDING
        if not self._should_save_frame(ev):
            return EVIDENCE_SKIP_DISABLED
        return ""

    def _camera_skip_reason(self) -> str:
        """Причина, по которой кадра камеры нет вообще или он устарел. "" — есть."""
        if self.cfg.headless or self.cfg.mock or self.capture is None:
            # В --mock события берутся из сценария: настоящий кадр к
            # выдуманному SECOND_FACE был бы подлогом, а не доказательством.
            return EVIDENCE_SKIP_NO_CAMERA
        if not self.capture.available():
            # Нет opencv: кадров не будет ни у камеры, ни у рекордера.
            return EVIDENCE_SKIP_NO_CAMERA
        with self._frame_lock:
            frame, frame_ts = self._last_frame, self._last_frame_ts
        if frame is None:
            return self._no_frame_reason()
        if time.time() - frame_ts > EVIDENCE_FRAME_MAX_AGE:
            return EVIDENCE_SKIP_STALE
        return ""

    def _no_frame_reason(self) -> str:
        """Кадра нет: камеры не было вовсе или она пропала.

        `_last_frame` за жизнь процесса не сбрасывается, поэтому None значит
        «камера в этом процессе не отдала ни одного кадра» — устройства нет,
        оно занято или доступ не выдан: это `no_camera`. `stale_frame` —
        только камере, которая кадры отдавала, а потом замолчала.
        """
        with self._frame_lock:
            ever = self._last_frame is not None
        return EVIDENCE_SKIP_STALE if ever else EVIDENCE_SKIP_NO_CAMERA

    def _attach_latest_frame(self, ev: ProctorEvent) -> None:
        """Последний кадр камеры -> доказательство события (блокирующий, to_thread)."""
        with self._frame_lock:
            frame, frame_ts = self._last_frame, self._last_frame_ts
            faces = self._last_faces
        if frame is None:
            self._mark_evidence_skipped(ev, EVIDENCE_SKIP_NO_CAMERA)
            return
        if time.time() - frame_ts > EVIDENCE_FRAME_MAX_AGE:
            self._mark_evidence_skipped(ev, EVIDENCE_SKIP_STALE)
            return
        self._attach_evidence([ev], frame, faces[0], faces, frame_ts)

    # ------------------------------------------- снимок окна экзамена (оболочка)
    #: Сколько разосланных событий помнить для приёма `screen_evidence`.
    SCREEN_REQUESTS_MAX = 512

    def _note_screen_request(self, ev: ProctorEvent) -> None:
        """Запомнить событие, к которому оболочку попросили снять окно экзамена."""
        now = time.time()
        self._screen_requests[ev.id] = {
            "kind": ev.kind.value, "at": now,
            "session": self.session.session_id, "state": "wait",
        }
        # dict хранит порядок вставки: старые записи — в начале.
        horizon = now - SCREEN_EVIDENCE_MAX_AGE
        while self._screen_requests:
            oldest_id = next(iter(self._screen_requests))
            oldest = self._screen_requests[oldest_id]
            if (len(self._screen_requests) > self.SCREEN_REQUESTS_MAX
                    or oldest["at"] < horizon):
                self._screen_requests.pop(oldest_id, None)
                continue
            break

    async def _cmd_screen_evidence(self, msg: dict[str, Any]) -> None:
        """Снимок окна экзамена от оболочки -> файл + запись `evidence_screen`.

        Сообщение приходит из сети, поэтому ядро ему не верит: принимает его
        только пока сессия пишется, только к событию, которое само разослало
        в этой сессии с `screen: true` не раньше SCREEN_EVIDENCE_MAX_AGE назад,
        не больше одного раза на событие, не больше SCREEN_EVIDENCE_MAX_BYTES
        и только JPEG (сигнатура FF D8 FF). Отклонённое пишется в лог, но не
        в журнал: иначе клиент заливал бы цепочку чем угодно.

        Снимок ложится в каталог доказательств, его sha256 — в хеш-цепочку
        записью `control`/`evidence_screen`; связь с событием — по `event_id`.
        """
        received_at = time.time()
        event_id = str(msg.get("event_id") or "").strip()[:64]

        def reject(reason: str) -> None:
            log.warning("снимок окна экзамена отклонён (событие %s): %s",
                        event_id or "?", reason)

        if not self.session.recording or self.store is None or not self._store_open:
            reject("сессия не пишется")
            return
        if not callable(getattr(self.store, "append_control", None)):
            # Резервный журнал (JSONL без SQLite) записей control не умеет:
            # файл снимка без sha256 в цепочке был бы непроверяемым
            # приложением к пакету. Отказ — ДО записи файла.
            reject("хранилище не умеет записи control — снимок не к чему привязать")
            return
        entry = self._screen_requests.get(event_id) if event_id else None
        if entry is None or entry.get("session") != self.session.session_id:
            reject("ядро не рассылало такое событие в этой сессии")
            return
        if received_at - float(entry.get("at") or 0.0) > SCREEN_EVIDENCE_MAX_AGE:
            reject(f"пришёл позже {SCREEN_EVIDENCE_MAX_AGE:.0f} с после события")
            return
        if entry.get("state") != "wait":
            reject("снимок к этому событию уже принят")
            return
        kind = str(entry.get("kind") or "")

        data_b64 = msg.get("data_b64")
        if data_b64 is None or data_b64 == "":
            error = msg.get("error")
            if error is None or not str(error).strip():
                reject("нет ни data_b64, ни error")
                return
            entry["state"] = "done"
            # Текст причины приходит от клиента и ложится в подписанный
            # журнал и в отчёт: одна строка, без управляющих символов, коротко.
            text = "".join(ch for ch in " ".join(str(error).split()) if ch.isprintable())
            await self._store_control(SCREEN_EVIDENCE_CONTROL, {
                "event_id": event_id,
                "kind": kind,
                "error": text[:200] or "без причины",
                "received_at": received_at,
            })
            return

        if not isinstance(data_b64, str):
            reject("data_b64 не строка")
            return
        # Предел проверяется ДО декодирования: 3 МБ в base64 — это ~4 МБ текста.
        if len(data_b64) > (SCREEN_EVIDENCE_MAX_BYTES * 4) // 3 + 8:
            reject("снимок больше 3 МБ")
            return
        mime = msg.get("mime")
        if mime is not None and str(mime).lower() != "image/jpeg":
            reject(f"тип {str(mime)[:40]!r} вместо image/jpeg")
            return

        # Пока идёт декодирование и запись, повтор того же event_id не пройдёт.
        entry["state"] = "busy"
        try:
            data = await asyncio.to_thread(_decode_screen_jpeg, data_b64)
        except ValueError as exc:
            entry["state"] = "wait"
            reject(str(exc))
            return
        if not self.session.recording or not self._store_open:
            # Сессия закрылась, пока снимок декодировался: файл без записи в
            # цепочке был бы непроверяемым приложением к пакету.
            entry["state"] = "wait"
            reject("сессия закрылась, пока снимок принимался")
            return
        target = self.session.evidence_file(f"{kind.lower()}_screen")
        if target is None:
            entry["state"] = "wait"
            reject("каталог сессии недоступен")
            return
        path, rel = target
        try:
            digest = await asyncio.to_thread(_write_evidence_bytes, path, data)
        except OSError as exc:
            # Снимок пришёл, но ядро не смогло его сохранить: это не отказ
            # клиенту, а факт о доказательстве, и в журнале он должен быть.
            entry["state"] = "done"
            log.warning("снимок окна экзамена не записан (%s): %s", rel, exc)
            await self._store_control(SCREEN_EVIDENCE_CONTROL, {
                "event_id": event_id,
                "kind": kind,
                "error": f"ядро не записало файл снимка: {exc}"[:200],
                "received_at": received_at,
            })
            return
        entry["state"] = "done"
        if not self.session.recording or not self._store_open:
            with contextlib.suppress(OSError):
                path.unlink()
            reject("сессия закрылась, пока снимок записывался")
            return
        source = str(msg.get("source") or "")
        await self._store_control(SCREEN_EVIDENCE_CONTROL, {
            "event_id": event_id,
            "kind": kind,
            "path": rel,
            "sha256": digest,
            "width": _clamp_int(msg.get("width"), 0, 20000),
            "height": _clamp_int(msg.get("height"), 0, 20000),
            "bytes": len(data),
            "captured_at": _finite_float(msg.get("captured_at")),
            "received_at": received_at,
            "source": source if source in SCREEN_EVIDENCE_SOURCES else "unknown",
        })

    def _profile_record(self) -> dict[str, Any]:
        """Объявленные правила оценки для записи `control`/`weight_profile`.

        Собирает модуль отчётов: таблица весов и разбор профилей живут там, и
        второй копии этой логики быть не должно. Модуль недоступен (отчёты
        отключены) — пишем честный минимум, чтобы запись в цепочке всё равно
        была и расхождение было видно.
        """
        maker = getattr(self.report_mod, "profile_record", None) if self.report_mod else None
        if callable(maker):
            try:
                return dict(maker(self.cfg_dict))
            except Exception as exc:
                log.warning("правила оценки не разобраны: %s", exc)
        report_cfg = self.cfg_dict.get("report")
        report_cfg = report_cfg if isinstance(report_cfg, dict) else {}
        return {
            "profile": str(report_cfg.get("weight_profile")
                           or self.cfg_dict.get("weight_profile") or "closed-book"),
            "available": False,
            "reason": "модуль отчётов недоступен, таблица весов не разобрана",
        }

    async def _store_control(self, name: str, payload: dict[str, Any]) -> str:
        """Записать действие над экзаменом в хеш-цепочку.

        Возвращает хеш записи — его можно продиктовать проктору как
        подтверждение того, что решение зафиксировано. Пустая строка означает,
        что хранилище недоступно; тогда факт остаётся хотя бы в логе и в
        событии, но непоправимой записи нет, и об этом говорится прямо.
        """
        store = self.store
        if store is None or not self._store_open:
            log.warning("действие «%s» не попало в хеш-цепочку: хранилище закрыто", name)
            return ""
        append = getattr(store, "append_control", None)
        if not callable(append):
            log.warning("действие «%s» не попало в хеш-цепочку: хранилище старой версии "
                        "не умеет записи control", name)
            return ""
        try:
            digest = await asyncio.to_thread(append, name, dict(payload or {}))
        except Exception as exc:
            log.warning("запись «%s» в хеш-цепочку не удалась: %s", name, exc)
            return ""
        log.info("в хеш-цепочку записано: %s (hash %s…)", name, str(digest)[:12])
        return str(digest or "")

    async def _store_clip_cancelled(self, items: list[dict[str, Any]]) -> None:
        """Снятые клипы -> записи `control`/`clip_cancelled` в хеш-цепочке.

        Событие уже лежит в цепочке с `evidence.clip_path`, а файла по этому
        пути не будет: отчёт и проверяющий должны видеть, что клип снят
        намеренно (посторонний вошёл в кадр, пока набиралось «после»), а не
        потерян. Связь с событием — по `event_id`. Обычно запись идёт после
        события; у события оболочки, клип которого сняли, пока оно писалось,
        может оказаться и раньше — порядок не гарантирован, связь только по id.
        """
        for item in items or []:
            if not isinstance(item, dict):
                continue
            event_id = str(item.get("event_id") or "")
            if not event_id:
                continue
            log.info("клип к событию %s (%s) снят до записи: %s", event_id,
                     item.get("kind") or "?", item.get("reason") or "?")
            await self._store_control(CLIP_CANCELLED_CONTROL, {
                "event_id": event_id,
                "kind": str(item.get("kind") or ""),
                "reason": str(item.get("reason") or CLIP_CANCEL_MULTIPLE_FACES_AFTER),
                "at": _finite_float(item.get("cancelled_at")) or time.time(),
            })

    def _cancel_pending_clips(self, reason: str) -> int:
        """Снять незакрытые клипы рекордера и поставить записи об этом в очередь.

        Зовётся из CV-потока и из рабочих потоков event loop (кадр к событию
        оболочки), поэтому в цепочку пишет не сам, а через `_post`
        (`call_soon_threadsafe`) — запись делает `_consume_cv` в event loop.
        Вызывать под `_frame_lock`: так регистрация клипа в `_attach_evidence`
        (тоже под ним) и его снятие не разъезжаются. Возвращает число снятых.
        """
        recorder = self.recorder
        cancel = getattr(recorder, "cancel_pending", None) if recorder is not None else None
        if not callable(cancel):
            return 0
        try:
            cancelled = list(cancel(reason) or [])
        except Exception as exc:
            log.warning("клипы не сняты: %s", exc)
            return 0
        if cancelled:
            self._post(("clip_cancelled", cancelled))
        return len(cancelled)

    async def _store_observations(self, rows: list[dict[str, Any]]) -> None:
        """Отправить закрытые окна сырого слоя в журнал."""
        if not rows:
            return
        store = self.store
        if store is None or not self._store_open or not self.session.recording:
            return
        append = getattr(store, "append_observation", None)
        if not callable(append):
            if not self._raw_truncation_noted:
                self._raw_truncation_noted = True
                log.warning("сырой слой не пишется: хранилище старой версии не умеет "
                            "записи observation")
            return

        def _write(batch: list[dict[str, Any]]) -> int:
            written = 0
            for row in batch:
                try:
                    append(row)
                    written += 1
                except Exception as exc:
                    log.debug("сырое наблюдение не записано: %s", exc)
            return written

        written = await asyncio.to_thread(_write, list(rows))
        self.raw_log.mark_written(written)

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
        # `requested` — то, что насчитала автоматика; `action` — то, что ей
        # РАЗРЕШЕНО сделать без человека. По умолчанию они расходятся ровно в
        # одной точке: LOCK превращается в приостановку.
        requested = self._risk_action()
        action = cap_auto_action(requested, self.auto_lock)
        if not force and abs(score - self._last_risk_sent) < self.cfg.risk_send_delta \
                and action is self._last_action:
            return
        self._last_risk_sent = score
        breakdown = self._risk_breakdown()
        await self._broadcast(envelope(
            MsgType.RISK, score=score, level=self._risk_level(action),
            breakdown=breakdown, action=action.value,
            requested_action=requested.value,
            review_required=bool(requested is VerdictAction.LOCK and not self.auto_lock),
        ))
        if action is not self._last_action or (
                requested is VerdictAction.LOCK and not self._lock_review):
            await self._apply_action(action, score, breakdown, requested)

    async def _apply_action(self, action: VerdictAction, score: float,
                            breakdown: list[dict[str, Any]],
                            requested: VerdictAction | None = None) -> None:
        """Смена уровня -> verdict + перевод сессии в paused/locked.

        `action` здесь уже ограничен политикой: см. `cap_auto_action()`.
        `requested` — чего просила автоматика до ограничения.
        """
        requested = requested or action
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

        # Автоматика дошла до порога блокировки, а блокировать ей нельзя:
        # открываем запрос решения проктора. Экзамен при этом ПРИОСТАНОВЛЕН —
        # ждать решения, продолжая принимать ответы, было бы хуже всего.
        review_opened = False
        if requested is VerdictAction.LOCK and not self.auto_lock \
                and self._lock_review is None:
            await self._open_lock_review(score, breakdown)
            review_opened = True

        reason = self._verdict_reason(action, breakdown, requested)
        # `review_required` стоит в КАЖДОМ вердикте, а не только при открытом
        # запросе. Оболочка держит это состояние у себя, и поле, которое то
        # есть, то нет, она обязана была бы угадывать: отсутствие означало бы
        # «не изменилось», и флаг оставался бы залипшим после снятия
        # приостановки. Контракт (docs/CONTRACT.md) объявляет поле булевым
        # всегда — здесь это и выполняется.
        payload: dict[str, Any] = {"action": action.value, "reason": reason, "score": score,
                                   "requested_action": requested.value,
                                   "review_required": bool(self._lock_review)}
        if self._lock_review:
            payload["review"] = self.review_info()
        await self._broadcast(envelope(MsgType.VERDICT, **payload))
        self.session.set_risk(score, action.value)
        try:
            if action is VerdictAction.LOCK and self.session.active:
                self.session.transition(SessionState.LOCKED, reason)
            elif action is VerdictAction.PAUSE and self.session.state in (
                    SessionState.RUNNING, SessionState.CALIBRATING):
                self.session.transition(SessionState.PAUSED, reason)
            elif action in (VerdictAction.NONE, VerdictAction.WARN) \
                    and self.session.state is SessionState.PAUSED:
                if self._lock_review:
                    # Спад риска НЕ снимает приостановку, пока проктор не
                    # решил. Иначе ожидание решения снималось бы само собой
                    # через минуту затухания — и человек, которого позвали,
                    # обнаруживал бы экзамен, который уже идёт дальше.
                    log.info("риск снизился, но экзамен остаётся приостановленным: "
                             "ждём решения проктора (код сессии %s)",
                             self.session.code_display or "—")
                else:
                    self.session.transition(SessionState.RUNNING, "риск снизился")
        except SessionStateError as exc:
            log.debug("переход состояния отклонён: %s", exc)
        if review_opened:
            log.warning("ПОРОГ БЛОКИРОВКИ ДОСТИГНУТ (%.1f). Экзамен приостановлен, "
                        "блокировку подтверждает проктор: команда proctor_lock "
                        "или proctor_release. Код сессии: %s",
                        score, self.session.code_display or "—")
        log.info("вердикт %s -> %s (%.1f): %s", prev.value, action.value, score, reason)

    async def _open_lock_review(self, score: float,
                                breakdown: list[dict[str, Any]]) -> None:
        """Открыть запрос решения проктора и зафиксировать его в журнале.

        Запись идёт и событием (попадает в хронологию отчёта), и записью
        `control` цепочки: в отчёте должно быть видно не только то, что риск
        дошёл до порога, но и то, что система на этом ОСТАНОВИЛАСЬ.
        """
        top = [str(row.get("label") or row.get("kind") or "") for row in breakdown[:3]]
        review = {
            "ts": time.time(),
            "score": round(float(score), 2),
            "threshold": float(self.cfg.risk_lock),
            "session_code": self.session.session_code,
            "session_code_display": self.session.code_display,
            "top": [item for item in top if item],
            # Текст на экран студенту. Он обязан объяснять, ЧТО произошло и
            # ЧТО будет дальше, и не называть студента нарушителем: решения
            # ещё нет, и принимать его будет человек.
            "explanation": (
                "Экзамен приостановлен автоматически: накопленные наблюдения "
                "достигли порога, при котором требуется решение проктора. "
                "Это не решение о нарушении. Сообщите проктору код сессии "
                f"{self.session.code_display or self.session.session_id}: он "
                "посмотрит запись и либо продолжит экзамен, либо закроет его."
            ),
        }
        self._lock_review = review
        await self._store_control("lock_review_requested", review)
        await self._emit(self._system_event(EventKind.LOCK_REVIEW_REQUESTED, {
            "score": review["score"],
            "threshold": review["threshold"],
            "lock_policy": self.lock_policy,
            "top": review["top"],
            "session_code": review["session_code"],
        }))

    def _verdict_reason(self, action: VerdictAction, breakdown: list[dict[str, Any]],
                        requested: VerdictAction | None = None) -> str:
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
        # Приостановка по достижении порога блокировки — ОСОБЫЙ случай, и
        # называть её просто «приостановлен» нельзя: студент и проктор должны
        # видеть, что дальше требуется действие человека, а не ожидание спада.
        if requested is VerdictAction.LOCK and action is not VerdictAction.LOCK:
            prefix = "Экзамен приостановлен, требуется решение проктора"
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
            # Дозревшие окна сырого слоя. Без этого последнее окно каждого
            # канала висело бы открытым до следующего наблюдения: пропал
            # сигнал — и в журнале нет записи именно про тот отрезок, где он
            # пропал, то есть ровно про самый интересный.
            with contextlib.suppress(Exception):
                await self._store_observations(self.raw_log.due(time.time()))
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
        if self._calib_reset_pending:
            # новая сессия: чужая база и чужая карта хуже, чем никакой
            self._calib_reset_pending = False
            if self.gaze_calib is not None:
                with contextlib.suppress(Exception):
                    self.gaze_calib.reset()
        with self._calib_lock:
            job = self._calib
            if job is None or job.finished:
                return

        if job.supersedes is not None:
            prev, job.supersedes = job.supersedes, None
            self._close_superseded(prev)
        stage = job.stage
        if stage.startswith("gaze"):
            self._gaze_quiet(ts)
        if not job.begun:
            job.begun = True
            self._begin_job(job)
        if job.finalize:
            # лицо для расчёта не нужно: карта строится по уже собранному
            self._finalize_grid(job)
            return
        if stage in ("gaze_center", "gaze_grid"):
            if face_obs is None or int(_get(face_obs, "face_count", 0) or 0) < 1:
                return
            settle = (self.cfg.calibration_settle_center if stage == "gaze_center"
                      else self.cfg.calibration_settle_grid)
            if ts - job.started < settle:
                return              # глаза ещё в пути к новой цели
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
        self._finish_identity(job)

    def _begin_job(self, job: _CalibrationJob) -> None:
        """Начало стадии — в CV-потоке, на первом кадре задания."""
        gc = self.gaze_calib
        if gc is None or job.finalize or not job.stage.startswith("gaze"):
            return
        try:
            if job.stage == "gaze_center":
                gc.begin_center()
            else:
                gc.begin_point(job.point, fresh=True)
        except Exception as exc:
            log.warning("не удалось начать стадию %s: %s", job.stage, exc)

    def _close_superseded(self, prev: _CalibrationJob) -> None:
        """Оболочка ушла со стадии по своему таймеру раньше, чем та набрала цель.

        Молча выбросить собранное нельзя: без итога центра пороги не дошли бы
        до детектора, а без итога эталона лица не было бы и эталона. Поэтому
        стадия закрывается тем, что успело собраться, с пометкой superseded.
        """
        if prev.finished:
            return
        if not prev.begun:
            self._finish_calibration(prev, {"ok": False, "superseded": True,
                                            "reason": "стадия не успела начаться"})
            return
        if prev.stage == "identity":
            self._finish_identity(prev, superseded=True)
            return
        result: dict[str, Any] = {"ok": True, "samples": prev.count, "superseded": True}
        gc = self.gaze_calib
        try:
            if gc is not None:
                computed = gc.finish_center()
                if isinstance(computed, dict):
                    result.update(computed)
            else:
                result["degraded"] = True
                if prev.samples and hasattr(self.face, "calibrate_center"):
                    computed = self.face.calibrate_center(prev.samples)
                    if isinstance(computed, dict):
                        result.update(computed)
        except Exception as exc:
            log.warning("досчёт центра по уходу оболочки упал: %s", exc)
            result.update({"ok": False, "reason": f"не удалось посчитать центр: {exc}"})
        if result.get("ok") and gc is not None:
            self._apply_gaze_calibration(result)
        self._finish_calibration(prev, result)

    def _gaze_quiet(self, now: float) -> None:
        """Продлить тишину взгляда, не выходя за бюджет сессии."""
        with self._calib_lock:
            until = now + float(self.cfg.calibration_gaze_quiet)
            if until <= self._gaze_quiet_until:
                return
            add = until - max(self._gaze_quiet_until, now)
            budget = float(self.cfg.calibration_gaze_quiet_budget)
            if self._gaze_quiet_spent + add > budget:
                if self._gaze_quiet_spent < budget:
                    log.warning("бюджет тишины взгляда (%.0f с) исчерпан: дальше взгляд "
                                "оценивается и во время калибровки", budget)
                    self._gaze_quiet_spent = budget
                return
            self._gaze_quiet_spent += add
            self._gaze_quiet_until = until

    def _finish_identity(self, job: _CalibrationJob, superseded: bool = False) -> None:
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
        if superseded:
            result["superseded"] = True
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
                collecting = time.time() - job.started - self.cfg.calibration_settle_grid
                ready = bool(gc.point_ready()) and \
                    collecting >= self.cfg.calibration_grid_min_collect
        except Exception as exc:
            log.warning("калибровка (%s) отказала: %s", job.stage, exc)
            return

        progress = {}
        with contextlib.suppress(Exception):
            progress = dict(gc.progress() or {})
        self._report_calibration_progress(job, job.count, progress)
        if not ready:
            return

        if job.stage == "gaze_grid":
            # Закрываем только эту точку. Карту строит _finalize_grid() по
            # сигналу конца обхода: ждать здесь «все точки набрали кадры»
            # нельзя — точка, попавшая на моргание, не наберёт их никогда,
            # а оболочка к тому времени уже показывает следующую.
            self._finish_calibration(job, {"ok": True, "samples": job.count,
                                           "point": job.point, "point_done": True},
                                     done=False,
                                     progress=float(progress.get("progress", 0.0) or 0.0))
            return

        result: dict[str, Any] = {"ok": True, "samples": job.count}
        try:
            computed = gc.finish_center()
            if isinstance(computed, dict):
                result.update(computed)
        except Exception as exc:
            log.warning("расчёт калибровки (%s) упал: %s", job.stage, exc)
            result["ok"] = False
            result["reason"] = f"не удалось посчитать калибровку: {exc}"
        if result.get("ok"):
            self._apply_gaze_calibration(result)
        self._finish_calibration(job, result)

    def _finalize_grid(self, job: _CalibrationJob) -> None:
        """Обход сетки закончен: обучить карту экрана по собранным точкам.

        Точки, не набравшие `min_grid_samples`, GazeCalibration отбрасывает сам;
        от 6 точек строится poly2, от 3 — аффинная карта, меньше — карты нет.
        """
        result: dict[str, Any] = {"ok": True, "all_points_done": True}
        gc = self.gaze_calib
        if gc is None:
            result.update({"degraded": True, "screen_map_applied": False,
                           "reason": "модуль калибровки недоступен, карта экрана не строится"})
            self._finish_calibration(job, result)
            return
        try:
            computed = gc.finish_grid()
            if isinstance(computed, dict):
                result.update(computed)
        except Exception as exc:
            log.warning("расчёт карты экрана упал: %s", exc)
            result.update({"ok": False, "screen_map_applied": False,
                           "reason": f"не удалось построить карту экрана: {exc}"})
        if result.get("ok"):
            self._apply_gaze_calibration(result)
        self._finish_calibration(job, result)

    def _apply_gaze_calibration(self, result: dict[str, Any]) -> None:
        """Отдать пороги (и карту, если ей можно верить) детектору, сохранить JSON."""
        gc = self.gaze_calib
        with contextlib.suppress(Exception):
            result["applied"] = bool(gc.attach(self.face))
        with contextlib.suppress(Exception):
            result["quality"] = dict(gc.quality() or {})
        with contextlib.suppress(Exception):
            usable = bool(gc.screen_map_usable())
            result["screen_map_applied"] = usable
            if not usable and result.get("screen_map", {}).get("kind"):
                result["screen_map_reason"] = ("карта неточная: детектор остаётся на "
                                               "персональных порогах")
        if self.session.dir is not None:
            with contextlib.suppress(Exception):
                saved = gc.save(str(self.session.dir))
                if saved:
                    result["saved"] = str(Path(saved).name)

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
            # итог этапа — на сигнале конца обхода (_finalize_grid)
            self._finish_calibration(job, result, done=False, progress=0.0)
            return
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
            "result": {"samples": count,
                       **({"point": job.point} if job.stage == "gaze_grid" else {}),
                       **({"quality": extra["result"]}
                          if isinstance(extra.get("result"), dict) else {})},
        }
        self._post(("calibration", payload))

    def _finish_calibration(self, job: _CalibrationJob, result: dict[str, Any],
                            done: bool = True, progress: float = 1.0) -> None:
        """Закрыть задание калибровки (потокобезопасно, ровно один раз).

        done=False — закрыта только точка сетки: оболочке уходит прогресс, этап
        продолжается, CALIBRATION_DONE в журнал не пишется.
        """
        with self._calib_lock:
            if job.finished:
                return
            job.finished = True
            if self._calib is job:
                self._calib = None
        payload = {"stage": job.stage, "progress": 1.0 if done else round(progress, 2),
                   "done": done, "result": result}
        self._post(("calibration", payload))
        if done:
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

    def _reset_calibration(self) -> None:
        """Калибровка принадлежит студенту: новая сессия начинает без неё.

        Иначе следующий студент шёл бы на базе и карте предыдущего, а при
        оборванной калибровке — весь экзамен. GazeCalibration сбрасывает
        CV-поток (единственный, кто его трогает); детектор — сразу.
        """
        with self._calib_lock:
            self._calib = None
            self._gaze_quiet_until = 0.0
            self._gaze_quiet_spent = 0.0
        self._calib_reset_pending = True
        clear = getattr(self.face, "clear_calibration", None)
        if callable(clear):
            with contextlib.suppress(Exception):
                clear()

    def _after_calibration_stage(self, payload: dict[str, Any]) -> None:
        """Стадия закрыта: вернуть сессию в running, когда калибровка действительно закончена."""
        result = payload.get("result")
        result = result if isinstance(result, dict) else {}
        if payload.get("stage") == "gaze_grid" and not result.get("all_points_done"):
            return  # сетка идёт дальше, оболочка пришлёт следующую точку
        if result.get("superseded"):
            return  # досчёт стадии, с которой оболочка уже ушла: идёт следующая
        if self.session.state is SessionState.CALIBRATING:
            with contextlib.suppress(SessionStateError):
                self.session.transition(SessionState.RUNNING, "калибровка завершена")

    def _check_calibration_timeout(self) -> None:
        """Калибровка не должна висеть вечно: по таймауту закрываем честным провалом."""
        with self._calib_lock:
            job = self._calib
            if job is None or job.finished:
                return
            # расчёт карты — один кадр CV-потока; оболочка ждёт его 4 с, и
            # карта, применённая позже, чем студенту сказали «не построена»,
            # хуже честного провала
            limit = 3.0 if job.finalize else self.cfg.calibration_timeout
            expired = time.time() - job.started >= limit
        if not expired:
            return
        if job.stage == "gaze_grid" and not job.finalize:
            # точка сетки не набрала кадров — это не провал этапа: собранное
            # пойдёт в карту, решение примет _finalize_grid()
            self._finish_calibration(job, {"ok": False, "point": job.point,
                                           "reason": "точка не набрала кадров"},
                                     done=False, progress=0.0)
            return
        self._finish_calibration(job, {
            "ok": False, "samples": len(job.samples),
            "reason": "калибровка не завершена: не хватило кадров с лицом",
            **({"all_points_done": True, "screen_map_applied": False} if job.finalize else {}),
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
    def _should_save_frame(self, ev: ProctorEvent) -> bool:
        """Правило КАДРА: severity не ниже `evidence_frame_min_severity`.

        По умолчанию "info", то есть кадр снимается к любому неслужебному
        событию. Служебные записи и запись вне сессии отсекает
        `_frame_skip_reason`, у неё же коды причин для отчёта.
        """
        if not self.cfg.save_evidence or not self.session.recording:
            return False
        floor_name = getattr(self.cfg, "evidence_frame_min_severity", Severity.INFO.value)
        floor = SEVERITY_ORDER.get(str(floor_name), 0)
        return SEVERITY_ORDER.get(ev.severity.value, 0) >= floor

    def _should_save_clip(self, ev: ProctorEvent, face_count: int,
                          multi_ts: float | None = None) -> str:
        """Правило КЛИПА. Возвращает причину НЕ писать клип ("" — писать).

        Клип пишется только при severity не ниже `evidence_min_severity`,
        никогда для `_NO_CLIP_KINDS`, никогда без детектора лиц и никогда,
        если в кадре больше одного лица: размывать посторонних рекордер умеет
        только на снимке, а клип уехал бы на диск с их биометрией. «В кадре»
        понимается с запасом — второе лицо, мелькнувшее в последние
        `evidence_clip_seconds`, тоже закрывает клип: его 10 с «до» берутся из
        буфера, и посторонний, ушедший секунду назад, в них есть. Вошедшего
        ПОСЛЕ события ловит `_note_faces` -> `_cancel_pending_clips`.

        Без FaceMesh (нет mediapipe, канал выключен флагом) лица считать
        нечем: «одно лицо» не доказано, клип не пишется (`faces_unknown`).

        `multi_ts` — `_multi_face_ts`, если вызывающий уже держит `_frame_lock`
        (лок не реентерабельный); None — прочитать самому.
        """
        floor = SEVERITY_ORDER.get(self.cfg.evidence_min_severity, 2)
        if SEVERITY_ORDER.get(ev.severity.value, 0) < floor:
            return "severity"
        if ev.kind in self._NO_CLIP_KINDS:
            return "kind"
        if self.face is None:
            return CLIP_SKIP_FACES_UNKNOWN
        window = max(float(self.cfg.evidence_clip_seconds or 0.0), 0.0)
        if multi_ts is None:
            with self._frame_lock:
                multi_ts = self._multi_face_ts
        if face_count > 1 or (multi_ts and time.time() - multi_ts <= window):
            return CLIP_SKIP_MULTIPLE_FACES
        return ""

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
    #: То же правило для ЛЮБОГО инцидента с двумя лицами в кадре — в
    #: `_should_save_clip`.
    _NO_CLIP_KINDS = (EventKind.SECOND_FACE,)

    def _attach_evidence(self, events: list[ProctorEvent], frame: Any, face_bbox: Any,
                         faces: tuple[Any, list[Any], int] | None = None,
                         frame_ts: float | None = None) -> None:
        """Сохранить кадр (и клип) инцидента, прописать пути в событие.

        Кроп делается только по bbox предмета-повода (телефон, книга). Для
        инцидентов про людей кроп не снимается — доказательством является
        полный кадр. На КАЖДОМ кадре размываются все лица, кроме основного
        (самого крупного): посторонний в кадре во время WINDOW_BLUR — такая же
        чужая биометрия, как во время SECOND_FACE. Сверх рамок FaceMesh кадр
        проходит каскад Хаара (`_fallback_blur`): FaceMesh видит не больше
        `max_num_faces` лиц и пропускает мелкие. Клип — по `_should_save_clip`;
        если каскад нашёл второе лицо, клипа к событию нет.

        `faces` — (bbox основного, bbox прочих, число лиц) для этого кадра;
        None — взять последний учтённый (`_last_faces`). Не удалось записать —
        в `detail["evidence_skipped"]` ложится причина, а не тишина.
        """
        if faces is None:
            with self._frame_lock:
                faces = self._last_faces
        _primary, others, face_count = faces
        shot_ts = float(frame_ts) if frame_ts else None
        # Запасное размытие (каскад Хаара) — один раз на кадр, а не на событие.
        fallback: tuple[list[Any], str, int] | None = None
        for ev in events:
            if ev.evidence is not None:
                continue
            reason = self._frame_skip_reason(ev)
            if reason:
                self._mark_evidence_skipped(ev, reason)
                continue
            if frame is None:
                self._mark_evidence_skipped(ev, self._no_frame_reason())
                continue
            target = self.session.evidence_file(ev.kind.value.lower())
            if target is None:
                self._mark_evidence_skipped(ev, EVIDENCE_SKIP_WRITE_FAILED)
                continue
            path, rel = target
            box = ev.detail.get("bbox") if ev.kind in self._OBJECT_KINDS else None
            if fallback is None:
                fallback = self._fallback_blur(frame, faces, shot_ts)
            extra_faces, blur_mode, new_faces, subject = fallback
            # Рамки FaceMesh и каскада одного и того же лица размываются обе
            # (они разной формы), а считается лицо один раз. Лицо студента
            # (`subject`) остаётся резким: запас вокруг чужих рамок на него не
            # заходит.
            size = (int(frame.shape[1]), int(frame.shape[0]))
            own = self._blur_regions(ev, others, subject, size)
            blur = own + self._blur_regions(None, extra_faces, subject, size)
            # Подпись на кадре — латиницей: cv2.putText кириллицу не рисует.
            caption = " ".join(part for part in (str(ev.detail.get("code") or ""),
                                                 ev.kind.value) if part)
            stamp = shot_ts or ev.ts
            extra: dict[str, Any] = {"frame_ts": stamp}
            if blur:
                extra["blurred_faces"] = len(own) + new_faces
            if blur_mode:
                extra["blur"] = blur_mode

            if self.recorder is not None:
                saved = self._safe_call(self.recorder.save_snapshot, frame, box, path,
                                        caption, blur, stamp, what="снимок")
                if not isinstance(saved, dict):
                    self._mark_evidence_skipped(ev, EVIDENCE_SKIP_WRITE_FAILED)
                    continue
                clip = None
                # Правило клипа и регистрация — под одним `_frame_lock`, тем же,
                # под которым `_note_faces` отмечает второе лицо и снимает
                # незакрытые клипы: иначе клип, зарегистрированный рабочим
                # потоком между этими двумя шагами CV-потока, не был бы снят.
                with self._frame_lock:
                    no_clip = self._should_save_clip(ev, face_count, self._multi_face_ts)
                    if not no_clip:
                        clip = self._safe_call(self.recorder.save_clip, ev.id, None, None,
                                               None, ev.ts, ev.kind.value, what="клип")
                if no_clip in (CLIP_SKIP_MULTIPLE_FACES, CLIP_SKIP_FACES_UNKNOWN):
                    extra["clip_skipped"] = no_clip
                extra.update({"crop_path": saved.get("crop_rel") or "",
                              "saved_at": time.time()})
                ev.evidence = Evidence(
                    frame_path=saved.get("frame_rel") or rel,
                    clip_path=str(clip) if clip else None,
                    bbox=saved.get("bbox"),
                    extra=extra,
                )
                self._mark_evidence_skipped(ev, "")
                continue

            # рекордера нет — пишем одиночный кадр сами, без кропов и клипов,
            # но посторонних размываем так же
            try:
                import cv2  # type: ignore
            except Exception as exc:
                log.debug("кадр-доказательство не сохранён: нет opencv (%s)", exc)
                self._mark_evidence_skipped(ev, EVIDENCE_SKIP_NO_CAMERA)
                continue
            try:
                image = _blurred(cv2, frame, blur)
                path.parent.mkdir(parents=True, exist_ok=True)
                ok = cv2.imwrite(str(path), image,
                                 [int(cv2.IMWRITE_JPEG_QUALITY),
                                  int(self.cfg.evidence_jpeg_quality)])
            except Exception as exc:
                log.debug("кадр-доказательство не сохранён: %s", exc)
                ok = False
            if not ok:
                self._mark_evidence_skipped(ev, EVIDENCE_SKIP_WRITE_FAILED)
                continue
            extra["saved_at"] = time.time()
            ev.evidence = Evidence(
                frame_path=rel,
                bbox=[float(v) for v in box] if isinstance(box, (list, tuple)) and len(box) >= 4 else None,
                extra=extra,
            )
            self._mark_evidence_skipped(ev, "")

    def _fallback_planner(self) -> Any:
        """`storage.evidence.plan_fallback_blur` — лениво, один раз; None, если нет."""
        if not self._fallback_tried:
            self._fallback_tried = True
            plan = self._import_attr("storage.evidence", "plan_fallback_blur")
            available = self._import_attr("storage.evidence", "fallback_available")
            ok = False
            if callable(plan) and callable(available):
                with contextlib.suppress(Exception):
                    ok = bool(available())
            if ok:
                self._fallback_plan = plan
            else:
                why = ""
                error = self._import_attr("storage.evidence", "fallback_error")
                if callable(error):
                    with contextlib.suppress(Exception):
                        why = str(error() or "")
                log.warning("запасной детектор лиц (каскад Хаара) недоступен%s: без "
                            "FaceMesh лица на кадрах доказательств НЕ размываются, "
                            "в доказательстве это помечается blur=unavailable",
                            f" ({why})" if why else "")
        return self._fallback_plan

    def _fallback_blur(self, frame: Any, faces: tuple[Any, list[Any], int],
                       frame_ts: float | None = None) -> tuple[list[Any], str, int, Any]:
        """Запасное размытие каскадом Хаара: (рамки, `extra.blur`, сколько лиц
        новых, рамка лица студента или None).

        Рамки — все лица каскада, кроме основного; «новых» — сколько из них
        FaceMesh не видел (для счётчика `blurred_faces`).

        Каскад проходит КАЖДЫЙ кадр, который уходит на диск, а не только кадр
        без FaceMesh или с двумя лицами. FaceMesh видит не больше
        `max_num_faces` (2), без mediapipe не видит никого, а и при одном
        найденном лице пропускает других: на кадре с тремя лицами разного
        размера он насчитал одно, хотя каскад нашёл все три. Цена — десятки
        миллисекунд на сохраняемый кадр, то есть на инцидент, а не на кадр
        потока.

        Метка: "haar" — каскад отработал, его лица (кроме основного) добавлены
        к размытию; "unavailable" — каскада нет, кадр пишется как есть сверх
        рамок FaceMesh, и это видно в доказательстве.

        Каскад видит в кадре больше одного лица (вместе с основным от
        FaceMesh) — это то же «второе лицо в кадре»: отметка `_multi_face_ts`
        и снятие незакрытых клипов.
        """
        if frame is None:
            return [], "", 0, None
        primary, others, _face_count = faces
        planner = self._fallback_planner()
        if planner is None:
            return [], BLUR_UNAVAILABLE, 0, primary
        # FaceMesh работал и лица не нашёл — крупнейшее лицо каскада может
        # оказаться посторонним, поэтому основного нет и размываются все.
        face_ran = self.face is not None
        try:
            plan = planner(frame, primary, list(others or []), face_ran=face_ran)
        except Exception as exc:
            log.warning("каскад Хаара упал на кадре: %s", exc)
            return [], BLUR_UNAVAILABLE, 0, primary
        if not isinstance(plan, dict) or not plan.get("available"):
            return [], BLUR_UNAVAILABLE, 0, primary
        boxes = [list(b) for b in (plan.get("others") or [])]
        subject = 1 if (primary is not None or plan.get("primary") is not None) else 0
        if len(boxes) + subject >= 2:
            with self._frame_lock:
                self._multi_face_ts = max(self._multi_face_ts,
                                          float(frame_ts or time.time()))
                self._cancel_pending_clips(CLIP_CANCEL_MULTIPLE_FACES_AFTER)
        subject_box = primary if primary is not None else plan.get("primary")
        return boxes, BLUR_HAAR, int(plan.get("new") or 0), subject_box

    def _note_faces(self, face: Any, ts: float) -> None:
        """Запомнить лица разобранного кадра для размытия и правила клипа."""
        boxes = [list(b) for b in (_get(face, "faces", []) or [])
                 if isinstance(b, (list, tuple)) and len(b) >= 4]
        primary = _get(face, "face_bbox")
        count = int(_get(face, "face_count", 0) or 0)
        if not boxes and isinstance(primary, (list, tuple)) and len(primary) >= 4:
            boxes = [list(primary)]
        others = boxes[1:]
        second = _get(face, "second_face_bbox")
        if (not others and count >= 2 and isinstance(second, (list, tuple))
                and len(second) >= 4):
            others = [list(second)]
        count = max(count, len(boxes))
        with self._frame_lock:
            self._last_faces = (primary, others, count)
            if count >= 2:
                self._multi_face_ts = max(self._multi_face_ts, float(ts or time.time()))
                # Посторонний вошёл, пока у клипов набирается «после»: их
                # снимаем, файлы не пишутся (запись `clip_cancelled` в цепочке).
                # Кадр уходит в буфер рекордера после этого вызова (`_cv_loop`),
                # поэтому в снятые клипы он попасть не успевает.
                self._cancel_pending_clips(CLIP_CANCEL_MULTIPLE_FACES_AFTER)

    @staticmethod
    def _blur_regions(ev: ProctorEvent | None, others: list[Any] | None = None,
                      keep: Any = None, size: tuple[int, int] | None = None) -> list[Any]:
        """Лица посторонних, которые нужно размыть перед записью кадра.

        Все лица кадра, кроме основного (`others`), плюс bbox второго лица из
        самого SECOND_FACE: факт присутствия второго человека фиксируется,
        биометрия нет. Рамка FaceMesh идёт по точкам сетки (от бровей до
        подбородка), поэтому расширяется с запасом — волосы и уши тоже
        узнаваемы.

        `keep` — рамка лица студента, `size` — (ширина, высота) кадра. С ними
        запас вокруг чужой рамки не заходит на лицо студента (тогда берётся
        рамка без запаса), а рамка, покрывающая заметную часть его лица, —
        это та же голова, и она не размывается.
        """
        keep_px = _norm_box(keep, size[0], size[1]) if keep is not None and size else None
        raw: list[Any] = list(others or [])
        if ev is not None and ev.kind is EventKind.SECOND_FACE:
            extra = ev.detail.get("second_face_bbox") or ev.detail.get("other_face_bbox")
            if isinstance(extra, (list, tuple)) and len(extra) >= 4:
                raw.append(extra)
        regions: list[Any] = []
        seen: set[tuple[int, ...]] = set()
        for box in raw:
            try:
                x, y, w, h = (float(v) for v in list(box)[:4])
            except (TypeError, ValueError):
                continue
            key = tuple(int(round(v * 1000 if max(w, h) <= 1.5 else v)) for v in (x, y, w, h))
            if w <= 0 or h <= 0 or key in seen:
                continue  # bbox из SECOND_FACE обычно совпадает с одним из `others`
            seen.add(key)
            if keep_px is not None:
                own = _norm_box([x, y, w, h], size[0], size[1])
                if own is None:
                    continue
                if _overlap_share(own, keep_px) >= 0.3:
                    continue  # та же голова, что у студента: не размываем
                ox, oy, ow, oh = own
                pad_x, pad_y = 0.25 * ow, 0.35 * oh
                padded = [ox - pad_x, oy - pad_y, ow + 2 * pad_x, oh + 2 * pad_y]
                padded_px = _norm_box(padded, size[0], size[1])
                regions.append(list(own) if padded_px is not None
                               and _overlap_share(padded_px, keep_px) > 0 else padded)
                continue
            pad_x, pad_y = 0.25 * w, 0.35 * h
            regions.append([x - pad_x, y - pad_y, w + 2 * pad_x, h + 2 * pad_y])
        return regions

    def _snapshot(self) -> str | None:
        """Снимок текущего кадра по команде оболочки (блокирующий, через to_thread)."""
        with self._frame_lock:
            frame, frame_ts = self._last_frame, self._last_frame_ts
            faces = self._last_faces
        if frame is None and self.capture is not None:
            frame, frame_ts = self.capture.peek()
        if frame is None:
            return None
        target = self.session.evidence_file("snapshot")
        if target is None:
            return None
        path, _rel = target
        # Посторонние размываются и здесь: снимок по команде — тоже кадр,
        # который уходит в каталог сессии. Тот же запасной каскад, что у кадров
        # к событиям; записи о снапшоте в цепочке нет, поэтому «размыть нечем»
        # говорится в логе.
        extra_faces, blur_mode, _new, subject = self._fallback_blur(frame, faces, frame_ts)
        if blur_mode == BLUR_UNAVAILABLE:
            log.warning("снапшот: лица сверх увиденных FaceMesh не размыты — "
                        "запасного детектора лиц нет")
        regions = self._blur_regions(None, list(faces[1] or []) + extra_faces, subject,
                                     (int(frame.shape[1]), int(frame.shape[0])))
        if self.recorder is not None:
            # Подпись латиницей (putText).
            saved = self._safe_call(self.recorder.save_snapshot, frame, None, path,
                                    "SNAPSHOT", regions, time.time(), what="снапшот")
            if isinstance(saved, dict):
                return str(saved.get("frame_path") or path)
            return None
        try:
            import cv2  # type: ignore
            path.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(path), _blurred(cv2, frame, regions),
                               [int(cv2.IMWRITE_JPEG_QUALITY),
                                int(self.cfg.evidence_jpeg_quality)]):
                raise OSError("cv2.imwrite вернул False")
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
        # Конфиг нужен отчёту ради двух вещей: путь к ключу подписи и МЕТКА
        # подписи (self_signed / institution). Без метки отчёт объявлял бы
        # подпись ключом студента доказательством авторства.
        try:
            if "config" in inspect.signature(build).parameters:
                path = await asyncio.to_thread(build, session_dir, db_path, out_html,
                                               self.cfg_dict)
            else:
                path = await asyncio.to_thread(build, session_dir, db_path, out_html)
        except Exception as exc:
            log.warning("отчёт не собрался: %s", exc)
            await self._error("report", f"Не удалось собрать отчёт: {exc}")
            return
        log.info("отчёт готов: %s", path or out_html)
        if self.cfg.sign_report and self.cfg.report_signing_key:
            sign = getattr(self.report_mod, "sign_report", None)
            if sign is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(sign, path or out_html,
                                            self.cfg.report_signing_key,
                                            self.cfg.signature_authority)

    async def _build_package(self, summary: dict[str, Any],
                             session_dir: str = "") -> dict[str, Any]:
        """Собрать `<session_id>.proctor.zip` — то, что проктор физически забирает.

        Внутри: отчёт, подпись, публичный ключ, журнал, кадры, метаданные и
        `manifest.json` со списком файлов и их sha256. Манифест подписан тем же
        ключом, что и отчёт, поэтому подписанный манифест закрывает весь пакет.

        Неудача сборки НЕ роняет завершение сессии: отчёт и журнал уже лежат в
        каталоге, и потеря архива не должна превращаться в потерю сессии. Факт
        неудачи фиксируется в `handover.json`, в `summary.json`, в логе и
        сообщением оболочке.

        Почему неудача не попадает в хеш-цепочку: к этому моменту цепочка уже
        закрыта записью `session_close`. Дописать в неё что-то после закрытия
        означало бы сломать ровно то свойство, ради которого цепочка есть, —
        такая запись выглядела бы как правка журнала задним числом.
        """
        sdir = session_dir or str(summary.get("session_dir") or "")
        if not sdir:
            return {}
        if not self.cfg.package_on_end:
            log.info("пакет доказательств не собирается (--no-package): "
                     "забирайте каталог сессии целиком — %s", sdir)
            return {}
        if handover_mod is None:
            await self._error("package", "Модуль передачи доказательств недоступен: "
                                         "пакет не собран, скопируйте каталог сессии "
                                         "целиком")
            return {}

        try:
            result = await asyncio.to_thread(
                handover_mod.build_package,
                sdir, None, self.session.meta_file(), summary,
                self._chain_state(), dict(self.handover),
                self.cfg.signature_authority, self.session.session_code,
                self.cfg.report_signing_key or None, True,
            )
            payload = result.to_dict()
        except Exception as exc:  # сборка пакета не имеет права ронять сессию
            log.exception("сборка пакета доказательств упала")
            payload = {"ok": False, "reason": str(exc),
                       "message": f"Пакет не собран: {exc}"}

        # Итог доставки дописывается в ЭТОТ словарь — в тот пакет, который
        # копировали. К концу копирования `last_package` может быть уже другим
        # (пересборка, следующая сессия), и чужой итог там был бы неправдой.
        pkg = dict(payload)
        self.last_package = pkg
        self._package_dir = sdir
        note = {
            "created_at": handover_mod.now_iso(),
            "session_id": str(summary.get("session_id") or ""),
            "session_code": self.session.session_code,
            "session_code_display": self.session.code_display,
            "handover": dict(self.handover),
            "signature_authority": self.cfg.signature_authority,
            "package": payload,
        }
        # Пересборка (export_report) переписывает записку целиком, но прежние
        # попытки доставки терять нельзя: «первая копия не ушла» — факт. Под
        # замком: поздний итог фоновой доставки пишет в тот же файл.
        async with self._note_lock:
            with contextlib.suppress(Exception):
                prev = await asyncio.to_thread(handover_mod.read_handover_note, sdir)
                attempts = prev.get("delivery_attempts")
                if isinstance(attempts, list) and attempts:
                    note["delivery_attempts"] = attempts
            with contextlib.suppress(Exception):
                await asyncio.to_thread(handover_mod.write_handover_note, sdir, note)

        if payload.get("ok"):
            for line in str(payload.get("message") or "").splitlines():
                log.info("%s", line)
        else:
            for line in str(payload.get("message")
                            or payload.get("reason") or "").splitlines():
                log.warning("пакет доказательств: %s", line)
            await self._error("package", (
                f"Пакет доказательств не собран: "
                f"{payload.get('reason') or 'причина не определена'}. "
                f"Отчёт и журнал на месте — забирайте каталог сессии целиком: {sdir}"))

        # Доставка в папку проктора — ПОСЛЕ записки о пакете: итог дописывается
        # в тот же handover.json. Только по завершённой сессии: export_report
        # посреди экзамена пересобирает пакет локально, но с машины во время
        # экзамена не уходит ничего — так сказано на экране согласия.
        # Копирование идёт ФОНОМ (`_schedule_delivery`): завершение сессии его
        # не ждёт, и зависшая шара не держит ни канал оболочки, ни финальный
        # экран. Итог — и `timed_out`, если мягкий срок вышел, — уходит
        # оболочке отдельным status.
        if self.delivery.get("configured"):
            if self.session.active:
                log.info("сессия идёт: пакет в папку проктора не копируется до её "
                         "завершения")
            elif payload.get("ok"):
                self._schedule_delivery(pkg, sdir)
            else:
                failed = {"ok": False, "dest": "", "bytes": 0, "sha256": "",
                          "verified": False, "at": time.time(),
                          "error": ("пакет не собран — копировать в папку проктора "
                                    "нечего: " + str(payload.get("reason")
                                                     or "причина не определена"))}
                await self._settle_delivery(pkg, sdir, "", failed)
        return dict(pkg)

    @property
    def _delivering(self) -> bool:
        """Идёт копирование в папку проктора — в том числе после мягкого срока."""
        task = self._delivery_task
        return task is not None and not task.done()

    def _schedule_delivery(self, pkg: dict[str, Any], session_dir: str) -> None:
        """Запустить доставку пакета фоном; идёт предыдущая — встать за ней."""
        prev = self._delivery_task if self._delivering else None
        self._delivery_task = asyncio.create_task(
            self._deliver(pkg, session_dir, prev), name="deliver")

    async def _settle_delivery(self, pkg: dict[str, Any], session_dir: str,
                               package: str, result: dict[str, Any], *,
                               replace: bool = False, attempt: bool = True,
                               running: bool = False) -> None:
        """Итог доставки — в словарь пакета, в `handover.json` и оболочке.

        `replace` — поздний итог попытки, уже записанной как `timed_out`: он
        заменяет ту запись. `attempt=False` — копии не было (`skipped`).
        `running` — копирование после этого итога ещё идёт (`timed_out`).
        Ошибкой (`error`) неудачу не рассылаем: оболочка показывает её строкой
        на финальном экране по `status`, а всплывающее «Сбой наблюдения» здесь
        было бы неправдой — наблюдение ни при чём.
        """
        pkg["delivery"] = result
        if session_dir and handover_mod is not None:
            async with self._note_lock:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(handover_mod.record_delivery, session_dir,
                                            result, package, replace, attempt)
        with contextlib.suppress(Exception):
            msg = self._status_message()
            if not running and self._delivery_task is asyncio.current_task():
                # Итог шлёт сама задача доставки, пока она формально «идёт»:
                # копирование уже кончилось, и status с итогом не должен
                # говорить in_progress:true (кнопка повтора была бы выключена
                # до следующего тика статуса).
                msg["delivery"]["in_progress"] = False
            await self._broadcast(msg)

    async def _deliver(self, pkg: dict[str, Any], session_dir: str,
                       prev: asyncio.Task[Any] | None = None) -> dict[str, Any]:
        """Скопировать пакет `pkg` в папку проктора, записать итог и сказать о нём.

        Копирует `handover.deliver_package`: исключительное создание, соседнее
        имя при занятом, fsync, сверка sha256 перечитыванием. Итог — в
        `pkg["delivery"]` (оттуда в `status.package.delivery`) и в
        `handover.json` каталога сессии (`delivery` + `delivery_attempts`).

        Мягкий срок — `deliver_timeout_sec`: не уложились — итогом становится
        `timed_out` («папка проктора не отвечает N с — пакет лежит здесь: …»),
        копирование идёт дальше, и поздний итог заменяет этот и в status, и в
        `handover.json`. Копия идёт в отдельном фоновом (daemon) потоке, а не в
        пуле `asyncio.to_thread`: поток пула процесс при выходе дожидается, и
        зависшая шара держала бы выход ядра сколько угодно.
        """
        if prev is not None:
            # Две копии одновременно не делаем: пересборка или повтор во время
            # идущего копирования ждут его окончания (и его итога ниже).
            await asyncio.wait({prev})
        package = str(pkg.get("package") or "")
        key = os.path.abspath(session_dir) if session_dir else ""
        prior = pkg.get("delivery") or {}
        if prior.get("ok"):
            # Этот самый пакет уже доставлен: второй копии нет.
            return dict(prior)
        delivered = self._delivered.get(key) if key else None
        if delivered is not None:
            # Пересборка (export_report) после успешной доставки: в папке
            # проктора уже лежит пакет этой сессии. Вторая копия рядом ломает
            # сверку числа пакетов со списком группы, поэтому её нет, а status
            # говорит прямо: пересобранный пакет остался здесь.
            result = {**delivered, "skipped": "already_delivered",
                      "note": (f"пакет пересобран после доставки и остался на этом "
                               f"компьютере: {package}. В папке проктора — копия "
                               f"прежней сборки: {delivered.get('dest') or '—'}; "
                               f"вторая копия не делается")}
            log.info("пакет пересобран после доставки: вторая копия в папку проктора "
                     "не делается (там уже %s), новый пакет остался здесь: %s",
                     delivered.get("dest") or "—", package)
            await self._settle_delivery(pkg, session_dir, package, result, attempt=False)
            return result
        if self._deliver_abort.is_set():
            result = {"ok": False, "dest": "", "bytes": 0, "sha256": "",
                      "verified": False, "at": time.time(),
                      "error": (f"ядро останавливается — копирование в папку проктора "
                                f"не начиналось, пакет лежит здесь: {package}")}
            await self._settle_delivery(pkg, session_dir, package, result)
            return result

        target = str(self.cfg.deliver_path or self.delivery.get("dir") or "")
        timeout = float(getattr(self.cfg, "deliver_timeout", 0)
                        or DEFAULT_DELIVER_TIMEOUT_SEC)
        timed_out = False
        try:
            fut = _in_daemon_thread(handover_mod.deliver_package, package, target,
                                    self._deliver_abort)
            try:
                result = await asyncio.wait_for(asyncio.shield(fut), timeout)
            except asyncio.TimeoutError:
                timed_out = True
                stalled = {"ok": False, "dest": "", "bytes": 0, "sha256": "",
                           "verified": False, "at": time.time(), "timed_out": True,
                           "error": (f"папка проктора не отвечает {timeout:g} с — "
                                     f"пакет лежит здесь: {package}")}
                log.warning("пакет НЕ доставлен в папку проктора за %g с: папка не "
                            "отвечает. Копирование продолжается, его итог заменит этот; "
                            "пакет лежит здесь: %s", timeout, package)
                await self._settle_delivery(pkg, session_dir, package, stalled, running=True)
                result = await fut
        except asyncio.CancelledError:
            # Останов не дождался копирования (`_finish_delivery_on_shutdown`).
            self._deliver_abort.set()
            gone = {"ok": False, "dest": "", "bytes": 0, "sha256": "",
                    "verified": False, "at": time.time(), "timed_out": True,
                    "error": (f"ядро остановлено, не дождавшись конца копирования в "
                              f"папку проктора — пакет лежит здесь: {package}")}
            with contextlib.suppress(Exception):
                await self._settle_delivery(pkg, session_dir, package, gone,
                                            replace=timed_out)
            raise
        except Exception as exc:  # deliver_package не бросает, но страхуемся
            log.exception("доставка пакета в папку проктора упала")
            result = {"ok": False, "dest": "", "bytes": 0, "sha256": "",
                      "verified": False, "at": time.time(),
                      "error": f"доставка не удалась: {exc}"}
        result = dict(result)
        if not result.get("ok") and self._deliver_abort.is_set():
            # Копию бросил останов (`_finish_delivery_on_shutdown`), а не папка:
            # итог тот же, что у снятой задачи (docs/CONTRACT.md) — с путём к
            # пакету; причина потока — в скобках.
            result.update(timed_out=True, error=(
                f"ядро остановлено, не дождавшись конца копирования в папку проктора — "
                f"пакет лежит здесь: {package} ({result.get('error') or 'причина не определена'})"))
        elif timed_out:
            # Поздний итог той же попытки: помечен, чтобы в handover.json было
            # видно, что мягкий срок она не уложила.
            result["late"] = True
        if result.get("ok") and not result.get("same_dir") and key:
            self._delivered[key] = result
        await self._settle_delivery(pkg, session_dir, package, result, replace=timed_out)
        if result.get("same_dir"):
            log.info("пакет уже лежит в папке проктора (каталог сессий задан туда же): "
                     "%s — копия не делалась", result.get("dest"))
        elif result.get("ok") and result.get("verified"):
            log.info("пакет доставлен в папку проктора: %s (sha256 сверен: %s…)",
                     result.get("dest"), str(result.get("sha256") or "")[:16])
        elif result.get("ok"):
            log.warning("пакет записан в папку проктора: %s, но НЕ сверен: %s",
                        result.get("dest"), result.get("error"))
        else:
            log.warning("пакет НЕ доставлен в папку проктора: %s. Он лежит здесь: %s",
                        result.get("error") or "причина не определена", package)
        return result

    async def _finish_delivery_on_shutdown(self) -> None:
        """Дать незаконченной доставке несколько секунд на останове — не больше.

        Дольше ждать нельзя: оболочка уходит, а зависшая шара держала бы процесс
        сколько угодно. Не успела — поток просим бросить копию (свою
        недописанную он удаляет, обрезанный файл под именем пакета в папке
        проктора не остаётся), задачу снимаем, и она записывает в
        `handover.json`, что копирование не закончилось и где лежит пакет.
        """
        task = self._delivery_task
        if task is None or task.done():
            return
        log.info("останов: копирование пакета в папку проктора ещё идёт — жду не "
                 "дольше %g с", DELIVER_SHUTDOWN_GRACE_SEC)
        done, _ = await asyncio.wait({task}, timeout=DELIVER_SHUTDOWN_GRACE_SEC)
        if done:
            return
        self._deliver_abort.set()
        done, _ = await asyncio.wait({task}, timeout=DELIVER_ABORT_GRACE_SEC)
        if not done:
            task.cancel()
            await asyncio.wait({task}, timeout=DELIVER_ABORT_GRACE_SEC)
        log.warning("останов: копирование в папку проктора не уложилось в %g с и "
                    "брошено — итог записан в handover.json, пакет остался на этом "
                    "компьютере: %s",
                    DELIVER_SHUTDOWN_GRACE_SEC, self.last_package.get("package") or "—")

    def _chain_state(self) -> dict[str, Any]:
        """Состояние цепочки для манифеста: genesis, голова, число записей."""
        state: dict[str, Any] = {"genesis": self.session.genesis}
        if self.store is None:
            return state
        with contextlib.suppress(Exception):
            last = getattr(self.store, "last_hash", None)
            if callable(last):
                state["last_hash"] = str(last() or "")
        with contextlib.suppress(Exception):
            stats = getattr(self.store, "stats", None)
            if callable(stats):
                data = stats()
                if isinstance(data, dict):
                    state["records"] = data.get("records", 0)
                    state["events_checked"] = data.get("events", 0)
                    state["observations"] = data.get("observations", 0)
                    state["controls"] = data.get("controls", 0)
                    state.setdefault("last_hash", data.get("chain_head", ""))
        return state

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

        # Завершение могло уже идти (оболочка прислала session_end и сразу
        # SIGTERM): тогда ждём его, а не гасим модули под незаконченной сборкой.
        if self.session.active or self._ending is not None:
            with contextlib.suppress(Exception):
                await self._cmd_session_end({"reason": reason or "остановка сайдкара"})
        # Доставка в папку проктора идёт фоном — ей несколько секунд, не больше.
        with contextlib.suppress(Exception):
            await self._finish_delivery_on_shutdown()

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

        # Токен одноразовый: он действует ровно столько, сколько живёт процесс.
        # Оставить файл после выхода означало бы, что следующий запуск или
        # сторонний процесс подберёт секрет от уже закрытого канала.
        if self.token_file is not None:
            with contextlib.suppress(OSError):
                self.token_file.unlink()
            log.debug("файл токена удалён: %s", self.token_file)
            self.token_file = None

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

    handover = p.add_argument_group("передача доказательств проктору")
    handover.add_argument(
        "--sessions-dir", dest="sessions_dir", default=None, metavar="PATH",
        help="каталог, куда писать сессии: сетевая папка вуза или USB-носитель. "
             "Приоритет: этот флаг > PROCTOR_SESSIONS_DIR > config.json > sessions",
    )
    handover.add_argument(
        "--deliver-to", "--deliver-dir", dest="deliver_to", default=None, metavar="PATH",
        help="папка проктора (флешка или сетевая папка вуза), куда ПОСЛЕ экзамена "
             "копируется готовый пакет <session_id>.proctor.zip со сверкой sha256. "
             "Сессия при этом пишется в каталог сессий как обычно. Относительный "
             "путь — от корня репозитория, как у --sessions-dir. Приоритет: этот "
             "флаг > PROCTOR_DELIVER_DIR > config.json:deliver_dir > не задана",
    )
    handover.add_argument(
        "--signing-key", dest="signing_key", default=None, metavar="PATH",
        help="приватный ключ учреждения для подписи отчёта. Файл должен "
             "СУЩЕСТВОВАТЬ: несуществующий путь останавливает запуск, новый ключ "
             "здесь не создаётся. Задан — подпись получает метку institution, "
             "которую экзаменатор может сверить со своим эталоном "
             "(verify_report.py --trusted-pub); без такой сверки метка не сильнее "
             "self_signed. Ключ при этом читается НА этой машине",
    )
    handover.add_argument(
        "--no-package", dest="no_package", action="store_true",
        help="не собирать <session_id>.proctor.zip по завершении сессии",
    )
    # --------------------------------------------------- профиль экзамена
    profile = p.add_argument_group(
        "профиль экзамена: что открывать и какие источники разрешены",
        "Профиль — УЛИКА, а не защита: он применяется процессом на машине, "
        "которой владеет студент, и обойти его там можно. Ценность в том, что "
        "хеш действовавших правил ВСЕГДА попадает в подписанную цепочку "
        "(отключить это нечем), а каждая попытка выйти за белый список "
        "остаётся в журнале с полным адресом. Профиль не задан — поведение "
        "прежнее: локальный тест.",
    )
    profile.add_argument(
        "--exam-profile", dest="exam_profile", default=None, metavar="PATH",
        help=f"путь к {EXAM_PROFILE_FILENAME}: exam_url, allowed_origins, "
             "allow_search, institution, exam_id, notes, issued_at, issued_by. "
             f"Приоритет: этот флаг > {EXAM_PROFILE_ENV_VAR} > "
             f"<каталог проктора>/{EXAM_PROFILE_FILENAME} > нет профиля",
    )
    profile.add_argument(
        "--exam-profile-pubkey", dest="exam_profile_pubkey", default=None,
        metavar="PATH",
        help="доверенный публичный ключ (hex) для проверки подписи профиля. "
             "Передаётся ОТДЕЛЬНО от профиля намеренно: подпись, проверенная "
             "ключом из того же файла, доказывает только целостность файла, а "
             f"не авторство вуза (или {EXAM_PROFILE_PUBKEY_ENV_VAR})",
    )
    profile.add_argument(
        "--allow-search", dest="allow_search", action="store_true",
        help="РАЗРЕШИТЬ поисковики и ИИ-ассистенты в белом списке. По умолчанию "
             "запрещены: Google и Яндекс показывают готовый ответ прямо в выдаче "
             "(блок быстрого ответа, ИИ-обзор), и переходить студенту никуда не "
             "нужно — белого списка с разрешённым поисковиком не существует. "
             "Флаг для экзаменов, где поиск разрешён правилами; факт его "
             "применения идёт в хеш-цепочку и в шапку отчёта "
             f"(или {ALLOW_SEARCH_ENV_VAR}=1)",
    )
    profile.add_argument(
        "--exam-profile-example", dest="exam_profile_example", nargs="?",
        const="ksu", default=None, metavar="ПРЕСЕТ",
        help="напечатать заготовку профиля и выйти. Пресеты: "
             + ", ".join(sorted(EXAM_PROFILE_PRESETS))
             + ". Пресет НЕ применяется сам по себе: в цепочку должен попасть "
               "хеш файла, который проктор видел и положил рядом с "
               "доказательствами, а не хеш строки из кода",
    )

    # ------------------------------------------------- политика блокировки
    policy = p.add_argument_group(
        "политика решений над экзаменом",
        "По умолчанию автоматика НЕ блокирует экзамен: максимум, который она "
        "делает сама, — приостановка. Порог блокировки переводит сессию в "
        "ожидание решения проктора.",
    )
    policy.add_argument(
        "--auto-lock", dest="auto_lock", action="store_true",
        help="ВЕРНУТЬ автоматическую блокировку по порогу, без участия человека. "
             "По умолчанию выключено. Включать только если правила вуза прямо "
             "требуют закрывать сессию без проктора: разбор опубликованного "
             "кейса Duolingo English Test показал, что прокторы подтверждали "
             "от 29%% до 50%% заведомо ложных сигналов, а здесь человека не "
             "будет вовсе",
    )
    policy.add_argument(
        "--risk-lock", dest="risk_lock", type=float, default=None, metavar="N",
        help="порог, на котором запрашивается решение проктора (по умолчанию 90)",
    )

    # ---------------------------------------------- аутентификация канала
    auth = p.add_argument_group("аутентификация локального канала")
    auth.add_argument(
        "--no-auth", dest="no_auth", action="store_true",
        help="открыть канал БЕЗ токена. Любой локальный процесс сможет "
             "представиться оболочкой, обнулить риск и читать поток событий. "
             "Только для стенда разработки; факт попадает в журнал сессии",
    )
    auth.add_argument(
        "--auth-token", dest="auth_token", default=None, metavar="TOKEN",
        help=f"задать токен вручную вместо случайного (или через {AUTH_TOKEN_ENV_VAR})",
    )
    auth.add_argument(
        "--auth-token-file", dest="auth_token_file", default=None, metavar="PATH",
        help="куда положить файл с токеном (по умолчанию во временный каталог ОС, "
             "права 0600)",
    )
    auth.add_argument(
        "--allow-multi-client", dest="allow_multi_client", action="store_true",
        help="разрешить больше одного одновременного клиента канала",
    )

    # --------------------------------------------------------- сырой слой
    raw = p.add_argument_group("сырой слой журнала")
    raw.add_argument(
        "--no-raw-log", dest="no_raw_log", action="store_true",
        help="не писать сырые наблюдения и телеметрию в журнал "
             "(останутся только готовые события)",
    )
    raw.add_argument(
        "--observation-window", dest="observation_window", type=float,
        default=None, metavar="SEC",
        help="длина окна агрегации сырых наблюдений, секунды (по умолчанию 1.0). "
             "Границы условия пишутся всегда, независимо от окна",
    )
    raw.add_argument(
        "--observation-max", dest="observation_max", type=int,
        default=None, metavar="N",
        help="потолок записей сырого слоя на сессию (по умолчанию 20000, 0 — без "
             "потолка). Обрезка фиксируется в журнале и в отчёте",
    )

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


def _env_flag(name: str) -> bool:
    """Логический флаг из окружения. Пусто и `0/false/no/off` — выключено."""
    raw = str(os.environ.get(name, "") or "").strip().lower()
    return raw not in ("", "0", "false", "no", "off")


def _env_float(name: str) -> float | None:
    """Число из окружения или None, если переменная не задана или мусор."""
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        log.warning("переменная %s не разбирается как число: %r", name, raw)
        return None


def apply_cli(cfg: ProctorConfig, args: argparse.Namespace) -> None:
    # Каталог сессий и ключ подписи: флаг сильнее переменной окружения и
    # config.json. ProctorConfig.load() уже просмотрел argv, но повторяем здесь
    # явно — иначе значение зависело бы от того, вызвали ли load() с argv.
    if getattr(args, "sessions_dir", None):
        cfg.sessions_dir = str(args.sessions_dir)
        cfg.sessions_dir_source = "cli"
    if getattr(args, "deliver_to", None):
        cfg.deliver_dir = str(args.deliver_to)
        cfg.deliver_dir_source = "cli"
    if getattr(args, "signing_key", None):
        cfg.signing_key_path = str(args.signing_key)
    if getattr(args, "no_package", False):
        cfg.package_on_end = False
    # Профиль экзамена: флаг сильнее переменной окружения и config.json.
    # `ProctorConfig.load()` уже просмотрел argv, но повторяем здесь явно — по
    # той же причине, что и для каталога сессий: иначе значение зависело бы от
    # того, вызвали ли load() с argv. САМ ФАЙЛ читается позже, в main(), после
    # того как разрешён каталог проктора: последний источник в приоритете —
    # файл в этом каталоге.
    if getattr(args, "exam_profile", None):
        cfg.exam_profile_path = str(args.exam_profile)
        cfg.exam_profile_source = PROFILE_SOURCE_CLI
    if getattr(args, "exam_profile_pubkey", None):
        cfg.exam_profile_pubkey = str(args.exam_profile_pubkey)
    if getattr(args, "allow_search", False):
        cfg.allow_search = True
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

    # Параметры этого модуля кладутся на объект конфига атрибутами: config.py
    # правят параллельно, и добавлять туда поля нельзя. `ProctorConfig` —
    # обычный dataclass без slots, так что атрибут ставится; в `to_dict()` он
    # не попадёт (asdict видит только объявленные поля), поэтому всё, что нужно
    # соседям, уходит через `cfg_dict["decision_policy"]`.
    # Какие флаги ядра были названы ЯВНО. Нужно именно это, а не итоговое
    # значение `enable_*`: по `enable_vision = False` нельзя отличить «сняли
    # флагом --no-yolo» от «выключено в config.json», а в отчёте это разные
    # утверждения (LAUNCH-02). Кладём атрибутом по той же причине, что и
    # auto_lock ниже: config.py правят параллельно.
    cfg.launch_flags = {
        "no_audio": bool(getattr(args, "no_audio", False)),
        "no_identity": bool(getattr(args, "no_identity", False)),
        "no_yolo": bool(getattr(args, "no_yolo", False)),
        "no_env": bool(getattr(args, "no_env", False)),
        "headless": bool(getattr(args, "headless", False)),
        "mock": bool(getattr(args, "mock", False)),
        "allow_multi_display": bool(getattr(args, "allow_multi_display", False)),
        "no_raw_log": bool(getattr(args, "no_raw_log", False)),
        "no_auth": bool(getattr(args, "no_auth", False)),
        "auto_lock": bool(getattr(args, "auto_lock", False)),
        # Разрешённый поиск меняет доказательную базу не меньше выключенного
        # канала: при нём готовый ответ доступен студенту прямо в выдаче.
        # Флаг обязан быть виден в записи `launch_env` наравне с остальными.
        "allow_search": bool(getattr(args, "allow_search", False)),
        "exam_profile": bool(getattr(args, "exam_profile", None)),
    }
    cfg.auto_lock = bool(getattr(args, "auto_lock", False)) or _env_flag("PROCTOR_AUTO_LOCK")
    cfg.no_auth = bool(getattr(args, "no_auth", False)) or _env_flag("PROCTOR_NO_AUTH")
    cfg.auth_token = str(getattr(args, "auth_token", None) or "")
    cfg.auth_token_file = str(getattr(args, "auth_token_file", None) or
                              os.environ.get("PROCTOR_WS_TOKEN_FILE", "") or "")
    cfg.allow_multi_client = bool(getattr(args, "allow_multi_client", False))
    cfg.log_raw = not (bool(getattr(args, "no_raw_log", False))
                       or _env_flag("PROCTOR_NO_RAW_LOG"))

    window = getattr(args, "observation_window", None)
    if window is None:
        window = _env_float("PROCTOR_OBSERVATION_WINDOW")
    cfg.observation_window = float(window) if window else 1.0

    max_records = getattr(args, "observation_max", None)
    if max_records is None:
        env_max = _env_float("PROCTOR_OBSERVATION_MAX")
        max_records = int(env_max) if env_max is not None else None
    cfg.observation_max_records = 20000 if max_records is None else int(max_records)

    if getattr(args, "risk_lock", None) is not None:
        cfg.risk_lock = float(args.risk_lock)


#: Сколько останов ждёт незаконченную доставку в папку проктора, секунды.
#: Оболочка на выходе шлёт session_end и сразу SIGTERM; ждать зависшую шару
#: дольше нескольких секунд значит держать процесс сколько угодно.
DELIVER_SHUTDOWN_GRACE_SEC = 5.0
#: Сколько ещё ждать, попросив поток бросить копию: флаг он проверяет между
#: кусками по 1 МиБ и удаляет свою недописанную копию.
DELIVER_ABORT_GRACE_SEC = 2.0


def _in_daemon_thread(fn: Any, *args: Any) -> "asyncio.Future[Any]":
    """`fn(*args)` в отдельном фоновом (daemon) потоке; итог — future цикла.

    Не `asyncio.to_thread`: поток пула исполнителей процесс при выходе
    ДОЖИДАЕТСЯ (`shutdown_default_executor`, затем atexit в
    `concurrent.futures`), и копия на зависшую шару держала бы выход ядра
    бесконечно. Daemon-поток выход не держит; ограничивает его
    `_finish_delivery_on_shutdown`.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()

    def settle(value: Any, error: BaseException | None) -> None:
        if fut.done():
            return
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(value)

    def work() -> None:
        value: Any = None
        error: BaseException | None = None
        try:
            value = fn(*args)
        except Exception as exc:
            error = exc
        with contextlib.suppress(RuntimeError):  # цикл закрыт — процесс выходит
            loop.call_soon_threadsafe(settle, value, error)

    threading.Thread(target=work, name="deliver", daemon=True).start()
    return fut


def _delivery_facts(cfg: Any, info: dict[str, Any]) -> dict[str, Any]:
    """Дополнить объект `delivery` фактами конфигурации.

    `package_on_end` — собирается ли пакет вообще (`--no-package`: копировать
    нечего, и предполётный экран обязан сказать это, а не «пакет уйдёт»).
    `same_dir` — папка проктора совпадает с каталогом сессий или лежит над
    ним: пакет и так собирается в ней, отдельной копии (и сверки копии) нет.
    """
    out = dict(info)
    out["package_on_end"] = bool(getattr(cfg, "package_on_end", True))
    same = False
    if out.get("configured") and handover_mod is not None:
        with contextlib.suppress(Exception):
            same = bool(handover_mod.deliver_covers(
                out.get("dir") or getattr(cfg, "deliver_path", None),
                getattr(cfg, "sessions_path", None)))
    out["same_dir"] = same
    return out


def _delivery_unprobed(cfg: Any, reason: str = "") -> dict[str, Any]:
    """Объект `delivery` без стартовой пробы: папка не задана или проверить нечем.

    Нужен и ядру, собранному не через `main()` (тесты, мок): `hello` обязан
    нести этот объект всегда, а «не проверялась» — не то же, что «доступна».
    """
    path = getattr(cfg, "deliver_path", None)
    configured = bool(path)
    return _delivery_facts(cfg, {
        "configured": configured,
        "dir": str(path or ""),
        "source": (str(getattr(cfg, "deliver_dir_source", "") or "default")
                   if configured else "default"),
        "writable": False,
        "reason": (reason or "папка проктора при старте не проверялась") if configured else "",
        "created": False,
        "probe_removed": None,
    })


def prepare_delivery(cfg: ProctorConfig) -> dict[str, Any]:
    """Стартовая проверка папки проктора (`--deliver-to`). Экзамен НЕ блокирует.

    Папка проктора — не каталог сессий: сессия пишется локально, а туда после
    экзамена копируется только готовый пакет. Поэтому недоступная папка здесь
    не причина не начинать: пакет останется на машине, доставку можно
    повторить после экзамена командой `deliver_package`. Но сказать об этом
    обязаны ДО экзамена — объект уходит в `hello` и в каждый `status`.
    """
    path = cfg.deliver_path
    if path is None:
        info = _delivery_unprobed(cfg)
        log.info("папка проктора не задана (--deliver-to или %s): пакет останется "
                 "на этом компьютере рядом с каталогом сессии", DELIVER_DIR_ENV_VAR)
    elif handover_mod is None:
        info = _delivery_unprobed(cfg, "модуль передачи доказательств недоступен")
        log.warning("папка проктора %s задана, но модуль storage/handover.py "
                    "недоступен: пакет скопирован не будет", path)
    else:
        info = _delivery_facts(cfg, handover_mod.probe_deliver_dir(
            path, cfg.deliver_dir_source))
        label = handover_mod.deliver_source_label(str(info.get("source") or ""))
        if info.get("created"):
            log.warning("папки проктора %s не было — она создана. Если это точка "
                        "монтирования флешки или сетевого диска, проверьте, что "
                        "носитель подключён: иначе пакет ляжет на локальный диск "
                        "этой же машины", info.get("dir"))
        if info.get("same_dir"):
            log.warning("папка проктора %s — это каталог сессий %s или папка над "
                        "ним: пакет и так собирается в ней, отдельной копии не будет "
                        "и «sha256 сверен» тоже — сверять нечего. Нужна копия на "
                        "другой носитель — укажите --deliver-to на него",
                        info.get("dir"), cfg.sessions_path)
        if info.get("writable"):
            log.info("папка проктора: %s (%s). Запись проверена%s",
                     info.get("dir"), label,
                     ": после экзамена пакет будет скопирован туда со сверкой sha256"
                     if cfg.package_on_end and not info.get("same_dir") else "")
            if info.get("probe_removed") is False:
                log.info("пробный файл в папке проктора удалить не удалось — папка "
                         "похожа на append-only (так и задумано для шары)")
        else:
            log.warning("папка проктора %s (%s) недоступна: %s. Экзамен НЕ "
                        "останавливается: пакет останется на этом компьютере, "
                        "доставку можно повторить после экзамена",
                        info.get("dir"), label, info.get("reason") or "причина не определена")
    if info.get("configured") and not cfg.package_on_end:
        log.warning("задана папка проктора, но пакет не собирается (--no-package): "
                    "доставлять будет нечего, копирования не будет")
    cfg.delivery_resolved = info
    # Признак same_dir — и в сведения о передаче: по ним отчёт и манифест
    # говорят «копируется» или «уже лежит в папке», не обращаясь к самой папке
    # (зависшая шара не должна держать сборку отчёта).
    if isinstance(getattr(cfg, "handover_resolved", None), dict):
        cfg.handover_resolved["deliver_same_dir"] = bool(info.get("same_dir"))
    return info


def prepare_handover(cfg: ProctorConfig) -> bool:
    """Разрешить каталог сессий и сказать человеку, что получилось.

    Возвращает False только в одном случае: писать доказательства НЕКУДА ни в
    каталог проктора, ни в запасной. Продолжать тогда нельзя — экзамен пройдёт,
    а доказательств не останется, и узнает об этом никто. Во всех остальных
    случаях возвращает True: недоступная сетевая папка понижает передачу до
    `degraded_handover`, но экзамен состоится.

    Текст в лог идёт ЯВНЫЙ и по-русски. Прежнее поведение — `mkdir(exist_ok)`
    и тишина — означало, что read-only сетевая папка обнаруживалась в момент
    сохранения первого кадра, то есть посреди экзамена.
    """
    if handover_mod is None:
        # Р-05: модуля передачи нет — работаем как раньше, но молчать нельзя.
        try:
            cfg.sessions_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("каталог сессий %s недоступен: %s", cfg.sessions_path, exc)
            return False
        cfg.handover_resolved = {**cfg.handover_info(),
                                 "effective_dir": str(cfg.sessions_path),
                                 "degraded_handover": False,
                                 "reason": "модуль передачи доказательств недоступен"}
        log.warning("модуль storage/handover.py недоступен: пакет доказательств "
                    "собран не будет, каталог сессии копируйте целиком")
        return True

    target = handover_mod.resolve_sessions_dir(
        cfg.sessions_dir if cfg.sessions_dir_source != "default" else "",
        cfg.sessions_fallback_path,
        cfg.sessions_dir_source,
    )
    for line in str(target.message).splitlines():
        if target.degraded:
            log.warning("передача доказательств: %s", line)
        else:
            log.info("передача доказательств: %s", line)

    # Каталог в конфиге становится фактическим: всё остальное (хранилище,
    # калибровка, отчёт) читает один и тот же путь и не расходится с логом.
    cfg.sessions_dir = str(target.path)
    cfg.handover_resolved = {**cfg.handover_info(), **target.to_dict()}

    ok, reason = handover_mod.check_writable(target.path)
    if not ok:
        log.error("Доказательства сохранить некуда: %s — %s", target.path, reason)
        log.error("Укажите доступный каталог: --sessions-dir /путь "
                  "(или %s=/путь) и запустите снова.",
                  handover_mod.SESSIONS_DIR_ENV_VAR)
        return False

    # Задан ключ учреждения — он ОБЯЗАН существовать и читаться, и это
    # проверяется ДО начала экзамена. Прежде `--signing-key /опечатка.key`
    # приводил к генерации нового ключа на этой машине и к метке institution;
    # после исправления в report.py метка честно понижается, но тогда проктор
    # узнаёт об опечатке из отчёта после экзамена. Правильное место — здесь:
    # экзамен, запущенный не с тем ключом, не должен начинаться.
    requested_key = str(cfg.signing_key_path or "").strip()
    if requested_key and report_startup_mod is not None:
        resolve = getattr(report_startup_mod, "resolve_authority", None)
        effective, why = (resolve(cfg.signing_key_abs, "institution")
                          if callable(resolve) else ("institution", ""))
        if effective != handover_mod.AUTHORITY_INSTITUTION:
            log.error("ключ учреждения не годится: %s", why)
            log.error("Путь из --signing-key: %s", cfg.signing_key_abs)
            log.error("Экзамен НЕ запущен. Новый ключ здесь не создаётся намеренно: "
                      "созданный на этой машине ключ не является ключом вуза, а "
                      "метка institution на нём была бы ложным доказательством "
                      "авторства. Проверьте путь или запустите без --signing-key.")
            return False
        log.info("ключ учреждения прочитан: %s", cfg.signing_key_abs)

    info = handover_mod.authority_info(cfg.signature_authority)
    log.info("подпись отчёта: %s — доказывает %s", info["label"], info["proves"])
    log.warning("подпись НЕ доказывает %s", info["not_proves"])
    if cfg.signature_authority == handover_mod.AUTHORITY_INSTITUTION:
        # Метку выдаём, но не притворяемся, что она самодостаточна.
        log.warning("метка institution будет проверяема ТОЛЬКО если экзаменатор "
                    "сверит публичный ключ с эталоном: "
                    "verify_report.py <пакет> --trusted-pub <файл>")
        log.warning("и даже тогда она значит «у процесса был доступ к ключу вуза», "
                    "а не «подписал вуз»: ключ читается на этой машине")
    else:
        log.warning("%s", info["advice"])
    return True


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

    # Заготовка профиля печатается до всего остального: это утилита, а не
    # запуск экзамена. Пресет вуза заказчика (ksu.edu.kz) — значение по
    # умолчанию флага.
    if getattr(args, "exam_profile_example", None):
        try:
            print(exam_profile_example(str(args.exam_profile_example)), end="")
        except KeyError as exc:
            print(str(exc))
            return 2
        return 0

    if args.list_cameras:
        cams = CameraCapture.list_cameras(cfg.camera_probe_max_index)
        if not cams:
            print("Камеры не найдены (или нет доступа / нет opencv)")
        for cam in cams:
            print(f"камера {cam['index']}: {cam['width']}x{cam['height']}")
        return 0

    if not prepare_handover(cfg):
        return 2
    prepare_delivery(cfg)

    # Профиль экзамена читается ЗДЕСЬ, после разрешения каталога проктора:
    # последний источник в приоритете — `exam-profile.json` в этом каталоге, и
    # до `prepare_handover()` его искать негде. Ошибка чтения не останавливает
    # запуск (Р-05): правила окажутся незаданными, и это будет записано в
    # цепочку как факт, а не как молчание.
    profile = cfg.resolve_exam_profile()
    for level, line in profile.log_lines():
        (log.warning if level == "warning" else log.info)("%s", line)
    if profile.dropped_origins:
        log.warning("записи белого списка, которые НЕ действуют (%d):",
                    len(profile.dropped_origins))
        for item in profile.dropped_origins:
            log.warning("  - %s: %s", item.get("origin"), item.get("reason"))

    sidecar = ProctorSidecar(cfg)
    # Токен печатается ДО загрузки моделей и старта сервера: подключиться
    # раньше, чем человек увидел токен, никто не должен.
    sidecar.prepare_auth()
    log.info("политика блокировки: %s (порог %.0f, автоматика не выше %s)",
             LOCK_POLICY_LABELS.get(sidecar.lock_policy, sidecar.lock_policy),
             cfg.risk_lock, AUTO_ACTION_CAP.value)
    if sidecar.auto_lock:
        log.warning("включён --auto-lock: экзамен будет заблокирован БЕЗ человека "
                    "по достижении порога. Факт записан в журнал сессии.")
    # Переменные окружения, меняющие доказательную базу, называются вслух ДО
    # загрузки моделей: человек у машины должен увидеть их до начала экзамена, а
    # не вычитать потом из отчёта. Ни одна из них не запрещается — на машине
    # студента запрет бесполезен, запускает он.
    for row in sidecar.launch_env["vars"]:
        if not row["set"]:
            continue
        log.warning("задана %s=%s (%s)%s", row["name"], row.get("value", ""),
                    row["affects"],
                    "" if row.get("exists", True) else " — путь не существует")
        log.warning("значение %s уходит в хеш-цепочку сессии записью launch_env",
                    row["name"])
    log.info("ядро запущено интерпретатором %s (%s)",
             sidecar.launch_env["python_executable"], sidecar.launch_env["python_version"])
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

"""
Единая конфигурация сайдкара.

Все числа, пороги, частоты и пути живут здесь. Детекторы и движки получают
конфиг как плоский dict (`ProctorConfig.to_dict()`) — так требует контракт
(`__init__(self, config: dict)`), поэтому имена полей здесь же являются
именами ключей конфига для соседних модулей.

Переопределение: файл `config.json` в корне репозитория (или путь из --config).
Поддерживаются как плоские ключи, так и вложенные секции — вложенность просто
разворачивается по именам полей:

    {"frame_width": 1280, "capture": {"target_fps": 10}, "risk": {"risk_half_life": 60}}

Неизвестные ключи игнорируются, их список возвращает `apply()` — вызывающий
пишет это в лог, но не падает.

РЕЖИМ РАЗВЁРТЫВАНИЯ `exam_mode` (решение Р-10)
----------------------------------------------
    "classroom" — компьютерный класс, значение по умолчанию;
    "remote"    — сдача из дома, тихая комната.

В `classroom` аудио-анализ выключен ЦЕЛИКОМ: `AudioMonitor` не стартует и микрофон
не захватывается, правила `VOICE_OTHER` и `SPEECH_WITHOUT_LIP_MOTION` в движке не
регистрируются. Причина: в аудитории на 30 человек чужая речь есть всегда, и оба
правила превращаются в генератор ложных обвинений (журнал решений, Р-10).
Угроза У-02 «наушник со связью» закрыта вместо этого детерминированной проверкой
`env_checks.check_audio_devices()` — она работает в обоих режимах.

Приоритет источников режима (далее — важнее):
    значение по умолчанию -> config.json -> переменная окружения
    PROCTOR_EXAM_MODE -> флаг CLI (`--exam-mode remote`, `--remote`, `--classroom`).

Флаг читается двумя путями. Владелец argparse (например `sidecar/main.py`) вызывает
`ProctorConfig.add_cli_arguments(parser)` и затем `cfg.apply_cli_args(args)`. Кроме
того `load()` сам просматривает `sys.argv` на предмет этих флагов, игнорируя всё
остальное, — чтобы режим не зависел от того, какая точка входа запущена. Чтобы
отключить просмотр argv, передайте `load(argv=[])`.

ПЕРЕДАЧА ДОКАЗАТЕЛЬСТВ: КАТАЛОГ СЕССИЙ И КЛЮЧ ПОДПИСИ
-----------------------------------------------------
Каталог сессий задаёт ПРОКТОР, а не код. Приоритет источников (далее — важнее):

    "sessions" по умолчанию -> config.json -> PROCTOR_SESSIONS_DIR -> --sessions-dir

Смысл: в компьютерном классе каталог указывают на сетевую папку вуза или на
USB-носитель, и студент не может удалить то, что ушло на чужой диск. Каталог
проверяется на запись при старте (`storage/handover.py`), а не в момент
сохранения первого кадра; недоступен — пишем в запасной локальный и помечаем
сессию как `degraded_handover`.

Папка проктора для доставки пакета — тот же порядок источников:

    "" (не задана) -> config.json:deliver_dir -> PROCTOR_DELIVER_DIR -> --deliver-to

Это НЕ каталог сессий: сессия по-прежнему пишется в каталог сессий (обычно
локальный), а в папку проктора после экзамена КОПИРУЕТСЯ готовый пакет
(`storage/handover.deliver_package`). Не задана — пакет остаётся на машине.

Ключ подписи — тем же способом: `--signing-key PATH` (или `PROCTOR_SIGNING_KEY`).
Ключ задан проктором -> подпись получает метку `institution` и доказывает
авторство. Ключ не задан -> ключ генерируется на машине студента, метка
`self_signed`, и подпись доказывает ТОЛЬКО целостность при передаче: приватный
ключ лежит там же, где журнал, поэтому журнал можно пересобрать и переподписать.
Разницу обязаны показывать и отчёт, и `scripts/verify_report.py`.

ПРОФИЛЬ ЭКЗАМЕНА: ЧТО ОТКРЫВАТЬ И ЧТО СЧИТАТЬ РАЗРЕШЁННЫМ ИСТОЧНИКОМ
--------------------------------------------------------------------
Приоритет тот же, что у каталога сессий:

    нет профиля -> <каталог проктора>/exam-profile.json -> PROCTOR_EXAM_PROFILE
    -> --exam-profile PATH

Профиль — УЛИКА, а не защита: его применяет процесс на машине, которой владеет
студент. Поэтому хеш действовавшего профиля пишется в подписанную цепочку
ВСЕГДА, без флага отключения, а отсутствие профиля записывается тем же
способом («правила экзамена не задавались»). Подробный разбор — в большом
комментарии к секции профиля ниже; там же три ошибки фильтрации, закрытые
кодом (`.edu`, класс `.edu.kz` целиком, поисковики по умолчанию).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from protocol import (  # noqa: E402  (путь настраиваем выше)
    DEFAULT_WS_HOST,
    DEFAULT_WS_PORT,
    RISK_LOCK,
    RISK_PAUSE,
    RISK_WARN,
    EventKind,
)

#: Корень репозитория: sidecar/config.py -> sidecar -> корень.
ROOT = _HERE.parent

#: Имя файла с переопределениями, которое ищется в корне репозитория.
CONFIG_FILENAME = "config.json"

#: Имена секций, которые читают соседние модули. Из config.json такие ключи
#: передаются их конструкторам как есть (см. ProctorConfig.module_overrides).
SECTION_NAMES = (
    "capture", "objects", "face_mesh", "face", "gaze", "vision", "identity",
    "liveness", "audio", "env", "events", "risk", "fusion", "storage", "report",
)

# ===========================================================================
# Режим развёртывания (Р-10)
# ===========================================================================
#: Компьютерный класс. Значение по умолчанию: кейс — ЛОКАЛЬНЫЙ прокторинг.
EXAM_MODE_CLASSROOM = "classroom"
#: Сдача из дома, тихая комната: аудио-канал осмыслен.
EXAM_MODE_REMOTE = "remote"
EXAM_MODES = (EXAM_MODE_CLASSROOM, EXAM_MODE_REMOTE)

#: Синонимы, которые встречаются в конфигах и в речи. Всё неизвестное трактуется
#: как `classroom` — безопасная сторона: лишний выключенный канал лучше, чем
#: включённый генератор ложных срабатываний.
_EXAM_MODE_ALIASES: dict[str, str] = {
    "classroom": EXAM_MODE_CLASSROOM,
    "class": EXAM_MODE_CLASSROOM,
    "lab": EXAM_MODE_CLASSROOM,
    "local": EXAM_MODE_CLASSROOM,
    "onsite": EXAM_MODE_CLASSROOM,
    "on-site": EXAM_MODE_CLASSROOM,
    "аудитория": EXAM_MODE_CLASSROOM,
    "класс": EXAM_MODE_CLASSROOM,
    "remote": EXAM_MODE_REMOTE,
    "home": EXAM_MODE_REMOTE,
    "online": EXAM_MODE_REMOTE,
    "distance": EXAM_MODE_REMOTE,
    "дом": EXAM_MODE_REMOTE,
    "удалённо": EXAM_MODE_REMOTE,
    "удаленно": EXAM_MODE_REMOTE,
}

#: Переменная окружения — путь для точек входа, чей argparse не знает про режим.
EXAM_MODE_ENV_VAR = "PROCTOR_EXAM_MODE"

#: Каталог сессий и ключ подписи: те же два пути (переменная окружения и флаг),
#: что и у режима экзамена. Имена держим в одном месте с `handover`, чтобы не
#: разъехались: модуль передачи доказательств — владелец этих понятий.
SESSIONS_DIR_ENV_VAR = "PROCTOR_SESSIONS_DIR"
SIGNING_KEY_ENV_VAR = "PROCTOR_SIGNING_KEY"
#: Папка проктора, куда после экзамена копируется пакет (`--deliver-to`).
DELIVER_DIR_ENV_VAR = "PROCTOR_DELIVER_DIR"

#: Значение `sessions_dir` по умолчанию. Оно же — запасной локальный каталог,
#: если указанный проктором недоступен на запись.
DEFAULT_SESSIONS_DIR = "sessions"

#: Правила, которые живут только в `remote`. В `classroom` они не регистрируются:
#: чужая речь в аудитории есть всегда, подтверждать по ней нечего.
AUDIO_ANALYSIS_KINDS: tuple[EventKind, ...] = (
    EventKind.VOICE_OTHER,
    EventKind.SPEECH_WITHOUT_LIP_MOTION,
)

#: Единая формулировка причины. Её показывает HUD, пишет отчёт и возвращает
#: `AudioMonitor.available()`. Формула гайда: факт -> контекст -> что это значит.
CLASSROOM_AUDIO_OFF_REASON = (
    "отключён в режиме аудитории: высокий уровень ложных срабатываний"
)


def normalize_exam_mode(value: Any) -> str:
    """Любое значение -> один из `EXAM_MODES`. Непонятное -> `classroom`."""
    key = str(value or "").strip().lower()
    return _EXAM_MODE_ALIASES.get(key, EXAM_MODE_CLASSROOM)


def exam_mode_from_argv(argv: Sequence[str] | None = None) -> str | None:
    """Вытащить режим из аргументов командной строки, игнорируя всё остальное.

    Понимает `--exam-mode remote`, `--exam-mode=remote`, `--mode remote`,
    а также короткие `--remote` и `--classroom`. Возвращает None, если про режим
    в argv ничего не сказано. Разбор ручной, а не argparse: функцию вызывают из
    `load()`, где чужие флаги не должны приводить к ошибке и выходу из процесса.
    """
    items = list(sys.argv[1:] if argv is None else argv)
    found: str | None = None
    index = 0
    while index < len(items):
        token = str(items[index])
        index += 1
        if token in ("--remote", "--exam-remote"):
            found = EXAM_MODE_REMOTE
            continue
        if token in ("--classroom", "--exam-classroom"):
            found = EXAM_MODE_CLASSROOM
            continue
        name, sep, inline = token.partition("=")
        if name not in ("--exam-mode", "--mode"):
            continue
        if sep:
            found = normalize_exam_mode(inline)
            continue
        if index < len(items) and not str(items[index]).startswith("-"):
            found = normalize_exam_mode(items[index])
            index += 1
    return found


def exam_mode_from_env(env: dict[str, str] | None = None) -> str | None:
    """Режим из `PROCTOR_EXAM_MODE`, если переменная задана непустой строкой."""
    source = os.environ if env is None else env
    raw = str(source.get(EXAM_MODE_ENV_VAR, "") or "").strip()
    return normalize_exam_mode(raw) if raw else None


def _value_from_argv(names: tuple[str, ...],
                     argv: Sequence[str] | None = None) -> str | None:
    """Значение флага со значением из argv. Понимает `--flag X` и `--flag=X`.

    Разбор ручной, по тем же причинам, что и у `exam_mode_from_argv`: функцию
    вызывает `load()`, где чужие флаги не должны приводить к выходу из процесса.
    Пустое значение (`--sessions-dir ""`) трактуется как «не задано»: это
    почти всегда незаполненная переменная в шелл-скрипте, а не намерение.
    """
    items = list(sys.argv[1:] if argv is None else argv)
    found: str | None = None
    index = 0
    while index < len(items):
        token = str(items[index])
        index += 1
        name, sep, inline = token.partition("=")
        if name not in names:
            continue
        if sep:
            value = inline.strip()
            if value:
                found = value
            continue
        if index < len(items):
            value = str(items[index]).strip()
            index += 1
            if value:
                found = value
    return found


def sessions_dir_from_argv(argv: Sequence[str] | None = None) -> str | None:
    """Каталог сессий из `--sessions-dir PATH`. None — в argv про него ничего."""
    return _value_from_argv(("--sessions-dir", "--sessions"), argv)


def sessions_dir_from_env(env: dict[str, str] | None = None) -> str | None:
    """Каталог сессий из `PROCTOR_SESSIONS_DIR`."""
    source = os.environ if env is None else env
    raw = str(source.get(SESSIONS_DIR_ENV_VAR, "") or "").strip()
    return raw or None


def deliver_dir_from_argv(argv: Sequence[str] | None = None) -> str | None:
    """Папка проктора из `--deliver-to PATH`. None — в argv про неё ничего."""
    return _value_from_argv(("--deliver-to", "--deliver-dir"), argv)


def deliver_dir_from_env(env: dict[str, str] | None = None) -> str | None:
    """Папка проктора из `PROCTOR_DELIVER_DIR`."""
    source = os.environ if env is None else env
    raw = str(source.get(DELIVER_DIR_ENV_VAR, "") or "").strip()
    return raw or None


def signing_key_from_argv(argv: Sequence[str] | None = None) -> str | None:
    """Ключ подписи из `--signing-key PATH`."""
    return _value_from_argv(("--signing-key", "--sign-key"), argv)


def signing_key_from_env(env: dict[str, str] | None = None) -> str | None:
    """Ключ подписи из `PROCTOR_SIGNING_KEY`."""
    source = os.environ if env is None else env
    raw = str(source.get(SIGNING_KEY_ENV_VAR, "") or "").strip()
    return raw or None


# ===========================================================================
# ПРОФИЛЬ ЭКЗАМЕНА: КАКОЙ АДРЕС ОТКРЫВАТЬ И ЧТО СЧИТАТЬ РАЗРЕШЁННЫМ ИСТОЧНИКОМ
#
# Откуда взялось понятие. Казахстанские вузы проводят тестирование в своих
# Moodle и Платонусе, а оболочка до этой правки умела открывать ТОЛЬКО
# локальный мок-тест через `loadFile` — настоящий LMS открыть было нельзя
# вообще. Профиль экзамена отвечает на два вопроса: какой адрес открывать и
# какие источники при этом считать разрешёнными.
#
# ГЛАВНОЕ: ПРОФИЛЬ — УЛИКА, А НЕ ЗАЩИТА. Это прямой вывод из У-13 модели угроз
# («правка файла конфигурации защищённого браузера»). Правила экзамена в Safe
# Exam Browser задаются файлом `.seb`; он раздавался в редактируемом виде, и
# его правили руками. Но сломало SEB не это, а вторая причина: Config Key,
# который LMS должна сверять, в Moodle включается ОТДЕЛЬНО и по умолчанию
# выключен. Подпись существовала, а проверять её было некому. Отказ был не
# техническим, а организационным: защиту сделали опциональной.
#
# Отсюда конструкция здесь:
#   * нигде не утверждается, что профиль нельзя обойти, и что подпись делает
#     его неподменяемым: студент владеет машиной, на которой всё это исполняется;
#   * хеш действовавшего профиля пишется в подписанную цепочку ВСЕГДА, без
#     флага отключения (см. `main._cmd_session_start`). Проверки, которую можно
#     забыть включить, у нас быть не должно — именно на этом сломался SEB;
#   * отсутствие профиля — тоже факт, и он тоже пишется: «правила экзамена
#     проктором не задавались»;
#   * профиль показывается в ШАПКЕ отчёта рядом с вердиктом, а не в разделе
#     ограничений внизу, где его никто не читает.
#
# ТРИ ОШИБКИ, РАЗОБРАННЫЕ С ЗАКАЗЧИКОМ И ЗАКРЫТЫЕ ЗДЕСЬ КОДОМ.
#
# 1. Фильтр по домену `.edu` НЕ ГОДИТСЯ. С 2001 года `.edu` выдаётся только
#    вузам США, аккредитованным агентствами, признанными Минобразования США.
#    Казахстанские вузы живут на `.edu.kz` — заказчик это `ksu.edu.kz`. Такой
#    фильтр пропустил бы Гарвард и заблокировал заказчика. Поэтому `edu` и
#    любой другой суффикс реестра попадает в `PROFILE_BROAD_SUFFIXES` и
#    отбрасывается из действующего списка с названной вслух причиной.
#
# 2. КЛАСС `.edu.kz` ЦЕЛИКОМ ТОЖЕ СЛИШКОМ ШИРОКО. Под ним живут форумы,
#    файлообменники, библиотеки и Moodle ДРУГИХ вузов с теми же курсами.
#    Правильно: белый список КОНКРЕТНЫХ источников на КОНКРЕТНЫЙ экзамен,
#    и задаёт его проктор. Поэтому запись вида `*.ksu.edu.kz` разрешена
#    (поддомены одного вуза: moodle, platonus), а `*.edu.kz` — нет.
#
# 3. ПОИСКОВИКИ НЕЛЬЗЯ РАЗРЕШАТЬ ПО УМОЛЧАНИЮ. Google и Яндекс показывают
#    готовый ответ прямо в выдаче — блок быстрого ответа, ИИ-обзор, — и
#    студенту не нужно никуда переходить. То есть белого списка с разрешённым
#    поисковиком не существует: разрешить поисковик значит разрешить ответ.
#    Поэтому поисковик (и ИИ-ассистент, который делает то же самое прямее)
#    вычёркивается из действующего списка, даже если проктор его туда вписал,
#    пока не задан ЯВНЫЙ флаг `--allow-search`. Флаг существует: есть экзамены,
#    где поиск разрешён правилами. Но его применение идёт в цепочку и в шапку
#    отчёта отдельным фактом, а не прячется в конфиге.
#
# ПУСТОЙ ПРОФИЛЬ = ПРЕЖНЕЕ ПОВЕДЕНИЕ: локальный мок-тест, никаких фильтров.
# Это не «всё разрешено», а «правила не задавались», и так это и записывается.
# ===========================================================================

#: Имя файла профиля в каталоге проктора. Приоритет источников — тот же, что у
#: `sessions_dir` и ключа подписи: --exam-profile PATH > PROCTOR_EXAM_PROFILE >
#: <каталог проктора>/exam-profile.json > нет профиля.
EXAM_PROFILE_FILENAME = "exam-profile.json"
EXAM_PROFILE_ENV_VAR = "PROCTOR_EXAM_PROFILE"
#: Доверенный публичный ключ для проверки подписи профиля. ЗАДАЁТСЯ ОТДЕЛЬНО
#: ОТ ПРОФИЛЯ намеренно: ключ, лежащий внутри подписанного им же файла, не
#: доказывает авторства — он доказывает только, что файл не побился по дороге.
EXAM_PROFILE_PUBKEY_ENV_VAR = "PROCTOR_EXAM_PROFILE_PUBKEY"
#: Разрешить поисковики. Переменная окружения — для точек входа без argparse.
ALLOW_SEARCH_ENV_VAR = "PROCTOR_ALLOW_SEARCH"

#: Откуда взят профиль. Строка идёт в цепочку и в шапку отчёта: «задан флагом»
#: и «подобран в каталоге проктора» требуют разного доверия.
PROFILE_SOURCE_NONE = "none"
PROFILE_SOURCE_CLI = "cli"
PROFILE_SOURCE_ENV = "env"
PROFILE_SOURCE_SESSIONS_DIR = "sessions_dir"

PROFILE_SOURCE_LABELS: dict[str, str] = {
    PROFILE_SOURCE_NONE: "профиль не задан",
    PROFILE_SOURCE_CLI: "задан флагом --exam-profile",
    PROFILE_SOURCE_ENV: f"задан переменной {EXAM_PROFILE_ENV_VAR}",
    PROFILE_SOURCE_SESSIONS_DIR: f"найден в каталоге проктора ({EXAM_PROFILE_FILENAME})",
}

#: Поля, которые ОПИСЫВАЮТ ПРАВИЛА и потому входят в канон и в хеш. Порядок
#: здесь не важен (канон сортирует ключи), важен состав: добавление поля меняет
#: хеш всем будущим профилям, поэтому список закреплён.
EXAM_PROFILE_RULE_FIELDS: tuple[str, ...] = (
    "exam_url", "allowed_origins", "allow_search",
    "institution", "exam_id", "notes", "issued_at", "issued_by",
)

#: Поля, которые ОБЪЯВЛЯЕТ ЧЕЛОВЕК, а не программа: кто выпустил правила,
#: когда, для какого вуза и какого экзамена. Пресет и инструмент разведки
#: оставляют в них заглушки `<...>`, и это сделано нарочно — подставлять их за
#: проктора нельзя. Но до ревью заглушки никто не проверял, и профиль с
#: `issued_by: "<ФИО и должность проктора>"` загружался как обычный: ядро не
#: возражало, `dropped_origins` был пуст, единственным замечанием шла
#: ненадёжная подпись. То есть МАШИННЫЙ ЧЕРНОВИК применялся как решение
#: проктора, а в подписанном отчёте следа об этом не оставалось.
PROFILE_HUMAN_FIELDS: tuple[str, ...] = (
    "institution", "exam_id", "issued_by", "issued_at",
)

#: Метка-страж, которую инструмент разведки (tools/discover) ставит в `notes`.
#: Живёт именно в `notes`, потому что `notes` входит в `EXAM_PROFILE_RULE_FIELDS`
#: — то есть в канон, в хеш и в запись цепочки. Служебный раздел `discovery`
#: ядро из канона выбрасывает, и пометка «это черновик», лежащая там, в
#: подписанный отчёт не попадала бы вовсе.
PROFILE_DRAFT_SENTINEL = "ЧЕРНОВИК-НЕ-УТВЕРЖДЁН"

#: Заглушка: `<что-нибудь>` внутри строкового поля.
_PLACEHOLDER_RE = re.compile(r"<[^>]*>")


def profile_unfilled_fields(rules: dict[str, Any]) -> list[str]:
    """Поля человека, оставшиеся незаполненными или с заглушкой `<...>`.

    Пустое поле и заглушка — одно и то же состояние: проктор не сказал, кто и
    когда выпустил эти правила. Разделять их незачем, а вот различать «проктор
    объявил» и «так и осталось от заготовки» обязательно.
    """
    raw = dict(rules or {})
    out: list[str] = []
    for name in PROFILE_HUMAN_FIELDS:
        value = str(raw.get(name) or "").strip()
        if not value or _PLACEHOLDER_RE.search(value):
            out.append(name)
    return out


def profile_has_draft_marker(rules: dict[str, Any]) -> bool:
    """Стоит ли в `notes` метка-страж машинного черновика."""
    return PROFILE_DRAFT_SENTINEL in str((rules or {}).get("notes") or "")


#: Состояния подписи профиля. Их четыре, а не два, по той же причине, по
#: которой у подписи отчёта три метки: «подпись сошлась» и «подпись сошлась с
#: ключом, которому можно верить» — разные утверждения.
PROFILE_SIG_ABSENT = "absent"              # подписи в файле нет
PROFILE_SIG_TRUSTED = "trusted"            # сошлась с ключом, переданным отдельно
PROFILE_SIG_SELF_DECLARED = "self_declared"  # сошлась с ключом из самого файла
PROFILE_SIG_BAD = "bad"                    # подпись есть и НЕ сходится
PROFILE_SIG_UNVERIFIABLE = "unverifiable"  # нечем проверить (нет cryptography)

#: Что именно доказывает каждое состояние. Формулировки осознанно скупые:
#: заказчик ломала SEB правкой конфига, и ни одна из них не имеет права
#: намекать, что подпись делает профиль неподменяемым.
PROFILE_SIG_LABELS: dict[str, str] = {
    PROFILE_SIG_ABSENT: "профиль не подписан",
    PROFILE_SIG_TRUSTED: "подпись сошлась с ключом, переданным отдельно от профиля",
    PROFILE_SIG_SELF_DECLARED: "подпись сошлась с ключом из самого файла профиля",
    PROFILE_SIG_BAD: "подпись НЕ сошлась: файл и подпись не соответствуют друг другу",
    PROFILE_SIG_UNVERIFIABLE: "подпись есть, проверить нечем",
}

PROFILE_SIG_PROVES: dict[str, str] = {
    PROFILE_SIG_ABSENT: "ничего: правила взяты из файла как есть",
    PROFILE_SIG_TRUSTED: "что эти правила выпущены владельцем доверенного ключа",
    PROFILE_SIG_SELF_DECLARED: "только то, что файл не побился по дороге: "
                               "ключ лежит в том же файле, что и подпись",
    PROFILE_SIG_BAD: "ничего: расхождение подписи и файла само является фактом",
    PROFILE_SIG_UNVERIFIABLE: "ничего: на этой машине нет пакета cryptography",
}

#: Что НЕ доказывает подпись профиля ни в одном из состояний. Эта строка едет
#: вместе с записью в цепочку, чтобы проверяющий не выводил её из документации.
PROFILE_SIG_NOT_PROVES = (
    "что профиль нельзя было подменить: он применяется процессом на машине, "
    "которой владеет студент. Профиль — улика о заявленных правилах, а не защита"
)

#: Суффиксы реестров и односоставные зоны: под каждым живут тысячи чужих
#: сайтов. Запись из этого набора в белый список не попадает НИКОГДА — ни с
#: флагом, ни без. Список не претендует на полноту PSL: он покрывает зоны,
#: которые реально встречаются в заявках вузов региона, а всё остальное
#: добирается правилом `_suffix_like()` ниже.
PROFILE_BROAD_SUFFIXES: frozenset[str] = frozenset({
    # то, из-за чего эта проверка вообще появилась: .edu с 2001 года — только
    # вузы США, а заказчик живёт на .edu.kz
    "edu", "edu.kz", "edu.ru", "ac.kz", "ac.uk", "ac.ru",
    "kz", "ru", "by", "ua", "uz", "kg", "tj", "tm", "ge", "am", "az",
    "com", "org", "net", "info", "biz", "name", "pro", "int", "gov", "mil",
    "io", "co", "ai", "app", "dev", "me", "tv", "cc", "su", "xyz", "site",
    "online", "store", "shop", "club", "top", "ws", "to",
    "com.kz", "org.kz", "net.kz", "gov.kz", "mil.kz", "int.kz", "kz.kz",
    "com.ru", "org.ru", "net.ru", "gov.ru", "com.ua", "org.ua",
    "co.uk", "org.uk", "gov.uk", "com.tr", "edu.tr", "com.cn", "edu.cn",
})

#: Вторые уровни, которые в любой стране означают «весь класс организаций»:
#: `edu.<cc>`, `ac.<cc>`, `com.<cc>`. Правило дополняет набор выше, чтобы
#: `edu.kg` или `ac.uz` не пришлось перечислять поимённо.
PROFILE_REGISTRY_LABELS: frozenset[str] = frozenset({
    "edu", "ac", "com", "co", "org", "net", "gov", "mil", "int", "or", "ne", "go",
})

#: Поисковики. Разрешать их по умолчанию нельзя: выдача содержит готовый ответ.
#: Сравнение — по регистрируемому имени и его поддоменам.
PROFILE_SEARCH_HOSTS: frozenset[str] = frozenset({
    "yandex.ru", "yandex.kz", "yandex.com", "yandex.com.tr", "ya.ru",
    "bing.com", "duckduckgo.com", "yahoo.com", "baidu.com",
    "mail.ru", "rambler.ru", "sputnik.ru", "nigma.ru",
    "startpage.com", "ecosia.org", "qwant.com", "search.brave.com",
    "searx.be", "mojeek.com", "ask.com", "aol.com", "naver.com", "daum.net",
})

#: ИИ-ассистенты. Делают то же, что блок быстрого ответа в выдаче, только
#: прямее, поэтому стоят за тем же флагом. Отдельный набор нужен для
#: ФОРМУЛИРОВКИ в журнале: «разрешён поисковик» и «разрешён ИИ-ассистент» —
#: разные строки в шапке отчёта, и сваливать их в одну нельзя.
PROFILE_ASSISTANT_HOSTS: frozenset[str] = frozenset({
    "chatgpt.com", "chat.openai.com", "openai.com", "claude.ai", "anthropic.com",
    "perplexity.ai", "copilot.microsoft.com", "you.com", "poe.com",
    "deepseek.com", "mistral.ai", "huggingface.co", "character.ai",
    "gigachat.ru", "sber.ru", "phind.com", "kagi.com", "t.me", "telegram.org",
})

#: Локальные адреса. Их разрешать можно (мок-тест, стенд вуза), но профиль,
#: указывающий на localhost, на настоящем экзамене — факт для отчёта.
PROFILE_LOCAL_HOSTS: frozenset[str] = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]",
})

#: Классы записи белого списка, которые возвращает `classify_origin()`.
ORIGIN_OK = "ok"
ORIGIN_LOCAL = "local"
ORIGIN_SEARCH = "search"
ORIGIN_ASSISTANT = "assistant"
ORIGIN_TOO_BROAD = "too_broad"
ORIGIN_INVALID = "invalid"

#: Пресет вуза заказчика. Отдаётся флагом `--exam-profile-example` как
#: заготовка, которую проктор правит под свой экзамен. Намеренно НЕ применяется
#: сам по себе: профиль, который действовал, обязан быть файлом с хешем, а не
#: строкой в коде — иначе в цепочку попадёт хеш того, чего никто не видел.
EXAM_PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "ksu": {
        "institution": "Костанайский региональный университет им. А. Байтұрсынұлы",
        "exam_id": "<код экзамена из расписания>",
        "exam_url": "https://moodle.ksu.edu.kz/login/index.php",
        # КОНКРЕТНЫЕ источники, а не класс .edu.kz: под классом лежат форумы,
        # файлообменники и Moodle других вузов с теми же курсами.
        "allowed_origins": [
            "moodle.ksu.edu.kz",
            "platonus.ksu.edu.kz",
        ],
        # Поисковики запрещены. Если правила конкретного экзамена их разрешают,
        # это задаётся ЗДЕСЬ или флагом --allow-search, и факт идёт в цепочку
        # и в шапку отчёта: молча разрешить поиск нельзя.
        "allow_search": False,
        "issued_by": "<ФИО и должность проктора>",
        "issued_at": "<дата выдачи, ISO 8601>",
        "notes": "Белый список — только LMS вуза. Поиск и ИИ-ассистенты закрыты "
                 "правилами экзамена; каждая попытка выхода за список попадает "
                 "в журнал с полным адресом.",
    },
}


def exam_profile_from_argv(argv: Sequence[str] | None = None) -> str | None:
    """Путь к профилю из `--exam-profile PATH`. None — в argv про него ничего."""
    return _value_from_argv(("--exam-profile", "--profile"), argv)


def exam_profile_from_env(env: dict[str, str] | None = None) -> str | None:
    """Путь к профилю из `PROCTOR_EXAM_PROFILE`."""
    source = os.environ if env is None else env
    raw = str(source.get(EXAM_PROFILE_ENV_VAR, "") or "").strip()
    return raw or None


def exam_profile_pubkey_from_argv(argv: Sequence[str] | None = None) -> str | None:
    """Доверенный ключ профиля из `--exam-profile-pubkey PATH`."""
    return _value_from_argv(("--exam-profile-pubkey", "--profile-pubkey"), argv)


def exam_profile_pubkey_from_env(env: dict[str, str] | None = None) -> str | None:
    """Доверенный ключ профиля из `PROCTOR_EXAM_PROFILE_PUBKEY`."""
    source = os.environ if env is None else env
    raw = str(source.get(EXAM_PROFILE_PUBKEY_ENV_VAR, "") or "").strip()
    return raw or None


def allow_search_from_argv(argv: Sequence[str] | None = None) -> bool:
    """Был ли `--allow-search` в командной строке.

    Разбор ручной, по тем же причинам, что у остальных функций этого модуля:
    `load()` вызывают из точек входа с чужими флагами, и падать из-за них нельзя.
    """
    items = list(sys.argv[1:] if argv is None else argv)
    return any(str(token).partition("=")[0] in ("--allow-search", "--allow-search-engines")
               for token in items)


def allow_search_from_env(env: dict[str, str] | None = None) -> bool:
    """Был ли разрешён поиск переменной `PROCTOR_ALLOW_SEARCH`."""
    source = os.environ if env is None else env
    raw = str(source.get(ALLOW_SEARCH_ENV_VAR, "") or "").strip().lower()
    return raw not in ("", "0", "false", "no", "off", "нет")


# ---------------------------------------------------------------------------
# Нормализация и разбор записей белого списка
# ---------------------------------------------------------------------------
def normalize_origin(raw: Any) -> str:
    """Запись белого списка -> `host[:port]` или `*.host[:port]`. "" — мусор.

    Белый список — это про ИСТОЧНИКИ, а не про транспорт, поэтому схема и путь
    отбрасываются: `https://moodle.ksu.edu.kz/mod/quiz/`, `moodle.ksu.edu.kz` и
    `HTTPS://Moodle.KSU.edu.KZ/` — одна и та же запись. Порт сохраняется: стенд
    вуза на `:8443` и его же сайт на 443 — разные источники.

    Звёздочка разрешена ТОЛЬКО как `*.host`: «host и его поддомены». `*` сам по
    себе, `*.edu.kz` и прочие способы сказать «весь класс» отбрасываются уже
    `classify_origin()`, но и здесь форма проверяется — иначе `mood*.kz`
    молча превратилось бы в точное имя с звёздочкой внутри.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    # схема и всё, что после хоста
    if "//" in text:
        text = text.split("//", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    # userinfo (user:pass@host) в белом списке смысла не имеет
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = text.strip().strip(".").lower()
    if not text:
        return ""
    wildcard = text.startswith("*.")
    if wildcard:
        text = text[2:]
    if not text or "*" in text:
        return ""
    host, port = _split_host_port(text)
    if not host:
        return ""
    # IDN: сравнение всегда идёт по punycode, иначе «мойвуз.kz» и его же
    # punycode-форма окажутся двумя разными источниками.
    try:
        host = host.encode("idna").decode("ascii")
    except Exception:
        # не IDN-совместимо (IP-литерал, подчёркивание) — оставляем как есть:
        # отбраковывать за это нельзя, 127.0.0.1 законная запись
        pass
    if not _host_like(host):
        return ""
    out = f"{host}:{port}" if port else host
    return f"*.{out}" if wildcard else out


def _split_host_port(text: str) -> tuple[str, str]:
    """`host:port` -> (host, port). IPv6 в скобках не режется по двоеточиям."""
    if text.startswith("["):
        host, sep, rest = text.partition("]")
        host = (host + sep).strip()
        port = rest.lstrip(":").strip()
        return host, port if port.isdigit() else ""
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        return host.strip(), port.strip() if port.strip().isdigit() else ""
    return text.strip(), ""


def _host_like(host: str) -> bool:
    """Похоже ли это на имя хоста или IP-литерал."""
    if not host or len(host) > 253:
        return False
    if host.startswith("[") and host.endswith("]"):
        return True
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-._:")
    return all(ch in allowed for ch in host) and ".." not in host


def _suffix_like(host: str) -> bool:
    """Запись описывает весь класс организаций, а не конкретный источник.

    Правило дополняет `PROFILE_BROAD_SUFFIXES`, чтобы не перечислять поимённо
    все `edu.<cc>`:

      * одна метка (`kz`, `edu`) — это зона целиком;
      * две метки, где первая из `PROFILE_REGISTRY_LABELS`, а вторая похожа на
        код страны (`edu.kz`, `ac.uz`, `com.tr`) — это класс организаций.

    `ksu.edu.kz` — три метки, первая не реестровая: конкретный источник, проходит.
    """
    host = host.split(":", 1)[0]
    labels = [p for p in host.split(".") if p]
    if len(labels) <= 1:
        return True
    if len(labels) == 2 and labels[0] in PROFILE_REGISTRY_LABELS and len(labels[1]) <= 3:
        return True
    return False


def _registrable(host: str) -> str:
    """Имя, по которому сравниваем с наборами поисковиков: `host` без порта."""
    return host.split(":", 1)[0].lstrip("*.")


def _in_host_set(host: str, names: frozenset[str]) -> str:
    """Имя из набора, которому принадлежит хост (сам или его поддомен). "" — нет."""
    base = _registrable(host)
    for name in names:
        if base == name or base.endswith("." + name):
            return name
    return ""


def _is_search_host(host: str) -> str:
    """Поисковик? Возвращает опознанное имя или "".

    Google живёт на десятках ccTLD (`google.kz`, `google.com.tr`), перечислять
    их поимённо бессмысленно — опознаём по регистрируемой метке.
    """
    base = _registrable(host)
    labels = base.split(".")
    if "google" in labels[:2] or base.startswith("www.google."):
        return "google"
    if labels and labels[0] in ("search", "go") and len(labels) > 1:
        inner = ".".join(labels[1:])
        found = _in_host_set(inner, PROFILE_SEARCH_HOSTS)
        if found:
            return found
    return _in_host_set(base, PROFILE_SEARCH_HOSTS)


def is_loopback_host(host: Any) -> bool:
    """Петля ли это. Принимает `host` или `host:port`, с портом и без.

    Зеркало `isLoopbackHost()` из shell/state.js. Нужно ядру, чтобы САМО
    опознать обращение на свой канал: верить присланному оболочкой признаку
    тут нельзя — подменённая оболочка как раз и назовёт петлёй что угодно.

    Форм записи одного адреса несколько, и закрыть надо все: иначе запрет
    снимается сменой написания. `::ffff:7f00:1` — тот же 127.0.0.1, только
    шестнадцатеричной записью.
    """
    text = str(host or "").strip().lower().strip(".")
    if not text:
        return False
    name, _ = _split_host_port(text)
    name = name.strip("[]")
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    if name in ("::1", "0:0:0:0:0:0:0:1", "0.0.0.0", "0"):
        return True
    if re.fullmatch(r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}", name):
        return True
    if re.fullmatch(r"::ffff:127\.\d{1,3}\.\d{1,3}\.\d{1,3}", name):
        return True
    # IPv4-mapped в шестнадцатеричной записи: предпоследняя группа 7f00..7fff
    # — это 127.0.0.0/8 (0x7f == 127).
    if ":" in name and re.fullmatch(r"[0-9a-f:]+", name):
        groups = [g for g in name.split(":") if g]
        if len(groups) >= 2 and "ffff" in groups:
            try:
                hi = int(groups[-2], 16)
            except ValueError:
                hi = -1
            if 0x7F00 <= hi <= 0x7FFF:
                return True
    return False


def is_control_channel_host(host: Any, control_port: Any) -> bool:
    """Обращение на канал оболочки к ядру: петля И порт самого ядра.

    Этот запрет не снимается ни одной записью белого списка, и проверяется он
    ЯДРОМ по собственному порту, а не по словам оболочки. По этому каналу ядро
    принимает наблюдения, и страница экзамена — сторонний сайт.
    """
    port = str(control_port or "").strip()
    if not port.isdigit() or int(port) <= 0:
        return False
    _, host_port = _split_host_port(str(host or "").strip().lower().strip("."))
    if host_port != str(int(port)):
        return False
    return is_loopback_host(host)


def classify_origin(origin: str) -> tuple[str, str]:
    """Класс записи белого списка и причина по-русски.

    Возвращает (класс, причина). Класс `ok` — запись годится как есть;
    `local` — годится, но факт стоит упомянуть в отчёте; `search` и
    `assistant` — годится только с `--allow-search`; `too_broad` и `invalid`
    не годятся никогда.
    """
    if not origin:
        return ORIGIN_INVALID, "запись не разбирается как адрес источника"
    base = _registrable(origin)
    if base in PROFILE_LOCAL_HOSTS or base.endswith(".localhost") or base.endswith(".test"):
        return ORIGIN_LOCAL, "локальный адрес: стенд или мок-тест, а не LMS вуза"
    if base in PROFILE_BROAD_SUFFIXES or _suffix_like(base):
        return ORIGIN_TOO_BROAD, (
            f"«{base}» — это зона или класс организаций целиком, а не конкретный "
            "источник: под ним лежат форумы, файлообменники, библиотеки и Moodle "
            "других вузов с теми же курсами")
    search = _is_search_host(base)
    if search:
        return ORIGIN_SEARCH, (
            f"«{search}» — поисковик: готовый ответ показывается прямо в выдаче "
            "(блок быстрого ответа, ИИ-обзор), переходить никуда не нужно")
    assistant = _in_host_set(base, PROFILE_ASSISTANT_HOSTS)
    if assistant:
        return ORIGIN_ASSISTANT, (
            f"«{assistant}» — ИИ-ассистент или мессенджер: отвечает на вопрос "
            "задания напрямую")
    return ORIGIN_OK, ""


def origin_matches(host: str, pattern: str) -> bool:
    """Подходит ли хост под запись белого списка.

    Запись без порта принимает любой порт: проктор пишет `moodle.ksu.edu.kz`, а
    не `moodle.ksu.edu.kz:443`. Запись С портом требует совпадения — иначе
    стенд на `:8443` открывал бы и обычный сайт того же имени.
    """
    if not host or not pattern:
        return False
    host_name, host_port = _split_host_port(str(host).strip().strip(".").lower())
    wildcard = pattern.startswith("*.")
    pat_name, pat_port = _split_host_port(pattern[2:] if wildcard else pattern)
    if pat_port and pat_port != host_port:
        return False
    if not host_name or not pat_name:
        return False
    if wildcard:
        return host_name == pat_name or host_name.endswith("." + pat_name)
    return host_name == pat_name


def url_host(url: Any) -> str:
    """Хост из адреса в виде `host[:port]`. "" — адрес без хоста.

    Схемы без сетевого хоста (`file:`, `data:`, `blob:`, `about:`) дают "":
    для них понятие «источник вне белого списка» не определено, и решение о
    них принимает вызывающий, а не эта функция.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "//" not in text:
        return ""
    scheme = text.split("//", 1)[0].rstrip(":").lower()
    if scheme in ("file", "data", "blob", "about", "javascript", "chrome", "devtools"):
        return ""
    rest = text.split("//", 1)[1]
    rest = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    host, port = _split_host_port(rest.strip().strip(".").lower())
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except Exception:
        pass
    return f"{host}:{port}" if port else host


# ---------------------------------------------------------------------------
# Канон, хеш и подпись профиля
# ---------------------------------------------------------------------------
def canonical_profile(rules: dict[str, Any]) -> str:
    """Каноническое представление ПРАВИЛ профиля. Основа хеша и подписи.

    Рецепт зафиксирован, потому что его обязан повторить тот, кто подписывает
    профиль на стороне вуза:

      1. берутся только поля `EXAM_PROFILE_RULE_FIELDS`, остальное отбрасывается
         (путь к файлу, подпись, служебные пометки в хеш не входят — иначе
         подпись меняла бы хеш того, что подписывает);
      2. строки обрезаются по краям, `allow_search` приводится к bool;
      3. `allowed_origins` нормализуются (`normalize_origin`), дедуплицируются
         и сортируются: один и тот же белый список, записанный в другом
         порядке или с другой схемой, обязан давать ТОТ ЖЕ хеш — иначе хеш
         описывал бы форматирование файла, а не правила экзамена;
      4. JSON с сортированными ключами, без пробелов, `ensure_ascii=False`,
         кодировка UTF-8.

    Рядом с этим хешем в цепочку идёт `file_sha256` — хеш байтов файла как он
    есть. Два хеша отвечают на два разных вопроса: «какие правила действовали»
    и «тот ли это файл».
    """
    raw = dict(rules or {})
    out: dict[str, Any] = {}
    for name in EXAM_PROFILE_RULE_FIELDS:
        value = raw.get(name)
        if name == "allowed_origins":
            items = value if isinstance(value, (list, tuple, set)) else []
            normalized = {normalize_origin(item) for item in items}
            out[name] = sorted(x for x in normalized if x)
        elif name == "allow_search":
            out[name] = bool(value) if not isinstance(value, str) else (
                value.strip().lower() in ("1", "true", "yes", "on", "да"))
        else:
            out[name] = str(value or "").strip()
    return json.dumps(out, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def profile_digest(rules: dict[str, Any]) -> str:
    """sha256 канонического представления правил, hex."""
    return hashlib.sha256(canonical_profile(rules).encode("utf-8")).hexdigest()


def _ed25519() -> Any:
    """Модуль Ed25519 или None. Нет пакета — подпись не проверяется (Р-05)."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519  # type: ignore
        return ed25519
    except Exception:
        return None


def _read_pubkey(path: str | Path | None) -> str:
    """Публичный ключ из файла: hex-строка. "" — файла нет или он не читается.

    Принимается тот же формат, что пишет `storage/report.py` рядом с отчётом:
    hex в одну строку. Это сознательно самый простой формат — ключ должен
    доезжать до проктора по почте и в мессенджере, не превращаясь по дороге.
    """
    if not path:
        return ""
    try:
        text = Path(str(path)).expanduser().read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    text = text.splitlines()[0].strip() if text else ""
    cleaned = "".join(ch for ch in text if ch not in " \t:")
    try:
        bytes.fromhex(cleaned)
    except ValueError:
        return ""
    return cleaned.lower()


def verify_profile_signature(rules: dict[str, Any], signature_hex: str,
                             embedded_key: str = "",
                             trusted_key: str = "") -> dict[str, Any]:
    """Проверить подпись профиля. Возвращает состояние, причину и ключ.

    ЧТО ЗДЕСЬ НЕЛЬЗЯ УТВЕРЖДАТЬ. Сошедшаяся подпись не делает профиль
    неподменяемым: профиль применяет процесс на машине, которой владеет
    студент, и выбор «какой файл подсунуть» остаётся за ним. Подпись отвечает
    на единственный вопрос — выпущены ли ЭТИ правила владельцем ключа, —
    и только если ключ пришёл ОТДЕЛЬНО от профиля. Ключ, лежащий внутри
    подписанного им же файла, доказывает лишь целостность файла, поэтому у
    такого случая отдельное состояние `self_declared`, а не `trusted`.

    Это прямое следствие У-13: в SEB подпись была, а сверять её было некому.
    """
    out = {"state": PROFILE_SIG_ABSENT, "reason": "", "key": "",
           "trusted_key_given": bool(trusted_key)}
    sig = str(signature_hex or "").strip().lower()
    if not sig:
        return out
    ed = _ed25519()
    if ed is None:
        out["state"] = PROFILE_SIG_UNVERIFIABLE
        out["reason"] = "пакет cryptography не установлен, подпись проверить нечем"
        return out
    try:
        sig_bytes = bytes.fromhex(sig)
    except ValueError:
        out["state"] = PROFILE_SIG_BAD
        out["reason"] = "подпись в профиле не является hex-строкой"
        return out
    blob = canonical_profile(rules).encode("utf-8")
    # Доверенный ключ проверяется ПЕРВЫМ: если он задан и не сошёлся, это
    # отказ, а не повод молча откатиться на ключ из файла.
    for key_hex, state in ((trusted_key, PROFILE_SIG_TRUSTED),
                           (embedded_key, PROFILE_SIG_SELF_DECLARED)):
        key_hex = str(key_hex or "").strip().lower()
        if not key_hex:
            continue
        try:
            public = ed.Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
            public.verify(sig_bytes, blob)
        except Exception as exc:
            out["state"] = PROFILE_SIG_BAD
            out["reason"] = (
                "подпись не сходится с "
                + ("доверенным ключом" if state == PROFILE_SIG_TRUSTED
                   else "ключом из файла профиля")
                + f": {type(exc).__name__}")
            if state == PROFILE_SIG_TRUSTED:
                return out
            continue
        out["state"] = state
        out["key"] = key_hex
        out["reason"] = PROFILE_SIG_LABELS[state]
        return out
    if out["state"] == PROFILE_SIG_ABSENT:
        out["state"] = PROFILE_SIG_BAD
        out["reason"] = ("в профиле есть подпись, но нет ключа для проверки: "
                         f"передайте доверенный ключ (--exam-profile-pubkey или "
                         f"{EXAM_PROFILE_PUBKEY_ENV_VAR})")
    return out


# ---------------------------------------------------------------------------
# Сам профиль
# ---------------------------------------------------------------------------
@dataclass
class ExamProfile:
    """Правила экзамена: какой адрес открывать и какие источники разрешены.

    Объект собирается один раз при старте (`load_exam_profile`) и дальше
    только читается. `present = False` означает «правила экзамена проктором не
    задавались» — это полноценное состояние, а не ошибка: пустой профиль
    возвращает прежнее поведение оболочки (локальный мок-тест).
    """

    # --- объявленные правила: входят в канон и в хеш ---
    exam_url: str = ""
    allowed_origins: list[str] = field(default_factory=list)
    allow_search: bool = False
    institution: str = ""
    exam_id: str = ""
    notes: str = ""
    issued_at: str = ""
    issued_by: str = ""

    # --- служебное: в канон и в хеш НЕ входит ---
    present: bool = False
    source: str = PROFILE_SOURCE_NONE
    path: str = ""
    profile_hash: str = ""
    file_sha256: str = ""
    signature_state: str = PROFILE_SIG_ABSENT
    signature_reason: str = ""
    signature_key: str = ""
    #: Поиск разрешён ФЛАГОМ запуска, а не самим профилем. Разделено намеренно:
    #: хеш описывает то, что объявил проктор, а флаг — то, что сделал оператор
    #: на этой машине. Склеить их значило бы потерять одно из двух.
    allow_search_by_flag: bool = False
    #: Действующий белый список: нормализованные записи, прошедшие разбор.
    effective_origins: list[str] = field(default_factory=list)
    #: Записи, выброшенные из действующего списка, с причиной.
    dropped_origins: list[dict[str, Any]] = field(default_factory=list)
    #: Записи вида `*.домен`, то есть КЛАСС поддоменов, а не конкретный
    #: источник. Такая запись законна (у Moodle бывают поддомены), но она
    #: принципиально шире того, о чём шёл разбор с заказчиком, и проверяющий
    #: обязан узнать, что действовал класс. Отдельным полем, а не только
    #: замечанием: замечания читают глазами, а поле печатается в отчёте.
    wildcard_origins: list[str] = field(default_factory=list)
    #: Замечания к профилю для лога, цепочки и шапки отчёта.
    warnings: list[str] = field(default_factory=list)
    #: Почему профиль не загрузился, если путь был задан.
    load_error: str = ""
    #: Поля проктора, оставшиеся заглушкой `<...>` или пустыми.
    unfilled_fields: list[str] = field(default_factory=list)
    #: В `notes` стоит метка-страж машинного черновика.
    draft_marker: bool = False

    # ------------------------------------------------------------- свойства
    @property
    def allow_search_effective(self) -> bool:
        """Разрешён ли поиск фактически: объявлен профилем ИЛИ разрешён флагом."""
        return bool(self.allow_search) or bool(self.allow_search_by_flag)

    @property
    def approved_by_human(self) -> bool:
        """Утверждал ли профиль ЧЕЛОВЕК, или это заготовка/машинный черновик.

        Два признака, и оба обязательны: ни одной заглушки `<...>` в полях
        проктора и ни одной метки-стража в `notes`. Проверка сознательно
        дешёвая и обходится одной правкой файла — она и не претендует на
        большее: заполнить `issued_by` своим именем это и есть то действие,
        которым проктор принимает на себя ответственность за список. Смысл
        проверки в том, чтобы НЕЛЬЗЯ БЫЛО ПРИМЕНИТЬ файл, которого никто не
        читал, не совершив при этом ни одного осознанного действия.

        Профиля нет — утверждать нечего, и это не «не утверждён»: отсутствие
        правил само попадает в шапку отчёта отдельной строкой.
        """
        if not self.present:
            return True
        return not self.unfilled_fields and not self.draft_marker

    @property
    def signed(self) -> bool:
        """Подпись есть и она сошлась с ключом, переданным ОТДЕЛЬНО.

        `self_declared` сюда не входит: ключ из того же файла не доказывает
        авторства, и называть такой профиль подписанным было бы ровно тем
        преувеличением, на котором сломался SEB.
        """
        return self.signature_state == PROFILE_SIG_TRUSTED

    @property
    def short_hash(self) -> str:
        """Хеш для шапки отчёта и для диктовки голосом."""
        return self.profile_hash[:12]

    @property
    def rules(self) -> dict[str, Any]:
        """Только объявленные правила — то, что входит в канон и в хеш."""
        return {
            "exam_url": self.exam_url,
            "allowed_origins": list(self.allowed_origins),
            "allow_search": bool(self.allow_search),
            "institution": self.institution,
            "exam_id": self.exam_id,
            "notes": self.notes,
            "issued_at": self.issued_at,
            "issued_by": self.issued_by,
        }

    # --------------------------------------------------------------- проверка
    def host_allowed(self, host: str) -> bool:
        """Разрешён ли хост действующим белым списком.

        Профиля нет — разрешено всё: правила не задавались, и выдумывать их
        за проктора нельзя. Решение «фильтровать или нет» принимает оболочка по
        полю `active` в `wire()`, а не эта функция.
        """
        if not self.present:
            return True
        host = str(host or "").strip().strip(".").lower()
        if not host:
            return True
        return any(origin_matches(host, pattern) for pattern in self.effective_origins)

    def url_allowed(self, url: str) -> bool:
        """Разрешён ли адрес. Адрес без сетевого хоста (`file:`) — разрешён."""
        host = url_host(url)
        if not host:
            return True
        return self.host_allowed(host)

    # ----------------------------------------------------------------- вывод
    def header_line(self) -> str:
        """Строка для ШАПКИ отчёта рядом с вердиктом.

        Две формулировки, и обе обязаны быть видны именно в шапке, а не в
        разделе ограничений внизу: «правила экзамена не задавались» — такой же
        важный факт для разбора, как и действовавший профиль. Именно
        необязательность проверки сломала SEB (У-13).
        """
        if not self.present:
            # ФЛАГ БЕЗ ФАЙЛА ПРОФИЛЯ. Раньше эта ветка возвращала только
            # «правила не задавались» и теряла `allow_search_by_flag`: факт
            # «поиск разрешён решением оператора на этой машине» уезжал в
            # цепочку (`record()`) и НЕ доходил до шапки отчёта, хотя
            # требование прямо говорит «и в цепочку, и в шапку». Молчание тут
            # опаснее обычного: правил нет, сверять поведение не с чем, а
            # поиск при этом открыт — и по документу это было не видно.
            if self.allow_search_by_flag:
                return ("правила экзамена проктором не задавались, "
                        "а поиск разрешён флагом запуска --allow-search")
            return "правила экзамена проктором не задавались"
        line = f"действовал профиль {self.short_hash}"
        # НЕУТВЕРЖДЁННЫЙ ЧЕРНОВИК — в ту же строку шапки, что и хеш. Иначе
        # документ сообщал бы «действовал профиль», умалчивая о том, что этот
        # профиль собрала программа и человек его не читал.
        if not self.approved_by_human:
            line += " — НЕУТВЕРЖДЁННЫЙ ЧЕРНОВИК: правила не объявлены человеком"
        if self.allow_search_effective:
            line += " (поисковики разрешены)"
        return line

    def record(self) -> dict[str, Any]:
        """Запись `control`/`exam_profile` для хеш-цепочки.

        Пишется ВСЕГДА, при каждом старте сессии, и отключить это нечем.
        Отсутствие профиля — тоже запись.
        """
        return {
            "present": bool(self.present),
            "profile_hash": self.profile_hash,
            "profile_hash_short": self.short_hash,
            "file_sha256": self.file_sha256,
            "profile_source": self.source,
            "profile_source_label": PROFILE_SOURCE_LABELS.get(self.source, self.source),
            "profile_path": self.path,
            "profile_signed": bool(self.signed),
            "signature_state": self.signature_state,
            "signature_label": PROFILE_SIG_LABELS.get(self.signature_state,
                                                      self.signature_state),
            "signature_proves": PROFILE_SIG_PROVES.get(self.signature_state, ""),
            # Строка едет в цепочку намеренно: проверяющий не должен выводить
            # границы доказательности подписи из документации.
            "signature_not_proves": PROFILE_SIG_NOT_PROVES,
            "signature_key": self.signature_key[:64],
            "exam_url": self.exam_url,
            "exam_url_host": url_host(self.exam_url),
            "institution": self.institution,
            "exam_id": self.exam_id,
            "issued_at": self.issued_at,
            "issued_by": self.issued_by,
            "notes": self.notes[:500],
            "allowed_origins": list(self.allowed_origins),
            "effective_origins": list(self.effective_origins),
            "dropped_origins": [dict(x) for x in self.dropped_origins],
            # Записи-классы (`*.домен`) отдельным полем: по списку источников
            # звёздочку можно не заметить, а разница между «два хоста Moodle» и
            # «весь домен вуза вместе с форумом и файлообменником» — это и есть
            # то, о чём шёл разбор с заказчиком.
            "wildcard_origins": list(self.wildcard_origins),
            "allow_search": bool(self.allow_search),
            "allow_search_effective": bool(self.allow_search_effective),
            "allow_search_by_flag": bool(self.allow_search_by_flag),
            "warnings": list(self.warnings),
            "load_error": self.load_error,
            # Утверждал ли профиль человек. В цепочку едет тремя полями, а не
            # одним: проверяющему нужно знать не только «нет», но и почему.
            "approved_by_human": bool(self.approved_by_human),
            "unfilled_fields": list(self.unfilled_fields),
            "draft_marker": bool(self.draft_marker),
            "header_line": self.header_line(),
            "canonical_recipe": "sha256 канона: поля правил, нормализованные и "
                                "отсортированные origins, JSON с сортированными "
                                "ключами, UTF-8",
        }

    def wire(self) -> dict[str, Any]:
        """Что уходит оболочке при рукопожатии, чтобы она применила фильтр.

        Оболочке нужно ровно четыре вещи: открывать ли внешний адрес, какой,
        что пропускать и считать ли поиск разрешённым. Разбор причин, хеши и
        подпись здесь тоже есть — HUD показывает хеш студенту, и он же
        называется в отчёте, так что расхождения между экраном и документом
        быть не должно.
        """
        return {
            "active": bool(self.present),
            "exam_url": self.exam_url,
            "allowed_origins": list(self.effective_origins),
            "allow_search": bool(self.allow_search_effective),
            # ЧЬЁ решение разрешить поиск: проктора, объявившего правила до
            # экзамена, или оператора на этой машине. HUD обязан говорить
            # студенту то же, что отчёт говорит комиссии, а без этого поля
            # оболочка не могла различить два случая и писала «профиль».
            "allow_search_by_flag": bool(self.allow_search_by_flag),
            # Класс поддоменов в действующем списке — для HUD и служебного
            # экрана: «разрешён весь домен» и «разрешены два хоста» студент
            # тоже должен видеть по-разному.
            "wildcard_origins": list(self.wildcard_origins),
            "profile_hash": self.profile_hash,
            "profile_hash_short": self.short_hash,
            "profile_source": self.source,
            "profile_signed": bool(self.signed),
            "signature_state": self.signature_state,
            "institution": self.institution,
            "exam_id": self.exam_id,
            # Неутверждённый черновик обязан быть виден и на экране студента:
            # он первый, кто заметит, что «правила экзамена» никто не подписал
            # своим именем. HUD показывает это строкой, оболочка — служебным
            # экраном.
            "approved_by_human": bool(self.approved_by_human),
            "unfilled_fields": list(self.unfilled_fields),
            "draft_marker": bool(self.draft_marker),
            "header_line": self.header_line(),
            "warnings": list(self.warnings),
        }

    def log_lines(self) -> list[tuple[str, str]]:
        """(уровень, строка) для лога при старте. Уровни: info | warning."""
        if not self.present:
            if self.load_error:
                return [("warning", f"профиль экзамена не загружен: {self.load_error}"),
                        ("warning", "правила экзамена не заданы — оболочка откроет "
                                    "локальный мок-тест, и это записано в цепочку")]
            return [("info", "профиль экзамена не задан: оболочка откроет локальный "
                             "мок-тест. Факт «правила экзамена не задавались» идёт "
                             "в цепочку и в шапку отчёта")]
        lines: list[tuple[str, str]] = [
            ("info", f"профиль экзамена: {self.short_hash} "
                     f"({PROFILE_SOURCE_LABELS.get(self.source, self.source)}: {self.path})"),
            ("info", f"адрес экзамена: {self.exam_url or '— не задан —'}"),
            ("info", "разрешённые источники: "
                     + (", ".join(self.effective_origins) or "— ни одного —")),
            ("info", f"подпись профиля: {PROFILE_SIG_LABELS.get(self.signature_state)}"
                     f" — доказывает {PROFILE_SIG_PROVES.get(self.signature_state)}"),
            ("warning", "профиль — УЛИКА, а не защита: он применяется процессом на "
                        "машине студента. Ценность в том, что каждая попытка выйти "
                        "за белый список попадает в журнал с полным адресом"),
        ]
        if self.allow_search_effective:
            lines.append(("warning", "ПОИСКОВИКИ РАЗРЕШЕНЫ"
                          + (" флагом --allow-search" if self.allow_search_by_flag
                             else " самим профилем")
                          + ": выдача содержит готовый ответ, переходить никуда не "
                            "нужно. Факт идёт в цепочку и в шапку отчёта"))
        for item in self.warnings:
            lines.append(("warning", f"профиль: {item}"))
        return lines


def _parse_profile_payload(raw: Any) -> tuple[dict[str, Any], str, str, list[str]]:
    """Разобрать JSON профиля. -> (правила, подпись, ключ из файла, замечания)."""
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return {}, "", "", ["файл профиля не является JSON-объектом"]
    rules: dict[str, Any] = {}
    for name in EXAM_PROFILE_RULE_FIELDS:
        if name in raw:
            rules[name] = raw[name]
    unknown = sorted(set(raw) - set(EXAM_PROFILE_RULE_FIELDS)
                     - {"signature", "public_key", "algorithm", "signed_at", "$schema"})
    if unknown:
        # Не ошибка: вуз может возить в файле свои пометки. Но в хеш они не
        # входят, и об этом надо сказать вслух — иначе проктор будет думать,
        # что его поле защищено хешем.
        warnings.append("поля не входят в канон и в хеш профиля: " + ", ".join(unknown))
    signature = str(raw.get("signature") or "").strip()
    embedded = str(raw.get("public_key") or "").strip()
    return rules, signature, embedded, warnings


def _apply_origin_policy(profile: ExamProfile) -> None:
    """Собрать ДЕЙСТВУЮЩИЙ белый список из объявленного.

    Здесь живут три разбора с заказчиком, превращённые в код: класс зоны
    (`edu`, `edu.kz`) не источник, поисковик и ИИ-ассистент — не источник без
    явного флага, мусор — не источник. Всё выброшенное попадает в
    `dropped_origins` с причиной и уезжает в цепочку: проктор обязан увидеть,
    что его запись не подействовала, а не обнаружить это по пустому фильтру.
    """
    effective: list[str] = []
    seen: set[str] = set()
    allow_search = profile.allow_search_effective
    for raw in profile.allowed_origins:
        normalized = normalize_origin(raw)
        kind, reason = classify_origin(normalized)
        if kind in (ORIGIN_OK, ORIGIN_LOCAL):
            if normalized not in seen:
                seen.add(normalized)
                effective.append(normalized)
            # КЛАСС ПОДДОМЕНОВ, А НЕ ИСТОЧНИК. `classify_origin()` такую
            # запись пропускает, и правильно: `*.ksu.edu.kz` — три метки,
            # первая не реестровая, зоной это не является, а у Moodle вуза
            # поддомены бывают по факультетам. Но молчать нельзя: под
            # `*.ksu.edu.kz` попадают и forum., и files., и library., и Moodle
            # другого факультета с теми же курсами — ровно то, что разобрано с
            # заказчиком как недопустимое. Раньше такая запись не давала НИ
            # ОДНОГО замечания, и отличить её от списка конкретных хостов по
            # отчёту было нельзя.
            if normalized.startswith("*."):
                if normalized not in profile.wildcard_origins:
                    profile.wildcard_origins.append(normalized)
                profile.warnings.append(
                    f"запись «{normalized}» разрешает ВСЕ поддомены "
                    f"«{normalized[2:]}»: это класс, а не конкретный источник. "
                    "Под него попадают форум, файлообменник, библиотека вуза и "
                    "Moodle других факультетов с теми же курсами. Если нужен "
                    "именно список — перечислите хосты поимённо")
            if kind == ORIGIN_LOCAL:
                profile.warnings.append(
                    f"в белом списке локальный адрес «{normalized}»: {reason}")
            continue
        if kind in (ORIGIN_SEARCH, ORIGIN_ASSISTANT) and allow_search:
            if normalized not in seen:
                seen.add(normalized)
                effective.append(normalized)
            profile.warnings.append(
                f"«{normalized}» оставлен в белом списке, потому что поиск разрешён: "
                + reason)
            continue
        profile.dropped_origins.append({
            "origin": str(raw), "normalized": normalized, "class": kind,
            "reason": reason,
        })
        profile.warnings.append(f"запись «{raw}» НЕ действует: {reason}")

    # Адрес самого экзамена добавляется в действующий список сам. Иначе первая
    # же загрузка страницы экзамена стала бы инцидентом «источник вне белого
    # списка» — то есть система обвиняла бы студента в том, что ему велели
    # открыть. Факт добавления виден в цепочке: запись есть в
    # `effective_origins` и отсутствует в `allowed_origins`.
    exam_host = url_host(profile.exam_url)
    if exam_host and not any(origin_matches(exam_host, p) for p in effective):
        effective.insert(0, exam_host)
        profile.warnings.append(
            f"хост адреса экзамена «{exam_host}» добавлен в действующий список "
            "автоматически: без этого страница экзамена сама была бы инцидентом")
    profile.effective_origins = effective

    if profile.present and not effective:
        profile.warnings.append(
            "действующий белый список ПУСТ: ни одна запись не прошла разбор, а "
            "адрес экзамена не задан. Оболочке нечего открывать")

    # ПРАВИЛА ЕСТЬ, А ОТКРЫВАТЬ ПО НИМ НЕЧЕГО. Отдельный случай, и раньше он
    # молчал: предупреждение ставилось только когда действующий список ПУСТ.
    # С непустым списком и без `exam_url` профиль считается действующим
    # (`present: true`), а оболочка страницу экзамена не создаёт вообще —
    # `attachExamView()` выходит на первой же строке, фильтр запросов не
    # ставится, студент остаётся в локальном мок-тесте. Шапка отчёта при этом
    # печатала зелёное «правила экзамена зафиксированы» и список разрешённых
    # источников по сессии, в которой фильтра не существовало.
    if profile.present and effective and not profile.exam_url:
        profile.warnings.append(
            "в профиле ЕСТЬ белый список, но НЕТ адреса экзамена (exam_url): "
            "оболочке нечего открывать, страница экзамена не создаётся и фильтр "
            "источников не ставится вовсе. Студент остаётся в локальном "
            "мок-тесте, а перечисленные источники фактически ни к чему не "
            "применяются. Добавьте exam_url — адрес входа в LMS")
    if profile.exam_url and not exam_host:
        profile.warnings.append(
            f"адрес экзамена «{profile.exam_url[:120]}» не содержит сетевого хоста: "
            "проверьте схему (нужен http:// или https://)")
    if profile.exam_url.lower().startswith("http://"):
        profile.warnings.append(
            "адрес экзамена открывается по http:// без шифрования: содержимое "
            "теста и ответы идут по сети в открытом виде")


def load_exam_profile(sessions_dir: Any = "", path: Any = "",
                      pubkey_path: Any = "",
                      allow_search_flag: bool = False,
                      source: str = "") -> ExamProfile:
    """Собрать профиль экзамена. Ошибка чтения не роняет запуск (Р-05).

    Приоритет источников задаёт вызывающий (`ProctorConfig.load()` разбирает
    флаг и переменную тем же способом, что для `sessions_dir`); здесь
    остаётся последний шаг — файл в каталоге проктора, который известен только
    после того, как каталог разрешён (`main.prepare_handover`).

    Профиля нет — возвращается пустой объект с `present = False`. Это
    работающее состояние: оболочка открывает локальный мок-тест, а в цепочку
    уходит запись «правила экзамена не задавались».
    """
    profile = ExamProfile(allow_search_by_flag=bool(allow_search_flag))
    candidate: Path | None = None
    chosen_source = str(source or "")
    explicit = str(path or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        chosen_source = chosen_source or PROFILE_SOURCE_CLI
    elif sessions_dir:
        in_dir = Path(str(sessions_dir)).expanduser() / EXAM_PROFILE_FILENAME
        if in_dir.is_file():
            candidate = in_dir
            chosen_source = PROFILE_SOURCE_SESSIONS_DIR
    if candidate is None:
        return profile

    profile.path = str(candidate)
    profile.source = chosen_source or PROFILE_SOURCE_CLI
    try:
        blob = candidate.read_bytes()
    except Exception as exc:
        profile.source = PROFILE_SOURCE_NONE
        profile.load_error = f"{candidate}: {exc}"
        return profile
    profile.file_sha256 = hashlib.sha256(blob).hexdigest()
    try:
        raw = json.loads(blob.decode("utf-8"))
    except Exception as exc:
        profile.source = PROFILE_SOURCE_NONE
        profile.load_error = f"{candidate}: профиль не разбирается как JSON: {exc}"
        return profile

    rules, signature, embedded, warnings = _parse_profile_payload(raw)
    if not rules and warnings:
        profile.source = PROFILE_SOURCE_NONE
        profile.load_error = f"{candidate}: {warnings[0]}"
        return profile

    profile.exam_url = str(rules.get("exam_url") or "").strip()
    origins = rules.get("allowed_origins")
    profile.allowed_origins = [str(x).strip() for x in origins
                               if str(x).strip()] if isinstance(
        origins, (list, tuple)) else []
    declared_search = rules.get("allow_search")
    profile.allow_search = (declared_search.strip().lower() in ("1", "true", "yes", "on", "да")
                            if isinstance(declared_search, str) else bool(declared_search))
    profile.institution = str(rules.get("institution") or "").strip()
    profile.exam_id = str(rules.get("exam_id") or "").strip()
    profile.notes = str(rules.get("notes") or "").strip()
    profile.issued_at = str(rules.get("issued_at") or "").strip()
    profile.issued_by = str(rules.get("issued_by") or "").strip()
    profile.warnings.extend(warnings)
    profile.present = True

    # НЕУТВЕРЖДЁННЫЙ ЧЕРНОВИК. Проверяется до хеша и до подписи, потому что
    # это вопрос не целостности файла, а того, читал ли его человек.
    profile.unfilled_fields = profile_unfilled_fields(profile.rules)
    profile.draft_marker = profile_has_draft_marker(profile.rules)
    if profile.draft_marker:
        profile.warnings.append(
            f"в notes профиля стоит метка «{PROFILE_DRAFT_SENTINEL}»: это "
            "машинный черновик инструмента разведки, собранный по фактическим "
            "запросам страницы LMS. Он НЕ является решением проктора: список "
            "обязан прочитать человек, вычеркнуть лишнее и убрать метку. "
            "Собрано при этом только то, что проходили руками, — полным "
            "списком источников вуза черновик не является")
    if profile.unfilled_fields:
        profile.warnings.append(
            "поля проктора остались заглушкой или пусты: "
            + ", ".join(profile.unfilled_fields)
            + ". Кто выпустил эти правила, когда и для какого экзамена — по "
              "профилю не установить, то есть отвечать за белый список "
              "формально некому")

    # Хеш считается по ПРАВИЛАМ в разобранном виде, а не по тому, что лежало в
    # файле: иначе лишний пробел в JSON менял бы «действовавший профиль».
    profile.profile_hash = profile_digest(profile.rules)
    sig = verify_profile_signature(profile.rules, signature, embedded,
                                   _read_pubkey(pubkey_path))
    profile.signature_state = str(sig["state"])
    profile.signature_reason = str(sig["reason"])
    profile.signature_key = str(sig["key"])
    if profile.signature_state == PROFILE_SIG_BAD:
        profile.warnings.append(f"подпись профиля: {profile.signature_reason}")
    if profile.signature_state == PROFILE_SIG_SELF_DECLARED:
        profile.warnings.append(
            "профиль подписан ключом, который лежит в этом же файле: такая "
            "подпись доказывает только целостность файла. Доверенный ключ "
            f"передаётся отдельно (--exam-profile-pubkey или {EXAM_PROFILE_PUBKEY_ENV_VAR})")
    if signature and not str(pubkey_path or "").strip():
        profile.warnings.append(
            "доверенный ключ для проверки подписи не передан: сверить профиль с "
            "эталоном вуза нечем")
    if profile.signature_state == PROFILE_SIG_ABSENT:
        # НАИМЕНЕЕ ДОВЕРЕННОЕ СОСТОЯНИЕ ПОДПИСИ МОЛЧАЛО. Замечание было для
        # `bad`, для `self_declared` и для «подпись есть, ключа нет» — а для
        # «подписи нет вовсе» не было ничего. Получалось, что самое слабое
        # состояние выглядело в отчёте спокойнее остальных: заголовок блока
        # зелёный, в списке третьим пунктом строка «профиль не подписан», и
        # всё. Файл профиля правится текстовым редактором — именно так
        # заказчик и ломала .seb, — и без подписи отличить правленный файл от
        # выданного вузом нечем вообще.
        profile.warnings.append(
            "профиль НЕ ПОДПИСАН: отличить файл, выданный вузом, от правленного "
            "руками нечем. Хеш профиля в цепочке доказывает только то, что "
            "правила не менялись ПОСЛЕ старта сессии, а не то, что их объявил "
            "проктор. Подпись выпускается вузом, доверенный ключ передаётся "
            f"отдельно (--exam-profile-pubkey или {EXAM_PROFILE_PUBKEY_ENV_VAR})")

    _apply_origin_policy(profile)
    return profile


def exam_profile_example(name: str = "ksu") -> str:
    """Пресет профиля как готовый JSON-текст. Для `--exam-profile-example`.

    Пресет не применяется сам по себе намеренно: в цепочку должен попасть хеш
    файла, который проктор видел и положил рядом с доказательствами, а не хеш
    строки из кода. Поэтому заготовка печатается, а дальше её правят руками.
    """
    preset = EXAM_PROFILE_PRESETS.get(str(name or "").strip().lower())
    if preset is None:
        known = ", ".join(sorted(EXAM_PROFILE_PRESETS))
        raise KeyError(f"неизвестный пресет профиля: {name!r} (есть: {known})")
    return json.dumps(preset, ensure_ascii=False, indent=2) + "\n"


def _default_confirm_windows() -> dict[str, float]:
    """Сколько секунд условие должно держаться, прежде чем станет событием.

    Быстрые и однозначные сигналы (второе лицо, телефон) подтверждаются быстро,
    «шумные» (взгляд, поворот головы) — дольше, иначе отчёт утонет в мусоре.
    """
    return {
        EventKind.NO_FACE.value: 2.5,
        EventKind.SECOND_FACE.value: 1.0,
        EventKind.IDENTITY_MISMATCH.value: 2.0,
        EventKind.LIVENESS_FAIL.value: 3.0,
        EventKind.PHONE_IN_FRAME.value: 0.8,
        EventKind.PHONE_RAISED.value: 0.6,
        EventKind.PHONE_AIMED_AT_SCREEN.value: 0.6,
        EventKind.FORBIDDEN_OBJECT.value: 2.0,
        EventKind.GAZE_DOWN.value: 1.8,
        EventKind.GAZE_SIDE.value: 1.8,
        EventKind.GAZE_OFF_SCREEN.value: 2.0,
        EventKind.HEAD_TURNED.value: 2.0,
        EventKind.VOICE_OTHER.value: 1.2,
        EventKind.SPEECH_WITHOUT_LIP_MOTION.value: 2.5,
        EventKind.SENSOR_LOST.value: 3.0,
    }


def _default_cooldowns() -> dict[str, float]:
    """Пауза между повторными событиями одного вида — защита от спама."""
    return {
        EventKind.NO_FACE.value: 15.0,
        EventKind.SECOND_FACE.value: 20.0,
        EventKind.IDENTITY_MISMATCH.value: 20.0,
        EventKind.LIVENESS_FAIL.value: 60.0,
        EventKind.PHONE_IN_FRAME.value: 12.0,
        EventKind.PHONE_RAISED.value: 12.0,
        EventKind.PHONE_AIMED_AT_SCREEN.value: 12.0,
        EventKind.FORBIDDEN_OBJECT.value: 30.0,
        EventKind.GAZE_DOWN.value: 10.0,
        EventKind.GAZE_SIDE.value: 10.0,
        EventKind.GAZE_OFF_SCREEN.value: 10.0,
        EventKind.HEAD_TURNED.value: 15.0,
        EventKind.VOICE_OTHER.value: 15.0,
        EventKind.SPEECH_WITHOUT_LIP_MOTION.value: 20.0,
        # Наушники никуда не денутся между циклами проверки окружения: событие
        # должно быть одной записью в отчёте, а не одной записью на каждый опрос.
        # Дедупликацию держит «прогон» в EventEngine (`STATEFUL_EXTERNAL`: одно
        # подключение = один инцидент с растущей длительностью), поэтому
        # cooldown здесь — только страховка для встроенной замены движка.
        # Значение осознанно МЕНЬШЕ окна отпускания прогона (30 с): после
        # отключения устройства инцидент закрывается через 30 с, и повторное
        # подключение должно давать новый инцидент сразу, а не ждать ещё.
        # 120 с (и даже 45 с) ломали повторный показ на защите: второе
        # подключение подряд не давало события вообще.
        EventKind.AUDIO_DEVICE_CONNECTED.value: 20.0,
        EventKind.MULTIPLE_DISPLAYS.value: 120.0,
        EventKind.VIRTUAL_CAMERA.value: 120.0,
        EventKind.REMOTE_ACCESS_SOFTWARE.value: 120.0,
        EventKind.VIRTUAL_MACHINE.value: 300.0,
        EventKind.SCREEN_RECORDING.value: 60.0,
        EventKind.BLACKLISTED_PROCESS.value: 60.0,
        EventKind.SENSOR_LOST.value: 30.0,
        EventKind.WINDOW_BLUR.value: 5.0,
        EventKind.FULLSCREEN_EXIT.value: 5.0,
    }


@dataclass
class ProctorConfig:
    """Все параметры системы в одном месте. Значения по умолчанию — рабочие."""

    # ------------------------------------------------------- режим развёртывания
    #: "classroom" (по умолчанию) | "remote". Управляет аудио-каналом целиком,
    #: см. докстринг модуля и Р-10. Задаётся в config.json, переопределяется
    #: переменной PROCTOR_EXAM_MODE и флагом --exam-mode / --remote / --classroom.
    exam_mode: str = EXAM_MODE_CLASSROOM

    # ------------------------------------------------------------------ транспорт
    ws_host: str = DEFAULT_WS_HOST
    ws_port: int = DEFAULT_WS_PORT
    ws_ping_interval: float = 20.0
    #: Предел одного сообщения оболочки — для всех типов, КРОМЕ `screen_evidence`
    #: (1 МиБ, больше оболочке не нужно). Больше — ядро не обрабатывает: отказ в
    #: лог и `error`/`too_large` отправителю (`_on_message`).
    #: Транспортный потолок WS-кадра выше: снимку окна экзамена до 3 МБ
    #: (SCREEN_EVIDENCE_MAX_BYTES) в base64 нужно ~4 МиБ, а сообщение больше
    #: max_size библиотека не отклоняет, а РВЁТ соединение (1009). Поэтому
    #: max_size = max(ws_max_message, WS_TRANSPORT_MAX_BYTES), а 1 МиБ для
    #: прочих типов проверяет само ядро.
    ws_max_message: int = 1 << 20

    # -------------------------------------------------------------- захват камеры
    camera_index: int = 0
    frame_width: int = 640
    frame_height: int = 480
    target_fps: int = 15
    camera_reopen_delay: float = 1.5          # пауза перед попыткой переоткрыть
    camera_max_read_failures: int = 15        # подряд неудачных чтений -> переоткрытие
    camera_read_timeout: float = 1.0          # сколько ждать свежий кадр в read()
    camera_probe_max_index: int = 5           # до какого индекса искать камеры

    # ------------------------------------------------- частоты запуска детекторов
    face_every_n_frames: int = 1              # FaceMesh — каждый кадр
    yolo_every_n_frames: int = 3              # YOLO — каждый третий
    identity_interval: float = 3.0            # сверка личности, сек
    env_interval: float = 20.0                # проверки окружения, сек
    audio_interval: float = 0.1               # опрос аудио, 10 Гц
    status_interval: float = 0.5              # status в оболочку, 2 Гц
    risk_decay_interval: float = 1.0          # спад risk-score, сек

    # --------------------------------- подтверждение событий (окна и гистерезис)
    confirm_window: float = 1.5               # дефолт, если вида нет в confirm_windows
    release_window: float = 1.0               # сколько условие должно отсутствовать, чтобы «отпустить»
    cooldown_sec: float = 10.0                # дефолтный cooldown
    confirm_windows: dict[str, float] = field(default_factory=_default_confirm_windows)
    cooldowns: dict[str, float] = field(default_factory=_default_cooldowns)

    # ------------------------------------------------------------ взгляд и голова
    # FaceAnalyzer отдаёт нормированные девиации (|dev| > 1 — выход за персональный
    # порог после калибровки). Основное решение принимается по ним и по gaze_zone;
    # лимиты в градусах — запасной путь, если девиаций в наблюдении нет.
    gaze_dev_limit: float = 1.0               # |gaze_dev_h| / |gaze_dev_v| выше -> мимо экрана
    gaze_off_dev_limit: float = 1.8           # явно вне карты экрана
    head_dev_limit: float = 1.0               # |head_dev_yaw| / |head_dev_pitch|
    gaze_yaw_limit: float = 20.0              # запасной порог по |gaze_yaw|
    gaze_pitch_down_limit: float = 18.0       # запасной порог взгляда вниз
    gaze_off_screen_limit: float = 32.0       # запасной порог «вне экрана»
    head_yaw_limit: float = 25.0              # град, поворот головы
    head_pitch_limit: float = 22.0            # град, наклон головы
    blink_ear_threshold: float = 0.20         # EAR ниже -> глаз закрыт
    liveness_no_blink_sec: float = 35.0       # лицо есть, морганий нет -> подозрение на фото
    mouth_open_speech: float = 0.22           # mouth_open_ratio выше -> губы двигаются

    # ------------------------------------------------------- объекты в кадре (YOLO)
    yolo_model_path: str = "models/yolov8n.pt"      # веса для ultralytics
    yolo_onnx_path: str = "models/yolov8n.onnx"     # веса для onnxruntime
    yolo_conf: float = 0.35
    yolo_imgsz: int = 416
    phone_labels: list[str] = field(default_factory=lambda: ["cell phone", "mobile phone", "phone"])
    forbidden_labels: list[str] = field(default_factory=lambda: ["book", "laptop", "tv", "monitor", "tvmonitor"])
    phone_min_area_ratio: float = 0.0035      # мелкие ложные срабатывания отбрасываем
    phone_raised_margin: float = 0.12         # доля высоты кадра: насколько выше центра лица
    phone_vertical_ratio: float = 1.15        # h/w телефона -> держат вертикально

    # ----------------------------------------------------------------- личность
    identity_threshold: float = 0.35          # косинусная близость эмбеддингов
    identity_fail_streak: int = 3             # подряд несовпадений -> IDENTITY_MISMATCH
    #: Сколько эталонных кадров СОБИРАЕМ. Было 5 — этого хватает на средний
    #: эмбеддинг, но не хватает на оценку разброса: 5 кадров дают 10 пар, и
    #: sigma по ним — шум. 12 кадров дают 66 пар, то есть устойчивую оценку для
    #: персонального порога (`identity_personal_min_samples`). Цена — несколько
    #: лишних эмбеддингов на калибровке, то есть десятки миллисекунд CPU.
    #: Стадию всё равно ограничивают `calibration_samples * 3` попыток и
    #: `calibration_timeout` (см. main.py), поэтому в плохих условиях
    #: калибровка не зависает, а честно собирает сколько смогла.
    identity_enroll_samples: int = 12
    #: Сколько эталонных кадров ДОСТАТОЧНО, чтобы эталон считался снятым.
    #: Сознательно отвязано от `identity_enroll_samples`: иначе рост числа
    #: собираемых кадров автоматически поднял бы и минимум, и в тёмной
    #: аудитории калибровка начала бы падать — ровно та предвзятость, против
    #: которой всё остальное здесь и сделано. Мало кадров -> эталон есть,
    #: персонального порога нет, `threshold_source = "global"` в detail.
    identity_enroll_min: int = 4

    # Пригодность кадра как УСЛОВИЕ сверки (решение Р-11).
    #
    # Почему это не «ещё одни пороги качества»: без них непригодный кадр
    # попадал в окно голосования как плохая проверка, и цепочка «темно ->
    # 5 плохих проверок -> IDENTITY_MISMATCH (вес 60) -> PAUSE -> LOCK»
    # останавливала экзамен примерно за 40 секунд по причине, которая лежит в
    # модели и в освещении, а не в поведении студента. NIST IR 8280 (декабрь
    # 2019) документирует дифференциалы FNMR распознавания лиц по полу, расе и
    # стране рождения; Respondus в собственном замере получил, что рост FPR при
    # тёмном освещении для тона кожи 6 по Фицпатрику значим и ИСЧЕЗАЕТ при
    # контроле освещения. Значит, различать «не смогли посмотреть» и
    # «посмотрели и не совпало» обязан код.
    #
    # Непригодный кадр -> состояние `undetermined`, отдельный счётчик и
    # формулировка «условия съёмки не позволяют подтвердить личность».
    # В окно голосования он не попадает.
    identity_quality_gate: bool = True
    #: Порог «кадр непригоден» СЛАБЕЕ порога отбраковки на калибровке: там можно
    #: ждать идеальный кадр, в сессии студент сидит как сидит.
    identity_verify_min_face_px: int = 70     # меньшая сторона bbox лица, px
    identity_verify_min_det_score: float = 0.45
    identity_verify_min_sharpness: float = 12.0   # дисперсия лапласиана кропа
    identity_verify_max_yaw_ratio: float = 0.34   # профиль сравнивать нельзя
    #: Яркость и контраст считаются по ОБЛАСТИ ЛИЦА, а не по кадру: яркая лампа
    #: за спиной при тёмном лице — ровно тот случай, который средняя яркость
    #: кадра скрывает, и ровно тот, что ломает распознавание.
    identity_min_face_luma: float = 42.0      # средняя яркость лица, 0..255
    identity_max_face_luma: float = 238.0     # пересвет: лицо «выбито» в белое
    identity_min_face_contrast: float = 10.0  # СКО яркости области лица
    #: Окно и доля непригодных кадров, после которых канал честно сообщает, что
    #: подтвердить личность не позволяют условия съёмки.
    identity_unusable_window: int = 10
    identity_unusable_ratio: float = 0.6
    identity_unusable_min_frames: int = 3

    # Персональная калибровка порога — прямой ответ на дифференциалы FNMR:
    # порог подстраивается под то, как модель видит ИМЕННО этого человека,
    # вместо одного глобального числа для всех.
    #
    # Калибровка может только ОСЛАБИТЬ порог (верхняя граница — глобальные
    # 0.35): ужесточение — это ровно тот вред, который документирует NIST, а
    # глобальное значение уже проверено на нашей модели. Нижняя граница
    # защищает от обратной ошибки: у человека с «рыхлым» эталоном порог не
    # должен упасть настолько, что сверку пройдёт посторонний.
    identity_personal_threshold: bool = True
    identity_personal_k: float = 2.0          # порог = mean(pairwise cos) - k*sigma
    identity_personal_min_samples: int = 6    # меньше кадров -> глобальный порог
    identity_threshold_floor: float = 0.22    # ниже не опускаемся никогда
    identity_threshold_ceiling: float = 0.35  # и не выше глобального порога

    # -------------------------------------------------------------------- аудио
    # ВНИМАНИЕ: все пороги ниже работают только при exam_mode == "remote".
    # В "classroom" канал выключен целиком и эти значения не читаются.
    audio_sample_rate: int = 16000
    audio_rms_threshold: float = 0.015        # ниже — тишина
    audio_owner_threshold: float = 0.60       # близость к голосу владельца
    audio_enroll_seconds: float = 6.0
    speech_without_lips_sec: float = 2.5      # речь без движения губ — наушник/подсказчик

    # --------------------------------------------------------------- окружение
    env_blacklist: list[str] = field(default_factory=lambda: [
        "teamviewer", "anydesk", "rustdesk", "radmin", "vnc", "realvnc", "tightvnc",
        "ammyy", "supremo", "dwservice", "chrome remote desktop", "parsec",
        "obs", "obs64", "obs-studio", "camtasia", "bandicam", "fraps",
        "zoom", "discord", "skype", "telegram", "whatsapp", "slack",
        "cheatengine", "manycam", "obs-virtualcam", "droidcam", "epoccam", "iriun",
    ])
    env_allow_multiple_displays: bool = False

    # Проверка активных аудио-устройств (замена аудио-анализа для угрозы У-02
    # «наушник со связью»). Работает в ОБОИХ режимах: это чтение состояния ОС,
    # а не анализ сигнала. Важная граница: проверка видит только устройства
    # ЭТОГО компьютера — гарнитура, спаренная с телефоном студента, системе не
    # видна и закрывается очным контролем и fusion-связкой «длинная пауза ->
    # мгновенный развёрнутый ответ».
    env_check_audio_devices: bool = True
    #: Отдельный интервал для аудио-устройств: общий `env_interval` = 20 с даёт
    #: окно обнаружения до 20.7 с, а подключение наушников нужно видеть сразу.
    #: Проверка дешёвая и кеширована (`_TTL_AUDIO` = 6 с в env_checks).
    env_audio_interval: float = 4.0
    #: Разрешённые аудио-устройства: имя, производитель или транспорт
    #: («logitech h390», «usb»), сравнение по подстроке без учёта регистра.
    #: Снимает ТОЛЬКО совпавшие устройства — выданная преподавателем гарнитура
    #: перестаёт быть инцидентом, а виртуальный аудио-вывод и чужие наушники
    #: рядом продолжают фиксироваться. Это рабочая ручка для аудирования,
    #: а не глухой выключатель.
    env_allowed_audio_devices: list[str] = field(default_factory=list)
    #: Разрешить наушники как КЛАСС устройств (аудирование, где звук нужен
    #: всем). Гасит только наушники и колонки; виртуальное аудио-устройство
    #: и внешний вывод продолжают фиксироваться. По умолчанию выключено.
    env_allow_headphones: bool = False
    #: Дополнительные названия наушников/гарнитур — для локальных марок и для
    #: локализаций ОС, которых нет во встроенном каталоге (он покрывает
    #: ru/en/kk/uk/de/tr/zh). Сравнение по подстроке, регистр не важен.
    env_headphone_names: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------- риск
    risk_warn: float = RISK_WARN
    risk_pause: float = RISK_PAUSE
    risk_lock: float = RISK_LOCK
    risk_max: float = 100.0
    risk_half_life: float = 90.0              # сек, за которые вклад события падает вдвое
    risk_send_delta: float = 0.5              # не шлём risk, если изменился меньше, чем на это

    # ------------------------------------------------------ доказательства и пути
    #: Куда писать сессии. Задаёт ПРОКТОР: --sessions-dir > PROCTOR_SESSIONS_DIR
    #: > config.json > "sessions". В классе указывается на сетевую папку вуза
    #: или на USB-носитель — то, что ушло на чужой диск, студент не удалит.
    sessions_dir: str = DEFAULT_SESSIONS_DIR
    #: Куда писать, если каталог проктора недоступен на запись. Сессия при этом
    #: помечается `degraded_handover`, и отчёт обязан это показать.
    sessions_fallback_dir: str = DEFAULT_SESSIONS_DIR
    #: Откуда взято значение `sessions_dir`: cli | env | config | default.
    #: Нужно для сообщения человеку: «каталог задан флагом» и «каталог взят по
    #: умолчанию» требуют разной реакции, если запись не удалась.
    sessions_dir_source: str = "default"
    #: Папка проктора: флешка или сетевая папка вуза, куда после экзамена
    #: КОПИРУЕТСЯ готовый пакет. Пусто — доставки нет, пакет остаётся рядом с
    #: каталогом сессии (прежнее поведение). Сессия сюда НЕ пишется.
    #: Приоритет: --deliver-to > PROCTOR_DELIVER_DIR > config.json > "".
    deliver_dir: str = ""
    #: Откуда взято значение `deliver_dir`: cli | env | config | default.
    deliver_dir_source: str = "default"
    #: Ключ подписи, переданный проктором. Задан -> метка подписи `institution`
    #: (доказывает авторство). Пусто -> ключ генерируется на машине студента,
    #: метка `self_signed` (доказывает только целостность при передаче).
    signing_key_path: str = ""
    #: Собирать `<session_id>.proctor.zip` по завершении сессии. Это и есть то,
    #: что проктор физически забирает.
    package_on_end: bool = True

    # ------------------------------------------------------- профиль экзамена
    #: Путь к `exam-profile.json`. Приоритет тот же, что у каталога сессий:
    #: --exam-profile > PROCTOR_EXAM_PROFILE > <каталог проктора>/exam-profile.json
    #: > нет профиля. Пусто и файла в каталоге нет -> прежнее поведение
    #: оболочки (локальный мок-тест), и этот факт тоже идёт в цепочку.
    exam_profile_path: str = ""
    #: Откуда взят путь: cli | env | sessions_dir | none. Нужно для записи в
    #: цепочку: «профиль задан флагом» и «профиль подобран в каталоге» —
    #: разные уровни доверия на разборе.
    exam_profile_source: str = PROFILE_SOURCE_NONE
    #: Доверенный публичный ключ для проверки подписи профиля. Передаётся
    #: ОТДЕЛЬНО от профиля: ключ из подписанного им же файла доказывает только
    #: целостность файла (см. `verify_profile_signature`).
    exam_profile_pubkey: str = ""
    #: Разрешить поисковики и ИИ-ассистенты в белом списке. По умолчанию НЕТ:
    #: выдача содержит готовый ответ, переходить никуда не нужно. Флаг нужен
    #: для экзаменов, где поиск разрешён правилами; факт его применения идёт в
    #: цепочку и в шапку отчёта, а не прячется в конфиге.
    allow_search: bool = False
    evidence_subdir: str = "evidence"
    models_dir: str = "models"
    db_filename: str = "evidence.sqlite"
    report_filename: str = "report.html"
    save_evidence: bool = True
    #: Ниже этой severity КЛИП не пишется. Клип — 15 с видео вокруг момента,
    #: дорогой и по диску, и по приватности, поэтому порог у него свой.
    evidence_min_severity: str = "medium"
    #: Ниже этой severity не снимается КАДР камеры. По умолчанию "info": кадр
    #: прикладывается к каждому неслужебному событию, включая события
    #: оболочки и окружения — без него инцидент в отчёте нечем проверить.
    evidence_frame_min_severity: str = "info"
    evidence_jpeg_quality: int = 85
    evidence_clip_seconds: float = 15.0
    sign_report: bool = False
    report_key_path: str = ""

    # ------------------------------------------------------------- калибровка
    calibration_samples: int = 30             # кадров на одну точку/стадию
    calibration_timeout: float = 20.0         # сек на стадию, дальше честный провал
    #: Сколько секунд после смены цели кадры взгляда НЕ берём в калибровку.
    #: Глазу нужно 0.25–0.45 с на реакцию и саккаду, конвейеру камеры — ~0.1 с;
    #: без паузы первые кадры точки сетки — это взгляд на ПРЕДЫДУЩУЮ точку.
    #: Центру нужно больше: человек сначала читает новую подсказку и только
    #: потом переводит глаза на перекрестье, а база центра — ноль всех порогов.
    calibration_settle_center: float = 1.0
    calibration_settle_grid: float = 0.5
    #: Сколько секунд после последнего действия калибровки взгляда взгляд и
    #: поворот головы не становятся инцидентами (см. main._face_observations).
    calibration_gaze_quiet: float = 2.0
    #: Сколько всего секунд тишины взгляда допускается за сессию. Калибровка с
    #: парой повторов укладывается в ~60 с; без потолка любой, кто прочитал
    #: токен канала, глушил бы взгляд на весь экзамен командой раз в 2 с.
    calibration_gaze_quiet_budget: float = 90.0
    #: Точка сетки готова не раньше, чем через столько секунд сбора после
    #: паузы на саккаду: при 30 к/с 15 кадров набираются за 0.5 с, и у
    #: человека с медленной реакцией почти все они — ещё взгляд на прошлую точку.
    calibration_grid_min_collect: float = 0.6

    # ------------------------------------------------------------------ каналы
    enable_vision: bool = True
    enable_gaze: bool = True
    enable_identity: bool = True
    #: Разрешение на аудио-анализ. Действует ТОЛЬКО в exam_mode == "remote":
    #: в "classroom" канал принудительно выключен (см. `audio_analysis_enabled`).
    enable_audio: bool = True
    enable_env: bool = True
    headless: bool = False                    # без камеры (проверка протокола)
    mock: bool = False                        # сценарные события для демо
    mock_loop: bool = True
    mock_pause_between_loops: float = 10.0

    # --------------------------------------------------------------- служебное
    log_level: str = "INFO"

    #: Сырые переопределения секций из config.json (например {"objects": {"conf": 0.5}}).
    #: Накладываются поверх рассчитанных секций — так можно донастроить чужой модуль,
    #: не трогая его код и не заводя новое поле здесь.
    module_overrides: dict[str, Any] = field(default_factory=dict)

    # ========================================================= режим и инварианты
    def __post_init__(self) -> None:
        # Намерение пользователя по аудио-каналу хранится отдельно от
        # действующего значения: иначе переключение classroom -> remote
        # не вернуло бы канал обратно.
        self._audio_requested: bool = bool(self.enable_audio)
        self._audio_applied: bool = bool(self.enable_audio)
        self.normalize()

    def normalize(self) -> None:
        """Привести режимные инварианты в согласованное состояние.

        Вызывается после `__init__`, после каждого `apply()` и при смене режима.
        Идемпотентна. Единственный инвариант: в `classroom` аудио-анализ выключен,
        каким бы ни был `enable_audio` в конфиге (Р-10).

        Прямое присваивание полю извне (`cfg.enable_audio = False` из обработчика
        `--no-audio`) распознаётся как новое намерение и не теряется при смене
        режима: сравниваем поле с тем значением, которое записали сами.
        """
        self.exam_mode = normalize_exam_mode(self.exam_mode)
        if not hasattr(self, "_audio_requested"):  # на случай обхода __post_init__
            self._audio_requested = bool(self.enable_audio)
            self._audio_applied = bool(self.enable_audio)
        if bool(self.enable_audio) != bool(self._audio_applied):
            self._audio_requested = bool(self.enable_audio)
        self.enable_audio = bool(self._audio_requested) and self.is_remote
        self._audio_applied = self.enable_audio
        # Аудио-устройства опрашиваются чаще общего цикла проверок ОС, но не
        # чаще раза в секунду: ниже выигрыша нет (в env_checks стоит кеш на 6 с),
        # и не реже самого цикла — иначе отдельный интервал бессмыслен.
        try:
            audio_every = float(self.env_audio_interval)
        except (TypeError, ValueError):
            audio_every = 4.0
        self.env_audio_interval = max(1.0, min(audio_every,
                                               max(1.0, float(self.env_interval))))

    @property
    def is_classroom(self) -> bool:
        return self.exam_mode == EXAM_MODE_CLASSROOM

    @property
    def is_remote(self) -> bool:
        return self.exam_mode == EXAM_MODE_REMOTE

    @property
    def audio_analysis_enabled(self) -> bool:
        """Действующее состояние аудио-канала: режим И разрешение в конфиге."""
        return bool(self._audio_requested) and self.is_remote

    @property
    def audio_off_reason(self) -> str:
        """Почему аудио-канал молчит. Пустая строка — канал включён."""
        if self.audio_analysis_enabled:
            return ""
        if self.is_classroom:
            return CLASSROOM_AUDIO_OFF_REASON
        return "выключен в конфигурации (enable_audio = false)"

    @property
    def disabled_event_kinds(self) -> list[str]:
        """Виды событий, правила для которых в этом режиме не регистрируются."""
        if self.audio_analysis_enabled:
            return []
        return [kind.value for kind in AUDIO_ANALYSIS_KINDS]

    def set_audio_enabled(self, enabled: bool) -> bool:
        """Явно разрешить или запретить аудио-анализ (например по `--no-audio`).

        Запрет сохраняется при смене режима; разрешение действует только в
        `remote`. Возвращает действующее состояние канала.
        """
        self._audio_requested = bool(enabled)
        self.normalize()
        return self.enable_audio

    def set_exam_mode(self, mode: Any) -> str:
        """Сменить режим и пересчитать зависящие от него каналы."""
        self.exam_mode = normalize_exam_mode(mode)
        self.normalize()
        return self.exam_mode

    def exam_mode_info(self) -> dict[str, Any]:
        """Сводка режима для `hello`, лога и шапки отчёта."""
        return {
            "exam_mode": self.exam_mode,
            "audio_analysis": self.audio_analysis_enabled,
            "audio_off_reason": self.audio_off_reason,
            "disabled_kinds": self.disabled_event_kinds,
            # замена аудио-анализа для угрозы «наушник со связью» (У-02)
            "audio_device_check": bool(self.enable_env and self.env_check_audio_devices),
        }

    # ------------------------------------------------------------------ CLI
    @staticmethod
    def add_cli_arguments(parser: Any) -> Any:
        """Зарегистрировать флаги режима в чужом argparse.

        Вызывается владельцем парсера (точка входа), сразу после — `apply_cli_args`.
        """
        group = parser.add_argument_group("режим экзамена")
        group.add_argument(
            "--exam-mode", "--mode", dest="exam_mode", default=None,
            choices=list(EXAM_MODES),
            help="classroom (аудитория, аудио-анализ выключен) | remote (из дома)",
        )
        group.add_argument(
            "--classroom", dest="exam_mode", action="store_const",
            const=EXAM_MODE_CLASSROOM, help="то же, что --exam-mode classroom",
        )
        group.add_argument(
            "--remote", dest="exam_mode", action="store_const",
            const=EXAM_MODE_REMOTE, help="то же, что --exam-mode remote",
        )
        return parser

    def apply_cli_args(self, args: Any) -> None:
        """Наложить разобранные аргументы: режим и явный запрет аудио-канала."""
        mode = getattr(args, "exam_mode", None)
        if mode:
            self.set_exam_mode(mode)
        # `--no-audio` понимается как намерение, а не как разовое значение поля:
        # иначе последующая смена режима вернула бы канал обратно.
        if getattr(args, "no_audio", False):
            self.set_audio_enabled(False)

    # ======================================================== пути и производные
    @property
    def root(self) -> Path:
        return ROOT

    def _abs(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (ROOT / p)

    @property
    def sessions_path(self) -> Path:
        return self._abs(self.sessions_dir)

    @property
    def sessions_fallback_path(self) -> Path:
        """Запасной локальный каталог. Абсолютный путь относительно репозитория."""
        return self._abs(self.sessions_fallback_dir or DEFAULT_SESSIONS_DIR)

    @property
    def deliver_path(self) -> Path | None:
        """Папка проктора абсолютным путём. None — доставка не настроена."""
        value = str(self.deliver_dir or "").strip()
        return self._abs(value) if value else None

    @property
    def signing_key_abs(self) -> Path | None:
        """Абсолютный путь к ключу проктора. None — ключ не задан."""
        value = str(self.signing_key_path or "").strip()
        return self._abs(value) if value else None

    @property
    def signature_authority(self) -> str:
        """Чем подписываем: `institution` (ключ проктора) или `self_signed`.

        Это ЗАЯВЛЕННОЕ намерение, и ничего больше. Фактическую метку ставит
        `storage/report.resolve_authority()`: он требует, чтобы файл ключа
        существовал и не совпадал с машинным ключом по умолчанию, иначе
        понижает метку до `self_signed` и записывает причину в файл подписи.
        Раньше этой проверки не было, и несуществующий путь молча превращался
        в новый ключ на этой машине плюс метку «авторство доказано».

        И даже поставленная метка `institution` НЕ является доказательством
        авторства: её ставит процесс на машине экзамена. Доказательной она
        становится только после сверки публичного ключа с эталоном вуза на
        стороне экзаменатора.
        """
        return "institution" if str(self.signing_key_path or "").strip() else "self_signed"

    @property
    def report_signing_key(self) -> str:
        """Ключ для подписи отчёта: ключ проктора, иначе прежний report_key_path."""
        explicit = self.signing_key_abs
        if explicit is not None:
            return str(explicit)
        return str(self.report_key_path or "")

    def handover_info(self) -> dict[str, Any]:
        """Сводка передачи доказательств для `hello`, лога и шапки отчёта."""
        return {
            "sessions_dir": str(self.sessions_path),
            "sessions_dir_source": self.sessions_dir_source,
            "sessions_fallback_dir": str(self.sessions_fallback_path),
            "signature_authority": self.signature_authority,
            "signing_key": str(self.signing_key_abs or ""),
            "package_on_end": bool(self.package_on_end),
            "deliver_dir": str(self.deliver_path or ""),
            "deliver_dir_source": self.deliver_dir_source,
        }

    # --------------------------------------------------------- профиль экзамена
    @property
    def exam_profile(self) -> ExamProfile:
        """Действующий профиль экзамена. Пустой объект, пока не загружен.

        Хранится атрибутом, а не полем dataclass, по той же причине, по которой
        `handover_resolved` живёт атрибутом: `asdict()` видит только объявленные
        поля, и вложенный dataclass в `to_dict()` попадал бы сырой структурой.
        Отдаём его в `to_dict()` явно — записью `exam_profile`, в том же виде,
        в котором она уходит в цепочку.
        """
        found = getattr(self, "_exam_profile", None)
        if isinstance(found, ExamProfile):
            return found
        empty = ExamProfile(allow_search_by_flag=bool(self.allow_search))
        self._exam_profile = empty
        return empty

    def resolve_exam_profile(self) -> ExamProfile:
        """Загрузить профиль по разрешённым путям и запомнить результат.

        Вызывается ПОСЛЕ того, как каталог проктора разрешён
        (`main.prepare_handover`): последний источник в приоритете — файл
        `exam-profile.json` в этом каталоге, и до разрешения каталога его
        искать негде.
        """
        profile = load_exam_profile(
            sessions_dir=self.sessions_path,
            path=self.exam_profile_path,
            pubkey_path=self._abs(self.exam_profile_pubkey) if self.exam_profile_pubkey else "",
            allow_search_flag=bool(self.allow_search),
            source=self.exam_profile_source,
        )
        if profile.present or profile.load_error:
            self.exam_profile_source = profile.source
            self.exam_profile_path = profile.path
        self._exam_profile = profile
        return profile

    def exam_profile_info(self) -> dict[str, Any]:
        """Сводка профиля для `hello`, лога, цепочки и шапки отчёта.

        Один и тот же словарь во всех четырёх местах — по той же причине, что у
        `policy_info()`: расхождение документа и поведения это именно то, на чём
        ловят на защите.
        """
        return self.exam_profile.record()

    @property
    def models_path(self) -> Path:
        return self._abs(self.models_dir)

    @property
    def yolo_model_abs(self) -> Path:
        return self._abs(self.yolo_model_path)

    def to_dict(self) -> dict[str, Any]:
        """Конфиг для соседних модулей (контракт: config: dict).

        Отдаём и плоские ключи, и именованные секции: модули читают конфиг по
        схеме `config[section][key]` -> `config[f"{section}_{key}"]` -> `config[key]`,
        поэтому секции здесь — это способ передать им наши значения, не навязывая
        остальные их дефолты. Плюс абсолютные пути: детекторам не нужно угадывать
        рабочий каталог процесса.
        """
        self.normalize()  # дёшево и идемпотентно; страхует от правки полей «снаружи»
        d = asdict(self)
        d["root"] = str(ROOT)
        d["sessions_path"] = str(self.sessions_path)
        d["sessions_fallback_path"] = str(self.sessions_fallback_path)
        d["deliver_path"] = str(self.deliver_path or "")
        d["signature_authority"] = self.signature_authority
        d["report_signing_key"] = self.report_signing_key
        d["models_path"] = str(self.models_path)
        d["yolo_model_abs"] = str(self.yolo_model_abs)
        # Режим виден и плоским ключом: детекторы читают его через свой
        # `_cfg(config, section, key, default)` с откатом на плоский ключ.
        d["exam_mode"] = self.exam_mode
        d["audio_analysis_enabled"] = self.audio_analysis_enabled
        d["audio_off_reason"] = self.audio_off_reason
        d["disabled_event_kinds"] = self.disabled_event_kinds
        # Профиль экзамена — в том же виде, в каком он уходит в цепочку.
        # Отчёт читает его из цепочки (там он защищён хешем), а отсюда — только
        # как запасной источник, если цепочка недоступна.
        d["exam_profile"] = self.exam_profile_info()
        d.update(self.sections())
        return d

    def sections(self) -> dict[str, dict[str, Any]]:
        """Секции конфига под интерфейсы соседних модулей."""
        base = self._base_sections()
        for name, override in (self.module_overrides or {}).items():
            if isinstance(override, dict):
                node = base.setdefault(name, {})
                node.update(override)
        # Режим сильнее любой секции из config.json: включить аудио-анализ в
        # аудитории нельзя даже вручную — это решение Р-10, а не настройка.
        base.setdefault("audio", {})["enabled"] = self.audio_analysis_enabled
        base["audio"]["exam_mode"] = self.exam_mode
        base["audio"]["disabled_reason"] = self.audio_off_reason
        base.setdefault("events", {})["disabled_kinds"] = self.disabled_event_kinds
        return base

    def _mode_filtered(self, table: dict[str, float]) -> dict[str, float]:
        """Убрать из таблицы порогов виды событий, выключенные режимом.

        Поля `confirm_windows` / `cooldowns` остаются полными (это значения для
        `remote`), но в секцию `events` уходит только то, что в этом режиме
        действительно может сработать.
        """
        disabled = set(self.disabled_event_kinds)
        return {k: v for k, v in dict(table).items() if k not in disabled}

    def _base_sections(self) -> dict[str, dict[str, Any]]:
        return {
            "capture": {
                "camera_index": self.camera_index,
                "width": self.frame_width,
                "height": self.frame_height,
                "fps": self.target_fps,
            },
            "objects": {
                "enabled": self.enable_vision,
                "imgsz": self.yolo_imgsz,
                "conf": self.yolo_conf,
                "model_path": str(self._abs(self.yolo_onnx_path)),
                "weights_pt": str(self.yolo_model_abs),
            },
            "face_mesh": {
                "max_num_faces": 2,
            },
            "identity": {
                "enabled": self.enable_identity,
                "threshold": self.identity_threshold,
                "check_interval_s": self.identity_interval,
                "enroll_frames": self.identity_enroll_samples,
                "min_enroll_frames": max(3, min(self.identity_enroll_min,
                                                self.identity_enroll_samples)),
                "model_root": str(self.models_path),
                # пригодность кадра как условие сверки (Р-11)
                "quality_gate": self.identity_quality_gate,
                "verify_min_face_px": self.identity_verify_min_face_px,
                "verify_min_det_score": self.identity_verify_min_det_score,
                "verify_min_sharpness": self.identity_verify_min_sharpness,
                "verify_max_yaw_ratio": self.identity_verify_max_yaw_ratio,
                "min_face_luma": self.identity_min_face_luma,
                "max_face_luma": self.identity_max_face_luma,
                "min_face_contrast": self.identity_min_face_contrast,
                "unusable_window": self.identity_unusable_window,
                "unusable_ratio": self.identity_unusable_ratio,
                "unusable_min_frames": self.identity_unusable_min_frames,
                # персональный порог (ответ на дифференциалы FNMR)
                "personal_threshold": self.identity_personal_threshold,
                "personal_k": self.identity_personal_k,
                "personal_min_samples": self.identity_personal_min_samples,
                "threshold_floor": self.identity_threshold_floor,
                # потолок калибровки не может быть выше глобального порога:
                # калибровка имеет право только ослаблять проверку
                "threshold_ceiling": min(self.identity_threshold_ceiling,
                                         self.identity_threshold),
            },
            "liveness": {
                "no_blink_timeout_s": self.liveness_no_blink_sec,
            },
            "audio": {
                # В classroom здесь всегда False: AudioMonitor увидит это в
                # конструкторе и не станет даже проверять микрофон.
                "enabled": self.audio_analysis_enabled,
                "exam_mode": self.exam_mode,
                "disabled_reason": self.audio_off_reason,
                "sample_rate": self.audio_sample_rate,
                "rms_gate": self.audio_rms_threshold,
                "owner_threshold": self.audio_owner_threshold,
                "enroll_min_speech_s": self.audio_enroll_seconds,
                "enroll_max_wait_s": self.audio_enroll_seconds * 3.0,
            },
            "env": {
                "blacklist": list(self.env_blacklist),
                "allow_multiple_displays": self.env_allow_multiple_displays,
                "interval": self.env_interval,
                # проверка устройств вывода звука — в обоих режимах
                "exam_mode": self.exam_mode,
                "check_audio_devices": self.env_check_audio_devices,
                # отдельный, более частый интервал: задержка обнаружения
                # наушников не должна равняться общему циклу проверок ОС
                "audio_interval": self.env_audio_interval,
                "allowed_audio_devices": list(self.env_allowed_audio_devices),
                "allow_headphones": self.env_allow_headphones,
                "headphone_names": list(self.env_headphone_names),
            },
            "events": {
                "confirm_window": self.confirm_window,
                "release_window": self.release_window,
                "cooldown_sec": self.cooldown_sec,
                "confirm_windows": self._mode_filtered(self.confirm_windows),
                "cooldowns": self._mode_filtered(self.cooldowns),
                # Движок не должен регистрировать правила выключенного канала:
                # в аудитории VOICE_OTHER и SPEECH_WITHOUT_LIP_MOTION не существуют.
                "disabled_kinds": self.disabled_event_kinds,
                "exam_mode": self.exam_mode,
            },
            "risk": {
                "warn": self.risk_warn,
                "pause": self.risk_pause,
                "lock": self.risk_lock,
                "max": self.risk_max,
                "half_life": self.risk_half_life,
            },
            "report": {
                # Ключ подписи отчёта: ключ проктора (--signing-key) сильнее
                # прежнего report_key_path. Метка подписи считается по тому же
                # признаку, но ставится уже в report.py — после чтения ключа.
                "key_path": self.report_signing_key,
                "authority": self.signature_authority,
            },
            "storage": {
                "sessions_path": str(self.sessions_path),
                "sessions_fallback_path": str(self.sessions_fallback_path),
                "sessions_dir_source": self.sessions_dir_source,
                "package_on_end": bool(self.package_on_end),
                "deliver_path": str(self.deliver_path or ""),
                "deliver_dir_source": self.deliver_dir_source,
                "signing_key": str(self.signing_key_abs or ""),
                "signature_authority": self.signature_authority,
                "evidence_subdir": self.evidence_subdir,
                "db_filename": self.db_filename,
                "report_filename": self.report_filename,
                "jpeg_quality": self.evidence_jpeg_quality,
                "clip_seconds": self.evidence_clip_seconds,
                "min_severity": self.evidence_min_severity,
                "frame_min_severity": self.evidence_frame_min_severity,
            },
        }

    # ============================================================ переопределения
    def apply(self, overrides: dict[str, Any]) -> list[str]:
        """Наложить словарь переопределений. Возвращает список непонятых ключей.

        Аудио-канал: явный `enable_audio` (или `audio.enabled`) запоминается как
        намерение, но действует только в `remote` — в аудитории канала нет.
        """
        known = {f.name for f in fields(self)}
        unknown: list[str] = []
        for key, value in (overrides or {}).items():
            if key == "enable_audio":
                self._audio_requested = _coerce(True, value)
                self.enable_audio = self._audio_requested
                continue
            if key == "audio" and isinstance(value, dict) and "enabled" in value:
                self._audio_requested = _coerce(True, value["enabled"])
            if key == "exam_mode":
                self.exam_mode = normalize_exam_mode(value)
                continue
            if key in known:
                setattr(self, key, _coerce(getattr(self, key), value))
            elif key in SECTION_NAMES and isinstance(value, dict):
                # секция чужого модуля — передаём как есть, не разбирая
                node = self.module_overrides.setdefault(key, {})
                node.update(value)
            elif isinstance(value, dict):
                # вложенная секция — разворачиваем по именам полей
                unknown.extend(f"{key}.{k}" for k in self.apply(value))
            else:
                unknown.append(key)
        self.normalize()
        return unknown

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        argv: Sequence[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple["ProctorConfig", list[str]]:
        """Собрать конфиг: значения по умолчанию + config.json, если он есть.

        Возвращает (config, предупреждения). Битый JSON не роняет сайдкар —
        попадает в предупреждения, работаем на значениях по умолчанию.

        Режим экзамена накладывается после файла: `PROCTOR_EXAM_MODE`, затем флаг
        из `argv` (`--exam-mode` / `--remote` / `--classroom`). `argv=None` — это
        `sys.argv`; `argv=[]` отключает просмотр командной строки. Чужие флаги
        игнорируются, разбора argparse здесь нет.
        """
        cfg = cls()
        warnings: list[str] = []
        candidates: list[Path] = []
        if path:
            candidates.append(Path(path).expanduser())
        else:
            candidates.append(ROOT / CONFIG_FILENAME)
            candidates.append(_HERE / CONFIG_FILENAME)

        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception as exc:  # битый JSON — не причина падать
                warnings.append(f"не удалось прочитать {candidate}: {exc}")
                continue
            if not isinstance(raw, dict):
                warnings.append(f"{candidate}: ожидался JSON-объект")
                continue
            unknown = cfg.apply(raw)
            if unknown:
                warnings.append(f"{candidate}: неизвестные ключи: {', '.join(sorted(unknown))}")
            warnings.append(f"конфиг загружен из {candidate}")
            if cfg.sessions_dir != DEFAULT_SESSIONS_DIR:
                cfg.sessions_dir_source = "config"
            if str(cfg.deliver_dir or "").strip():
                cfg.deliver_dir_source = "config"
            break
        else:
            if path:
                warnings.append(f"файл конфига не найден: {path}")

        # --- каталог сессий и ключ подписи: окружение, затем командная строка ---
        # Порядок тот же, что у режима экзамена: CLI сильнее переменной,
        # переменная сильнее config.json. Точка входа без собственного флага
        # (мок-сайдкар, make-цель) всё равно получит значение из окружения.
        dir_env = sessions_dir_from_env(env)
        if dir_env:
            cfg.sessions_dir = dir_env
            cfg.sessions_dir_source = "env"
            warnings.append(f"{SESSIONS_DIR_ENV_VAR}={dir_env}")
        dir_cli = sessions_dir_from_argv(argv)
        if dir_cli:
            cfg.sessions_dir = dir_cli
            cfg.sessions_dir_source = "cli"
            warnings.append(f"каталог сессий задан флагом CLI: {dir_cli}")

        # --- папка проктора для доставки пакета: тот же порядок ---
        deliver_env = deliver_dir_from_env(env)
        if deliver_env:
            cfg.deliver_dir = deliver_env
            cfg.deliver_dir_source = "env"
            warnings.append(f"{DELIVER_DIR_ENV_VAR}={deliver_env}")
        deliver_cli = deliver_dir_from_argv(argv)
        if deliver_cli:
            cfg.deliver_dir = deliver_cli
            cfg.deliver_dir_source = "cli"
            warnings.append(f"папка проктора задана флагом CLI: {deliver_cli}")

        # --- профиль экзамена: путь, доверенный ключ, разрешение поиска ---
        # Тот же порядок и тот же стиль разбора, что у каталога сессий: флаг
        # сильнее переменной, переменная сильнее config.json. САМ ФАЙЛ здесь не
        # читается: последний источник в приоритете — `exam-profile.json` в
        # каталоге проктора, а каталог становится известен только после
        # `prepare_handover()`. Чтение делает `resolve_exam_profile()`.
        profile_env = exam_profile_from_env(env)
        if profile_env:
            cfg.exam_profile_path = profile_env
            cfg.exam_profile_source = PROFILE_SOURCE_ENV
            warnings.append(f"{EXAM_PROFILE_ENV_VAR}={profile_env}")
        profile_cli = exam_profile_from_argv(argv)
        if profile_cli:
            cfg.exam_profile_path = profile_cli
            cfg.exam_profile_source = PROFILE_SOURCE_CLI
            warnings.append(f"профиль экзамена задан флагом CLI: {profile_cli}")

        pubkey_env = exam_profile_pubkey_from_env(env)
        if pubkey_env:
            cfg.exam_profile_pubkey = pubkey_env
            warnings.append(f"{EXAM_PROFILE_PUBKEY_ENV_VAR}={pubkey_env}")
        pubkey_cli = exam_profile_pubkey_from_argv(argv)
        if pubkey_cli:
            cfg.exam_profile_pubkey = pubkey_cli
            warnings.append(f"доверенный ключ профиля задан флагом CLI: {pubkey_cli}")

        if allow_search_from_env(env):
            cfg.allow_search = True
            warnings.append(f"{ALLOW_SEARCH_ENV_VAR}: поисковики разрешены")
        if allow_search_from_argv(argv):
            cfg.allow_search = True
            warnings.append("--allow-search: поисковики разрешены флагом CLI")
        if cfg.allow_search:
            warnings.append(
                "поисковики и ИИ-ассистенты разрешены: выдача содержит готовый "
                "ответ, переходить никуда не нужно. Факт идёт в хеш-цепочку и в "
                "шапку отчёта")

        key_env = signing_key_from_env(env)
        if key_env:
            cfg.signing_key_path = key_env
            warnings.append(f"{SIGNING_KEY_ENV_VAR}={key_env}")
        key_cli = signing_key_from_argv(argv)
        if key_cli:
            cfg.signing_key_path = key_cli
            warnings.append(f"ключ подписи задан флагом CLI: {key_cli}")

        # --- режим экзамена: окружение, затем командная строка ---
        from_env = exam_mode_from_env(env)
        if from_env:
            cfg.set_exam_mode(from_env)
            warnings.append(f"{EXAM_MODE_ENV_VAR}={from_env}")
        from_cli = exam_mode_from_argv(argv)
        if from_cli:
            cfg.set_exam_mode(from_cli)
            warnings.append(f"режим задан флагом CLI: {from_cli}")

        cfg.normalize()
        if cfg.is_classroom:
            warnings.append(
                "режим аудитории (classroom): аудио-анализ выключен — "
                f"{CLASSROOM_AUDIO_OFF_REASON}; угроза «наушник со связью» "
                "закрыта проверкой активных аудио-устройств"
            )
        else:
            warnings.append(
                "режим remote: аудио-канал "
                + ("включён" if cfg.audio_analysis_enabled else "выключен в конфиге")
            )

        if cfg.signature_authority == "institution":
            # НЕ «будет доказывать авторство». Метку institution ставит тот же
            # процесс, который собирает отчёт, по единственному признаку —
            # передан ли флаг. Проверяемой она становится только тогда, когда
            # экзаменатор сверит публичный ключ со своим эталоном, и даже
            # тогда означает «у процесса был доступ к ключу», а не «подписал
            # вуз»: ключ читается на этой машине.
            warnings.append(
                f"запрошена подпись ключом учреждения: {cfg.signing_key_abs}. "
                "Метка institution сама по себе ничего не доказывает — она "
                "становится проверяемой только после сверки публичного ключа с "
                "эталоном вуза на стороне экзаменатора "
                "(verify_report.py --trusted-pub). Ключ при этом читается НА этой "
                "машине, то есть R3 из модели угроз флагом не закрывается"
            )
        else:
            warnings.append(
                "ключ подписи не задан (--signing-key): отчёт подписывается ключом "
                "с этой машины, подпись доказывает только целостность при передаче, "
                "но не авторство"
            )

        return cfg, warnings

    def confirm_window_for(self, name: str) -> float:
        return float(self.confirm_windows.get(name, self.confirm_window))

    def cooldown_for(self, name: str) -> float:
        return float(self.cooldowns.get(name, self.cooldown_sec))


def _coerce(current: Any, value: Any) -> Any:
    """Привести значение из JSON к типу текущего значения поля."""
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "да")
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return current
    if isinstance(current, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return current
    if isinstance(current, str):
        return str(value)
    if isinstance(current, list):
        return list(value) if isinstance(value, (list, tuple)) else current
    if isinstance(current, dict):
        if isinstance(value, dict):
            merged = dict(current)
            merged.update(value)
            return merged
        return current
    return value


__all__ = [
    "ProctorConfig",
    "ExamProfile",
    "ROOT",
    "CONFIG_FILENAME",
    "SECTION_NAMES",
    "EXAM_MODES",
    "EXAM_MODE_CLASSROOM",
    "EXAM_MODE_REMOTE",
    "EXAM_MODE_ENV_VAR",
    "SESSIONS_DIR_ENV_VAR",
    "SIGNING_KEY_ENV_VAR",
    "DELIVER_DIR_ENV_VAR",
    "DEFAULT_SESSIONS_DIR",
    "AUDIO_ANALYSIS_KINDS",
    "CLASSROOM_AUDIO_OFF_REASON",
    "normalize_exam_mode",
    "exam_mode_from_argv",
    "exam_mode_from_env",
    "sessions_dir_from_argv",
    "sessions_dir_from_env",
    "signing_key_from_argv",
    "signing_key_from_env",
    "deliver_dir_from_argv",
    "deliver_dir_from_env",
    # --- профиль экзамена ---
    "EXAM_PROFILE_FILENAME",
    "EXAM_PROFILE_ENV_VAR",
    "EXAM_PROFILE_PUBKEY_ENV_VAR",
    "ALLOW_SEARCH_ENV_VAR",
    "EXAM_PROFILE_PRESETS",
    "EXAM_PROFILE_RULE_FIELDS",
    "PROFILE_SOURCE_NONE",
    "PROFILE_SOURCE_CLI",
    "PROFILE_SOURCE_ENV",
    "PROFILE_SOURCE_SESSIONS_DIR",
    "PROFILE_SOURCE_LABELS",
    "PROFILE_SIG_ABSENT",
    "PROFILE_SIG_TRUSTED",
    "PROFILE_SIG_SELF_DECLARED",
    "PROFILE_SIG_BAD",
    "PROFILE_SIG_UNVERIFIABLE",
    "PROFILE_SIG_LABELS",
    "PROFILE_SIG_PROVES",
    "PROFILE_SIG_NOT_PROVES",
    "ORIGIN_OK",
    "ORIGIN_LOCAL",
    "ORIGIN_SEARCH",
    "ORIGIN_ASSISTANT",
    "ORIGIN_TOO_BROAD",
    "ORIGIN_INVALID",
    "canonical_profile",
    "profile_digest",
    "verify_profile_signature",
    "classify_origin",
    "normalize_origin",
    "origin_matches",
    "url_host",
    "load_exam_profile",
    "exam_profile_example",
    "exam_profile_from_argv",
    "exam_profile_from_env",
    "exam_profile_pubkey_from_argv",
    "exam_profile_pubkey_from_env",
    "allow_search_from_argv",
    "allow_search_from_env",
]

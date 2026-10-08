"""
Передача доказательств проктору: каталог, пакет, код сверки, семантика подписи.

Модуль отвечает на вопрос заказчика «а как экзаменатор получит доказательства
все?». До него ответ был «никак»: отчёт, журнал, кадры и подпись оставались в
`sessions/` на машине студента, а `export_report` лишь пересобирал HTML там же.

Здесь пять независимых вещей.

1. КАТАЛОГ СЕССИЙ ЗАДАЁТ ПРОКТОР (`resolve_sessions_dir`).
   В компьютерном классе каталог указывают на сетевую папку вуза или на
   USB-носитель. Каталог проверяется на запись ДО начала сессии — пробной
   записью, а не наличием пути: сетевая папка может существовать и быть
   read-only. Недоступен — пишем в запасной локальный и помечаем это как
   `degraded_handover`, потому что молча писать на машину студента, пока
   проктор думает, что копия ушла на сервер, — худший из возможных отказов.

   ЧЕГО ЭТО НЕ ДАЁТ, и это надо произносить самим. «Что ушло на чужой диск,
   студент удалить не может» — верно ТОЛЬКО если у него на шаре есть право
   записи и нет права удаления и перезаписи (append-only). `check_writable()`
   доказывает ровно обратное — что писать можно, — и свой пробный файл сам же
   удаляет, то есть в типовой конфигурации право удаления есть. Append-only —
   требование к развёртыванию, оно записано в `docs/LIMITATIONS.md`, и система
   его не проверяет и проверить не может.

   Второе: `source` («флаг --sessions-dir», «переменная окружения») сообщает о
   себе тот же процесс, который запустил студент. Отличить шару проктора от
   локального каталога студента манифест не может и не притворяется, что
   может. Единственный надёжный детектор отсутствия пакета — СОДЕРЖИМОЕ шары
   против списка группы, а не отчёт. Это строка в runbook, а не фича.

   Третье: деградация вычисляется один раз, при старте. Шара, отвалившаяся
   посреди экзамена, флага не даёт.

2. ПАКЕТ ОДНИМ ФАЙЛОМ (`build_package`).
   `<session_id>.proctor.zip` рядом с каталогом сессии. Внутри — отчёт,
   журнал, кадры, подпись, публичный ключ, метаданные и `manifest.json` со
   списком файлов и их sha256. Пакет — то, что проктор физически забирает.

3. КОД СВЕРКИ (`session_code`) — ИНВЕНТАРНАЯ БИРКА, НЕ АУТЕНТИФИКАТОР.
   Шесть символов base32, выведенные ДЕТЕРМИНИРОВАННО из genesis-хеша
   цепочки и идентификатора студента.

   Что код даёт. Он выводится из genesis, а genesis — из защищённых
   метаданных сессии, поэтому проверяющий пересчитывает код сам
   (`expected_code`) и видит манифест, собранный не из этой цепочки. Плюс код
   показывается на экране при старте (`SESSION_STARTED`, `status`): если
   проктор при старте обходит класс и переписывает коды НА БУМАГУ, код ловит
   «перезапустил сессию и сдал ту, что вышла чище» — другая сессия даёт
   другой код. Контролем код становится только в момент переноса на бумагу,
   и этот шаг описан в `memory/demo-runbook.md`.

   Чего код НЕ даёт, хотя прежняя версия этого модуля утверждала обратное.
   «Список кодов» до экзамена существовать не может: код выведен из genesis,
   genesis — из `meta()`, где лежит `started_at`. Проктор получает коды
   только из самих пакетов, значит пропажа пакета проверкой по кодам НЕ
   обнаруживается — обнаруживается сверкой числа пакетов на шаре со списком
   группы. И код НЕ привязывает пакет к человеку: `student_id` приходит из UI
   студента, а genesis — из его же сессии.

   Почему код НЕ входит в защищённые genesis-метаданные: genesis выводится из
   них, вывести код из genesis и одновременно положить его внутрь нельзя —
   это цикл. Код лежит в `summary` (а он попадает в цепочку записью
   `session_close`, то есть защищён) и пересчитывается из genesis + student_id.
   Сам genesis манифеста обязан быть сверен с `sessions.genesis_hash` внутри
   `evidence.sqlite` — иначе пересчёт замкнут на числе, которое выбрал
   подписывающий. Эту сверку делает `cross_check_manifest`.

4. ЧЕСТНАЯ СЕМАНТИКА ПОДПИСИ (`authority_info`).
   Ключ на машине студента (`self_signed`) доказывает ТОЛЬКО целостность при
   передаче: студент может собрать новый журнал с нужными событиями,
   пересчитать хеш-цепочку и переподписать тем же ключом — подпись сойдётся.

   Метка `institution` (путь к ключу задал проктор через `--signing-key`) сама
   по себе НЕ доказывает ничего: её ставит тот же процесс, который собрал
   пакет, то есть процесс на машине студента. Поэтому метка существует в двух
   состояниях, и `authority_info(authority, verified=...)` их различает:

     * `verified=False` — публичный ключ с эталоном вуза НЕ сверен. Сила
       утверждения ровно как у `self_signed`: целостность да, авторство нет.
     * `verified=True`  — публичный ключ совпал с эталоном, который проверяющий
       задал САМ (`--trusted-pub`). Только тогда можно говорить об авторстве,
       и даже тогда корректная формулировка — «в момент подписания у процесса
       был доступ к ключу вуза», а не «подписал вуз»: приватный ключ в этой
       схеме читается процессом на машине студента, где студент администратор.

   Из этого же следует, что `--signing-key` НЕ закрывает R3 из модели угроз:
   он приносит ключ вуза на атакуемую машину. Меняется класс ущерба — вместо
   подделки одного отчёта утечка одного ключа обесценивает все отчёты потока.

5. ДОСТАВКА В ПАПКУ ПРОКТОРА (`probe_deliver_dir`, `deliver_package`).
   Отдельно от каталога сессий: сессия пишется ЛОКАЛЬНО (обычный каталог
   сессий), а в папку проктора — флешку или сетевую папку вуза, `--deliver-to`
   / `PROCTOR_DELIVER_DIR` / `config.json:deliver_dir` — после экзамена
   КОПИРУЕТСЯ один готовый пакет. Во время экзамена туда не пишется ничего,
   кроме пробного файла при старте.

   Копия делается так, чтобы ничего чужого не тронуть: имя открывается с
   исключительным созданием (`xb`), занятое имя -> соседнее `-2`, `-3` …,
   ничего в папке не переименовывается, не удаляется и не перезаписывается,
   кроме недописанной (или не сошедшейся по sha256) копии этой же попытки.
   После `fsync` копия перечитывается и сверяется по sha256 с локальным пакетом.

   ЧЕГО ЭТО НЕ ДАЁТ — те же оговорки, что в п. 1, плюс свои.
   * Копию делает процесс на машине студента. Удалить её из папки потом может
     любой, у кого есть право удаления на шаре, — append-only по-прежнему
     требование к развёртыванию, а не свойство кода.
   * «sha256 сверен» значит: файл, который ОС отдаёт по этому пути после
     `fsync`, совпал с пакетом. Перечитывание может прийти из кеша ОС, а не с
     носителя; извлечённая без «безопасного извлечения» флешка всё ещё может
     потерять хвост. Окончательная проверка — `verify_report.py` на машине
     экзаменатора.
   * Папку, которой нет, стартовая проверка создаёт, если существует её
     родитель. Если это точка монтирования неподключённой флешки, папка
     появится на ЛОКАЛЬНОМ диске и «доставка» туда ничего не доставит. Поэтому
     факт создания говорится в логе вслух, а сама доставка папку уже не
     создаёт: пропала папка — это ошибка доставки, а не повод завести новую.

Своей криптографии здесь нет: sha256 из `hashlib`, подпись — `sign_report()`
из `storage/report.py` (Ed25519 через `cryptography`).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ===========================================================================
# Константы
# ===========================================================================
#: Переменная окружения с каталогом сессий. Нужна точкам входа, чей argparse
#: про каталог не знает (мок-сайдкар, make-цели, .command-скрипты).
SESSIONS_DIR_ENV_VAR = "PROCTOR_SESSIONS_DIR"

#: Переменная окружения с ключом вуза — парная к `--signing-key`.
SIGNING_KEY_ENV_VAR = "PROCTOR_SIGNING_KEY"

#: Переменная окружения с папкой проктора — парная к `--deliver-to`. Туда
#: после экзамена КОПИРУЕТСЯ готовый пакет; сама сессия пишется локально.
DELIVER_DIR_ENV_VAR = "PROCTOR_DELIVER_DIR"

#: Версия формата пакета. Меняется при любом изменении состава manifest.json.
PACKAGE_FORMAT = "qih-proctor-package/v1"

#: Расширение пакета. Двойное осознанно: `.zip` открывается любым архиватором,
#: `.proctor` сразу говорит, что это не просто архив, а комплект доказательств.
PACKAGE_SUFFIX = ".proctor.zip"

#: Домен хеширования кода сверки. Защищает от переноса кода между системами.
CODE_DOMAIN = "qih-proctor-code/v1"

#: Длина кода сверки. Шесть символов base32 — 30 бит: человек диктует их
#: голосом, и на поток в 30 студентов коллизия практически исключена.
CODE_LENGTH = 6

#: Файлы комплекта, по одному в корне каталога сессии. Порядок — порядок
#: важности для проверяющего: сначала то, что он читает, потом то, чем
#: проверяет, потом метаданные.
PACKAGE_FILES: tuple[str, ...] = (
    "report.html",
    "report.html.sig",
    "report.html.pub",
    "evidence.sqlite",
    "meta.json",
    "summary.json",
    # Деградированный режим Р-05: SQLite недоступен, журнал в JSONL. Пакет
    # обязан увезти и его, иначе в этом режиме доказательств не остаётся.
    "events.jsonl",
    "handover.json",
)

#: Подкаталоги с кадрами и клипами — забираются целиком.
PACKAGE_DIRS: tuple[str, ...] = ("evidence",)

#: Имя манифеста внутри пакета и в каталоге сессии.
MANIFEST_NAME = "manifest.json"

#: Имя записки о передаче в каталоге сессии.
HANDOVER_NAME = "handover.json"

#: Файл-проба для проверки каталога на запись.
_PROBE_NAME = ".proctor-write-probe"

#: Файл-проба для папки проктора. Имя своё: в папке проктора могут лежать
#: пробы других машин класса, и путать их с пробой каталога сессий незачем.
_DELIVER_PROBE_NAME = ".proctor-deliver-probe"

#: Сколько соседних имён (`-2`, `-3` …) пробовать, прежде чем сдаться.
_DELIVER_MAX_SUFFIX = 999

#: Кусок потокового копирования: пакет с клипами в память целиком не лезет.
_DELIVER_CHUNK = 1 << 20

# --- семантика подписи -----------------------------------------------------
AUTHORITY_SELF = "self_signed"
AUTHORITY_INSTITUTION = "institution"
AUTHORITY_NONE = "none"

#: Что метка `institution` НЕ значит. Вынесено в константу, потому что эту
#: фразу обязаны печатать одинаково verify_report.py, HTML-отчёт и лог старта.
INSTITUTION_UNVERIFIED_NOTE = (
    "метку «institution» ставит тот же процесс, который собрал пакет, то есть "
    "процесс на машине студента: достаточно запустить сайдкар с --signing-key на "
    "любой свой файл ключа. Пока публичный ключ не сверен с эталоном вуза "
    "(--trusted-pub), метка не доказывает ничего сверх self-signed")

#: Что `institution` значит ДАЖЕ при совпавшем эталоне. Формально корректная
#: формулировка: сильнее этого схема с ключом на машине студента не даёт.
INSTITUTION_VERIFIED_NOTE = (
    "в момент подписания у процесса был доступ к приватному ключу вуза. Это НЕ "
    "равно «подписал вуз»: по модели угроз студент на своей машине администратор, "
    "и принесённый туда ключ может быть скопирован — тогда им подписывается любая "
    "сессия задним числом. Утечка одного ключа обесценивает все отчёты потока")

_AUTHORITY_TABLE: dict[str, dict[str, str]] = {
    AUTHORITY_SELF: {
        "authority": AUTHORITY_SELF,
        "label": "ключ на машине студента (self-signed)",
        "proves": "целостность при передаче: файл не испортился и не изменился "
                  "по дороге от студента к экзаменатору",
        "not_proves": "авторство: приватный ключ лежит на той же машине, поэтому "
                      "студент может собрать другой журнал, пересчитать хеш-цепочку "
                      "и переподписать тем же ключом — подпись сойдётся",
        "verdict": "ЦЕЛОСТНОСТЬ ПОДТВЕРЖДЕНА, АВТОРСТВО — НЕТ",
        "advice": "Усилить это можно только ключом, которого на машине экзамена нет "
                  "в момент подписания. Флаг --signing-key приносит ключ вуза НА эту "
                  "машину: он даёт проверяемую метку, но не убирает доступ студента "
                  "к ключу. Полное решение — подпись на стороне вуза, см. "
                  "docs/LIMITATIONS.md, раздел про R3.",
    },
    # institution-unverified: метка есть, эталона нет. Сила утверждения —
    # ровно self-signed, и формулировки обязаны быть такими же слабыми.
    AUTHORITY_INSTITUTION: {
        "authority": AUTHORITY_INSTITUTION,
        "label": "заявлен ключ учреждения (institution), эталон НЕ сверен",
        "proves": "целостность при передаче: файл не испортился и не изменился "
                  "по дороге от студента к экзаменатору",
        "not_proves": "авторство: " + INSTITUTION_UNVERIFIED_NOTE,
        "verdict": "ЦЕЛОСТНОСТЬ ПОДТВЕРЖДЕНА, АВТОРСТВО — НЕТ",
        "advice": "Сверьте публичный ключ с эталоном вуза: --trusted-pub <файл>. "
                  "Без эталона метка institution ничем не сильнее self-signed.",
    },
    AUTHORITY_NONE: {
        "authority": AUTHORITY_NONE,
        "label": "подписи нет",
        "proves": "ничего: подпись не сформирована",
        "not_proves": "ни целостность, ни авторство файла отчёта",
        "verdict": "ПОДПИСИ НЕТ",
        "advice": "Целостность журнала всё равно проверяется хеш-цепочкой — "
                  "содержимое подменить незаметно нельзя.",
    },
}

#: institution с совпавшим эталоном — единственное состояние, в котором вообще
#: допустимо слово «авторство». Отдельной записью, а не ветвью в таблице:
#: получить её можно только передав `verified=True`, то есть после сравнения.
_AUTHORITY_INSTITUTION_VERIFIED: dict[str, str] = {
    "authority": AUTHORITY_INSTITUTION,
    "label": "ключ учреждения (institution), эталон сверен",
    "proves": "целостность и доступ к ключу вуза: " + INSTITUTION_VERIFIED_NOTE,
    "not_proves": "что отчёт собрала система, а не человек с доступом к ключу вуза "
                  "и к машине экзамена",
    "verdict": "ЦЕЛОСТНОСТЬ ПОДТВЕРЖДЕНА, КЛЮЧ ВУЗА СВЕРЕН",
    "advice": "Храните эталонный публичный ключ отдельно от пакетов и ротируйте "
              "приватный ключ после каждого потока.",
}


def normalize_authority(value: Any) -> str:
    """Любое значение -> одна из трёх меток. Непонятное -> self_signed.

    Безопасная сторона — САМАЯ слабая метка: ошибочно назвать ключ студента
    ключом вуза значит выдать несуществующее доказательство авторства.
    """
    key = str(value or "").strip().lower().replace("-", "_")
    if key in (AUTHORITY_INSTITUTION, "institutional", "university", "вуз", "учреждение"):
        return AUTHORITY_INSTITUTION
    if key in (AUTHORITY_NONE, "", "unsigned", "нет"):
        return AUTHORITY_NONE if key else AUTHORITY_SELF
    return AUTHORITY_SELF


def authority_info(authority: Any, verified: bool = False) -> dict[str, str]:
    """Что именно доказывает подпись этого вида. Единый источник формулировок.

    `verified` — сверил ли ПРОВЕРЯЮЩИЙ публичный ключ с эталоном, который задал
    сам. Это не свойство пакета, а результат действия человека, поэтому и
    параметр отдельный: метка в файле подписи приезжает от подписывающего и
    права повышать формулировки не имеет.

    Для `self_signed` и `none` параметр игнорируется: там сверять не с чем по
    построению (эталон — тот же ключ с той же машины).
    """
    normalized = normalize_authority(authority)
    if normalized == AUTHORITY_INSTITUTION and verified:
        return dict(_AUTHORITY_INSTITUTION_VERIFIED)
    return dict(_AUTHORITY_TABLE[normalized])


# ===========================================================================
# 3. Код сверки
# ===========================================================================
def session_code(genesis: str, student_id: str = "",
                 length: int = CODE_LENGTH) -> str:
    """Короткий человекочитаемый код сессии: base32 от sha256(genesis+student).

    Детерминирован от содержимого: тот же genesis и тот же студент дают тот же
    код на любой машине. Алфавит base32 (A-Z, 2-7) не содержит 0, 1, 8 и 9,
    поэтому I/1 и O/0 перепутать при чтении вслух нельзя.

    Пустой genesis -> пустой код: выдумывать код там, где цепочки нет, значит
    выдавать проверяемое за непроверяемое.
    """
    genesis = str(genesis or "").strip()
    if not genesis:
        return ""
    seed = f"{CODE_DOMAIN}|{genesis}|{str(student_id or '')}".encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    code = base64.b32encode(digest).decode("ascii").rstrip("=")
    return code[:max(1, int(length))]


def format_code(code: str) -> str:
    """Код для глаз и для диктовки: `ABC-DEF`. Пустой код -> «—»."""
    code = str(code or "").strip().upper()
    if not code:
        return "—"
    if len(code) <= 3:
        return code
    half = len(code) // 2
    return f"{code[:half]}-{code[half:]}"


# ===========================================================================
# 1. Каталог сессий
# ===========================================================================
@dataclass
class HandoverTarget:
    """Куда реально пишутся доказательства и почему именно туда."""

    path: Path
    #: Что просил проктор ("" — ничего не просил, работаем по умолчанию).
    requested: str = ""
    #: cli | env | config | default
    source: str = "default"
    #: Запрошенный каталог не подошёл, пишем в запасной локальный.
    degraded: bool = False
    #: Причина по-русски. Пустая строка — всё в порядке.
    reason: str = ""
    #: Полный текст для лога и для показа человеку.
    message: str = ""
    #: Результат `probe_dir` по фактическому каталогу: можно ли удалять и
    #: перезаписывать. Нужен проверяющему: «студент не может удалить» — это
    #: свойство прав на шаре, и оно обязано приезжать как измеренный факт.
    probe: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        probe = dict(self.probe or {})
        return {
            "effective_dir": str(self.path),
            "requested_dir": self.requested,
            "source": self.source,
            # Источник сообщает о себе процесс, который запустил студент.
            # Пишем это рядом со значением, чтобы ни один читатель не принял
            # `source: "cli"` за доказательство того, что каталог задал проктор.
            "source_is_self_reported": True,
            "degraded_handover": bool(self.degraded),
            "reason": self.reason,
            "message": self.message,
            "can_delete": probe.get("can_delete"),
            "can_overwrite": probe.get("can_overwrite"),
            "append_only": probe.get("append_only"),
            "append_only_note": append_only_note(probe),
        }


_SOURCE_LABELS = {
    "cli": "флаг --sessions-dir",
    "env": f"переменная {SESSIONS_DIR_ENV_VAR}",
    "config": "config.json",
    "default": "значение по умолчанию",
}


def source_label(source: str) -> str:
    return _SOURCE_LABELS.get(str(source or "default"), str(source))


def probe_dir(path: str | Path) -> dict[str, Any]:
    """Пробная запись в каталог: что РЕАЛЬНО разрешено процессу.

    Проверка именно пробной записью, а не `os.access` и не наличием пути:
    смонтированная сетевая папка вуза может существовать, отвечать на stat и
    при этом быть read-only, а флешку могли подключить с защитой от записи.
    Узнать об этом в момент старта, а не в момент сохранения первого кадра.

    Кроме записи проверяются ПЕРЕЗАПИСЬ и УДАЛЕНИЕ, и это не любопытство.
    Утверждение «что ушло на чужой диск, студент удалить не может» верно
    только на append-only шаре. Если процесс на машине студента может удалить
    и перезаписать свой пробный файл, он может удалить и перезаписать пакет —
    свой и, при общей папке, чужой. Поэтому результат пробы попадает в
    `handover.json`, в отчёт и в вывод проверки как ФАКТ о каталоге, а не как
    предположение о правах.

    Возвращает {"writable", "reason", "can_overwrite", "can_delete",
    "append_only"}. Значение None — проверить не удалось.
    """
    out: dict[str, Any] = {"writable": False, "reason": "",
                           "can_overwrite": None, "can_delete": None,
                           "append_only": None}
    target = Path(str(path)).expanduser()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        out["reason"] = f"не удалось создать каталог: {_oserror(exc)}"
        return out
    if not target.is_dir():
        out["reason"] = "путь существует, но это не каталог"
        return out
    probe = target / f"{_PROBE_NAME}-{os.getpid()}"
    try:
        probe.write_bytes(b"proctor")
    except OSError as exc:
        out["reason"] = f"каталог не принимает запись: {_oserror(exc)}"
        return out
    out["writable"] = True
    try:
        probe.write_bytes(b"proctor-again")
        out["can_overwrite"] = True
    except OSError:
        out["can_overwrite"] = False
    try:
        probe.unlink()
        out["can_delete"] = True
    except OSError:
        out["can_delete"] = False
    if out["can_delete"] is False and out["can_overwrite"] is False:
        out["append_only"] = True
    elif out["can_delete"] is True or out["can_overwrite"] is True:
        out["append_only"] = False
    return out


def append_only_note(info: dict[str, Any] | None) -> str:
    """Одна фраза про сохранность каталога. Пустая строка — сказать нечего."""
    info = dict(info or {})
    if info.get("append_only") is True:
        return ("Каталог append-only: пробный файл нельзя ни перезаписать, ни "
                "удалить. Записанный пакет из него убрать нельзя — это и есть "
                "та гарантия, ради которой каталог задаёт проктор.")
    if info.get("append_only") is False:
        return ("ВНИМАНИЕ. Каталог НЕ append-only: процесс, запущенный на машине "
                "экзамена, может удалить или перезаписать уже записанный файл. "
                "Значит и готовый пакет доказательств из этого каталога можно "
                "убрать или подменить — в том числе чужой, если папка общая. "
                "Сохранность обеспечивается правами на шаре (запись без удаления "
                "и перезаписи), а не этой системой.")
    return ""


def check_writable(path: str | Path) -> tuple[bool, str]:
    """Каталог доступен на запись? Обёртка над `probe_dir` для старых вызовов."""
    info = probe_dir(path)
    return bool(info.get("writable")), str(info.get("reason") or "")


def free_space_mb(path: str | Path) -> float:
    """Сколько мегабайт свободно. -1.0 — узнать не удалось."""
    try:
        usage = os.statvfs(str(path))
        return round(usage.f_bavail * usage.f_frsize / (1024 * 1024), 1)
    except (OSError, AttributeError, ValueError):
        return -1.0


def resolve_sessions_dir(requested: str | Path | None,
                         fallback: str | Path,
                         source: str = "default") -> HandoverTarget:
    """Выбрать каталог сессий и сказать человеку, что получилось.

    `requested` — чего хочет проктор (CLI, переменная окружения или config.json),
    `fallback` — запасной локальный каталог. Если запрошенный недоступен на
    запись, пишем в запасной и ставим `degraded = True`: отчёт обязан показать,
    что копия НЕ ушла на сетевой диск и пакет надо забрать с машины вручную.

    Если и запасной недоступен — отдаём его же с `degraded = True` и причиной;
    решение «падать или нет» принимает вызывающий, здесь не падаем никогда.
    """
    fallback_path = Path(str(fallback)).expanduser()
    wanted = str(requested or "").strip()

    if not wanted:
        probe = probe_dir(fallback_path)
        reason = str(probe.get("reason") or "")
        if probe.get("writable"):
            note = append_only_note(probe)
            return HandoverTarget(
                path=fallback_path.resolve(), requested="", source=source,
                probe=probe,
                message=(f"Каталог сессий: {fallback_path} "
                         f"({source_label(source)}). Запись проверена.\n"
                         f"Каталог проктором НЕ задан: доказательства остаются на "
                         f"машине экзамена под контролем того, кто за ней работает. "
                         f"Пакет придётся забирать с этой машины вручную.\n"
                         + (note + "\n" if note else "")
                         + f"Чтобы копия уходила на шару вуза: --sessions-dir /путь "
                           f"(или {SESSIONS_DIR_ENV_VAR}=/путь)."))
        return HandoverTarget(
            path=fallback_path, requested="", source=source, degraded=True,
            reason=reason, probe=probe,
            message=(f"ВНИМАНИЕ. Каталог сессий {fallback_path} недоступен на запись: "
                     f"{reason}. Доказательства сохранить НЕКУДА — освободите место "
                     f"или укажите другой каталог: --sessions-dir /путь."))

    wanted_path = Path(wanted).expanduser()
    probe = probe_dir(wanted_path)
    reason = str(probe.get("reason") or "")
    if probe.get("writable"):
        note = append_only_note(probe)
        return HandoverTarget(
            path=wanted_path.resolve(), requested=str(wanted_path), source=source,
            probe=probe,
            message=(f"Каталог сессий: {wanted_path} ({source_label(source)}). "
                     f"Запись проверена, свободно {free_space_mb(wanted_path)} МБ."
                     + (f"\n{note}" if note else "")))

    fb_ok, fb_reason = check_writable(fallback_path)
    degraded_reason = (
        f"каталог {wanted_path}, заданный проктором ({source_label(source)}), "
        f"недоступен на запись: {reason}")
    if fb_ok:
        return HandoverTarget(
            path=fallback_path.resolve(), requested=str(wanted_path), source=source,
            degraded=True, reason=degraded_reason, probe=probe_dir(fallback_path),
            message=(
                f"ВНИМАНИЕ. {_cap(degraded_reason)}.\n"
                f"Доказательства пишутся в запасной локальный каталог: {fallback_path}.\n"
                f"Это помечено в отчёте и в пакете как degraded_handover: копия НЕ ушла "
                f"на указанный диск, пакет остался на машине студента и его нужно "
                f"забрать вручную.\n"
                f"Проверьте: смонтирован ли диск, есть ли право записи, не защищён ли "
                f"носитель от записи."))
    return HandoverTarget(
        path=fallback_path, requested=str(wanted_path), source=source,
        degraded=True, reason=f"{degraded_reason}; запасной каталог тоже недоступен: {fb_reason}",
        message=(
            f"ВНИМАНИЕ. {_cap(degraded_reason)}.\n"
            f"Запасной каталог {fallback_path} тоже недоступен: {fb_reason}.\n"
            f"Доказательства сохранить НЕКУДА. Исправьте каталог до начала экзамена: "
            f"--sessions-dir /путь или {SESSIONS_DIR_ENV_VAR}=/путь."))


def sessions_dir_from_env(env: dict[str, str] | None = None) -> str:
    """Каталог сессий из `PROCTOR_SESSIONS_DIR`. Пусто — переменной нет."""
    source = os.environ if env is None else env
    return str(source.get(SESSIONS_DIR_ENV_VAR, "") or "").strip()


def signing_key_from_env(env: dict[str, str] | None = None) -> str:
    """Ключ подписи из `PROCTOR_SIGNING_KEY`. Пусто — переменной нет."""
    source = os.environ if env is None else env
    return str(source.get(SIGNING_KEY_ENV_VAR, "") or "").strip()


def deliver_dir_from_env(env: dict[str, str] | None = None) -> str:
    """Папка проктора из `PROCTOR_DELIVER_DIR`. Пусто — переменной нет."""
    source = os.environ if env is None else env
    return str(source.get(DELIVER_DIR_ENV_VAR, "") or "").strip()


# ===========================================================================
# 2. Пакет
# ===========================================================================
def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    """sha256 файла потоком: клипы доказательств в память целиком не лезут."""
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class PackageResult:
    """Что получилось при сборке пакета."""

    ok: bool = False
    path: str = ""
    session_id: str = ""
    code: str = ""
    files: int = 0
    bytes_total: int = 0
    missing: list[str] = field(default_factory=list)
    reason: str = ""
    message: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    #: По этому имени уже лежал пакет другой сессии — мы его не тронули.
    collision: str = ""
    collision_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "package": self.path, "session_id": self.session_id,
            "session_code": self.code, "files": self.files,
            "bytes_total": self.bytes_total, "missing": list(self.missing),
            "reason": self.reason, "message": self.message,
            "collision": self.collision, "collision_note": self.collision_note,
        }


def _collect(session_dir: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    """Что положить в пакет: (относительное имя, путь). Плюс список недостающего."""
    found: list[tuple[str, Path]] = []
    missing: list[str] = []
    for name in PACKAGE_FILES:
        candidate = session_dir / name
        if candidate.is_file():
            found.append((name, candidate))
        elif name not in ("events.jsonl", "handover.json"):
            # events.jsonl есть только в деградированном режиме, handover.json
            # пишется этим же модулем — их отсутствие нормально.
            missing.append(name)
    for folder in PACKAGE_DIRS:
        base = session_dir / folder
        if not base.is_dir():
            continue
        for item in sorted(base.rglob("*")):
            if item.is_file():
                found.append((item.relative_to(session_dir).as_posix(), item))
    return found, missing


def build_manifest(session_dir: str | Path,
                   meta: dict[str, Any] | None = None,
                   summary: dict[str, Any] | None = None,
                   chain: dict[str, Any] | None = None,
                   handover: dict[str, Any] | None = None,
                   authority: str = AUTHORITY_SELF,
                   code: str = "") -> tuple[dict[str, Any], list[tuple[str, Path]]]:
    """Собрать manifest.json и список файлов пакета.

    Манифест — это то, по чему проверяющий понимает, что комплект полон:
    список файлов с размерами и sha256, состав цепочки, код сверки и честная
    метка подписи. Сам манифест в список файлов не входит (он бы ссылался на
    себя), поэтому рядом с ним в пакет кладётся `manifest.json.sig`.
    """
    sdir = Path(str(session_dir))
    meta = dict(meta or {})
    summary = dict(summary or {})
    chain = dict(chain or {})
    found, missing = _collect(sdir)

    entries: list[dict[str, Any]] = []
    total = 0
    for name, path in found:
        try:
            size = path.stat().st_size
            entries.append({"path": name, "size": size, "sha256": sha256_file(path)})
            total += size
        except OSError as exc:
            missing.append(f"{name} ({_oserror(exc)})")

    info = authority_info(authority)
    session_id = str(meta.get("session_id") or summary.get("session_id") or sdir.name)
    code = str(code or summary.get("session_code") or meta.get("session_code") or "")

    manifest = {
        "format": PACKAGE_FORMAT,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "session_id": session_id,
        "session_code": code,
        "session_code_display": format_code(code),
        "session_code_basis": (
            "base32(sha256(\"" + CODE_DOMAIN + "|\" + genesis + \"|\" + student_id))"
            f"[:{CODE_LENGTH}] — пересчитывается проверяющим из genesis-хеша цепочки"),
        "student_id": str(meta.get("student_id") or summary.get("student_id") or ""),
        "student_name": str(meta.get("student_name") or summary.get("student_name") or ""),
        "exam_id": str(meta.get("exam_id") or summary.get("exam_id") or ""),
        "started_at_iso": str(meta.get("started_at_iso") or ""),
        "ended_at_iso": str(summary.get("ended_at_iso") or ""),
        "events_total": summary.get("events_total", 0),
        "max_severity": str(summary.get("max_severity") or ""),
        "final_risk": summary.get("final_risk", 0),
        "signature": {
            "algorithm": "Ed25519",
            "authority": info["authority"],
            "authority_label": info["label"],
            "proves": info["proves"],
            "not_proves": info["not_proves"],
            # Метку ставит подписывающий, то есть процесс на машине экзамена.
            # Она НЕ самоподтверждается, и манифест говорит это о себе сам:
            # иначе читатель принимает значение поля за проверенный факт.
            "authority_is_self_declared": True,
            "authority_check": (
                "institution: сверьте публичный ключ с эталоном вуза — "
                "verify_report.py <пакет> --trusted-pub <файл>. Без эталона метка "
                "не сильнее self_signed."
                if info["authority"] == AUTHORITY_INSTITUTION else
                "self_signed: сверять не с чем — приватный ключ лежит на той же "
                "машине, где собран пакет."),
        },
        # Совпадение имён сессий — факт о передаче, а не о цепочке: он значит,
        # что в каталоге уже лежала сессия с этим именем. Проверяющий обязан
        # увидеть его в разделе «передача», поэтому он едет внутри `handover`.
        "handover": {**dict(handover or {}),
                     **({"id_collision": str(summary.get("id_collision") or "")}
                        if summary.get("id_collision") else {})},
        "chain": {
            "genesis": str(chain.get("genesis") or ""),
            "last_hash": str(chain.get("last_hash") or chain.get("chain_head") or ""),
            "records": chain.get("records", chain.get("checked", 0)),
            "events_checked": chain.get("events_checked", 0),
        },
        "files": entries,
        "files_total": len(entries),
        "bytes_total": total,
        "missing": missing,
        "verify": "python3 scripts/verify_report.py <пакет>.proctor.zip",
    }
    return manifest, found


def build_package(session_dir: str | Path,
                  out_path: str | Path | None = None,
                  meta: dict[str, Any] | None = None,
                  summary: dict[str, Any] | None = None,
                  chain: dict[str, Any] | None = None,
                  handover: dict[str, Any] | None = None,
                  authority: str = AUTHORITY_SELF,
                  code: str = "",
                  key_path: str | Path | None = None,
                  sign: bool = True) -> PackageResult:
    """Собрать `<session_id>.proctor.zip` рядом с каталогом сессии.

    Никогда не бросает: пакет — приятное дополнение к отчёту, и неудача сборки
    не имеет права ронять завершение сессии. Причина отказа возвращается
    текстом, вызывающий пишет её в журнал передачи и показывает человеку.
    """
    result = PackageResult()
    try:
        sdir = Path(str(session_dir)).resolve()
    except OSError as exc:
        result.reason = f"каталог сессии недоступен: {_oserror(exc)}"
        result.message = f"Пакет не собран: {result.reason}."
        return result
    if not sdir.is_dir():
        result.reason = f"каталога сессии нет: {sdir}"
        result.message = f"Пакет не собран: {result.reason}."
        return result

    # Метка в манифесте обязана совпадать с меткой, которую РЕАЛЬНО поставит
    # подпись. Иначе пакет уезжает с `signature.authority = institution` в
    # манифесте и `self_signed` в файле подписи отчёта — расхождение внутри
    # одного комплекта, которое проверка честно трактует как признак подмены.
    authority, downgrade = _effective_authority(key_path, authority)
    manifest, found = build_manifest(sdir, meta, summary, chain, handover,
                                     authority, code)
    if downgrade:
        manifest["signature"]["authority_requested"] = AUTHORITY_INSTITUTION
        manifest["signature"]["authority_downgraded"] = downgrade
    result.session_id = str(manifest.get("session_id") or sdir.name)
    result.code = str(manifest.get("session_code") or "")
    result.missing = list(manifest.get("missing") or [])

    if not found:
        result.reason = "в каталоге сессии нет ни одного файла комплекта"
        result.message = (f"Пакет не собран: {result.reason}. "
                          f"Каталог: {sdir}")
        return result

    target = (Path(str(out_path)) if out_path
              else sdir.parent / f"{result.session_id}{PACKAGE_SUFFIX}")

    # Чужой пакет не перезаписываем НИКОГДА. На общей шаре имя пакета
    # предсказуемо (`<session_id>.proctor.zip`), а `session_id` складывается из
    # времени и student_id, то есть студент-администратор может сдвинуть часы и
    # получить ровно то же имя. Прежний `os.replace` затирал чужой пакет без
    # единого слова, то есть уничтожал чужие доказательства штатным кодом.
    # Сравниваем по genesis: тот же genesis — это наша же сессия, пересборка
    # разрешена; другой genesis — другая цепочка, уходим в соседнее имя.
    # Проверка не зависит от того, задан ли `out_path` явно: затирать пакет
    # ДРУГОЙ сессии неправильно в любом случае, а свой собственный (тот же
    # genesis) пересобрать можно.
    if target.is_file():
        existing = read_manifest(target)
        our_genesis = str((manifest.get("chain") or {}).get("genesis") or "")
        its_genesis = str((existing.get("chain") or {}).get("genesis") or "")
        if existing and (its_genesis or our_genesis) and its_genesis != our_genesis:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            base = target.name[:-len(PACKAGE_SUFFIX)] if target.name.endswith(
                PACKAGE_SUFFIX) else target.stem
            moved = target.with_name(f"{base}.{stamp}{PACKAGE_SUFFIX}")
            result.collision = str(target)
            result.collision_note = (
                f"В каталоге уже лежал пакет {target.name} ДРУГОЙ сессии "
                f"(genesis {its_genesis[:16] or '—'} вместо {our_genesis[:16] or '—'}). "
                f"Он не затронут. Наш пакет записан рядом как {moved.name}. "
                f"Экзаменатору: два пакета с одним session_id в одном каталоге — "
                f"признак совпадения имён, разберитесь, какой чей, по genesis.")
            target = moved

    # Манифест кладём и в каталог сессии: проктор видит состав комплекта и код
    # сверки, не распаковывая архив, а verify_report.py работает и по каталогу.
    manifest_path = sdir / MANIFEST_NAME
    extra: list[tuple[str, Path]] = []
    try:
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        extra.append((MANIFEST_NAME, manifest_path))
    except OSError as exc:
        result.reason = f"не удалось записать {MANIFEST_NAME}: {_oserror(exc)}"
        result.message = f"Пакет не собран: {result.reason}."
        return result

    # Подпись манифеста закрывает весь пакет сразу: в манифесте лежат sha256
    # всех файлов, значит подписанный манифест = подписанный комплект. Своей
    # криптографии нет — та же функция, что подписывает отчёт.
    if sign:
        # Метку подписи передаём ЯВНО. Без неё манифест, подписанный ключом
        # учреждения, уезжал с меткой self_signed, и проверка объявляла пакет
        # подписанным посторонним ключом: подпись отчёта institution, подпись
        # манифеста «с машины студента» — расхождение внутри одного пакета.
        sig_path = _sign_manifest(manifest_path, key_path, authority)
        if sig_path is not None and sig_path.is_file():
            extra.append((f"{MANIFEST_NAME}.sig", sig_path))
            pub = manifest_path.with_name(f"{MANIFEST_NAME}.pub")
            if pub.is_file():
                extra.append((f"{MANIFEST_NAME}.pub", pub))

    tmp = target.with_name(target.name + ".part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as archive:
            for name, path in found + extra:
                archive.write(str(path), arcname=name)
        os.replace(str(tmp), str(target))
    except (OSError, zipfile.BadZipFile) as exc:
        with _suppress():
            tmp.unlink()
        result.reason = f"запись архива не удалась: {_oserror(exc)}"
        result.message = (f"Пакет не собран: {result.reason}. "
                          f"Отчёт и журнал остались в каталоге сессии: {sdir}")
        return result

    result.ok = True
    result.path = str(target)
    result.files = len(found) + len(extra)
    result.bytes_total = int(manifest.get("bytes_total") or 0)
    result.manifest = manifest
    size_mb = round(target.stat().st_size / (1024 * 1024), 2)
    result.message = (
        f"Пакет доказательств собран: {target} ({size_mb} МБ, файлов {result.files}).\n"
        f"Код сверки сессии: {format_code(result.code)}\n"
        f"Проверка на машине преподавателя: "
        f"python3 scripts/verify_report.py {target.name}")
    if result.missing:
        result.message += ("\nВ комплекте не хватает: "
                           + ", ".join(result.missing)
                           + " — проверка это покажет и назовёт неполной.")
    if result.collision_note:
        result.message += "\nВНИМАНИЕ. " + result.collision_note
    return result


def write_handover_note(session_dir: str | Path, payload: dict[str, Any]) -> str:
    """Записка о передаче в каталоге сессии. Возвращает путь ('' при отказе).

    Отдельный файл нужен ровно для одного случая: пакет не собрался. Цепочка в
    этот момент уже закрыта записью `session_close`, и дописывать в неё что-то
    после закрытия нельзя — такая запись сама выглядела бы как подделка.
    Поэтому факт неудачной передачи фиксируется рядом: в `handover.json`, в
    `summary.json` и сообщением оболочке.
    """
    sdir = Path(str(session_dir))
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        path = sdir / HANDOVER_NAME
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        return str(path)
    except OSError:
        return ""


def _effective_authority(key_path: str | Path | None,
                         authority: Any) -> tuple[str, str]:
    """Какую метку реально поставит подпись. -> (метка, причина понижения).

    Решение принимает `storage/report.py` (там же, где подписывают), чтобы
    манифест и файл подписи не могли сказать разного. Модуля нет — остаёмся
    на самой слабой метке, как и везде в этом файле.
    """
    normalized = normalize_authority(authority)
    if normalized != AUTHORITY_INSTITUTION:
        return normalized, ""
    try:
        from . import report as report_mod  # type: ignore
    except Exception:
        try:
            import report as report_mod  # type: ignore
        except Exception:
            return AUTHORITY_SELF, ("модуль подписи недоступен, метку institution "
                                    "подтвердить нечем")
    resolve = getattr(report_mod, "resolve_authority", None)
    if not callable(resolve):
        return normalized, ""
    try:
        effective, reason = resolve(key_path, normalized)
    except Exception:
        return AUTHORITY_SELF, "проверить ключ учреждения не удалось"
    return normalize_authority(effective), str(reason or "")


def _sign_manifest(manifest_path: Path, key_path: str | Path | None,
                   authority: str = AUTHORITY_SELF) -> Path | None:
    """Подписать манифест функцией из storage/report.py. Нечем — None.

    Метка подписи обязана совпадать с меткой подписи отчёта: манифест и отчёт
    подписываются одним ключом, и расхождение метки внутри одного пакета
    проверка честно трактует как признак подмены.
    """
    try:
        from . import report as report_mod  # type: ignore
    except Exception:
        try:
            import report as report_mod  # type: ignore
        except Exception:
            return None
    sign = getattr(report_mod, "sign_report", None)
    if not callable(sign):
        return None
    try:
        produced = sign(manifest_path, key_path, normalize_authority(authority))
    except TypeError:
        # Старая версия report.py без параметра метки: подписываем без неё,
        # проверка тогда прочитает подпись как self_signed — безопасная сторона.
        try:
            produced = sign(manifest_path, key_path)
        except Exception:
            return None
    except Exception:
        return None
    return Path(str(produced)) if produced else None


# ===========================================================================
# Доставка пакета в папку проктора (п. 5 описания модуля)
# ===========================================================================
_DELIVER_SOURCE_LABELS = {
    "cli": "флаг --deliver-to",
    "env": f"переменная {DELIVER_DIR_ENV_VAR}",
    "config": "config.json",
    "default": "не задана",
}


def deliver_source_label(source: str) -> str:
    return _DELIVER_SOURCE_LABELS.get(str(source or "default"), str(source))


def probe_deliver_dir(path: str | Path | None,
                      source: str = "default") -> dict[str, Any]:
    """Стартовая проверка папки проктора: есть ли она и принимает ли запись.

    Проверка — пробной записью, как у каталога сессий: смонтированная шара
    может отвечать на stat и быть read-only. Отличие от `probe_dir` в одном:
    папку здесь создаём, только если её нет, а РОДИТЕЛЬ есть, и не создаём
    цепочку каталогов. Иначе `/media/usb/exam`, набранный при вынутой флешке,
    молча превращался бы в каталог на локальном диске. Даже одноуровневое
    создание этот риск не убирает (точка монтирования без носителя — обычный
    локальный каталог), поэтому факт создания возвращается как `created` и
    вызывающий обязан сказать о нём вслух.

    Пробный файл открывается с исключительным созданием: ни один чужой файл
    проба затереть не может. Не удалился (append-only шара) — это не ошибка,
    а факт `probe_removed: False`.

    Возвращает {"configured", "dir", "source", "writable", "reason",
    "created", "probe_removed"}. Никогда не бросает.
    """
    wanted = str(path or "").strip()
    out: dict[str, Any] = {
        "configured": bool(wanted), "dir": "",
        "source": str(source or "default") if wanted else "default",
        "writable": False, "reason": "", "created": False, "probe_removed": None,
    }
    if not wanted:
        return out
    try:
        target = Path(wanted).expanduser().absolute()
    except (OSError, RuntimeError, ValueError) as exc:
        out["dir"] = wanted
        out["reason"] = f"путь не разбирается: {_oserror(exc)}"
        return out
    out["dir"] = str(target)
    try:
        if not target.exists():
            if not target.parent.is_dir():
                out["reason"] = (f"папки нет, и нет её родителя {target.parent} — "
                                 f"не подключён носитель или сетевой диск?")
                return out
            target.mkdir()
            out["created"] = True
    except OSError as exc:
        out["reason"] = f"папки нет, создать её не удалось: {_oserror(exc)}"
        return out
    if not target.is_dir():
        out["reason"] = "путь существует, но это не папка"
        return out
    probe = target / f"{_DELIVER_PROBE_NAME}-{os.getpid()}-{time.time_ns()}"
    try:
        handle = open(probe, "xb")
    except OSError as exc:
        out["reason"] = f"папка не принимает запись: {_oserror(exc)}"
        return out
    try:
        with handle:
            handle.write(b"proctor")
    except OSError as exc:
        # Файл создан, но не записан (место кончилось): он наш — убираем.
        _drop_own_file(probe)
        out["reason"] = f"папка не принимает запись: {_oserror(exc)}"
        return out
    out["writable"] = True
    try:
        probe.unlink()
        out["probe_removed"] = True
    except OSError:
        out["probe_removed"] = False
    return out


def _numbered_name(name: str, n: int) -> str:
    """`X.proctor.zip` -> `X-2.proctor.zip`: двойное расширение сохраняется."""
    if n <= 1:
        return name
    if name.endswith(PACKAGE_SUFFIX):
        return f"{name[:-len(PACKAGE_SUFFIX)]}-{n}{PACKAGE_SUFFIX}"
    stem = Path(name)
    return f"{stem.stem}-{n}{stem.suffix}"


def _drop_own_file(path: Path, what: str = "недописанный файл") -> str:
    """Удалить файл, созданный ЭТОЙ попыткой. -> '' или оговорка для ошибки.

    Единственное, что доставка вправе удалить в папке проктора. Файла уже нет
    (папка отвалилась вместе с ним) — значит и оставлять нечего.
    """
    try:
        path.unlink()
        return ""
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return (f"; {what} {path.name} остался в папке проктора "
                f"(удалить не удалось: {_oserror(exc)})")


def _fsync_dir(path: Path) -> None:
    """Сбросить запись каталога на носитель. Где так нельзя (Windows) — молча."""
    if not hasattr(os, "O_DIRECTORY"):
        return
    with _suppress():
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _short(exc: BaseException) -> str:
    """Причина отказа коротко и по-русски, где перевод известен."""
    if isinstance(exc, OSError):
        return _oserror(exc)
    return str(exc)[:200] or exc.__class__.__name__


def deliver_package(zip_path: str | Path, deliver_dir: str | Path) -> dict[str, Any]:
    """Скопировать готовый пакет в папку проктора и сверить копию по sha256.

    Правила, ради которых функция и существует:
      * имя назначения — имя пакета; открывается с исключительным созданием
        (`xb`), занятое имя -> `-2`, `-3` … перед `.proctor.zip`. В папке
        проктора НИЧЕГО не перезаписывается, не переименовывается и не
        удаляется — кроме недописанного или не сошедшегося файла, созданного
        этой же попыткой;
      * копирование потоком, затем `flush` + `os.fsync`, закрытие;
      * копия открывается заново и её sha256 сравнивается с sha256 локального
        пакета (`verified`). Не сошлось — копия удаляется, `ok: False`.
        Перечитать не дали (шара «только запись») — `ok: True`,
        `verified: False` и причина в `error`: файл записан, но сверен НЕ был.

    Пакет и папка — те, что переданы. Папку здесь НЕ создаём: пропала папка к
    концу экзамена — значит отключён носитель или шара, и новая папка на
    локальном диске под тем же путём была бы «доставкой» в никуда.

    Возвращает {"ok", "dest", "bytes", "sha256", "verified", "at", "error"}:
    `sha256` — локального пакета (то, с чем сверяли), `bytes` — сколько
    скопировано, `at` — время окончания попытки (epoch). Никогда не бросает.
    """
    out: dict[str, Any] = {"ok": False, "dest": "", "bytes": 0, "sha256": "",
                           "verified": False, "at": 0.0, "error": ""}

    def done(error: str = "") -> dict[str, Any]:
        out["error"] = error
        out["at"] = time.time()
        return out

    wanted = str(deliver_dir or "").strip()
    if not wanted:
        return done("папка проктора не задана")
    raw_src = str(zip_path or "").strip()
    src = Path(raw_src).expanduser() if raw_src else Path()
    if not raw_src or not src.is_file():
        return done(f"локального пакета нет: {raw_src or '—'}")
    dest_dir = Path(wanted).expanduser()
    if not dest_dir.is_dir():
        return done(f"папка проктора недоступна: нет папки {dest_dir} "
                    f"(отключён носитель или сетевой диск?)")

    # Пакет уже лежит в папке проктора (каталог сессий задан туда же): вторая
    # копия рядом с первой никому не нужна. Сверять — с самим собой.
    same = False
    with _suppress():
        same = os.path.samefile(str(src.parent), str(dest_dir))
    if same:
        try:
            out["sha256"] = sha256_file(src)
            out["bytes"] = src.stat().st_size
        except OSError as exc:
            return done(f"пакет не читается: {_oserror(exc)}")
        out.update(ok=True, dest=str(src), verified=True)
        return done()

    handle = None
    dest: Path | None = None
    for n in range(1, _DELIVER_MAX_SUFFIX + 1):
        candidate = dest_dir / _numbered_name(src.name, n)
        try:
            handle = open(candidate, "xb")
        except FileExistsError:
            continue
        except OSError as exc:
            return done(f"папка проктора не принимает запись: {_oserror(exc)}")
        dest = candidate
        break
    if handle is None or dest is None:
        return done(f"в папке проктора заняты все имена от {src.name} до "
                    f"{_numbered_name(src.name, _DELIVER_MAX_SUFFIX)}")

    digest = hashlib.sha256()
    copied = 0
    try:
        with handle:
            with open(src, "rb") as reader:
                for block in iter(lambda: reader.read(_DELIVER_CHUNK), b""):
                    handle.write(block)
                    digest.update(block)
                    copied += len(block)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:  # любая ошибка посреди копии — убрать СВОЙ хвост
        left = _drop_own_file(dest)
        return done(f"копирование прервано: {_short(exc)}{left}")
    _fsync_dir(dest_dir)

    out["bytes"] = copied
    out["sha256"] = digest.hexdigest()
    try:
        dest_size = dest.stat().st_size
        dest_sha = sha256_file(dest)
    except OSError as exc:
        out.update(ok=True, dest=str(dest))
        return done(f"копия записана, но перечитать её для сверки sha256 не "
                    f"удалось: {_oserror(exc)}")
    if dest_size != copied or dest_sha != out["sha256"]:
        left = _drop_own_file(dest, "несовпавшая копия")
        return done(f"копия в папке проктора не совпала с пакетом по sha256 "
                    f"({dest_size} из {copied} байт){left or ' и удалена'}")
    out.update(ok=True, dest=str(dest), verified=True)
    return done()


def read_handover_note(session_dir: str | Path) -> dict[str, Any]:
    """Прочитать `handover.json` каталога сессии. Нет или битый — пустой dict."""
    try:
        raw = json.loads((Path(str(session_dir)) / HANDOVER_NAME)
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def record_delivery(session_dir: str | Path, result: dict[str, Any],
                    package: str = "") -> str:
    """Дописать итог доставки в `handover.json`. Возвращает путь ('' при отказе).

    `delivery` — последняя попытка, `delivery_attempts` — все попытки по
    порядку (повтор из оболочки не стирает прежний отказ: проктору важно, что
    первая копия НЕ ушла и почему). Цепочка к этому моменту закрыта, поэтому
    место этой записи — рядом с ней, как и у записки о пакете.
    """
    note = read_handover_note(session_dir)
    entry = dict(result or {})
    attempts = note.get("delivery_attempts")
    attempts = list(attempts) if isinstance(attempts, list) else []
    attempts.append({**entry, "package": str(package or "")})
    note["delivery"] = entry
    note["delivery_attempts"] = attempts
    note["delivery_updated_at"] = now_iso()
    return write_handover_note(session_dir, note)


# ===========================================================================
# 5. Проверка пакета
# ===========================================================================
def read_manifest(zip_path: str | Path) -> dict[str, Any]:
    """Прочитать manifest.json из пакета. Нет или битый — пустой dict."""
    try:
        with zipfile.ZipFile(str(zip_path)) as archive:
            raw = archive.read(MANIFEST_NAME).decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


#: Пределы распаковки. Пакет приезжает от студента, то есть это недоверенный
#: ввод, и распаковывается он на диск ЭКЗАМЕНАТОРА. 2 ГБ с запасом покрывают
#: трёхчасовую сессию с клипами; всё, что больше, — либо не наш пакет, либо
#: zip-бомба, и в обоих случаях правильный ответ один: отказаться и сказать.
MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXTRACT_FILES = 20000
#: Коэффициент распаковки. Отчёт и JSON жмутся хорошо, поэтому порог высокий;
#: 199 КБ -> 200 МБ (коэффициент 1000) он ловит, а честный пакет — нет.
MAX_EXTRACT_RATIO = 200.0


def extract_package(zip_path: str | Path, dest: str | Path,
                    max_bytes: int = MAX_EXTRACT_BYTES,
                    max_files: int = MAX_EXTRACT_FILES,
                    max_ratio: float = MAX_EXTRACT_RATIO) -> tuple[Path | None, str]:
    """Распаковать пакет в каталог. Возвращает (каталог, причина отказа).

    Пути проверяются до извлечения: `..` и абсолютные имена внутри архива
    отбрасываются. Архив приезжает от студента, то есть это недоверенный ввод,
    и ZipSlip здесь — не теория.

    Объём тоже ограничен, и по двум независимым числам. Заявленные в заголовках
    размеры проверяются ДО распаковки (дешёвый отказ), но заголовку верить
    нельзя — он часть недоверенного ввода. Поэтому то же ограничение считается
    второй раз по фактически записанным байтам, и при превышении распаковка
    прерывается, а уже записанное удаляется. Без этого пакет на 50 МБ выносил
    системный диск экзаменатора, а проверка печатала «комплект полон».
    """
    target = Path(str(dest))
    try:
        target.mkdir(parents=True, exist_ok=True)
        root = target.resolve()
        with zipfile.ZipFile(str(zip_path)) as archive:
            infos = [i for i in archive.infolist() if not i.is_dir()]
            if len(infos) > max_files:
                return None, (f"в архиве {len(infos)} файлов — больше предела "
                              f"{max_files}; это не комплект доказательств")
            declared = sum(max(0, int(i.file_size or 0)) for i in infos)
            packed = sum(max(0, int(i.compress_size or 0)) for i in infos)
            if declared > max_bytes:
                return None, (f"архив заявляет {declared // (1024 * 1024)} МБ после "
                              f"распаковки — больше предела "
                              f"{max_bytes // (1024 * 1024)} МБ; распаковка отменена, "
                              f"диск не тронут")
            if packed > 0 and declared / packed > max_ratio:
                return None, (f"коэффициент распаковки {declared / packed:.0f}:1 "
                              f"(предел {max_ratio:.0f}:1) — похоже на zip-бомбу, "
                              f"распаковка отменена")
            written = 0
            for info in infos:
                name = info.filename
                candidate = (root / name).resolve()
                if not str(candidate).startswith(str(root) + os.sep):
                    return None, f"в архиве небезопасное имя файла: {name}"
                candidate.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, open(candidate, "wb") as dst:
                    while True:
                        block = src.read(1 << 20)
                        if not block:
                            break
                        written += len(block)
                        if written > max_bytes:
                            dst.close()
                            _rmtree(target)
                            return None, (
                                f"при распаковке превышен предел "
                                f"{max_bytes // (1024 * 1024)} МБ — заголовки архива "
                                f"занижали размер. Распакованное удалено")
                        dst.write(block)
        return target, ""
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return None, f"распаковать не удалось: {_oserror(exc)}"


def _rmtree(path: Path) -> None:
    import shutil
    with _suppress():
        shutil.rmtree(str(path), ignore_errors=True)


#: Файлы пакета, которых в манифесте нет И быть не может: манифест не может
#: содержать свой собственный хеш, а подпись и ключ считаются по готовому
#: манифесту. Всё остальное, чего в манифесте нет, — посторонний файл.
_MANIFEST_EXEMPT: frozenset[str] = frozenset({
    MANIFEST_NAME, f"{MANIFEST_NAME}.sig", f"{MANIFEST_NAME}.pub",
})


def verify_manifest(root: str | Path,
                    manifest: dict[str, Any]) -> dict[str, Any]:
    """Сверить файлы в каталоге с манифестом: наличие, размер, sha256.

    Сверка идёт В ДВЕ СТОРОНЫ, и вторая важнее первой. Обход только записей
    манифеста отвечает на вопрос «всё ли заявленное на месте и не подменено».
    Он НЕ отвечает на вопрос «нет ли в пакете лишнего»: файл, которого в
    манифесте нет, распаковывался на диск экзаменатора и не упоминался вообще,
    хотя подписью он не закрыт. Так в пакет попадал, например,
    `report_настоящий.html` — документ, который человек откроет и примет за
    часть комплекта, при напечатанном «ни один файл пакета не подменён».

    Возвращает {"ok", "checked", "problems": [строки по-русски], "missing",
    "extra"}.
    """
    base = Path(str(root))
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return {"ok": False, "checked": 0, "problems": ["в манифесте нет списка файлов"],
                "missing": [], "extra": []}
    problems: list[str] = []
    missing: list[str] = []
    listed: set[str] = set()
    checked = 0
    for entry in files:
        if not isinstance(entry, dict):
            problems.append("в списке файлов манифеста битая запись")
            continue
        name = str(entry.get("path") or "")
        if not name:
            problems.append("в манифесте запись без имени файла")
            continue
        listed.add(name)
        path = base / name
        if not path.is_file():
            missing.append(name)
            problems.append(f"файла нет в пакете: {name}")
            continue
        checked += 1
        want_size = entry.get("size")
        try:
            size = path.stat().st_size
        except OSError as exc:
            problems.append(f"{name}: прочитать не удалось ({_oserror(exc)})")
            continue
        if isinstance(want_size, int) and size != want_size:
            problems.append(f"{name}: размер {size} вместо {want_size} байт")
        want_hash = str(entry.get("sha256") or "").strip().lower()
        if not want_hash:
            problems.append(f"{name}: в манифесте нет sha256")
            continue
        try:
            actual = sha256_file(path)
        except OSError as exc:
            problems.append(f"{name}: прочитать не удалось ({_oserror(exc)})")
            continue
        if actual != want_hash:
            problems.append(f"{name}: sha256 НЕ СОВПАЛ — файл изменён после сборки пакета")
    # Обратная сторона: что лежит в пакете, но в манифесте не заявлено.
    # Подписью такой файл не закрыт, а человек, открывший архив, этого не
    # видит — значит проверка обязана назвать его вслух.
    extra: list[str] = []
    try:
        for item in sorted(base.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(base).as_posix()
            if rel in listed or rel in _MANIFEST_EXEMPT:
                continue
            extra.append(rel)
    except OSError as exc:
        problems.append(f"не удалось перечислить файлы пакета: {_oserror(exc)}")
    for rel in extra[:20]:
        problems.append(
            f"ЛИШНИЙ ФАЙЛ: {rel} — есть в пакете, но в манифесте не заявлен, "
            f"значит подписью не закрыт и частью комплекта не является")
    if len(extra) > 20:
        problems.append(f"…и ещё {len(extra) - 20} файлов не заявлены в манифесте")

    declared_missing = manifest.get("missing")
    if isinstance(declared_missing, list) and declared_missing:
        for name in declared_missing:
            problems.append(f"комплект был неполным уже при сборке: не хватало {name}")
    return {"ok": not problems, "checked": checked, "problems": problems,
            "missing": missing, "extra": extra}


def expected_code(manifest: dict[str, Any],
                  genesis: str = "", student_id: str = "") -> dict[str, Any]:
    """Пересчитать код сверки и сравнить с заявленным в манифесте.

    ВАЖНО, откуда брать genesis. Если взять его из `manifest["chain"]`, то
    пересчёт замкнут: подписывающий выбрал и код, и genesis, из которого код
    выводится, и они сойдутся всегда — в том числе для пакета чистой сессии,
    выданного под кодом грязной. Поэтому вызывающий ОБЯЗАН передать `genesis`,
    прочитанный из `sessions.genesis_hash` внутри `evidence.sqlite`, а
    genesis манифеста при этом сверяется с базой отдельно
    (`cross_check_manifest`). Без базы пересчёт честно помечается как
    незамкнутый: `source="manifest"`, `closed=False`.
    """
    declared = str(manifest.get("session_code") or "")
    chain = manifest.get("chain") if isinstance(manifest.get("chain"), dict) else {}
    from_db = bool(str(genesis or "").strip())
    genesis = str(genesis or chain.get("genesis") or "")
    student = str(student_id or manifest.get("student_id") or "")
    recomputed = session_code(genesis, student)
    base = {"source": "evidence.sqlite" if from_db else "manifest",
            "closed": from_db}
    if not genesis:
        return {**base, "ok": None, "declared": declared, "recomputed": "",
                "reason": "genesis-хеш недоступен, пересчитать код нечем"}
    if not declared:
        return {**base, "ok": None, "declared": "", "recomputed": recomputed,
                "reason": "в манифесте нет кода сверки"}
    ok = declared == recomputed
    if not ok:
        reason = ("НЕ СОВПАЛ: код в манифесте не выводится из genesis-хеша "
                  "этой цепочки")
    elif from_db:
        reason = "совпал с пересчётом из genesis журнала (evidence.sqlite)"
    else:
        reason = ("совпал, но пересчитан из genesis САМОГО ЖЕ манифеста — это "
                  "самосогласованность, а не проверка. Журнал (evidence.sqlite) "
                  "в комплекте недоступен, сверить код с цепочкой нечем")
    return {**base, "ok": ok, "declared": declared, "recomputed": recomputed,
            "reason": reason}


def cross_check_manifest(manifest: dict[str, Any],
                         sessions: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Сверить манифест с журналом: это одна и та же сессия?

    Манифест собирает тот же процесс, который подписывает, поэтому внутри себя
    он согласован всегда. Единственная внешняя привязка — `evidence.sqlite`:
    в нём лежат `session_id`, `student_id` и `genesis_hash`, защищённые
    цепочкой. Без этой сверки пакет ОДНОЙ сессии уезжает под `session_id`,
    кодом и фамилией ДРУГОЙ: прежняя проверка печатала «код пересчитан из
    genesis — совпал» в разделе 1 и показывала другой `session_id` в разделе 2,
    ничем их не связывая.

    Возвращает {"ok", "problems", "genesis", "session_id", "student_id",
    "checked"}; `genesis` — значение ИЗ БАЗЫ, его и надо подставлять в
    `expected_code`.
    """
    out: dict[str, Any] = {"ok": None, "problems": [], "genesis": "",
                           "session_id": "", "student_id": "", "checked": False}
    rows = [r for r in (sessions or []) if isinstance(r, dict)]
    if not rows:
        out["problems"].append(
            "журнал (evidence.sqlite) в комплекте недоступен — сверить манифест "
            "с цепочкой нечем: session_id, код сверки и фамилия остаются "
            "утверждением манифеста о себе")
        return out
    out["checked"] = True
    want_sid = str(manifest.get("session_id") or "")
    chain = manifest.get("chain") if isinstance(manifest.get("chain"), dict) else {}
    want_genesis = str(chain.get("genesis") or "")

    def _sid(row: dict[str, Any]) -> str:
        return str(row.get("session_id") or "")

    match = next((r for r in rows if _sid(r) == want_sid), None)
    if match is None:
        out["ok"] = False
        out["problems"].append(
            f"СЕССИИ ИЗ МАНИФЕСТА НЕТ В ЖУРНАЛЕ: манифест заявляет «{want_sid or '—'}», "
            f"а в evidence.sqlite лежат "
            f"{', '.join(_sid(r) or '—' for r in rows[:5]) or '—'}. "
            f"Пакет собран не из этого журнала — например, журнал одной сессии "
            f"приложен к манифесту другой.")
        # genesis из базы всё равно отдаём: если сессия одна, пересчёт кода по
        # ней покажет, какому коду этот журнал соответствует на самом деле.
        if len(rows) == 1:
            out["genesis"] = str(rows[0].get("genesis_hash") or "")
            out["session_id"] = _sid(rows[0])
            out["student_id"] = str(rows[0].get("student_id") or "")
        return out

    out["genesis"] = str(match.get("genesis_hash") or "")
    out["session_id"] = _sid(match)
    out["student_id"] = str(match.get("student_id") or "")

    if want_genesis and out["genesis"] and want_genesis != out["genesis"]:
        out["problems"].append(
            f"GENESIS НЕ СОВПАЛ: в манифесте {want_genesis[:16]}…, в журнале "
            f"{out['genesis'][:16]}…. Код сверки в манифесте выведен не из этой "
            f"цепочки.")
    for key, label in (("student_id", "идентификатор студента"),
                       ("exam_id", "идентификатор экзамена")):
        want = str(manifest.get(key) or "")
        got = str(match.get(key) or "")
        if want and got and want != got:
            out["problems"].append(
                f"{_cap(label)} в манифесте «{want}» не совпадает с журналом «{got}»")
    extra = [_sid(r) for r in rows if _sid(r) != want_sid]
    if extra:
        out["problems"].append(
            "в журнале есть посторонние сессии: " + ", ".join(extra[:5])
            + " — журнал одной сессии не должен содержать чужих записей")
    out["ok"] = not out["problems"]
    return out


def package_authority(manifest: dict[str, Any]) -> str:
    """Метка подписи из манифеста. Нет метки — считаем self_signed."""
    sig = manifest.get("signature")
    if isinstance(sig, dict):
        return normalize_authority(sig.get("authority"))
    return AUTHORITY_SELF


def is_package(path: str | Path) -> bool:
    """Это пакет доказательств? Проверяется содержимым, а не именем файла."""
    candidate = Path(str(path))
    if not candidate.is_file():
        return False
    if not zipfile.is_zipfile(str(candidate)):
        return False
    try:
        with zipfile.ZipFile(str(candidate)) as archive:
            return MANIFEST_NAME in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


# ===========================================================================
# Служебное
# ===========================================================================
def _cap(text: str) -> str:
    """Первая буква заглавной, остальное БЕЗ изменений.

    Не `str.capitalize()`: он приводит хвост строки к нижнему регистру, а в
    причине отказа лежит путь к каталогу. Путь с заменённым регистром — это
    уже другой путь, и человек по нему ничего не найдёт.
    """
    text = str(text or "")
    return text[:1].upper() + text[1:] if text else text


def _oserror(exc: BaseException) -> str:
    """Причина OSError по-русски, без стектрейса и без английского errno."""
    text = getattr(exc, "strerror", None) or str(exc)
    table = {
        "Permission denied": "нет прав на запись",
        "Read-only file system": "файловая система только для чтения",
        "No space left on device": "на диске нет места",
        "No such file or directory": "пути не существует",
        "Not a directory": "это не каталог",
        "Operation not permitted": "операция запрещена системой",
        "Device not configured": "устройство не подключено",
        "Input/output error": "ошибка ввода-вывода (диск или сеть отвалились)",
    }
    return table.get(str(text).strip(), str(text))


class _suppress:
    """Локальный contextlib.suppress(Exception) без лишнего импорта."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: Any) -> bool:
        return True


def now_iso() -> str:
    """Отметка времени для манифеста и записки о передаче."""
    return datetime.now().isoformat(timespec="seconds")


__all__ = [
    "SESSIONS_DIR_ENV_VAR", "SIGNING_KEY_ENV_VAR", "PACKAGE_FORMAT",
    "PACKAGE_SUFFIX", "MANIFEST_NAME", "HANDOVER_NAME", "CODE_DOMAIN",
    "CODE_LENGTH", "PACKAGE_FILES", "PACKAGE_DIRS",
    "AUTHORITY_SELF", "AUTHORITY_INSTITUTION", "AUTHORITY_NONE",
    "INSTITUTION_UNVERIFIED_NOTE", "INSTITUTION_VERIFIED_NOTE",
    "normalize_authority", "authority_info",
    "session_code", "format_code",
    "HandoverTarget", "resolve_sessions_dir", "check_writable", "probe_dir",
    "append_only_note", "free_space_mb",
    "sessions_dir_from_env", "signing_key_from_env", "source_label",
    "DELIVER_DIR_ENV_VAR", "deliver_dir_from_env", "deliver_source_label",
    "probe_deliver_dir", "deliver_package", "read_handover_note",
    "record_delivery",
    "PackageResult", "build_manifest", "build_package", "write_handover_note",
    "sha256_file", "read_manifest", "extract_package", "verify_manifest",
    "expected_code", "cross_check_manifest", "package_authority", "is_package",
    "now_iso", "MAX_EXTRACT_BYTES", "MAX_EXTRACT_FILES", "MAX_EXTRACT_RATIO",
]

#!/usr/bin/env python3
"""
Проверка доказательств о прокторинге — инструмент экзаменатора.

Принимает три вида аргумента, и это важно: экзаменатору приносят то, что
принесли, а не то, что удобно скрипту.

    python3 scripts/verify_report.py 20261016-101500_s123.proctor.zip   # пакет
    python3 scripts/verify_report.py sessions/20261016-101500_s123      # каталог
    python3 scripts/verify_report.py sessions/.../report.html           # один файл

Отвечает на ТРИ разных вопроса и никогда не смешивает их в один:

  1. КОМПЛЕКТ — все файлы на месте, ни один не подменён и ЛИШНИХ НЕТ?
     (manifest.json + sha256 в обе стороны) И манифест описывает ту же сессию,
     что лежит в журнале?
  2. ЦЕЛОСТНОСТЬ — журнал не правили после экзамена? (hash-chain в SQLite)
     И: файл отчёта совпадает со своей подписью?
  3. АВТОРСТВО — публичный ключ совпал с эталоном, который принёс ПРОВЕРЯЮЩИЙ?

Почему третий вопрос отделён от второго. Приватный ключ по умолчанию лежит в
`keys/` на машине студента. Имея его, можно собрать журнал с другими событиями,
пересчитать хеш-цепочку и переподписать — подпись сойдётся, и первые два
вопроса ответят «да». Поэтому подпись ключом с этой же машины (`self_signed`)
доказывает ТОЛЬКО целостность при передаче.

МЕТКА `institution` САМА ПО СЕБЕ НЕ ДОКАЗЫВАЕТ НИЧЕГО. Её ставит тот же
процесс, который собрал пакет, то есть процесс на машине экзамена: достаточно
запустить сайдкар с `--signing-key` на любой файл ключа. Авторство проверяется
не меткой, а сравнением публичного ключа с эталоном, который экзаменатор
хранит у себя: `--trusted-pub`. Без эталона institution-пакет ровно так же
силён, как self-signed, и этот скрипт так и печатает. Эталона по умолчанию
НЕТ: ключ `keys/` машины проверяющего эталоном не является (сравнение с ним
объявляло подделкой честные self-signed пакеты).

Даже при совпавшем эталоне корректная формулировка — «в момент подписания у
процесса был доступ к ключу вуза», а не «подписал вуз»: ключ читается на
машине, где студент администратор.

Прежняя версия печатала «ОТЧЁТ ПОДЛИННЫЙ». Это вводило в заблуждение ровно
там, где цена ошибки максимальна — при разборе апелляции.

Коды возврата:
    0 — проверено всё: комплект, целостность И сверка ключа с эталоном;
    1 — обнаружена подделка или расхождение;
    2 — подделки не найдено, но проверка НЕПОЛНАЯ (в том числе `self_signed` и
        несверенный `institution`: целостность подтверждена, авторство — нет).

Сетевых обращений нет: проверка полностью локальная.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_SIDECAR = _ROOT / "sidecar"
for _p in (str(_SIDECAR), str(_SIDECAR / "storage")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

LINE = "─" * 72

#: Метки подписи. Дублируют storage/handover.py: скрипт обязан работать и тогда,
#: когда его положили рядом с пакетом, а исходников системы под рукой нет.
AUTHORITY_SELF = "self_signed"
AUTHORITY_INSTITUTION = "institution"

#: Имена записей `control` о доказательствах, а не о решениях. Дублируют
#: SCREEN_EVIDENCE_CONTROL и CLIP_CANCELLED_CONTROL из sidecar/protocol.py по
#: той же причине: скрипт работает и без исходников системы.
SCREEN_CONTROL = "evidence_screen"
CLIP_CANCELLED_CONTROL = "clip_cancelled"

#: Итог по одной оси проверки.
OK, FAIL, UNKNOWN = "ok", "fail", "unknown"

_AXIS_LABELS = {
    OK: "подтверждено",
    FAIL: "НАРУШЕНО",
    UNKNOWN: "не подтверждено",
}


def _import(*names: str) -> Any:
    import importlib
    for name in names:
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None


def _load_modules() -> tuple[Any, Any, Any]:
    return (_import("storage.report", "report"),
            _import("storage.db", "db"),
            _import("storage.handover", "handover"))


def _say(text: str = "") -> None:
    print(text)


def _block(title: str) -> None:
    _say()
    _say(LINE)
    _say(title)
    _say(LINE)


def _short(value: Any, limit: int = 32) -> str:
    text = str(value or "")
    return (text[:limit] + "…") if len(text) > limit else (text or "—")


# ===========================================================================
# Что нам дали: пакет, каталог или файл
# ===========================================================================
class Target:
    """Распакованный комплект доказательств и то, откуда он взялся."""

    def __init__(self) -> None:
        self.kind: str = "file"          # package | dir | file
        self.source: Path | None = None  # что указал человек
        self.root: Path | None = None    # каталог, где лежат файлы комплекта
        self.report: Path | None = None
        self.db: Path | None = None
        self.manifest: dict[str, Any] = {}
        self.tmp: Path | None = None     # каталог распаковки, который надо убрать
        self.problem: str = ""

    def cleanup(self) -> None:
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None


def _resolve_target(args: argparse.Namespace, handover_mod: Any) -> Target:
    """Понять, что перед нами, и привести к «каталог с файлами комплекта»."""
    target = Target()
    raw = args.report or args.package or args.db
    if args.package:
        raw = args.package
    if not raw:
        return target

    given = Path(str(raw)).expanduser()
    target.source = given

    # --- пакет -------------------------------------------------------------
    is_pkg = False
    if handover_mod is not None and hasattr(handover_mod, "is_package"):
        is_pkg = bool(handover_mod.is_package(given))
    elif given.is_file() and given.name.endswith(".zip"):
        is_pkg = True
    if is_pkg:
        target.kind = "package"
        if handover_mod is None:
            target.problem = ("это пакет доказательств, но модуль "
                              "sidecar/storage/handover.py недоступен — распаковать нечем")
            return target
        target.manifest = handover_mod.read_manifest(given)
        tmp = Path(tempfile.mkdtemp(prefix="proctor-verify-"))
        target.tmp = tmp
        root, reason = handover_mod.extract_package(given, tmp)
        if root is None:
            target.problem = reason
            return target
        target.root = root
        target.report = root / "report.html" if (root / "report.html").is_file() else None
        for name in ("evidence.sqlite", "evidence.db"):
            if (root / name).is_file():
                target.db = root / name
                break
        return target

    # --- каталог сессии ----------------------------------------------------
    if given.is_dir():
        target.kind = "dir"
        target.root = given
        inner = given / "report.html"
        target.report = inner if inner.is_file() else None
        for name in ("evidence.sqlite", "evidence.db"):
            if (given / name).is_file():
                target.db = given / name
                break
        if handover_mod is not None and (given / "manifest.json").is_file():
            import json
            try:
                parsed = json.loads((given / "manifest.json").read_text(encoding="utf-8"))
                target.manifest = parsed if isinstance(parsed, dict) else {}
            except Exception:
                target.manifest = {}
        return target

    # --- один файл ---------------------------------------------------------
    target.kind = "file"
    if args.db and not args.report:
        target.db = Path(str(args.db)).expanduser()
        target.root = target.db.parent
        candidate = target.root / "report.html"
        target.report = candidate if candidate.is_file() else None
        return target
    target.report = given
    target.root = given.parent
    if args.db:
        target.db = Path(str(args.db)).expanduser()
    else:
        for name in ("evidence.sqlite", "evidence.db"):
            if (given.parent / name).is_file():
                target.db = given.parent / name
                break
    return target


# ===========================================================================
# 1. Комплект: manifest.json и sha256
# ===========================================================================
def _check_package(target: Target, handover_mod: Any,
                   sessions: list[dict[str, Any]] | None = None) -> tuple[str, list[str]]:
    lines: list[str] = []
    if target.kind != "package" and not target.manifest:
        return UNKNOWN, ["  Проверяется не пакет, а отдельные файлы — состав комплекта "
                         "сверить не с чем.",
                         "  Полный комплект приезжает одним файлом "
                         "<сессия>.proctor.zip; попросите его."]
    if not target.manifest:
        return FAIL, ["  В пакете нет manifest.json — состав комплекта подтвердить нечем.",
                      "  Это либо не пакет этой системы, либо из него удалили манифест."]
    if handover_mod is None or target.root is None:
        return UNKNOWN, ["  Модуль sidecar/storage/handover.py недоступен — "
                         "манифест проверить нечем."]

    manifest = target.manifest
    lines.append(f"  Формат пакета:  {manifest.get('format') or '—'}")
    lines.append(f"  Сессия:         {manifest.get('session_id') or '—'}")
    student = manifest.get("student_name") or manifest.get("student_id") or "—"
    lines.append(f"  Студент:        {student}")
    lines.append(f"  Экзамен:        {manifest.get('exam_id') or '—'}")
    lines.append(f"  Собран:         {manifest.get('created_at') or '—'}")

    result = handover_mod.verify_manifest(target.root, manifest)
    lines.append(f"  Файлов по манифесту: {manifest.get('files_total', '—')}, "
                 f"проверено: {result.get('checked', 0)}")

    # --- манифест против журнала -------------------------------------------
    # Это главная проверка раздела, и раньше её не было вообще. Без неё пакет
    # ОДНОЙ сессии уезжал под session_id, кодом и фамилией ДРУГОЙ: код в
    # манифесте пересчитывался из genesis, лежащего в том же манифесте, то
    # есть сверялся сам с собой, а другой session_id в разделе 2 ничем не был
    # помечен. Genesis для пересчёта кода берём ИЗ БАЗЫ.
    cross: dict[str, Any] = {}
    db_genesis = ""
    if hasattr(handover_mod, "cross_check_manifest"):
        cross = handover_mod.cross_check_manifest(manifest, sessions)
        db_genesis = str(cross.get("genesis") or "")
        if cross.get("checked"):
            if cross.get("ok"):
                lines.append("  Манифест сверен с журналом: session_id, genesis и "
                             "идентификатор студента совпали.")
            else:
                lines.append("  МАНИФЕСТ НЕ СОВПАЛ С ЖУРНАЛОМ:")
                for problem in list(cross.get("problems") or [])[:10]:
                    lines.append(f"    · {problem}")
        else:
            for problem in list(cross.get("problems") or [])[:10]:
                lines.append(f"  {problem}")

    # --- код сверки: пересчитываем из genesis ЖУРНАЛА, а не манифеста ------
    code = handover_mod.expected_code(manifest, db_genesis)
    lines.append(f"  Код сверки:     {handover_mod.format_code(code.get('declared'))}")
    if code.get("ok") is True and code.get("closed"):
        lines.append("  Код пересчитан из genesis журнала (evidence.sqlite) — совпал.")
    elif code.get("ok") is True:
        lines.append(f"  Код совпал, НО: {code.get('reason')}")
    elif code.get("ok") is False:
        lines.append(f"  КОД СВЕРКИ НЕ СОШЁЛСЯ: пересчёт из genesis даёт "
                     f"{handover_mod.format_code(code.get('recomputed'))}, "
                     f"а в манифесте написано "
                     f"{handover_mod.format_code(code.get('declared'))}.")
        lines.append("  Значит манифест собран не из этой цепочки.")
    else:
        lines.append(f"  Код не пересчитан: {code.get('reason')}")

    # --- подпись манифеста закрывает весь пакет сразу ---
    sig = target.root / "manifest.json.sig"
    if sig.is_file():
        lines.append("  Подпись манифеста: файл manifest.json.sig в пакете есть "
                     "(проверяется в разделе 3).")
    else:
        lines.append("  Подпись манифеста: нет файла manifest.json.sig — список "
                     "sha256 подписью не закрыт.")

    problems = list(result.get("problems") or [])
    if code.get("ok") is False:
        problems.append("код сверки не выводится из genesis-хеша цепочки")
    if cross.get("ok") is False:
        problems.extend(str(p) for p in (cross.get("problems") or []))
    if not problems:
        # Формулировка ограничена тем, что реально проверено. «Ни один файл не
        # подменён» теперь имеет право звучать только потому, что сверка идёт в
        # обе стороны: заявленное на месте И лишнего в пакете нет.
        lines.append("  РЕЗУЛЬТАТ: комплект полон, все sha256 совпали, посторонних "
                     "файлов в пакете нет.")
        if not cross.get("checked"):
            lines.append("  Но: сверить манифест с журналом не удалось — заявления "
                         "манифеста о сессии")
            lines.append("  (session_id, код, фамилия) остаются его утверждениями о "
                         "себе.")
            return UNKNOWN, lines
        return OK, lines
    lines.append("  РЕЗУЛЬТАТ: С КОМПЛЕКТОМ ЕСТЬ ПРОБЛЕМЫ:")
    for problem in problems[:20]:
        lines.append(f"    · {problem}")
    if len(problems) > 20:
        lines.append(f"    · …и ещё {len(problems) - 20}")
    return FAIL, lines


# ===========================================================================
# 2-3. Подпись: отдельно целостность, отдельно авторство
# ===========================================================================
def _trusted_pub(explicit: str | None) -> tuple[str, str]:
    """Эталонный публичный ключ: (hex, откуда взят). Задаёт его только человек.

    Ключ, приехавший ВМЕСТЕ с отчётом, эталоном не считается никогда: его
    подменяет тот же, кто подменяет отчёт.

    НЕЯВНОГО ЭТАЛОНА ЗДЕСЬ БОЛЬШЕ НЕТ, и это исправление ошибки, которая
    срабатывала в конфигурации по умолчанию. Раньше при отсутствии
    `--trusted-pub` эталоном молча становился `keys/report_ed25519.key.pub` —
    ключ МАШИНЫ ПРОВЕРЯЮЩЕГО. Достаточно было один раз запустить сайдкар на
    компьютере преподавателя, чтобы этот файл появился, и дальше каждый
    честный self-signed пакет потока получал «ПОДПИСЬ ПОСТОРОННИМ КЛЮЧОМ» и
    код 1 — то есть худшую ошибку этого скрипта по его же формулировке, на
    честных материалах.

    Сравнение с машинным ключом не имело смысла и по сути: для self-signed
    эталон — тот же ключ с той же машины, что признаёт сам вывод скрипта, а
    для institution ключ вуза с машинным не совпадает по замыслу. Эталон
    бывает только один — тот, который проверяющий принёс сам.
    """
    if not explicit:
        return "", ""
    path = Path(explicit).expanduser()
    try:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip().lower()
            if value:
                return value, str(path)
        return "", f"{path} (файл не найден или пуст)"
    except OSError as exc:
        return "", f"{path} (прочитать не удалось: {exc})"


def _check_signature(report_mod: Any, handover_mod: Any, target: Target,
                     args: argparse.Namespace,
                     trusted_hex: str, trusted_src: str) -> tuple[str, str, list[str]]:
    """Вернуть (целостность файла, авторство, строки вывода).

    Две оси считаются раздельно, потому что это РАЗНЫЕ утверждения, и склеивать
    их в один вердикт — ровно та ошибка, из-за которой self-signed отчёт
    объявлялся подлинным.

    ПОРЯДОК ЗДЕСЬ ВАЖЕН, и раньше он был обратный. Формулировки «что подпись
    доказывает» печатались по метке ИЗ ФАЙЛА ПОДПИСИ, до всякого сравнения
    ключей. Метку выбирает тот, кто подписывает, поэтому `institution` был
    строго выгоднее `self_signed`: он и отключал сравнение ключа, и печатал
    «подписать этим ключом мог только тот, у кого он есть, а у студента его
    нет» — на пакете, собранном и подписанном студентом. Теперь сначала
    сравнение, и только потом формулировки, выбранные по его результату.
    """
    lines: list[str] = []
    report = target.report
    if report is None or not report.is_file():
        return UNKNOWN, UNKNOWN, ["  Файла отчёта нет — подпись проверить нечем."]
    if report_mod is None or not hasattr(report_mod, "verify_report_detailed"):
        return UNKNOWN, UNKNOWN, ["  Модуль sidecar/storage/report.py недоступен — "
                                  "подпись проверить нечем."]

    result = report_mod.verify_report_detailed(report, args.sig, args.pub)
    lines.append(f"  Отчёт:          {report}")
    lines.append(f"  Файл подписи:   {result.get('sig_path') or '—'}")
    lines.append(f"  Публичный ключ: {result.get('pub_path') or '—'}")
    if result.get("public_key"):
        lines.append(f"  Ключ (hex):     {_short(result['public_key'])}")
    if result.get("signed_at"):
        lines.append(f"  Подписан:       {result['signed_at']}")
    if result.get("sha256"):
        lines.append(f"  sha256 в подписи: {_short(result['sha256'])}")
    if result.get("sha256_actual"):
        lines.append(f"  sha256 файла:     {_short(result['sha256_actual'])}")

    if not result.get("ok"):
        reason = str(result.get("reason") or "причина не определена")
        if "не найден" in reason or "не установлен" in reason:
            lines.append(f"  РЕЗУЛЬТАТ: проверить не удалось — {reason}.")
            return UNKNOWN, UNKNOWN, lines
        lines.append(f"  РЕЗУЛЬТАТ: ПОДПИСЬ НЕ СОШЛАСЬ — {reason}.")
        return FAIL, FAIL, lines

    # Подпись и файл согласованы: целостность файла отчёта подтверждена.
    authority = str(result.get("authority") or AUTHORITY_SELF)
    actual = str(result.get("public_key") or "").strip().lower()

    # --- СНАЧАЛА сравнение ключа, ПОТОМ формулировки ----------------------
    # Эталон бывает только явный (см. `_trusted_pub`). Совпал — и только тогда
    # метка institution получает право говорить об авторстве.
    mismatch = bool(trusted_hex) and bool(actual) and actual != trusted_hex
    verified = (authority == AUTHORITY_INSTITUTION and bool(trusted_hex)
                and bool(actual) and actual == trusted_hex)

    info = _authority_info(handover_mod, authority, verified)
    lines.append(f"  Чем подписано:  {info['label']}")
    if not result.get("authority_declared"):
        lines.append("  (метки подписи в файле нет — считаем ключом с машины "
                     "студента, повысить её до ключа вуза нельзя)")
    if result.get("authority_downgraded"):
        lines.append("  При подписании ЗАПРАШИВАЛАСЬ метка institution, но она НЕ "
                     "поставлена:")
        lines.append(f"    {result['authority_downgraded']}")
        lines.append("  Чаще всего это опечатка проктора в пути к ключу, а не "
                     "подделка.")
    lines.append("  Файл отчёта совпадает со своей подписью — после подписания "
                 "его не меняли.")
    lines.append(f"  Подпись доказывает:     {info['proves']}")
    lines.append(f"  Подпись НЕ доказывает:  {info['not_proves']}")

    if mismatch:
        lines.append(f"  Эталонный ключ: {trusted_src} (задан проверяющим)")
        lines.append(f"  Эталон (hex):   {_short(trusted_hex)}")
        lines.append("  РЕЗУЛЬТАТ: ПОДПИСЬ ПОСТОРОННИМ КЛЮЧОМ — подписано не тем "
                     "ключом, которому доверяет проверяющий.")
        lines.append("  Так выглядит отчёт, пересобранный и подписанный заново: пара "
                     "«отчёт + подпись» согласована, но ключ чужой.")
        lines.append("  Прежде чем считать это подделкой, убедитесь, что эталон — "
                     "ключ вуза ЭТОГО потока:")
        lines.append("  несовпадение даёт и подмена, и устаревший эталон, и ротация "
                     "ключа.")
        return OK, FAIL, lines

    if verified:
        lines.append(f"  Эталонный ключ: {trusted_src} — СОВПАЛ.")
        lines.append("  РЕЗУЛЬТАТ: подписано ключом, который экзаменатор считает ключом "
                     "вуза.")
        lines.append("  Это сильнейшее утверждение, доступное схеме: в момент "
                     "подписания у процесса был")
        lines.append("  доступ к этому ключу. Это НЕ «подписал вуз»: ключ читался на "
                     "машине экзамена, где")
        lines.append("  студент администратор, и мог быть с неё скопирован — тогда им "
                     "подписывается любая")
        lines.append("  сессия задним числом. Ротируйте ключ после каждого потока.")
        return OK, OK, lines

    if authority == AUTHORITY_INSTITUTION:
        # Метка есть, эталона нет. Это НЕ «почти доказано»: метку поставил тот
        # же процесс, который собрал пакет, то есть процесс на машине экзамена.
        lines.append("  Эталонный ключ: НЕ ЗАДАН"
                     + (f" — {trusted_src}" if trusted_src else "") + ".")
        lines.append("  РЕЗУЛЬТАТ: метка «institution» в пакете есть, но сама по себе "
                     "она НИЧЕГО НЕ ДОКАЗЫВАЕТ.")
        lines.append("  Её ставит тот же процесс, который собрал пакет: достаточно "
                     "запустить сайдкар с")
        lines.append("  --signing-key на любой свой файл ключа, и метка будет такой же. "
                     "Пока публичный ключ")
        lines.append("  не сверен с эталоном вуза, этот пакет ровно так же силён, как "
                     "self-signed:")
        lines.append("  ЦЕЛОСТНОСТЬ подтверждена, АВТОРСТВО — нет.")
        lines.append("  Эталон берут НЕ из пакета: --trusted-pub <файл с публичным "
                     "ключом вуза>.")
        return OK, UNKNOWN, lines

    lines.append("  РЕЗУЛЬТАТ: подписано ключом с машины студента (self-signed).")
    if trusted_hex:
        lines.append(f"  Эталонный ключ: {trusted_src} — совпал, но для self-signed "
                     f"это ничего не добавляет:")
        lines.append("  совпадение означает лишь, что подписано тем ключом, который "
                     "есть и у вас.")
    lines.append("  ЦЕЛОСТНОСТЬ подтверждена. АВТОРСТВО — НЕТ: приватный ключ лежит там "
                 "же, где журнал,")
    lines.append("  поэтому можно собрать журнал с другими событиями, пересчитать "
                 "хеш-цепочку и переподписать")
    lines.append("  тем же ключом — и эта проверка пройдёт. Сверка ключа с эталоном "
                 "здесь не помогает:")
    lines.append("  эталон — тот же ключ с той же машины.")
    lines.append(f"  {info['advice']}")
    return OK, UNKNOWN, lines


def _authority_info(handover_mod: Any, authority: str,
                    verified: bool = False) -> dict[str, str]:
    """Формулировки для этой метки. `verified` — сверил ли ключ ПРОВЕРЯЮЩИЙ.

    Запасные тексты (когда модуль системы недоступен) обязаны быть такими же
    слабыми, как в `handover.py`: несверенный institution не сильнее
    self-signed, и скрипт, положенный рядом с пакетом без исходников системы,
    не имеет права говорить мягче.
    """
    if handover_mod is not None and hasattr(handover_mod, "authority_info"):
        try:
            return dict(handover_mod.authority_info(authority, verified))
        except TypeError:
            pass
        except Exception:
            pass
    if authority == AUTHORITY_INSTITUTION and verified:
        return {"label": "ключ учреждения (institution), эталон сверен",
                "proves": "целостность и доступ к ключу вуза в момент подписания",
                "not_proves": "что отчёт собрала система, а не человек с доступом "
                              "к ключу вуза и к машине экзамена",
                "advice": "Ротируйте приватный ключ после каждого потока."}
    if authority == AUTHORITY_INSTITUTION:
        return {"label": "заявлен ключ учреждения (institution), эталон НЕ сверен",
                "proves": "целостность при передаче",
                "not_proves": "авторство: метку institution ставит тот же процесс, "
                              "который собрал пакет — без сверки с эталоном вуза она "
                              "не сильнее self-signed",
                "advice": "Сверьте ключ с эталоном вуза: --trusted-pub <файл>."}
    return {"label": "ключ на машине студента (self-signed)",
            "proves": "целостность при передаче",
            "not_proves": "авторство: приватный ключ лежит на той же машине",
            "advice": "Усилить это может только ключ, которого на машине экзамена "
                      "нет; --signing-key даёт проверяемую метку, но приносит ключ "
                      "вуза на эту машину. См. docs/LIMITATIONS.md."}


def _check_manifest_signature(report_mod: Any, target: Target,
                              trusted_hex: str) -> tuple[str, list[str]]:
    """Подпись манифеста закрывает список sha256, то есть весь пакет сразу.

    Эталон применяется по тому же правилу, что и к подписи отчёта: только
    явный, заданный проверяющим, и независимо от метки. Метка `institution`
    больше не отключает сравнение — иначе её выгодно поставить.
    """
    if target.root is None:
        return UNKNOWN, []
    manifest = target.root / "manifest.json"
    sig = target.root / "manifest.json.sig"
    if not manifest.is_file() or not sig.is_file():
        return UNKNOWN, ["  Подпись манифеста: не приложена."]
    if report_mod is None or not hasattr(report_mod, "verify_report_detailed"):
        return UNKNOWN, ["  Подпись манифеста: проверить нечем."]
    result = report_mod.verify_report_detailed(manifest, str(sig), None)
    if result.get("ok"):
        actual = str(result.get("public_key") or "").strip().lower()
        if trusted_hex and actual and actual != trusted_hex:
            return FAIL, ["  Подпись манифеста: действительна, но ПОСТОРОННИМ ключом "
                          "(не совпал с эталоном, заданным проверяющим)."]
        lines = ["  Подпись манифеста: действительна — список sha256 всего пакета "
                 "закрыт подписью."]
        if str(result.get("authority") or "") == AUTHORITY_INSTITUTION and not trusted_hex:
            lines.append("  Метка манифеста institution с эталоном не сверена — она "
                         "ничего не добавляет.")
        return OK, lines
    return FAIL, [f"  Подпись манифеста: НЕ СОШЛАСЬ — {result.get('reason')}."]


# ===========================================================================
# 2. Цепочка журнала
# ===========================================================================
def _read_sessions(db_mod: Any, db: Path | None) -> list[dict[str, Any]]:
    """Строки таблицы `sessions` из журнала. Нужны ДО проверки манифеста.

    Манифест внутри себя согласован всегда (его собрал и подписал один
    процесс), поэтому единственная внешняя привязка — journal: `session_id`,
    `student_id` и `genesis_hash` в `evidence.sqlite`, защищённые цепочкой.
    """
    if db is None or not db.is_file():
        return []
    if db_mod is None or not hasattr(db_mod, "EvidenceStore"):
        return []
    try:
        store = db_mod.EvidenceStore(None, db_path=str(db))
    except Exception:
        return []
    try:
        return list(store.list_sessions())
    except Exception:
        return []
    finally:
        try:
            store.close()
        except Exception:
            pass


def _check_chain(db_mod: Any, db: Path | None) -> tuple[str, list[str]]:
    lines: list[str] = []
    if db is None:
        return UNKNOWN, ["  База доказательств не указана и не найдена рядом с отчётом."]
    if not db.is_file():
        return UNKNOWN, [f"  База доказательств не найдена: {db}"]
    if db_mod is None or not hasattr(db_mod, "EvidenceStore"):
        return UNKNOWN, ["  Модуль sidecar/storage/db.py недоступен — цепочку "
                         "проверить нечем."]
    store = db_mod.EvidenceStore(None, db_path=str(db))
    controls: list[dict[str, Any]] = []
    try:
        detailed = store.verify_chain_detailed()
        ok, number = store.verify_chain()
        sessions = store.list_sessions()
        loader = getattr(store, "load_controls", None)
        if callable(loader):
            try:
                controls = list(loader() or [])
            except Exception:
                controls = []
    finally:
        try:
            store.close()
        except Exception:
            pass

    lines.append(f"  База:           {db}")
    for row in sessions:
        name = row.get("student_name") or row.get("student_id") or "—"
        lines.append(f"  Сессия:         {row.get('session_id')} · {name} · "
                     f"экзамен {row.get('exam_id') or '—'}")
        lines.append(f"  Записей:        {row.get('records')} "
                     f"(инцидентов: {row.get('events_total')})")
        lines.append(f"  Genesis:        {_short(row.get('genesis_hash'))}")
    lines.append(f"  Проверено записей: {detailed.get('checked')} "
                 f"(из них инцидентов: {detailed.get('events_checked')})")
    # Сырой слой и записи решений — часть ТОЙ ЖЕ цепочки. Печатаем их отдельной
    # строкой: экзаменатор должен видеть, что в журнале лежит не только список
    # инцидентов, и что вмешательства в ход экзамена (сброс риска, решение
    # проктора) тоже проверены хешами, а не дописаны мимо журнала.
    raw_checked = int(detailed.get("observations_checked") or 0)
    ctl_checked = int(detailed.get("controls_checked") or 0)
    # Записи control о доказательствах — снимок окна экзамена и снятый клип —
    # к решениям над экзаменом не относятся: их по одной на инцидент, и в
    # общей сумме они выдали бы десятки «решений» там, где их не было.
    # Считаем их отдельно, остальное — записи о решениях и условиях экзамена.
    by_name: dict[str, int] = {}
    for record in controls:
        name = str(record.get("control") or "")
        by_name[name] = by_name.get(name, 0) + 1
    screens = by_name.get(SCREEN_CONTROL, 0)
    clips_cancelled = by_name.get(CLIP_CANCELLED_CONTROL, 0)
    decisions = max(ctl_checked - screens - clips_cancelled, 0)
    if raw_checked or ctl_checked:
        lines.append(f"  В той же цепочке:  сырых наблюдений {raw_checked}, "
                     f"записей о решениях {decisions}")
        if screens or clips_cancelled:
            lines.append(f"                     снимков окна теста: {screens}, "
                         f"отменённых клипов: {clips_cancelled}")
    if detailed.get("last_hash"):
        lines.append(f"  Хеш последней записи: {detailed['last_hash']}")
    if ok:
        lines.append("  РЕЗУЛЬТАТ: цепочка целостна — журнал инцидентов не правился.")
        return OK, lines
    lines.append(f"  РЕЗУЛЬТАТ: ЦЕПОЧКА НАРУШЕНА на записи #{number}.")
    lines.append(f"  Причина: {detailed.get('reason') or 'не определена'}")
    lines.append("  Это означает, что содержимое журнала изменили после экзамена:")
    lines.append("  запись, её порядок или метаданные сессии не соответствуют хешам.")
    return FAIL, lines


# ===========================================================================
# Пересчёт integrity по другому профилю весов
#
# Зачем это экзаменатору. Оценка в отчёте посчитана по правилам ОДНОГО вида
# экзамена — того, под который сайдкар был настроен. Но отчёт попадает к
# человеку, который знает правила СВОЕГО экзамена: если материалы были
# разрешены, половина штрафа в отчёте начислена за разрешённое поведение.
#
# Пересчёт отвечает на этот вопрос, не трогая журнал. Читается та же база, та
# же хеш-цепочка проверяется заново, состав событий остаётся прежним — меняется
# только таблица весов. Поэтому рядом с каждой цифрой печатается состояние
# цепочки: пересчёт по сломанному журналу не значит ничего, и подать его как
# «вторую оценку» было бы хуже, чем не считать вовсе.
#
# Чего пересчёт НЕ делает: он не разбирает сырой поток заново и не может
# добавить или убрать событие. Это была бы уже другая запись, а не та же самая.
# ===========================================================================
def _recompute_lines(report_mod: Any, target: Target,
                     profiles: list[str] | None) -> tuple[str, list[str]]:
    """(состояние цепочки, строки вывода) для режима пересчёта."""
    lines: list[str] = []
    if report_mod is None or not hasattr(report_mod, "recompute_integrity"):
        return UNKNOWN, ["  Модуль sidecar/storage/report.py недоступен или старой "
                         "версии — пересчитать нечем."]
    if target.root is None:
        return UNKNOWN, ["  Каталог сессии не определён."]

    try:
        result = report_mod.recompute_integrity(
            target.root, target.db, profiles or None)
    except Exception as exc:
        return UNKNOWN, [f"  Пересчёт не выполнен: {exc}"]

    chain = result.get("chain") or {}
    rows = result.get("profiles") or []
    if not rows:
        return UNKNOWN, ["  Ни один профиль весов не известен — пересчитывать нечем."]

    lines.append(f"  Сессия:         {result.get('session_id') or '—'}")
    lines.append(f"  Инцидентов в журнале: {result.get('incidents')} "
                 f"(всего записей событий: {result.get('events_total')})")
    if chain.get("available"):
        if chain.get("ok"):
            lines.append("  Цепочка журнала: ЦЕЛА — пересчёт идёт по неизменённой "
                         "записи.")
        else:
            lines.append(f"  Цепочка журнала: НАРУШЕНА на записи "
                         f"#{chain.get('broken_at')} — "
                         f"{chain.get('reason') or 'причина не определена'}.")
            lines.append("  Любая цифра ниже посчитана по журналу, который правили: "
                         "опираться на неё нельзя.")
    else:
        lines.append("  Цепочка журнала: не проверена "
                     f"({chain.get('reason') or 'база недоступна'}).")

    # Какие правила вуз объявил ДО экзамена и чем это подтверждается.
    applied = str(result.get("applied") or "")
    declared = result.get("declared") or {}
    lines.append("")
    if declared.get("available"):
        lines.append(f"  Объявленные правила: профиль «{applied}» из записи "
                     f"control/weight_profile")
        lines.append("                       хеш-цепочки — зафиксирован при старте "
                     "сессии, до")
        lines.append("                       первого события, и защищён хешем.")
    else:
        lines.append(f"  Объявленные правила: НЕ ПОДТВЕРЖДЕНЫ ЖУРНАЛОМ (профиль "
                     f"«{applied}»).")
        lines.append("                       Записи control/weight_profile в цепочке "
                     "нет; профиль")
        lines.append("                       взят из конфигурации рядом с отчётом. По "
                     "каким")
        lines.append("                       правилам вуз оценивал работу, проверить "
                     "нечем.")
    if declared.get("overridden"):
        lines.append("")
        lines.append("  ВНИМАНИЕ: встроенное имя профиля ПЕРЕОПРЕДЕЛЕНО конфигурацией.")
        lines.append(f"  Профиль «{applied}» носит имя встроенного, но его веса заданы")
        lines.append("  конфигурацией вуза, а не protocol.py. Сравнивать эту оценку с")
        lines.append("  оценкой по эталонному профилю того же имени нельзя.")
    changed = list(declared.get("changed") or [])
    if changed:
        lines.append("")
        lines.append(f"  Отклонения весов от protocol.py ({len(changed)}):")
        for i in range(0, len(changed), 3):
            lines.append("    " + ", ".join(str(k) for k in changed[i:i + 3]))
        lines.append("  Вид, которого в списке нет, весит как в закрытой книге.")
    elif declared.get("available"):
        lines.append("  Отклонений весов от protocol.py нет: веса эталонные.")

    lines.append("")
    lines.append("  ПРОФИЛЬ ВЕСОВ                    INTEGRITY    Δ")
    lines.append("  " + "─" * 62)
    for row in rows:
        name = str(row.get("profile") or "")
        label = str(row.get("label") or name)
        score = float(row.get("score") or 0.0)
        delta = row.get("delta")
        # Отметка ставится ОБЪЯВЛЕННОМУ профилю, а не closed-book. Раньше здесь
        # стояло жёсткое "closed-book", и сессия, собранная под open-book,
        # получала «<- базовый» на чужой строке и все Δ от неё: экзаменатору
        # выдавалось неверное утверждение о правилах оценки работы.
        mark = " <- объявленный" if (row.get("applied") or name == applied) else ""
        delta_text = (f"{float(delta):+.1f}" if isinstance(delta, (int, float))
                      and abs(float(delta)) >= 0.05 else "—")
        lines.append(f"  {label[:30]:<30} {score:>9.1f}   {delta_text:>7}{mark}")
        lines.append(f"    {name}: {row.get('description') or ''}")
    lines.append("")
    lines.append("  Δ считается от объявленного профиля: это та оценка, которая стоит")
    lines.append("  в отчёте. Остальные строки отвечают на вопрос «сколько это значит")
    lines.append("  по правилам другого экзамена».")
    lines.append("")
    lines.append("  Пересчёт меняет ТОЛЬКО вес зафиксированных наблюдений. Он не")
    lines.append("  разбирает сырой поток заново и не может добавить или убрать")
    lines.append("  событие: экзамен не перезапускался, журнал не переписывался,")
    lines.append("  хеш последней записи тот же.")

    if chain.get("available") and not chain.get("ok"):
        return FAIL, lines
    if not chain.get("available"):
        return UNKNOWN, lines
    return OK, lines


def _run_recompute(args: argparse.Namespace, target: Target,
                   report_mod: Any) -> int:
    """Режим `--recompute`: только пересчёт, без разбора комплекта и подписи."""
    profiles = [p.strip() for p in str(args.recompute or "").split(",") if p.strip()]
    if profiles == ["all"]:
        profiles = []
    status, lines = _recompute_lines(report_mod, target, profiles)
    if not args.quiet:
        _say()
        _say("ПЕРЕСЧЁТ INTEGRITY ПО ПОДПИСАННОМУ ЖУРНАЛУ")
        _say("Журнал не изменяется: меняется только таблица весов, по которой")
        _say("считается оценка. Состав событий остаётся тем, который зафиксировал")
        _say("движок во время экзамена.")
        _block("ПЕРЕСЧЁТ")
    for line in lines:
        _say(line)
    _say()
    if status == FAIL:
        _say("ИТОГ: журнал НАРУШЕН — пересчёт недействителен.")
        return 1
    if status == UNKNOWN:
        _say("ИТОГ: пересчёт неполный — целостность журнала не подтверждена.")
        return 2
    _say("ИТОГ: пересчёт выполнен по целой хеш-цепочке.")
    return 0


def _list_profiles(report_mod: Any) -> int:
    """Показать известные профили весов и выйти."""
    if report_mod is None or not hasattr(report_mod, "weight_profiles"):
        _say("Модуль sidecar/storage/report.py недоступен — профили не прочитать.")
        return 2
    profiles = report_mod.weight_profiles()
    base = getattr(report_mod, "BASE_PROFILE", "closed-book")
    _say()
    _say("ПРОФИЛИ ВЕСОВ")
    _say("Профиль описывает правила конкретного экзамена: что в нём разрешено, а")
    _say("что остаётся отклонением. Базовый профиль — веса из protocol.py.")
    _say()
    _say("Это ВСТРОЕННЫЕ профили из protocol.py, а не правила конкретной сессии.")
    _say("Вуз вправе переопределить любой из них своей конфигурацией, в том числе")
    _say("под встроенным именем. Какие правила объявлены для конкретной сессии и")
    _say("чем это подтверждено, показывает --recompute: он читает их из записи")
    _say("control/weight_profile подписанной хеш-цепочки, а не из конфигурации")
    _say("машины, на которой запущена проверка.")
    _say()
    for name, body in profiles.items():
        mark = "  (базовый)" if name == base else ""
        _say(f"  {name}{mark}")
        _say(f"    {body.get('label') or name}")
        _say(f"    {body.get('description') or ''}")
        allow = body.get("allow") or []
        keep = body.get("keep") or []
        if keep:
            _say(f"    учитываются только: {', '.join(str(k) for k in keep)}")
        elif allow:
            _say(f"    разрешено (вес 0): {', '.join(str(k) for k in allow)}")
        _say()
    _say("Пересчёт:  python3 scripts/verify_report.py <сессия> --recompute all")
    return 0


# ===========================================================================
# Передача: код сверки и degraded_handover
# ===========================================================================
def _check_handover(target: Target, handover_mod: Any) -> list[str]:
    """Что сказать проктору про саму передачу. Это не вердикт, а контекст.

    Прежняя версия печатала здесь «Передача штатная: запись шла в каталог,
    заданный экзаменатором». Это было самое тихое ложное утверждение всей
    системы: `source` в манифест пишет тот же процесс, который студент
    запустил, поэтому `PROCTOR_SESSIONS_DIR=./моё` давало `degraded=False`,
    `source="env"` и ровно эту успокаивающую строку. Отличить шару вуза от
    локального каталога студента манифест не может — ни это поле, ни любое
    другое. Честный пробел лучше напечатанного факта, поэтому теперь здесь
    написано, чего это утверждение стоит, и названо то, что действительно
    проверяемо: содержимое шары против списка группы.
    """
    lines: list[str] = []
    manifest = target.manifest or {}
    info = manifest.get("handover") if isinstance(manifest.get("handover"), dict) else {}
    code = str(manifest.get("session_code") or "")
    if handover_mod is not None and code:
        code = handover_mod.format_code(code)
    lines.append(f"  Код сверки сессии: {code or '—'}")
    if info.get("effective_dir"):
        lines.append(f"  Каталог записи:    {info['effective_dir']}")
    if info.get("requested_dir") and info.get("requested_dir") != info.get("effective_dir"):
        lines.append(f"  Просил проктор:    {info['requested_dir']}")
    if info.get("degraded_handover"):
        lines.append("  ПЕРЕДАЧА ДЕГРАДИРОВАНА: каталог, заданный при запуске, был")
        lines.append(f"  недоступен — {info.get('reason') or 'причина не записана'}.")
        lines.append("  Доказательства писались на машину экзамена, то есть всё время")
        lines.append("  находились под контролем того, кто за ней работает. Это не")
        lines.append("  подделка, но и не та сохранность, на которую рассчитывали.")
    elif info.get("requested_dir"):
        lines.append("  Каталог при запуске был задан. ЧЕГО ЭТО НЕ ЗНАЧИТ: что его "
                     "задал экзаменатор.")
        lines.append("  Поля «каталог» и «источник настройки» в манифест пишет тот же "
                     "процесс, который")
        lines.append("  запустили на машине экзамена; любой локальный каталог даёт "
                     "здесь такую же запись.")
        lines.append("  Проверяемый признак один: пакет лежит на шаре вуза, и число "
                     "пакетов на шаре")
        lines.append("  сходится со списком группы. Отсутствие пакета видно по "
                     "содержимому шары, а не по")
        lines.append("  отчёту: у пропавшей сессии отчёта просто нет.")
    elif info:
        lines.append("  Каталог при запуске НЕ задавался: комплект остался на машине "
                     "экзамена и всё время")
        lines.append("  был под контролем того, кто за ней работает. Забирать его "
                     "нужно с этой машины.")
    if info.get("append_only") is False:
        lines.append("  Каталог записи НЕ append-only (проба при старте): процесс на "
                     "машине экзамена мог")
        lines.append("  удалить и перезаписать уже записанный файл, то есть и готовый "
                     "пакет — свой или,")
        lines.append("  если папка общая, чужой. Сохранность обеспечивают права на "
                     "шаре, не эта система.")
    elif info.get("append_only") is True:
        lines.append("  Каталог записи append-only: записанный файл нельзя было ни "
                     "удалить, ни перезаписать.")
    if info.get("degraded_handover") is not None:
        lines.append("  Признак деградации вычислен ОДИН РАЗ, при старте: шара, "
                     "отвалившаяся посреди")
        lines.append("  экзамена, его не ставит.")
    if not info and not code:
        lines.append("  Сведений о передаче в комплекте нет (пакет собран старой версией")
        lines.append("  системы либо проверяются отдельные файлы, а не пакет).")
    if info.get("id_collision"):
        lines.append(f"  СОВПАДЕНИЕ ИМЁН СЕССИЙ: {info['id_collision']}")
    return lines


# ===========================================================================
# Вердикт
# ===========================================================================
def _verdict(package: str, integrity_file: str, chain: str,
             authorship: str, manifest_sig: str) -> int:
    """Напечатать итог и вернуть код возврата.

    Две оси вердикта принципиально раздельны:
      ЦЕЛОСТНОСТЬ — ничего не подменено после экзамена;
      АВТОРСТВО  — подписал тот, у кого ключ, недоступный студенту.
    Полный вердикт «подлинно» требует обеих. Одна целостность даёт код 2.
    """
    integrity = _combine(package, integrity_file, chain, manifest_sig)

    _block("ИТОГОВЫЙ ВЕРДИКТ")
    _say(f"  Комплект (manifest + sha256):  {_AXIS_LABELS[package]}")
    _say(f"  Журнал (hash-chain):           {_AXIS_LABELS[chain]}")
    _say(f"  Файл отчёта (подпись):         {_AXIS_LABELS[integrity_file]}")
    _say(f"  Подпись манифеста:             {_AXIS_LABELS[manifest_sig]}")
    _say()
    _say(f"  ЦЕЛОСТНОСТЬ: {_AXIS_LABELS[integrity]}")
    _say(f"  АВТОРСТВО:   {_AXIS_LABELS[authorship]}")
    _say()

    if FAIL in (package, integrity_file, chain, authorship, manifest_sig):
        _say("  ДОКАЗАТЕЛЬСТВА НЕ ПРОШЛИ ПРОВЕРКУ.")
        _say("  Как минимум одна из проверок выше показала расхождение. Опираться")
        _say("  на эти материалы при разборе нельзя: разбирайтесь по исходной сессии")
        _say("  на машине, где проходил экзамен, и по журналу сетевого каталога.")
        _say()
        return 1

    if integrity == OK and authorship == OK:
        _say("  ПРОВЕРЕНО ПОЛНОСТЬЮ: ЦЕЛОСТНОСТЬ И КЛЮЧ ВУЗА СВЕРЕН.")
        _say("  Комплект полон и не подменён, журнал связан хешами и пересчёт сошёлся,")
        _say("  публичный ключ совпал с эталоном, который вы задали сами.")
        _say()
        _say("  Граница этого вывода, и её надо знать до апелляции: доказано, что в")
        _say("  момент подписания у процесса был доступ к ключу вуза. Приватный ключ")
        _say("  при такой схеме читается на машине экзамена, где студент — "
             "администратор,")
        _say("  поэтому «подписал вуз» это не означает. Если ключ потока мог утечь,")
        _say("  пакет доказывает не больше, чем self-signed. Ключ ротируют после")
        _say("  каждого потока, а при споре опираются ещё и на очный контроль.")
        _say()
        return 0

    if integrity == OK and authorship == UNKNOWN:
        _say("  ЦЕЛОСТНОСТЬ ПОДТВЕРЖДЕНА. АВТОРСТВО — НЕТ.")
        _say("  Доказано: ничего не подменили после того, как материалы собрали и")
        _say("  подписали. НЕ доказано: что собрала их система, а не человек с доступом")
        _say("  к машине экзамена — приватный ключ по умолчанию лежит там же.")
        _say()
        _say("  Что это значит на практике: как доказательство целостности передачи")
        _say("  материалы годны. Как доказательство того, что события происходили, —")
        _say("  только вместе с очным контролем или ключом вуза.")
        _say()
        _say("  Чтобы АВТОРСТВО вообще стало проверяемым, нужны ОБА шага, и второй")
        _say("  не менее важен:")
        _say("    1) экзамен запускают с ключом вуза:")
        _say("       sidecar --signing-key /путь/к/ключу/вуза")
        _say("    2) проверяющий сверяет ключ с эталоном, который хранит сам:")
        _say("       verify_report.py <пакет> --trusted-pub /путь/к/публичному/ключу")
        _say("  Без шага 2 метка institution в пакете не значит ничего: её ставит тот")
        _say("  же процесс, который пакет собрал.")
        _say()
        return 2

    _say("  ПРОВЕРКА НЕПОЛНАЯ.")
    _say("  Признаков подделки не найдено, но часть проверок выполнить не удалось")
    _say("  (см. выше). Запросите пакет целиком: <сессия>.proctor.zip — в нём есть")
    _say("  manifest.json, report.html с подписью и публичным ключом, evidence.sqlite")
    _say("  и кадры. По отдельным файлам полную проверку собрать нельзя.")
    _say()
    return 2


def _combine(*axes: str) -> str:
    """Свести оси целостности в одну.

    FAIL сильнее всего: одно расхождение означает, что опираться нельзя ни на
    что. Иначе достаточно одного подтверждённого «ok», чтобы считать
    целостность подтверждённой: журнал и файл отчёта защищены независимо, и
    отсутствие пакета не отменяет сошедшейся хеш-цепочки. Если не подтвердилось
    ничего — UNKNOWN, и вердикт назовёт проверку неполной.
    """
    if FAIL in axes:
        return FAIL
    return OK if OK in axes else UNKNOWN


# ===========================================================================
# main
# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_report.py",
        description="Проверка комплекта доказательств: комплектность, целостность, "
                    "авторство.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Принимает пакет <сессия>.proctor.zip, каталог сессии или report.html.",
    )
    parser.add_argument("report_pos", nargs="?", metavar="ЧТО_ПРОВЕРЯТЬ",
                        help="пакет .proctor.zip, каталог сессии или report.html")
    parser.add_argument("--package", help="пакет доказательств <сессия>.proctor.zip")
    parser.add_argument("--report", help="путь к HTML-отчёту или к каталогу сессии")
    parser.add_argument("--sig", help="файл подписи (по умолчанию <отчёт>.sig)")
    parser.add_argument("--pub", help="публичный ключ (по умолчанию <отчёт>.pub)")
    parser.add_argument("--db", help="SQLite-журнал доказательств (evidence.sqlite)")
    parser.add_argument("--trusted-pub", dest="trusted_pub",
                        help="эталонный публичный ключ вуза — файл, который ВЫ храните "
                             "отдельно от пакетов. Без него метка institution не "
                             "проверяется и не доказывает авторства. По умолчанию "
                             "эталона нет: ключ машины проверяющего эталоном не "
                             "является")
    parser.add_argument("--keep-extracted", action="store_true",
                        help="не удалять каталог, в который распакован пакет")
    parser.add_argument("--quiet", action="store_true", help="только итоговый вердикт")

    recompute = parser.add_argument_group(
        "пересчёт оценки по другому профилю весов",
        "Оценка в отчёте посчитана по правилам одного вида экзамена. Если на "
        "вашем экзамене материалы были разрешены, часть штрафа начислена за "
        "разрешённое поведение. Пересчёт отвечает на этот вопрос по тому же "
        "подписанному журналу: экзамен не перезапускается, журнал не меняется.",
    )
    recompute.add_argument(
        "--recompute", nargs="?", const="all", default=None, metavar="ПРОФИЛЬ",
        help="пересчитать integrity: имя профиля, несколько через запятую или "
             "`all` для всех известных. Печатает цифры рядом с состоянием "
             "хеш-цепочки",
    )
    recompute.add_argument(
        "--list-profiles", dest="list_profiles", action="store_true",
        help="показать известные профили весов и выйти",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.report_pos and not (args.report or args.package):
        args.report = args.report_pos

    report_mod, db_mod, handover_mod = _load_modules()

    if args.list_profiles:
        return _list_profiles(report_mod)

    if not (args.report or args.package or args.db):
        _say("Не указано, что проверять. Примеры:")
        _say("  python3 scripts/verify_report.py <сессия>.proctor.zip")
        _say("  python3 scripts/verify_report.py sessions/<сессия>")
        _say("  python3 scripts/verify_report.py sessions/<сессия>/report.html")
        return 2

    target = _resolve_target(args, handover_mod)
    try:
        return _run(args, target, report_mod, db_mod, handover_mod)
    finally:
        if args.keep_extracted and target.tmp is not None:
            _say(f"Распакованный пакет оставлен здесь: {target.tmp}")
            target.tmp = None
        target.cleanup()


def _run(args: argparse.Namespace, target: Target,
         report_mod: Any, db_mod: Any, handover_mod: Any) -> int:
    if target.problem:
        _say(f"Проверить не удалось: {target.problem}")
        return 2
    if target.root is None:
        _say(f"Не найдено: {target.source}")
        return 2
    if target.report is None and target.db is None:
        _say(f"В {target.source} нет ни report.html, ни evidence.sqlite — "
             f"проверять нечего.")
        return 2

    # Пересчёт — отдельный вопрос к той же записи, а не часть проверки
    # подлинности. Смешивать их в одном выводе нельзя: «подпись сошлась» и
    # «по вашим правилам оценка другая» — утверждения разной природы.
    if args.recompute:
        return _run_recompute(args, target, report_mod)

    if not args.quiet:
        _say()
        _say("ПРОВЕРКА ДОКАЗАТЕЛЬСТВ О ПРОКТОРИНГЕ")
        _say("Проверяются три независимые вещи: полнота комплекта, целостность")
        _say("(журнал и файл отчёта) и авторство подписи. Целостность и авторство")
        _say("не одно и то же, поэтому в вердикте они стоят отдельными строками.")
        kinds = {"package": "пакет доказательств", "dir": "каталог сессии",
                 "file": "отдельный файл"}
        _say(f"Источник: {kinds.get(target.kind, target.kind)} — {target.source}")

    trusted_hex, trusted_src = _trusted_pub(args.trusted_pub)
    if args.trusted_pub and not trusted_hex:
        _say()
        _say(f"ВНИМАНИЕ: эталонный ключ не прочитан — {trusted_src}. "
             f"Сверка ключа не выполнялась.")

    # Журнал читается ДО проверки манифеста: манифест внутри себя согласован
    # всегда, и единственная внешняя привязка — session_id и genesis в
    # evidence.sqlite.
    sessions = _read_sessions(db_mod, target.db)

    pkg_status, pkg_lines = _check_package(target, handover_mod, sessions)
    sig_status, author_status, sig_lines = _check_signature(
        report_mod, handover_mod, target, args, trusted_hex, trusted_src)
    msig_status, msig_lines = _check_manifest_signature(
        report_mod, target, trusted_hex)
    chain_status, chain_lines = _check_chain(db_mod, target.db)
    handover_lines = _check_handover(target, handover_mod)

    if not args.quiet:
        _block("1. КОМПЛЕКТ ДОКАЗАТЕЛЬСТВ (manifest.json + sha256)")
        for line in pkg_lines:
            _say(line)
        for line in msig_lines:
            _say(line)
        _block("2. ЦЕПОЧКА ХЕШЕЙ ЖУРНАЛА (SQLite)")
        for line in chain_lines:
            _say(line)
        _block("3. ПОДПИСЬ: ЦЕЛОСТНОСТЬ ФАЙЛА И АВТОРСТВО (Ed25519)")
        for line in sig_lines:
            _say(line)
        _block("4. ПЕРЕДАЧА ДОКАЗАТЕЛЬСТВ")
        for line in handover_lines:
            _say(line)

    return _verdict(pkg_status, sig_status, chain_status, author_status, msig_status)


if __name__ == "__main__":
    raise SystemExit(main())

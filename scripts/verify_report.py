#!/usr/bin/env python3
"""
Проверка отчёта о прокторинге — инструмент преподавателя.

Отвечает на два вопроса:
  1. Этот HTML-файл — тот самый, который сформировала система? (подпись Ed25519)
  2. Журнал инцидентов не правили после экзамена? (hash-chain в SQLite)

Запуск (всё, кроме пути к отчёту, находится само):

    python3 scripts/verify_report.py sessions/20261016-101500_s123/report.html
    python3 scripts/verify_report.py --db sessions/.../evidence.sqlite
    python3 scripts/verify_report.py --report r.html --sig r.html.sig \\
                                     --pub r.html.pub --db evidence.sqlite

Коды возврата:
    0 — всё сошлось;
    1 — обнаружена подделка или расхождение;
    2 — проверить не удалось (нет файлов, нет cryptography и т.п.).

Сетевых обращений нет: проверка полностью локальная.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_SIDECAR = _ROOT / "sidecar"
for _p in (str(_SIDECAR), str(_SIDECAR / "storage")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

LINE = "─" * 72


def _import(*names: str) -> Any:
    import importlib
    for name in names:
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None


def _load_modules() -> tuple[Any, Any]:
    report_mod = _import("storage.report", "report")
    db_mod = _import("storage.db", "db")
    return report_mod, db_mod


def _find_report(args: argparse.Namespace) -> Path | None:
    if args.report:
        path = Path(args.report).expanduser()
        # Преподавателю естественно указать каталог сессии, а не файл внутри него
        # (именно каталог целиком и копируют на флешку). Раньше это давало
        # «Файл отчёта не найден» на совершенно правильной команде.
        if path.is_dir():
            inner = path / "report.html"
            return inner if inner.is_file() else path
        return path
    if args.db:
        candidate = Path(args.db).expanduser().parent / "report.html"
        if candidate.is_file():
            return candidate
    return None


def _find_db(args: argparse.Namespace, report: Path | None) -> Path | None:
    if args.db:
        return Path(args.db).expanduser()
    if report is not None:
        for name in ("evidence.sqlite", "evidence.db"):
            candidate = report.parent / name
            if candidate.is_file():
                return candidate
    return None


def _say(text: str = "") -> None:
    print(text)


def _block(title: str) -> None:
    _say()
    _say(LINE)
    _say(title)
    _say(LINE)


def _check_signature(report_mod: Any, report: Path | None,
                     sig: str | None, pub: str | None) -> tuple[str, list[str]]:
    """Вернуть ('ok'|'fail'|'skip', строки вывода)."""
    lines: list[str] = []
    if report is None:
        return "skip", ["  Файл отчёта не указан — подпись не проверялась."]
    if report_mod is None or not hasattr(report_mod, "verify_report_detailed"):
        return "skip", ["  Модуль sidecar/storage/report.py недоступен — подпись "
                        "проверить нечем."]
    result = report_mod.verify_report_detailed(report, sig, pub)
    lines.append(f"  Отчёт:          {report}")
    lines.append(f"  Файл подписи:   {result.get('sig_path') or '—'}")
    lines.append(f"  Публичный ключ: {result.get('pub_path') or '—'}")
    if result.get("public_key"):
        lines.append(f"  Ключ (hex):     {str(result['public_key'])[:32]}…")
    if result.get("signed_at"):
        lines.append(f"  Подписан:       {result['signed_at']}")
    if result.get("sha256"):
        lines.append(f"  sha256 в подписи: {str(result['sha256'])[:32]}…")
    if result.get("sha256_actual"):
        lines.append(f"  sha256 файла:     {str(result['sha256_actual'])[:32]}…")
    if result.get("ok"):
        lines.append("  РЕЗУЛЬТАТ: подпись действительна — файл отчёта не изменялся.")
        return "ok", lines
    reason = str(result.get("reason") or "причина не определена")
    if "не найден" in reason or "не установлен" in reason:
        lines.append(f"  РЕЗУЛЬТАТ: проверить не удалось — {reason}.")
        return "skip", lines
    lines.append(f"  РЕЗУЛЬТАТ: ПОДПИСЬ НЕ СОШЛАСЬ — {reason}.")
    return "fail", lines


def _check_chain(db_mod: Any, db: Path | None) -> tuple[str, list[str]]:
    lines: list[str] = []
    if db is None:
        return "skip", ["  База доказательств не указана и не найдена рядом с отчётом."]
    if not db.is_file():
        return "skip", [f"  База доказательств не найдена: {db}"]
    if db_mod is None or not hasattr(db_mod, "EvidenceStore"):
        return "skip", ["  Модуль sidecar/storage/db.py недоступен — цепочку "
                        "проверить нечем."]
    store = db_mod.EvidenceStore(None, db_path=str(db))
    try:
        detailed = store.verify_chain_detailed()
        ok, number = store.verify_chain()
        sessions = store.list_sessions()
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
        lines.append(f"  Genesis:        {str(row.get('genesis_hash') or '')[:32]}…")
    lines.append(f"  Проверено записей: {detailed.get('checked')} "
                 f"(из них инцидентов: {detailed.get('events_checked')})")
    if detailed.get("last_hash"):
        lines.append(f"  Хеш последней записи: {detailed['last_hash']}")
    if ok:
        lines.append("  РЕЗУЛЬТАТ: цепочка целостна — журнал инцидентов не правился.")
        return "ok", lines
    lines.append(f"  РЕЗУЛЬТАТ: ЦЕПОЧКА НАРУШЕНА на записи #{number}.")
    lines.append(f"  Причина: {detailed.get('reason') or 'не определена'}")
    lines.append("  Это означает, что содержимое журнала изменили после экзамена:")
    lines.append("  запись, её порядок или метаданные сессии не соответствуют хешам.")
    return "fail", lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_report.py",
        description="Проверка подписи отчёта и целостности журнала прокторинга.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("report_pos", nargs="?", metavar="ОТЧЁТ",
                        help="путь к report.html (можно без остальных флагов)")
    parser.add_argument("--report", help="путь к HTML-отчёту")
    parser.add_argument("--sig", help="файл подписи (по умолчанию <отчёт>.sig)")
    parser.add_argument("--pub", help="публичный ключ (по умолчанию <отчёт>.pub)")
    parser.add_argument("--db", help="SQLite-журнал доказательств (evidence.sqlite)")
    parser.add_argument("--quiet", action="store_true", help="только итоговый вердикт")
    args = parser.parse_args(argv)
    if args.report_pos and not args.report:
        args.report = args.report_pos

    report_mod, db_mod = _load_modules()
    report = _find_report(args)
    db = _find_db(args, report)

    if report is None and db is None:
        _say("Не указано, что проверять. Пример:")
        _say("  python3 scripts/verify_report.py sessions/<сессия>/report.html")
        return 2
    if report is not None and not report.is_file():
        _say(f"Файл отчёта не найден: {report}")
        return 2

    if not args.quiet:
        _say()
        _say("ПРОВЕРКА ОТЧЁТА О ПРОКТОРИНГЕ")
        _say("Проверяются две независимые вещи: подлинность файла отчёта")
        _say("и целостность журнала инцидентов (hash-chain).")

    sig_status, sig_lines = _check_signature(report_mod, report, args.sig, args.pub)
    chain_status, chain_lines = _check_chain(db_mod, db)

    if not args.quiet:
        _block("1. ПОДПИСЬ ОТЧЁТА (Ed25519)")
        for line in sig_lines:
            _say(line)
        _block("2. ЦЕПОЧКА ХЕШЕЙ ЖУРНАЛА (SQLite)")
        for line in chain_lines:
            _say(line)

    _block("ИТОГОВЫЙ ВЕРДИКТ")
    labels = {"ok": "в порядке", "fail": "НАРУШЕНО", "skip": "не проверялось"}
    _say(f"  Подпись отчёта:   {labels[sig_status]}")
    _say(f"  Цепочка журнала:  {labels[chain_status]}")
    _say()

    if "fail" in (sig_status, chain_status):
        _say("  ОТЧЁТ НЕ ПРОШЁЛ ПРОВЕРКУ.")
        _say("  Данные были изменены после завершения экзамена и не могут")
        _say("  использоваться как доказательство. Разбираться следует по")
        _say("  исходной сессии на машине, где проходил экзамен.")
        _say()
        return 1
    if sig_status == "ok" and chain_status == "ok":
        _say("  ОТЧЁТ ПОДЛИННЫЙ.")
        _say("  Файл подписан ключом системы и не изменялся; каждая запись")
        _say("  журнала связана хешем с предыдущей, пересчёт сошёлся.")
        _say()
        return 0
    _say("  ПРОВЕРКА НЕПОЛНАЯ.")
    _say("  Признаков подделки не найдено, но часть проверок выполнить не удалось")
    _say("  (см. выше). Запросите полный комплект: report.html, report.html.sig,")
    _say("  report.html.pub и evidence.sqlite из каталога сессии.")
    _say()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Сверка черновика с КЛАССИФИКАЦИЕЙ ЯДРА. Запускается инструментом разведки.

Зачем отдельный процесс, а не ещё одна копия правил на JavaScript: копия
расходится. Таблицы ядра (`sidecar/config.py`) живут своей жизнью, и
единственный способ честно ответить на вопрос «а ядро это примет?» — спросить
само ядро. Поэтому инструмент не угадывает, а вызывает `classify_origin()`
ровно тем кодом, который будет разбирать профиль на экзамене.

Вход — JSON-массив записей (stdin). Выход — JSON-объект
`{запись: {"class": ..., "reason": ..., "normalized": ...}}` (stdout).

Расхождение между советом инструмента и ответом ядра — это НЕ ошибка
инструмента и не повод молчать: оно печатается человеку отдельной строкой,
потому что именно такие случаи и ломают внедрение. Живой пример с LMS
заказчика: счётчик `mc.yandex.ru` ядро относит к классу `search` и из белого
списка выбрасывает — запись бы просто не подействовала, и проктор узнал бы об
этом из инцидентов, а не из разбора.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from sidecar.config import classify_origin, normalize_origin
    except Exception as exc:  # noqa: BLE001 — важно имя причины, а не тип
        json.dump({"__error__": f"ядро не импортируется: {exc}"}, sys.stdout,
                  ensure_ascii=False)
        return 2

    try:
        items = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001
        json.dump({"__error__": f"вход не разбирается как JSON: {exc}"}, sys.stdout,
                  ensure_ascii=False)
        return 2
    if not isinstance(items, list):
        json.dump({"__error__": "ожидался JSON-массив записей"}, sys.stdout,
                  ensure_ascii=False)
        return 2

    out: dict[str, dict[str, str]] = {}
    for raw in items:
        key = str(raw)
        normalized = normalize_origin(key)
        kind, reason = classify_origin(normalized)
        out[key] = {"class": kind, "reason": reason, "normalized": normalized}
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

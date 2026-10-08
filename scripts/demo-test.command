#!/bin/bash
# ПОКАЗ ДЛЯ ЖЮРИ: заготовленный тест на этом компьютере, без LMS и без сети.
#
# Двойной клик по этому файлу — и можно проходить. Шесть простых вопросов,
# десять минут. Во время теста работает наблюдение (камера, окно, ввод),
# после сдачи собирается подписанный пакет доказательств, копируется в папку
# проктора, и этот же скрипт сразу проверяет копию — так комиссия видит путь
# целиком: экзамен → пакет → доставка проктору → проверка.
#
# Папка проктора на показе — ~/Documents/proctor-deliver: она изображает
# флешку проктора или сетевую папку вуза. Другая папка или без доставки:
#   DELIVER_DIR=/Volumes/PROCTOR scripts/demo-test.command
#   DELIVER_DIR= scripts/demo-test.command
#
# Встроенный браузер с настоящим Moodle — отдельный показ:
# scripts/demo-lms.command. В один прогон их не смешиваем: страница экзамена
# одна, и здесь это локальный тест.
#
# Запускается из Terminal.app: разрешение на камеру macOS выдаёт приложению,
# владеющему процессом, а не Python.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

EXAMDIR="${PROCTOR_DEMO_TEST_DIR:-$HOME/Documents/proctor-demo-test}"
mkdir -p "$EXAMDIR" || { echo "Не удалось создать $EXAMDIR"; read -r _; exit 1; }
# Профиль экзамена здесь не нужен: без него открывается локальный тест. Каталог
# свой, отдельный от показа LMS, поэтому удаляем только собственный файл.
rm -f "$EXAMDIR/exam-profile.json"

# Папка проктора. Не задана — берём PROCTOR_DELIVER_DIR, иначе папку показа.
# Явно пустая (DELIVER_DIR=) — доставки нет, пакет остаётся рядом с сессией.
DEMO_DELIVER_DIR="$HOME/Documents/proctor-deliver"
DELIVER_DIR="${DELIVER_DIR-${PROCTOR_DELIVER_DIR:-$DEMO_DELIVER_DIR}}"
# Создаём только собственную папку показа. Чужой путь не создаём: /Volumes/…
# без вставленной флешки стал бы папкой на локальном диске, и «доставка» туда
# ничего бы не доставила. Его проверит сайдкар и скажет причину вслух.
if [ "$DELIVER_DIR" = "$DEMO_DELIVER_DIR" ]; then
    mkdir -p "$DELIVER_DIR" || { echo "Не удалось создать $DELIVER_DIR"; read -r _; exit 1; }
fi

clear
cat <<'TXT'
==========================================================
  ПОКАЗ: заготовленный тест с наблюдением
==========================================================

ПОРЯДОК
  1. Согласие: отметьте галочку и нажмите «Продолжить».
  2. Предполётная проверка. Если она упрётся в запущенные
     программы (мессенджеры, ИИ-клиенты), нажмите «Открыть
     страницу теста без экзаменационного режима»: это тестовый
     запуск, и отчёт честно это отметит.
  3. Калибровка: смотрите в центр, затем за точкой по экрану.
  4. Тест: шесть простых вопросов, десять минут. Попробуйте
     отвернуться, взять телефон, вставить текст из буфера —
     всё это появится в панели наблюдения справа.
  5. Сдайте тест — откроется отчёт с оценкой доверия. Строка
     о доставке: «Пакет доставлен в папку проктора: … (sha256
     сверен)». Не доставлен — там же причина и кнопка
     «Повторить доставку».
  6. Закройте программу (Ctrl+C в этом окне). Скрипт сам найдёт
     пакет доказательств — копию в папке проктора — и проверит
     его подпись и цепочку.

TXT
echo "Пакеты сессий:  $EXAMDIR"
if [ -n "$DELIVER_DIR" ]; then
    echo "Папка проктора: $DELIVER_DIR"
else
    echo "Папка проктора: не задана — пакет останется рядом с сессией"
fi
echo
echo "Нажмите Enter, чтобы запустить."
read -r _

# Ctrl+C гасит запуск, а этот скрипт доживает до проверки пакета. Обработчик,
# а не игнор: игнор унаследовал бы run-safe.command, и Ctrl+C не работал бы.
trap ':' INT
env PROCTOR_SESSIONS_DIR="$EXAMDIR" PROCTOR_DELIVER_DIR="$DELIVER_DIR" \
    "$(dirname "$0")/run-safe.command"
trap - INT

echo
echo "=================================================="
echo " ПРОВЕРКА ПАКЕТА"
echo "=================================================="
PKG="$(ls -t "$EXAMDIR"/*.proctor.zip 2>/dev/null | head -1)"
# Куда этот пакет доставлен — со слов ядра (handover.json сессии), а не по
# свежему файлу в папке проктора: на общей шаре там пакеты всей группы.
DEST=""
if [ -n "$PKG" ] && [ -n "$DELIVER_DIR" ]; then
    DEST="$(.venv/bin/python - "$EXAMDIR" "$PKG" <<'PYEOF' 2>/dev/null
import json
import sys
from pathlib import Path

pkg = Path(sys.argv[2]).resolve()
for note_path in Path(sys.argv[1]).glob("*/handover.json"):
    try:
        note = json.loads(note_path.read_text(encoding="utf-8"))
        local = str((note.get("package") or {}).get("package") or "")
        res = note.get("delivery") or {}
        if local and Path(local).resolve() == pkg and res.get("ok") and res.get("dest"):
            print(res["dest"])
            break
    except (OSError, ValueError, AttributeError):
        continue
PYEOF
)"
fi
if [ -n "$DEST" ] && [ -f "$DEST" ]; then
    echo "Копия в папке проктора:  $DEST"
    echo "Оригинал на этой машине: $PKG"
    echo
    echo "Проверяю КОПИЮ в папке проктора — ровно то, что получит экзаменатор:"
    echo
    .venv/bin/python scripts/verify_report.py "$DEST"
    echo
    echo "Показать пакет в Finder: open -R \"$DEST\""
elif [ -n "$PKG" ]; then
    if [ -n "$DEST" ]; then
        echo "Копии по пути $DEST сейчас нет (носитель извлечён?)."
        echo "Проверяю пакет на этой машине."
    elif [ -n "$DELIVER_DIR" ]; then
        echo "В папку проктора пакет НЕ доставлен (причина — выше и на финальном экране)."
        echo "Проверяю пакет на этой машине."
    fi
    echo "Пакет: $PKG"
    echo
    .venv/bin/python scripts/verify_report.py "$PKG"
else
    echo "Пакет не найден: сессия не была завершена сдачей теста."
fi
echo
echo "Нажмите Enter, чтобы закрыть."
read -r _

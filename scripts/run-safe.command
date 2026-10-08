#!/bin/bash
# Безопасный запуск для тестирования: сайдкар + оболочка, БЕЗ блокировок окружения.
#
# Запускается из Terminal.app, потому что разрешение на камеру macOS выдаёт
# приложению-владельцу процесса, а не Python.
#
# Флаги:
#   --no-lockdown  блокировки не включаются никогда: ни перехвата сочетаний,
#                  ни очистки буфера обмена, ни окна поверх остальных
#   --no-kiosk     окно обычное, его можно двигать, сворачивать и закрывать
#
# Ctrl+C в этом окне останавливает оба процесса.
#
# ПЕРЕДАЧА ДОКАЗАТЕЛЬСТВ. Каталог сессий и ключ подписи задаёт проктор — любым
# из двух способов, они равносильны:
#
#   PROCTOR_SESSIONS_DIR=/Volumes/EXAM ./scripts/run-safe.command
#   ./scripts/run-safe.command --sessions-dir /Volumes/EXAM --signing-key /Volumes/EXAM/uni.key
#
# Зачем каталог на чужом диске: то, что ушло на сетевую папку вуза или на
# USB-носитель проктора, студент удалить не может. Если каталог недоступен на
# запись, сайдкар скажет об этом ЯВНО при старте, запишет доказательства в
# локальный каталог и пометит сессию как degraded_handover — отчёт это покажет.
#
# Зачем ключ вуза: без него подпись ставится ключом с ЭТОЙ машины и доказывает
# только целостность при передаче. Приватный ключ лежит в keys/ здесь же,
# поэтому журнал можно пересобрать и переподписать. Ключ вуза на машине
# студента не хранится — только с ним подпись доказывает авторство.
#
# ПАПКА ПРОКТОРА. Второй способ передачи, и он не заменяет первый: сессия
# пишется в каталог сессий как обычно (на этой машине), а ПОСЛЕ экзамена готовый
# пакет <сессия>.proctor.zip копируется в папку проктора со сверкой sha256:
#
#   PROCTOR_DELIVER_DIR=/Volumes/PROCTOR ./scripts/run-safe.command
#   ./scripts/run-safe.command --deliver-to /Volumes/PROCTOR
#
# Во время экзамена туда не пишется ничего, кроме пробного файла при старте.
# Папка недоступна — экзамен НЕ останавливается: пакет остаётся здесь, а на
# финальном экране есть кнопка «Повторить доставку». Куда пакет дошёл, скрипт
# печатает в конце.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PY=".venv/bin/python"
PORT=8787

# Флаги передачи доказательств: из командной строки, иначе из окружения.
# Собственные флаги скрипта (--no-lockdown, --no-kiosk) уходят оболочке, а эти
# три — сайдкару, поэтому разбираем их здесь и из "$@" убираем.
SESSIONS_DIR="${PROCTOR_SESSIONS_DIR:-}"
SIGNING_KEY="${PROCTOR_SIGNING_KEY:-}"
DELIVER_DIR="${PROCTOR_DELIVER_DIR:-}"
SHELL_ARGS=()
# Флаг последним словом без значения: `shift 2` при одном аргументе не сдвигает
# ничего, и цикл крутился бы вечно. Поэтому второй сдвиг — только если есть что.
while [ $# -gt 0 ]; do
    case "$1" in
        --sessions-dir)   SESSIONS_DIR="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --sessions-dir=*) SESSIONS_DIR="${1#*=}"; shift ;;
        --signing-key)    SIGNING_KEY="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --signing-key=*)  SIGNING_KEY="${1#*=}"; shift ;;
        --deliver-to)     DELIVER_DIR="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --deliver-to=*)   DELIVER_DIR="${1#*=}"; shift ;;
        *)                SHELL_ARGS+=("$1"); shift ;;
    esac
done

HANDOVER_ARGS=()
[ -n "$SESSIONS_DIR" ] && HANDOVER_ARGS+=(--sessions-dir "$SESSIONS_DIR")
[ -n "$SIGNING_KEY" ] && HANDOVER_ARGS+=(--signing-key "$SIGNING_KEY")
[ -n "$DELIVER_DIR" ] && HANDOVER_ARGS+=(--deliver-to "$DELIVER_DIR")

# Отметка старта: в конце ищем записки о передаче (handover.json), написанные
# ПОСЛЕ неё, — это сессии этого запуска, а не прошлых.
START_TS="$(date +%s)"

# Системный bash на macOS — 3.2, и под `set -u` раскрытие "${A[@]}" ПУСТОГО
# массива не даёт пустоты: оно даёт «unbound variable» и выход из скрипта.
# Ровно это и происходило, когда камера не находилась и CAM_ARGS оставался
# пустым: сайдкар не запускался вообще, а причина выглядела как «не поднялся».
# Поэтому массивы раскрываются формой ${A[@]+"${A[@]}"}: ноль аргументов и
# на 3.2, и на 5.x, с сохранением кавычек у путей с пробелами.

echo "=================================================="
echo " ПРОКТОРИНГ — безопасный тестовый запуск"
echo " Блокировки окружения ВЫКЛЮЧЕНЫ (--no-lockdown)"
echo "=================================================="
echo

# Прежние процессы снимаем ПЕРВЫМ делом. Иначе подбор камеры ниже упрётся
# в устройство, занятое старым сайдкаром, решит что рабочих камер нет и
# запустится вслепую без зрения. Именно так и вышло при первой правке.
echo "--- Останавливаю прежние процессы ---"
pkill -f "sidecar/main.py" 2>/dev/null
pkill -f "industry_hackaton/node_modules/electron" 2>/dev/null
sleep 2

# Индекс камеры подбираем САМИ. Спрашивать оказалось вредно: на macOS iPhone
# как Continuity Camera то занимает индекс 0, то исчезает, и человек угадывает
# вслепую. Дважды привело к запуску на несуществующем индексе и пустым каналам.
echo "--- Камера ---"
# Явное указание сильнее любой догадки:
#   PROCTOR_CAMERA=1 ./scripts/run-safe.command
# Зачем это вообще нужно. На macOS подключённый iPhone регистрируется как
# Continuity Camera и ОТДАЁТ КАДРЫ, поэтому правило «беру первую рабочую»
# выбирает телефон. Проверено на машине заказчика: system_profiler перечисляет
# встроенную FaceTime первой, а OpenCV отдаёт её под индексом 1 — порядки НЕ
# совпадают, и определить встроенную по имени из системы нельзя.
# Поэтому: берём указанную, иначе показываем всё найденное с разрешением
# и выбираем по признаку встроенной (1280x720), а выбор печатаем вслух.
CAM_INDEX="${PROCTOR_CAMERA:-}"
if [ -n "$CAM_INDEX" ]; then
    echo "Камера задана явно: индекс $CAM_INDEX (PROCTOR_CAMERA)"
    CAM_ARGS=(--camera "$CAM_INDEX")
else
    CAM_INDEX="$("$PY" - <<'PYEOF' 2>/dev/null
import cv2, sys
found = []
for i in range(5):
    cap = cv2.VideoCapture(i)
    ok, frame = (False, None)
    if cap.isOpened():
        ok, frame = cap.read()
    cap.release()
    if ok and frame is not None:
        h, w = frame.shape[:2]
        found.append((i, w, h))
for i, w, h in found:
    kind = "похожа на встроенную" if (w, h) == (1280, 720) else "возможно, телефон или внешняя"
    print(f"  индекс {i}: {w}x{h} — {kind}", file=sys.stderr)
builtin = [i for i, w, h in found if (w, h) == (1280, 720)]
pick = builtin[0] if builtin else (found[0][0] if found else None)
if pick is None:
    sys.exit(1)
print(pick)
PYEOF
)"
    if [ -n "$CAM_INDEX" ]; then
        echo "Выбрана камера: индекс $CAM_INDEX"
        echo "Если это не та камера — задайте явно: PROCTOR_CAMERA=N ./scripts/run-safe.command"
        CAM_ARGS=(--camera "$CAM_INDEX")
    else
        echo "Ни одна камера не открылась."
        echo "Системные настройки -> Конфиденциальность и безопасность -> Камера -> включить Terminal."
        echo "Запускаю без зрения: остальные каналы работают."
        CAM_ARGS=()
    fi
fi

echo
echo "--- Проверка доступа к камере (главный поток) ---"
"$PY" - "${CAM_INDEX:-0}" <<'PYEOF' 2>&1 | grep -v "^OpenCV:\|^\[ WARN"
import cv2, sys
idx = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0
cap = cv2.VideoCapture(idx)
ok = cap.isOpened()
frame_ok, frame = (False, None)
if ok:
    frame_ok, frame = cap.read()
cap.release()
if frame_ok:
    print(f"Камера {idx}: кадр {frame.shape[1]}x{frame.shape[0]} — доступна.")
else:
    print(f"Камера {idx} недоступна.")
    print("Если диалога доступа не было: Системные настройки ->")
    print("Конфиденциальность и безопасность -> Камера -> включить Terminal.")
    print("Запускаю дальше без зрения: остальные каналы работают.")
PYEOF

echo
echo "--- Останавливаю прежние процессы ---"
pkill -f "sidecar/main.py" 2>/dev/null
pkill -f "industry_hackaton/node_modules/electron" 2>/dev/null
sleep 1

# Куда ушёл пакет этого запуска. Источник — handover.json каталога сессии: туда
# ядро пишет итог каждой попытки доставки, в том числе повтора с финального
# экрана. Догадка по содержимому папки проктора не годится: на общей шаре там
# лежат пакеты всей группы.
delivery_summary() {
    echo
    echo "--- Пакет и папка проктора ---"
    "$PY" - "$START_TS" "${DELIVER_DIR:+1}" "${SESSIONS_DIR:-}" sessions <<'PYEOF' 2>/dev/null \
        || echo "Итог не прочитан: смотрите финальный экран и handover.json в каталоге сессии."
import json
import sys
from pathlib import Path

since = float(sys.argv[1])
configured = bool(sys.argv[2])
found, seen = [], set()
for base in sys.argv[3:]:
    if not base:
        continue
    for note_path in Path(base).expanduser().glob("*/handover.json"):
        try:
            key = note_path.resolve()
            mtime = note_path.stat().st_mtime
            if key in seen or mtime < since:
                continue
            seen.add(key)
            note = json.loads(note_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(note, dict):
            found.append((mtime, note_path.parent, note))

if not found:
    print("Пакета этого запуска нет: тест не сдавали, или сессия не завершилась.")
    sys.exit(0)
for _, sdir, note in sorted(found, key=lambda item: item[0]):
    pkg = note.get("package") if isinstance(note.get("package"), dict) else {}
    local = str(pkg.get("package") or "") or "пакет не собран"
    res = note.get("delivery") if isinstance(note.get("delivery"), dict) else None
    print(f"Сессия:  {sdir.name}")
    if res is None and not configured:
        print(f"  Папка проктора не задана — пакет остался на этом компьютере: {local}")
    elif res is None:
        print("  Пакет НЕ доставлен: итог копирования не записан "
              "(программу закрыли раньше, чем оно закончилось?).")
        print(f"  Он лежит здесь: {local}")
    elif res.get("ok") and res.get("verified"):
        print(f"  Пакет доставлен в папку проктора: {res.get('dest')} (sha256 сверен)")
    elif res.get("ok"):
        print(f"  Пакет записан в папку проктора: {res.get('dest')}, но sha256 копии "
              f"НЕ сверен: {res.get('error') or 'причина не записана'}")
    else:
        print(f"  Пакет НЕ доставлен: {res.get('error') or 'причина не записана'}")
        print(f"  Он лежит здесь: {local}")
    attempts = note.get("delivery_attempts")
    if isinstance(attempts, list) and len(attempts) > 1:
        print(f"  Попыток доставки: {len(attempts)}, все — в {sdir / 'handover.json'}")
PYEOF
}

cleanup() {
    # Второй Ctrl+C во время ожидания ниже — выход сразу, без повторной уборки.
    trap - INT TERM
    echo
    echo "--- Останавливаю всё ---"
    pkill -f "industry_hackaton/node_modules/electron" 2>/dev/null
    if [ -n "${SIDECAR_PID:-}" ] && kill -0 "$SIDECAR_PID" 2>/dev/null; then
        kill "$SIDECAR_PID" 2>/dev/null
        # Сайдкар на сигнал закрывает незавершённую сессию, собирает пакет и
        # копирует его в папку проктора. Ждём его, а не бросаем: иначе итог
        # ниже читался бы раньше, чем ядро успело его записать.
        waited=0
        while kill -0 "$SIDECAR_PID" 2>/dev/null && [ "$waited" -lt 60 ]; do
            [ "$waited" -eq 2 ] && echo "Жду, пока сайдкар закроет сессию и соберёт пакет…"
            sleep 0.5
            waited=$((waited + 1))
        done
    fi
    delivery_summary
    echo
    echo "Готово."
    exit 0
}
trap cleanup INT TERM

echo "--- Передача доказательств ---"
if [ -n "$SESSIONS_DIR" ]; then
    echo "Каталог сессий:  $SESSIONS_DIR"
    if [ -d "$SESSIONS_DIR" ] && [ -w "$SESSIONS_DIR" ]; then
        echo "Запись:          доступна"
    else
        echo "Запись:          ПРОВЕРЬТЕ — каталога нет или он только для чтения."
        echo "                 Сайдкар скажет точную причину и уйдёт в локальный"
        echo "                 каталог, пометив сессию как degraded_handover."
    fi
else
    echo "Каталог сессий:  sessions/ на этой машине (по умолчанию)."
    echo "                 В классе указывайте диск проктора:"
    echo "                 --sessions-dir /Volumes/EXAM"
fi
if [ -n "$DELIVER_DIR" ]; then
    echo "Папка проктора:  $DELIVER_DIR"
    if [ -d "$DELIVER_DIR" ] && [ -w "$DELIVER_DIR" ]; then
        echo "                 после экзамена пакет будет скопирован туда (со сверкой sha256)"
    else
        echo "                 ПРОВЕРЬТЕ — папки нет или она только для чтения."
        echo "                 Экзамен это не остановит: сайдкар скажет точную причину,"
        echo "                 пакет останется на этой машине, а доставку можно"
        echo "                 повторить кнопкой на финальном экране."
    fi
else
    echo "Папка проктора:  не задана — пакет останется на этой машине."
    echo "                 Флешка или сетевая папка вуза: --deliver-to /Volumes/PROCTOR"
fi
if [ -n "$SIGNING_KEY" ]; then
    echo "Ключ подписи:    $SIGNING_KEY (ключ учреждения)"
    echo "Подпись докажет: целостность И авторство"
else
    echo "Ключ подписи:    не задан, будет использован ключ с этой машины"
    echo "Подпись докажет: только целостность при передаче, но НЕ авторство"
    echo "                 Ключ вуза: --signing-key /путь/к/ключу"
fi
echo

echo "--- Сайдкар на 127.0.0.1:$PORT ---"
"$PY" sidecar/main.py --host 127.0.0.1 --port "$PORT" \
    ${CAM_ARGS[@]+"${CAM_ARGS[@]}"} ${HANDOVER_ARGS[@]+"${HANDOVER_ARGS[@]}"} &
SIDECAR_PID=$!
sleep 3

if ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
    echo "Сайдкар не поднялся. Смотрите вывод выше."
    echo "Нажмите Enter, чтобы закрыть."
    read -r _
    exit 1
fi

echo "--- Оболочка (обычное окно, без блокировок) ---"
echo
echo "Окно можно двигать, сворачивать и закрывать."
echo "Остановить всё: Ctrl+C здесь."
echo
npx electron . --no-kiosk --no-lockdown --ws-port "$PORT" \
    ${SHELL_ARGS[@]+"${SHELL_ARGS[@]}"}

cleanup

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
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PY=".venv/bin/python"
PORT=8787

# Флаги передачи доказательств: из командной строки, иначе из окружения.
# Собственные флаги скрипта (--no-lockdown, --no-kiosk) уходят оболочке, а эти
# два — сайдкару, поэтому разбираем их здесь и из "$@" убираем.
SESSIONS_DIR="${PROCTOR_SESSIONS_DIR:-}"
SIGNING_KEY="${PROCTOR_SIGNING_KEY:-}"
SHELL_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --sessions-dir)   SESSIONS_DIR="${2:-}"; shift 2 ;;
        --sessions-dir=*) SESSIONS_DIR="${1#*=}"; shift ;;
        --signing-key)    SIGNING_KEY="${2:-}"; shift 2 ;;
        --signing-key=*)  SIGNING_KEY="${1#*=}"; shift ;;
        *)                SHELL_ARGS+=("$1"); shift ;;
    esac
done

HANDOVER_ARGS=()
[ -n "$SESSIONS_DIR" ] && HANDOVER_ARGS+=(--sessions-dir "$SESSIONS_DIR")
[ -n "$SIGNING_KEY" ] && HANDOVER_ARGS+=(--signing-key "$SIGNING_KEY")

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

cleanup() {
    echo
    echo "--- Останавливаю всё ---"
    [ -n "${SIDECAR_PID:-}" ] && kill "$SIDECAR_PID" 2>/dev/null
    pkill -f "industry_hackaton/node_modules/electron" 2>/dev/null
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

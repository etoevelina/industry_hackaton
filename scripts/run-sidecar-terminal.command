#!/bin/bash
# Запуск сайдкара из Terminal.app.
#
# Зачем отдельный файл: macOS выдаёт доступ к камере приложению, которое владеет
# процессом. Шелл внутри среды разработки принадлежит ей, а не Терминалу, поэтому
# запрос уходит не туда и диалог не появляется. Двойной клик по этому файлу
# (или `open -a Terminal`) делает ответственным Terminal.app — и система спрашивает.
#
# Второе: OpenCV запрашивает доступ только с ГЛАВНОГО потока. Наш CameraCapture
# открывает камеру в рабочем потоке, поэтому запрос оттуда не срабатывает никогда
# («can not spin main run loop from other thread»). Поэтому ниже сначала идёт
# короткое открытие камеры с главного потока — оно и вызывает диалог.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
PY=".venv/bin/python"

echo "=== Проверка доступа к камере ==="
echo "Если macOS спросит разрешение — нажмите «Разрешить»."
echo

"$PY" - <<'PYEOF'
import cv2, sys
cap = cv2.VideoCapture(0)          # главный поток: здесь запрос разрешения работает
ok = cap.isOpened()
frame_ok = False
if ok:
    frame_ok, frame = cap.read()
cap.release()
if frame_ok:
    print(f"Камера доступна, кадр получен: {frame.shape[1]}x{frame.shape[0]}")
    sys.exit(0)
print("Камера НЕ доступна.")
print("Откройте: Системные настройки -> Конфиденциальность и безопасность -> Камера")
print("и включите Terminal. Затем запустите этот файл снова.")
sys.exit(1)
PYEOF

CAM_OK=$?
if [ $CAM_OK -ne 0 ]; then
    echo
    echo "Камеры нет — но сайдкар всё равно запускаю."
    echo "Каналы зрения и взгляда будут пустыми, остальное работает:"
    echo "проверки окружения, интерфейс, телеметрия набора, отчёт."
    echo "Это штатная деградация, а не отказ."
    echo
fi

echo
echo "=== Останавливаю прежний сайдкар, если он был ==="
pkill -f "sidecar/main.py" 2>/dev/null
sleep 1

echo "=== Запускаю сайдкар на 127.0.0.1:8787 ==="
echo "Остановить: Ctrl+C в этом окне."
echo
# open(1) передаёт --args приложению Terminal, а не скрипту, поэтому индекс
# камеры спрашиваем здесь. Пустой ввод — индекс по умолчанию из конфига.
CAM_INDEX="${1:-}"
if [ -z "$CAM_INDEX" ]; then
    echo "Индекс камеры (Enter — по умолчанию; на macOS iPhone"
    echo "как Continuity Camera часто занимает 0, встроенная тогда 1):"
    read -r CAM_INDEX
fi
if [ -n "$CAM_INDEX" ]; then
    echo "Камера: индекс $CAM_INDEX"
    exec "$PY" sidecar/main.py --host 127.0.0.1 --port 8787 --camera "$CAM_INDEX"
fi
exec "$PY" sidecar/main.py --host 127.0.0.1 --port 8787

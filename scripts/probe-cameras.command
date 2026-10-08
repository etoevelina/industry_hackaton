#!/bin/bash
# Какие камеры видит система и что отдаёт каждый индекс OpenCV.
#
# Зачем: на macOS iPhone подключается как Continuity Camera и может занять
# индекс 0, оттеснив встроенную. OpenCV имён устройств не отдаёт — различать
# приходится по разрешению и порядку. Этот файл запускается из Terminal.app,
# потому что разрешение на камеру принадлежит приложению-владельцу процесса.
#
# Кадры НЕ сохраняются: берётся только размер.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

echo "=== Камеры по данным системы ==="
system_profiler SPCameraDataType 2>/dev/null | sed -n '1,40p'
echo
echo "=== Что отдаёт каждый индекс OpenCV ==="

.venv/bin/python - <<'PYEOF' 2>&1 | grep -v "^OpenCV:\|^\[ WARN"
import cv2

print(f"{'индекс':<8}{'разрешение':<16}{'кадр':<8}предположительно")
print("-" * 60)
found = 0
for i in range(5):
    cap = cv2.VideoCapture(i)
    if not cap.isOpened():
        cap.release()
        continue
    ok, frame = cap.read()
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if ok and frame is not None:
        w, h = frame.shape[1], frame.shape[0]
    # Continuity Camera обычно отдаёт вертикальный или очень широкий кадр
    # высокого разрешения; встроенная FaceTime HD — 1280x720.
    if h > w:
        guess = "iPhone (Continuity), кадр вертикальный"
    elif w >= 1900:
        guess = "iPhone (Continuity) либо внешняя камера"
    elif (w, h) == (1280, 720):
        guess = "встроенная FaceTime HD"
    else:
        guess = "неизвестно"
    found += 1
    print(f"{i:<8}{f'{w}x{h}':<16}{'да' if ok else 'нет':<8}{guess}")

if not found:
    print("Ни один индекс не открылся.")
    print("Системные настройки -> Конфиденциальность и безопасность -> Камера -> включить Terminal.")
PYEOF

echo
echo "Чтобы запустить сайдкар на нужной камере:"
echo "  .venv/bin/python sidecar/main.py --camera N"
echo "или через запускалку:"
echo "  scripts/run-sidecar-terminal.command N"
echo
echo "Совет: чтобы iPhone не перехватывал камеру, на телефоне"
echo "Настройки -> Основные -> AirPlay и Continuity -> Камера Continuity — выключить."
echo
echo "Нажмите Enter, чтобы закрыть."
read -r _

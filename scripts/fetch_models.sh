#!/usr/bin/env bash
#
# Подготовка весов детектора объектов: models/yolov8n.onnx (opset 12, imgsz 640).
#
# Запускать ОДИН РАЗ при подготовке стенда, до экзамена. В рантайме система в сеть
# не ходит — это требование кейса. Скрипт качает yolov8n.pt (~6 МБ) и экспортирует
# его в ONNX, дальше сайдкар работает только с локальным файлом.
#
# Использование:
#   bash scripts/fetch_models.sh            # обычный запуск
#   FORCE=1 bash scripts/fetch_models.sh    # перезаписать существующий onnx
#   PYTHON=python3.12 bash scripts/fetch_models.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
MODELS_DIR="${ROOT_DIR}/models"
PT_PATH="${MODELS_DIR}/yolov8n.pt"
ONNX_PATH="${MODELS_DIR}/yolov8n.onnx"
IMGSZ="${IMGSZ:-640}"
OPSET="${OPSET:-12}"
PYTHON_BIN="${PYTHON:-python3}"
FORCE="${FORCE:-0}"

say()  { printf '%s\n' "$*"; }
fail() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

say "=== Подготовка моделей для детектора объектов ==="
say "Каталог моделей: ${MODELS_DIR}"

mkdir -p "${MODELS_DIR}"

# 1. Модель уже на месте — ничего не делаем.
if [ -f "${ONNX_PATH}" ] && [ "${FORCE}" != "1" ]; then
    SIZE="$(wc -c < "${ONNX_PATH}" | tr -d ' ')"
    if [ "${SIZE}" -gt 1000000 ]; then
        say "Модель уже есть: ${ONNX_PATH} (${SIZE} байт). Ничего делать не нужно."
        say "Чтобы пересоздать её принудительно: FORCE=1 bash scripts/fetch_models.sh"
        exit 0
    fi
    say "Файл ${ONNX_PATH} подозрительно мал (${SIZE} байт) — пересоздаю."
    rm -f "${ONNX_PATH}"
fi

# 2. Проверяем интерпретатор и зависимости.
command -v "${PYTHON_BIN}" >/dev/null 2>&1 \
    || fail "не найден интерпретатор '${PYTHON_BIN}'. Укажите свой: PYTHON=/path/to/python3 bash scripts/fetch_models.sh"

say "Python: $("${PYTHON_BIN}" -V 2>&1)"

if ! "${PYTHON_BIN}" -c "import ultralytics" >/dev/null 2>&1; then
    say "Пакет ultralytics не установлен — без него нельзя экспортировать модель."
    say "Установите его и повторите запуск:"
    say "    ${PYTHON_BIN} -m pip install ultralytics onnx"
    fail "нет зависимости ultralytics"
fi

if ! "${PYTHON_BIN}" -c "import onnx" >/dev/null 2>&1; then
    say "Пакет onnx не установлен — экспорт в ONNX без него не работает."
    say "Установите его и повторите запуск:"
    say "    ${PYTHON_BIN} -m pip install onnx"
    fail "нет зависимости onnx"
fi

# 3. Экспорт. ultralytics сам докачает yolov8n.pt, если его нет (нужен интернет).
say "Экспортирую yolov8n.pt -> ONNX (opset ${OPSET}, imgsz ${IMGSZ})."
say "Если весов ещё нет, они будут скачаны (~6 МБ). Это единственный шаг, которому нужна сеть."

PT_PATH="${PT_PATH}" ONNX_PATH="${ONNX_PATH}" IMGSZ="${IMGSZ}" OPSET="${OPSET}" \
"${PYTHON_BIN}" - <<'PYCODE'
import os
import shutil
import sys
from pathlib import Path

pt_path = Path(os.environ["PT_PATH"])
onnx_path = Path(os.environ["ONNX_PATH"])
imgsz = int(os.environ["IMGSZ"])
opset = int(os.environ["OPSET"])

pt_path.parent.mkdir(parents=True, exist_ok=True)

try:
    from ultralytics import YOLO
except Exception as exc:  # на всякий случай, выше уже проверяли
    print(f"не удалось импортировать ultralytics: {exc}", file=sys.stderr)
    sys.exit(2)

try:
    # Путь передаём целиком, чтобы веса легли в models/, а не в текущий каталог.
    model = YOLO(str(pt_path))
except Exception as exc:
    print(f"не удалось получить веса yolov8n.pt: {exc}", file=sys.stderr)
    print("Проверьте доступ в интернет или положите yolov8n.pt в models/ вручную.", file=sys.stderr)
    sys.exit(3)

try:
    exported = model.export(format="onnx", opset=opset, imgsz=imgsz, dynamic=False, simplify=False)
except Exception as exc:
    print(f"экспорт в ONNX не удался: {exc}", file=sys.stderr)
    sys.exit(4)

src = Path(str(exported)) if exported else pt_path.with_suffix(".onnx")
if not src.is_file():
    print(f"ожидал файл {src}, но его нет", file=sys.stderr)
    sys.exit(5)

if src.resolve() != onnx_path.resolve():
    shutil.move(str(src), str(onnx_path))

print(f"готово: {onnx_path} ({onnx_path.stat().st_size} байт)")
PYCODE

# 4. Проверяем результат.
[ -f "${ONNX_PATH}" ] || fail "файл ${ONNX_PATH} так и не появился"
SIZE="$(wc -c < "${ONNX_PATH}" | tr -d ' ')"
[ "${SIZE}" -gt 1000000 ] || fail "файл ${ONNX_PATH} слишком мал (${SIZE} байт), экспорт сломан"
say "Модель готова: ${ONNX_PATH} (${SIZE} байт)."

# 5. Необязательная проверка, что onnxruntime реально её открывает.
if "${PYTHON_BIN}" -c "import onnxruntime" >/dev/null 2>&1; then
    say "Проверяю модель через onnxruntime."
    ONNX_PATH="${ONNX_PATH}" "${PYTHON_BIN}" - <<'PYCODE'
import os
import onnxruntime as ort

path = os.environ["ONNX_PATH"]
sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
inp = sess.get_inputs()[0]
out = sess.get_outputs()[0]
print(f"вход: {inp.name} {inp.shape}")
print(f"выход: {out.name} {out.shape}")
PYCODE
    say "onnxruntime открывает модель без ошибок."
else
    say "Пакет onnxruntime не установлен — пропускаю проверку загрузки."
    say "Для работы детектора его нужно поставить: ${PYTHON_BIN} -m pip install onnxruntime"
fi

say "=== Готово. Детектор объектов будет использовать models/yolov8n.onnx ==="

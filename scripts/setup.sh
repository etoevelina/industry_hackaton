#!/usr/bin/env bash
#
# Подготовка стенда: .venv, зависимости сайдкара, зависимости оболочки, самопроверка.
#
# Что делает:
#   1. проверяет Python (нужен >= 3.10, целевой 3.12) и Node (нужен >= 20);
#   2. создаёт .venv в корне репозитория (или переиспользует существующий);
#   3. ставит секцию CORE из sidecar/requirements.txt — без неё сайдкар не поднимется,
#      плюс секцию REPORT (jinja2 + cryptography: отчёт и подпись, колёса лёгкие);
#   4. по флагам ставит опциональные секции VISION / IDENTITY / AUDIO;
#   5. ставит npm-зависимости оболочки (electron, ws);
#   6. показывает таблицу каналов: что реально доступно, что выключится в рантайме.
#
# Секции разбираются из sidecar/requirements.txt — пины живут только там, здесь их нет.
#
# Использование:
#   bash scripts/setup.sh                 # CORE + REPORT + npm  (минимум для запуска)
#   bash scripts/setup.sh --vision        # + mediapipe, ultralytics, onnxruntime
#   bash scripts/setup.sh --audio         # + sounddevice, webrtcvad, librosa
#   bash scripts/setup.sh --identity      # + insightface (нужен компилятор, долго)
#   bash scripts/setup.sh --all           # всё из requirements.txt целиком
#   bash scripts/setup.sh --skip-npm      # только Python-часть
#   PYTHON=python3.12 bash scripts/setup.sh
#
# Сеть нужна только здесь, на этапе подготовки. В рантайме система наружу не ходит.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQ_FILE="${ROOT_DIR}/sidecar/requirements.txt"
VENV_DIR="${ROOT_DIR}/.venv"
VENV_PY="${VENV_DIR}/bin/python3"
PYTHON_BIN="${PYTHON:-python3}"

WANT_VISION=0
WANT_IDENTITY=0
WANT_AUDIO=0
WANT_ALL=0
SKIP_NPM=0

TMP_DIR=""

say()  { printf '%s\n' "$*"; }
head1() { printf '\n=== %s ===\n' "$*"; }
warn() { printf 'ВНИМАНИЕ: %s\n' "$*" >&2; }
fail() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

cleanup() {
    [ -n "${TMP_DIR}" ] && [ -d "${TMP_DIR}" ] && rm -rf "${TMP_DIR}"
    return 0
}
trap cleanup EXIT

usage() {
    sed -n '3,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# --------------------------------------------------------------------------- аргументы
while [ $# -gt 0 ]; do
    case "$1" in
        --vision)    WANT_VISION=1 ;;
        --identity)  WANT_IDENTITY=1 ;;
        --audio)     WANT_AUDIO=1 ;;
        --all)       WANT_ALL=1 ;;
        --skip-npm)  SKIP_NPM=1 ;;
        -h|--help)   usage; exit 0 ;;
        *)           fail "неизвестный аргумент '$1'. Справка: bash scripts/setup.sh --help" ;;
    esac
    shift
done

[ -f "${REQ_FILE}" ] || fail "не найден ${REQ_FILE} — запускайте скрипт из репозитория проекта"

# --------------------------------------------------------------------------- Python
head1 "Проверка Python"

command -v "${PYTHON_BIN}" >/dev/null 2>&1 \
    || fail "не найден интерпретатор '${PYTHON_BIN}'. Укажите свой: PYTHON=/путь/к/python3 bash scripts/setup.sh"

PY_VERSION="$("${PYTHON_BIN}" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
say "Интерпретатор: ${PYTHON_BIN} (${PY_VERSION})"

"${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' \
    || fail "нужен Python >= 3.10 (целевая версия проекта — 3.12), найден ${PY_VERSION}"

"${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' \
    || warn "проект собирался на Python 3.12; у вас ${PY_VERSION} — колёса mediapipe/onnxruntime могут не найтись"

say "Платформа: $("${PYTHON_BIN}" -c 'import platform; print(platform.system(), platform.machine())')"

# --------------------------------------------------------------------------- venv
head1 "Виртуальное окружение"

if [ -x "${VENV_PY}" ]; then
    say "Окружение уже есть: ${VENV_DIR} ($("${VENV_PY}" -V 2>&1))"
else
    say "Создаю окружение: ${VENV_DIR}"
    "${PYTHON_BIN}" -m venv "${VENV_DIR}" \
        || fail "не удалось создать .venv (на некоторых системах нужен пакет python3-venv)"
    say "Готово."
fi

[ -x "${VENV_PY}" ] || fail "в ${VENV_DIR} нет исполняемого python3 — удалите каталог и повторите"

say "Обновляю pip и wheel."
"${VENV_PY}" -m pip install --upgrade --quiet pip wheel \
    || warn "обновить pip не удалось — продолжаю с тем, что есть"

# --------------------------------------------------------------------------- секции
# Секция requirements.txt — строки между заголовком '# ИМЯ — ...' и следующим заголовком.
section_to_file() {
    local name="$1" out="$2"
    awk -v want="${name}" '
        /^# (CORE|VISION|IDENTITY|AUDIO|REPORT) / { cur = $2; next }
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }
        cur == want {
            line = $0
            sub(/[[:space:]]*#.*$/, "", line)   # инлайновый комментарий
            if (line != "") print line
        }
    ' "${REQ_FILE}" > "${out}"
    [ -s "${out}" ]
}

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/proctor-setup.XXXXXX")"

install_section() {
    local name="$1" why="$2"
    local req="${TMP_DIR}/${name}.txt"
    if ! section_to_file "${name}" "${req}"; then
        warn "секция ${name} не найдена в requirements.txt — пропускаю"
        return 0
    fi
    say ""
    say "--- ${name}: ${why}"
    sed 's/^/      /' "${req}"
    if "${VENV_PY}" -m pip install --upgrade -r "${req}"; then
        say "Секция ${name} установлена."
        return 0
    fi
    if [ "${name}" = "CORE" ]; then
        fail "секция CORE не установилась — без неё сайдкар не запустится. Проверьте доступ в интернет."
    fi
    warn "секция ${name} не установилась. Канал просто выключится в рантайме — система не упадёт."
    return 0
}

head1 "Зависимости сайдкара"

install_section CORE "обязательная: websockets, numpy, opencv, psutil"
install_section REPORT "HTML-отчёт и подпись Ed25519"

if [ "${WANT_ALL}" = "1" ]; then
    install_section VISION "взгляд и объекты: mediapipe, ultralytics, onnxruntime"
    install_section IDENTITY "верификация личности: insightface"
    install_section AUDIO "аудио-канал: sounddevice, webrtcvad, librosa"
else
    [ "${WANT_VISION}" = "1" ]   && install_section VISION "взгляд и объекты: mediapipe, ultralytics, onnxruntime"
    [ "${WANT_IDENTITY}" = "1" ] && install_section IDENTITY "верификация личности: insightface"
    [ "${WANT_AUDIO}" = "1" ]    && install_section AUDIO "аудио-канал: sounddevice, webrtcvad, librosa"
fi

# --------------------------------------------------------------------------- Node
head1 "Зависимости оболочки"

if [ "${SKIP_NPM}" = "1" ]; then
    say "Пропущено по флагу --skip-npm. Поставить позже: npm install"
elif ! command -v node >/dev/null 2>&1; then
    warn "node не найден. Оболочка не запустится; сайдкар работать будет."
    say "Поставьте Node 20: brew install node@20"
elif ! command -v npm >/dev/null 2>&1; then
    warn "npm не найден, хотя node есть — проверьте установку Node."
else
    NODE_VERSION="$(node -v)"
    say "Node: ${NODE_VERSION}, npm: $(npm -v)"
    NODE_MAJOR="$(printf '%s' "${NODE_VERSION}" | sed 's/^v//' | cut -d. -f1)"
    if [ "${NODE_MAJOR}" -lt 20 ] 2>/dev/null; then
        warn "нужен Node >= 20, у вас ${NODE_VERSION}. Electron 33 на старом Node не соберётся."
    fi
    say "Ставлю npm-зависимости (electron ~100 МБ, первый раз это долго)."
    if (cd "${ROOT_DIR}" && npm install); then
        say "npm-зависимости установлены."
    else
        warn "npm install не прошёл. Оболочка не поднимется, сайдкар — да."
        say "Можно продолжить без оболочки: make run-mock поднимет только сайдкар с событиями."
    fi
fi

# --------------------------------------------------------------------------- самопроверка
head1 "Самопроверка"

say "Python-модули проекта:"
(cd "${ROOT_DIR}" && PYTHONPATH="${ROOT_DIR}/sidecar" "${VENV_PY}" - <<'PYCODE'
import importlib
import sys

# (модуль, подпись в выводе, обязателен ли)
targets = [
    ("protocol", "контракт событий", True),
    ("config", "конфигурация", True),
    ("session", "состояние сессии", True),
    ("capture", "захват камеры", True),
    ("main", "ядро сайдкара", True),
    ("detectors.objects", "детектор объектов", False),
    ("detectors.face_mesh", "лицо и взгляд", False),
    ("detectors.identity", "верификация личности", False),
    ("detectors.audio", "аудио-канал", False),
    ("engine.events", "движок событий", False),
    ("engine.risk", "risk-score", False),
    ("engine.fusion", "fusion", False),
    ("engine.calibration", "калибровка взгляда", False),
    ("env_checks", "проверки окружения", False),
    ("storage.db", "доказательная база", False),
    ("storage.report", "отчёт", False),
]

bad = 0
for name, label, required in targets:
    try:
        importlib.import_module(name)
    except Exception as exc:
        mark = "ОШИБКА " if required else "нет    "
        print(f"  {mark} {name:24s} {label}: {type(exc).__name__}: {exc}")
        if required:
            bad += 1
    else:
        print(f"  ок     {name:24s} {label}")

print()
print("Внешние библиотеки по каналам:")
channels = [
    ("транспорт",  ["websockets"],                      True),
    ("кадры",      ["cv2", "numpy"],                    True),
    ("окружение",  ["psutil"],                          True),
    ("взгляд",     ["mediapipe"],                       False),
    ("объекты",    ["onnxruntime"],                     False),
    ("личность",   ["insightface", "onnxruntime"],      False),
    ("аудио",      ["sounddevice", "webrtcvad"],        False),
    ("отчёт",      ["jinja2", "cryptography"],          False),
]
for label, mods, required in channels:
    missing = []
    for mod in mods:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(mod)
    if not missing:
        print(f"  доступен      {label}")
    elif required:
        print(f"  НЕ РАБОТАЕТ   {label}: нет {', '.join(missing)}")
        bad += 1
    else:
        print(f"  выключен      {label}: нет {', '.join(missing)}")

sys.exit(1 if bad else 0)
PYCODE
) || fail "самопроверка не прошла: обязательная часть не импортируется. Смотрите строки 'ОШИБКА' выше."

if [ -d "${ROOT_DIR}/node_modules" ] && command -v node >/dev/null 2>&1; then
    say ""
    say "JS-файлы оболочки:"
    for f in shell/main.js shell/lockdown.js shell/ipc.js shell/preload.js \
             shell/renderer/app.js shell/renderer/hud.js shell/renderer/exam.js \
             shell/renderer/calibration.js shell/renderer/telemetry.js; do
        if [ -f "${ROOT_DIR}/${f}" ]; then
            if (cd "${ROOT_DIR}" && node --check "${f}" 2>/dev/null); then
                say "  ок     ${f}"
            else
                say "  ОШИБКА ${f} — синтаксис"
            fi
        fi
    done
fi

if [ -f "${ROOT_DIR}/models/yolov8n.onnx" ]; then
    say ""
    say "Веса детектора объектов: models/yolov8n.onnx на месте."
else
    say ""
    say "Весов models/yolov8n.onnx нет — детектор объектов выключится."
    say "Подготовить один раз: make models   (или bash scripts/fetch_models.sh)"
fi

# --------------------------------------------------------------------------- итог
head1 "Готово"
say "Дальше:"
say "  make run-mock     — демо без камеры и моделей, события по scripts/demo_scenario.json"
say "  make run          — полный запуск: сайдкар + оболочка"
say "  make test         — быстрая проверка, что всё импортируется и парсится"
say ""
say "Опциональные секции ставятся отдельно:"
say "  bash scripts/setup.sh --vision     # взгляд, поза головы, детекция объектов"
say "  bash scripts/setup.sh --audio      # чужой голос, речь без движения губ"
say "  bash scripts/setup.sh --identity   # непрерывная сверка личности (нужен компилятор)"
say ""
say "Отсутствие любой опциональной секции не ломает систему: канал помечается"
say "недоступным в hello, HUD показывает это честно, остальное работает."

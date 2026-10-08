#!/usr/bin/env bash
#
# Параллельный запуск двух процессов системы: Python-сайдкар (CV/аудио) и Electron-оболочка.
#
# Почему скрипт, а не npm start: процессов два, и они должны умирать вместе.
# Ctrl+C гасит оба (trap + SIGTERM, через 6 с — SIGKILL), выход любого из них
# останавливает второй. Иначе после каждой репетиции в системе остаётся висеть
# сайдкар, порт 8787 занят, и следующий запуск молча не видит камеру.
#
# Использование:
#   bash scripts/run-dev.sh                 # сайдкар + оболочка, камера и модели как есть
#   bash scripts/run-dev.sh --mock          # демо без камеры: события из scripts/demo_scenario.json
#   bash scripts/run-dev.sh --headless      # реальный сайдкар, но без камеры (проверка протокола)
#   bash scripts/run-dev.sh --no-kiosk      # окно оболочки можно двигать (отладка)
#   bash scripts/run-dev.sh --allow-multi-display  # проектор расширением экрана: не блокировать тест
#   bash scripts/run-dev.sh --sidecar-only  # только сайдкар
#   bash scripts/run-dev.sh --shell-only    # только оболочка (подключится к уже запущенному)
#   bash scripts/run-dev.sh --port 8800     # другой порт WS для обоих процессов
#   bash scripts/run-dev.sh -- --no-yolo    # всё после '--' уходит сайдкару как есть
#   bash scripts/run-dev.sh --mock -- --speed 2 --no-loop   # сценарий вдвое быстрее, один раз
#
# Правила экзамена (какой LMS открывать и какие источники разрешены):
#   bash scripts/run-dev.sh --exam-profile exam-profile.json
#   bash scripts/run-dev.sh --exam-profile exam-profile.json --exam-profile-pubkey key.pub
#   bash scripts/run-dev.sh --exam-profile exam-profile.json --allow-search
#   Заготовку файла печатает ядро:
#     .venv/bin/python sidecar/main.py --exam-profile-example ksu > exam-profile.json
#   Правила ПРИНИМАЕТ ЯДРО: оно читает файл, считает хеш, кладёт его в
#   подписанную цепочку и присылает оболочке готовый действующий список.
#   Флагов --exam-url/--exam-origin/--exam-preset у оболочки больше нет: два
#   владельца одних правил давали два противоречащих документа об одной
#   сессии (подробно — в shell/main.js у CLI.examProfilePath).
#
# Переменные окружения:
#   PROCTOR_PYTHON  — интерпретатор сайдкара (по умолчанию .venv/bin/python3, затем python3)
#   PROCTOR_LOG     — уровень логов сайдкара (DEBUG/INFO/WARNING)
#
# Наружу в сеть ничего не идёт: оба процесса общаются только по loopback.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

WS_HOST="127.0.0.1"
WS_PORT="8787"
MOCK=0
HEADLESS=0
NO_KIOSK=0
ALLOW_MULTI_DISPLAY=0
SIDECAR_ONLY=0
SHELL_ONLY=0
PASSTHROUGH=0
EXTRA_SIDECAR=()

SIDECAR_PID=""
SHELL_PID=""
EXIT_CODE=0

say()  { printf '[run] %s\n' "$*"; }
warn() { printf '[run] ВНИМАНИЕ: %s\n' "$*" >&2; }
fail() { printf '[run] ОШИБКА: %s\n' "$*" >&2; exit 1; }

usage() { sed -n "3,39p" "${BASH_SOURCE[0]}" | sed "s|^# \{0,1\}||"; }

# --------------------------------------------------------------------------- аргументы
while [ $# -gt 0 ]; do
    if [ "${PASSTHROUGH}" = "1" ]; then
        EXTRA_SIDECAR+=("$1"); shift; continue
    fi
    case "$1" in
        --mock)          MOCK=1 ;;
        --headless)      HEADLESS=1 ;;
        --no-kiosk)      NO_KIOSK=1 ;;
        --allow-multi-display) ALLOW_MULTI_DISPLAY=1 ;;
        --sidecar-only)  SIDECAR_ONLY=1 ;;
        --shell-only)    SHELL_ONLY=1 ;;
        --port)          shift; [ $# -gt 0 ] || fail "--port без значения"; WS_PORT="$1" ;;
        --port=*)        WS_PORT="${1#--port=}" ;;
        --host)          shift; [ $# -gt 0 ] || fail "--host без значения"; WS_HOST="$1" ;;
        --host=*)        WS_HOST="${1#--host=}" ;;
        # --- правила экзамена: уходят ЯДРУ, не оболочке ---
        # Без этих веток единственный рабочий путь задать правила был
        # `run-dev.sh -- --exam-profile <файл>`, а про `--` на демо забывают.
        # Хуже: `run-dev.sh --exam-profile ...` падал с «неизвестный аргумент»,
        # и человек под часы шёл запускать оболочку напрямую — то есть ровно в
        # тот путь, где правила не попадают в цепочку.
        --exam-profile)  shift; [ $# -gt 0 ] || fail "--exam-profile без значения"
                         [ -f "$1" ] || fail "файл профиля не найден: '$1'. Заготовку печатает ядро: --exam-profile-example ksu"
                         EXTRA_SIDECAR+=(--exam-profile "$1") ;;
        --exam-profile=*) _v="${1#--exam-profile=}"
                         [ -f "${_v}" ] || fail "файл профиля не найден: '${_v}'"
                         EXTRA_SIDECAR+=(--exam-profile "${_v}") ;;
        --exam-profile-pubkey) shift; [ $# -gt 0 ] || fail "--exam-profile-pubkey без значения"
                         [ -f "$1" ] || fail "файл ключа не найден: '$1'"
                         EXTRA_SIDECAR+=(--exam-profile-pubkey "$1") ;;
        --exam-profile-pubkey=*) _v="${1#--exam-profile-pubkey=}"
                         [ -f "${_v}" ] || fail "файл ключа не найден: '${_v}'"
                         EXTRA_SIDECAR+=(--exam-profile-pubkey "${_v}") ;;
        # Поиск запрещён по умолчанию. Флаг — для экзаменов, где он разрешён
        # правилами; факт идёт в цепочку и в шапку отчёта.
        --allow-search)  EXTRA_SIDECAR+=(--allow-search) ;;
        # Флаги, которых больше нет. Отказ с объяснением, а не «неизвестный
        # аргумент»: человек под часы должен узнать, куда идти, а не что он
        # опечатался.
        --exam-url|--exam-url=*|--exam-origin|--exam-origin=*|--exam-preset|--exam-preset=*)
            fail "флаг '${1%%=*}' убран: правила экзамена задаёт ЯДРО файлом профиля.
    1) заготовка: .venv/bin/python sidecar/main.py --exam-profile-example ksu > exam-profile.json
    2) правка руками: адрес экзамена и КОНКРЕТНЫЕ источники, а не класс .edu.kz
    3) запуск:    bash scripts/run-dev.sh --exam-profile exam-profile.json
  Почему так: профиль, которого нет у ядра, не попадает в подписанную цепочку —
  HUD показывал бы «действовал профиль», а отчёт «правила не задавались»." ;;
        --)              PASSTHROUGH=1 ;;
        -h|--help)       usage; exit 0 ;;
        *)               fail "неизвестный аргумент '$1'. Справка: bash scripts/run-dev.sh --help" ;;
    esac
    shift
done

if [ "${SIDECAR_ONLY}" = "1" ] && [ "${SHELL_ONLY}" = "1" ]; then
    fail "--sidecar-only и --shell-only вместе смысла не имеют"
fi

# --------------------------------------------------------------------------- остановка
# Гасим по порядку: сначала оболочка (она снимает kiosk и отпускает хоткеи),
# потом сайдкар (он успевает закрыть сессию и собрать отчёт).
stop_proc() {
    local pid="$1" name="$2" i=0
    [ -n "${pid}" ] || return 0
    kill -0 "${pid}" 2>/dev/null || return 0
    say "останавливаю ${name} (pid ${pid})"
    kill -TERM "${pid}" 2>/dev/null || true
    while [ "${i}" -lt 24 ]; do
        kill -0 "${pid}" 2>/dev/null || return 0
        sleep 0.25
        i=$((i + 1))
    done
    warn "${name} не завершился за 6 с — SIGKILL"
    if command -v pkill >/dev/null 2>&1; then
        pkill -KILL -P "${pid}" 2>/dev/null || true
    fi
    kill -KILL "${pid}" 2>/dev/null || true
    return 0
}

CLEANED=0
cleanup() {
    [ "${CLEANED}" = "1" ] && return 0
    CLEANED=1
    trap '' INT TERM EXIT
    if [ -n "${SHELL_PID}" ] || [ -n "${SIDECAR_PID}" ]; then
        stop_proc "${SHELL_PID}" "оболочку"
        stop_proc "${SIDECAR_PID}" "сайдкар"
        say "процессы остановлены"
    fi
    return 0
}

on_signal() {
    say "сигнал остановки — гашу оба процесса"
    cleanup
    exit 130
}

trap on_signal INT TERM
trap cleanup EXIT

# --------------------------------------------------------------------------- интерпретатор
pick_python() {
    if [ -n "${PROCTOR_PYTHON:-}" ]; then printf '%s' "${PROCTOR_PYTHON}"; return 0; fi
    for candidate in "${ROOT_DIR}/.venv/bin/python3" "${ROOT_DIR}/.venv/bin/python"; do
        [ -x "${candidate}" ] && { printf '%s' "${candidate}"; return 0; }
    done
    command -v python3 >/dev/null 2>&1 && { printf '%s' "python3"; return 0; }
    return 1
}

PYTHON_BIN="$(pick_python)" || fail "не найден python3. Соберите окружение: make setup"

# --------------------------------------------------------------------------- точка входа сайдкара
# --mock: сначала ищем отдельный мок-сайдкар (он читает сценарий из JSON),
# иначе используем встроенный режим --mock ядра. Оба говорят по одному протоколу,
# оболочка разницы не видит — это и есть смысл контракта.
SIDECAR_ARGS=()
SCENARIO="${ROOT_DIR}/scripts/demo_scenario.json"
MOCK_ENTRY=""
for candidate in "${ROOT_DIR}/sidecar/tools/mock_sidecar.py" "${ROOT_DIR}/sidecar/mock_sidecar.py"; do
    [ -f "${candidate}" ] && { MOCK_ENTRY="${candidate}"; break; }
done

if [ "${MOCK}" = "1" ]; then
    if [ -n "${MOCK_ENTRY}" ]; then
        SIDECAR_ENTRY="${MOCK_ENTRY}"
        SIDECAR_LABEL="mock-сайдкар (сценарий из scripts/demo_scenario.json)"
        [ -f "${SCENARIO}" ] && SIDECAR_ARGS+=(--scenario "${SCENARIO}")
        # По кругу — чтобы на репетиции и на сцене сценарий не кончился раньше речи.
        # Отменяется так: bash scripts/run-dev.sh --mock -- --no-loop
        SIDECAR_ARGS+=(--loop)
    else
        SIDECAR_ENTRY="${ROOT_DIR}/sidecar/main.py"
        SIDECAR_LABEL="ядро в режиме --mock --headless (встроенный сценарий)"
        SIDECAR_ARGS+=(--mock --headless)
        warn "mock_sidecar.py не найден — иду через встроенный --mock ядра."
        warn "scripts/demo_scenario.json при этом НЕ читается: сценарий зашит в sidecar/main.py."
    fi
else
    SIDECAR_ENTRY="${ROOT_DIR}/sidecar/main.py"
    SIDECAR_LABEL="сайдкар (камера и детекторы)"
    [ "${HEADLESS}" = "1" ] && SIDECAR_ARGS+=(--headless)
fi

# Проектор на сцене подключён расширением экрана: и оболочка, и проверки
# окружения должны знать, что второй экран сейчас разрешён.
if [ "${ALLOW_MULTI_DISPLAY}" = "1" ] && [ "${MOCK}" != "1" ]; then
    SIDECAR_ARGS+=(--allow-multi-display)
fi

[ -f "${SIDECAR_ENTRY}" ] || fail "не найдена точка входа сайдкара: ${SIDECAR_ENTRY}"

SIDECAR_ARGS+=(--host "${WS_HOST}" --port "${WS_PORT}")
[ -n "${PROCTOR_LOG:-}" ] && SIDECAR_ARGS+=(--log-level "${PROCTOR_LOG}")
if [ "${#EXTRA_SIDECAR[@]}" -gt 0 ]; then
    SIDECAR_ARGS+=("${EXTRA_SIDECAR[@]}")
fi

# --------------------------------------------------------------------------- бинарь electron
ELECTRON_BIN=""
if [ -x "${ROOT_DIR}/node_modules/.bin/electron" ]; then
    ELECTRON_BIN="${ROOT_DIR}/node_modules/.bin/electron"
fi

if [ "${SIDECAR_ONLY}" != "1" ] && [ -z "${ELECTRON_BIN}" ]; then
    warn "electron не установлен (нет node_modules/.bin/electron) — запускаю только сайдкар."
    warn "Поставить: npm install   (или make setup)"
    SIDECAR_ONLY=1
fi

# Оба процесса отключены — запускать нечего. Без этой проверки скрипт уходил бы
# в бесконечное ожидание несуществующих процессов.
if [ "${SHELL_ONLY}" = "1" ] && [ "${SIDECAR_ONLY}" = "1" ]; then
    fail "запускать нечего: сайдкар выключен флагом --shell-only, а оболочка недоступна (нет electron).
      Поставьте зависимости оболочки: npm install"
fi

# --------------------------------------------------------------------------- порт
# Занятость порта проверяем ПОПЫТКОЙ ЗАНЯТЬ его, а не подключением.
# Подключаться нельзя: websockets-сервер на оборванном рукопожатии печатает
# простыню трейсбека, и на демо это читается как «система сломалась».
port_busy() {
    "${PYTHON_BIN}" - "${WS_HOST}" "${WS_PORT}" <<'PYCODE' >/dev/null 2>&1
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind((host, port))
except OSError:
    sys.exit(0)          # занять не смогли -> порт занят, кто-то слушает
finally:
    sock.close()
sys.exit(1)              # заняли свободно -> никто не слушает
PYCODE
}

if [ "${SHELL_ONLY}" != "1" ] && port_busy; then
    fail "порт ${WS_HOST}:${WS_PORT} уже занят — вероятно, сайдкар с прошлого прогона жив.
      Проверить: lsof -iTCP:${WS_PORT} -sTCP:LISTEN
      Другой порт: bash scripts/run-dev.sh --port 8800"
fi

# --------------------------------------------------------------------------- запуск
say "корень: ${ROOT_DIR}"
say "python: ${PYTHON_BIN}"
say "канал:  ws://${WS_HOST}:${WS_PORT}"

if [ "${SHELL_ONLY}" != "1" ]; then
    say "старт: ${SIDECAR_LABEL}"
    (
        cd "${ROOT_DIR}"
        PYTHONUNBUFFERED=1 exec "${PYTHON_BIN}" "${SIDECAR_ENTRY}" "${SIDECAR_ARGS[@]}"
    ) &
    SIDECAR_PID=$!

    # Ждём, пока поднимется WS-сервер: оболочка умеет реконнект с бэкоффом,
    # но на демо неприятно видеть «ядро недоступно» первые две секунды.
    # Попутно это ловит падение на старте (нет websockets, занят порт).
    waited=0
    ready=0
    while [ "${waited}" -lt 30 ]; do
        if ! kill -0 "${SIDECAR_PID}" 2>/dev/null; then
            wait "${SIDECAR_PID}" 2>/dev/null || EXIT_CODE=$?
            fail "сайдкар упал на старте (код ${EXIT_CODE}). Смотрите его лог выше."
        fi
        if port_busy; then ready=1; break; fi
        sleep 0.5
        waited=$((waited + 1))
    done
    if [ "${ready}" = "1" ]; then
        say "сайдкар слушает ws://${WS_HOST}:${WS_PORT}"
    else
        warn "сайдкар за 15 с не открыл порт — оболочка подключится сама, когда он будет готов"
    fi
fi

if [ "${SIDECAR_ONLY}" != "1" ]; then
    SHELL_ARGS=(. --ws-host "${WS_HOST}" --ws-port "${WS_PORT}")
    [ "${NO_KIOSK}" = "1" ] && SHELL_ARGS+=(--no-kiosk)
    [ "${ALLOW_MULTI_DISPLAY}" = "1" ] && SHELL_ARGS+=(--allow-multi-display)
    say "старт: оболочка Electron"
    say "аварийный выход из kiosk-окна: Cmd+Alt+Shift+Q"
    (
        cd "${ROOT_DIR}"
        ELECTRON_ENABLE_LOGGING=1 exec "${ELECTRON_BIN}" "${SHELL_ARGS[@]}"
    ) &
    SHELL_PID=$!
fi

say "работаю. Ctrl+C — остановить оба процесса."

# --------------------------------------------------------------------------- присмотр
# `wait -n` в bash 3.2 (системный bash на macOS) отсутствует, поэтому опрашиваем сами.
while :; do
    if [ -z "${SIDECAR_PID}" ] && [ -z "${SHELL_PID}" ]; then
        warn "ни один процесс не запущен — выходим"
        break
    fi
    if [ -n "${SIDECAR_PID}" ] && ! kill -0 "${SIDECAR_PID}" 2>/dev/null; then
        EXIT_CODE=0
        wait "${SIDECAR_PID}" 2>/dev/null || EXIT_CODE=$?
        say "сайдкар завершился (код ${EXIT_CODE})"
        SIDECAR_PID=""
        break
    fi
    if [ -n "${SHELL_PID}" ] && ! kill -0 "${SHELL_PID}" 2>/dev/null; then
        EXIT_CODE=0
        wait "${SHELL_PID}" 2>/dev/null || EXIT_CODE=$?
        say "оболочка завершилась (код ${EXIT_CODE})"
        SHELL_PID=""
        break
    fi
    sleep 0.5
done

exit "${EXIT_CODE}"

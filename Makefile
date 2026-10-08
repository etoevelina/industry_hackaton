# Точки входа проекта. Всё, что нужно команде и жюри, запускается отсюда.
#
#   make setup      — собрать окружение (один раз на машине)
#   make run        — запустить систему: сайдкар + оболочка
#   make run-mock   — демо без камеры и моделей (план Б на сцене)
#   make models     — подготовить веса детектора объектов (нужен интернет, однократно)
#   make report     — собрать HTML-отчёт по последней сессии
#   make package    — собрать пакет доказательств <сессия>.proctor.zip
#   make verify     — проверить комплект: комплектность, целостность, авторство
#   make test       — быстрая проверка: всё импортируется, парсится, сценарий валиден
#   make clean      — убрать мусор сборки (доказательства НЕ трогает)
#
# Дополнительные аргументы прокидываются через ARGS:
#   make run ARGS="--no-kiosk --port 8800"
#   make setup ARGS="--vision --audio"
#
# ПЕРЕДАЧА ДОКАЗАТЕЛЬСТВ. Каталог сессий задаёт проктор, и все цели смотрят
# туда же, куда писал сайдкар:
#   PROCTOR_SESSIONS_DIR=/Volumes/EXAM make verify
#   make run ARGS="--sessions-dir /Volumes/EXAM --signing-key /Volumes/EXAM/uni.key"
# Ключ учреждения (PROCTOR_SIGNING_KEY или --signing-key) превращает подпись из
# доказательства целостности в доказательство авторства: приватный ключ вуза на
# машине студента не хранится. Без него подпись доказывает только, что файл не
# менялся по дороге, и verify это прямо печатает.

SHELL := /bin/bash
.DEFAULT_GOAL := help

ROOT := $(CURDIR)
ARGS ?=

# Каталог сессий: переменная проктора сильнее значения по умолчанию. Та же
# переменная, что читает сайдкар, — иначе make report собирал бы отчёт по
# пустому локальному каталогу, пока доказательства лежат на сетевом диске.
SESSIONS := $(if $(PROCTOR_SESSIONS_DIR),$(PROCTOR_SESSIONS_DIR),$(ROOT)/sessions)

# Интерпретатор: .venv, затем системный. Переопределяется через PROCTOR_PYTHON.
PY := $(shell if [ -n "$$PROCTOR_PYTHON" ]; then echo "$$PROCTOR_PYTHON"; \
	elif [ -x "$(CURDIR)/.venv/bin/python3" ]; then echo "$(CURDIR)/.venv/bin/python3"; \
	else echo python3; fi)

JS_FILES := shell/main.js shell/state.js shell/lockdown.js shell/ipc.js \
	shell/preload.js \
	shell/renderer/app.js shell/renderer/hud.js shell/renderer/exam.js \
	shell/renderer/calibration.js shell/renderer/telemetry.js \
	tools/discover/main.js tools/discover/classify.js \
	tools/discover/check.js tools/discover/strip-preload.js

SH_FILES := scripts/setup.sh scripts/run-dev.sh scripts/fetch_models.sh \
	scripts/run-safe.command scripts/discover-origins.command

.PHONY: help setup run run-mock discover models report package verify test clean clean-sessions

# ---------------------------------------------------------------------------
help:
	@echo "Локальная система прокторинга — доступные цели:"
	@echo ""
	@echo "  make setup      собрать .venv, поставить зависимости, проверить версии"
	@echo "                  ARGS=\"--vision --audio --identity --all --skip-npm\""
	@echo "  make run        запустить сайдкар и оболочку, Ctrl+C гасит оба"
	@echo "                  ARGS=\"--no-kiosk --headless --port 8800\""
	@echo "  make run-mock   демо без камеры: события по scripts/demo_scenario.json"
	@echo "  make discover   разведка источников LMS: открыть систему вуза и собрать"
	@echo "                  ЧЕРНОВИК белого списка по фактическим запросам."
	@echo "                  URL=https://lms.вуз.edu.kz/login/index.php"
	@echo "                  ARGS=\"--out /путь/черновик.json --seconds 40\""
	@echo "                  Это настройка, а не экзамен: kiosk и блокировки не"
	@echo "                  включаются, фильтр не блокирует, профиль НЕ применяется"
	@echo "  make models     подготовить models/yolov8n.onnx (однократно, нужен интернет)"
	@echo "  make report     собрать отчёт по последней сессии"
	@echo "  make package    собрать пакет доказательств <сессия>.proctor.zip"
	@echo "                  ARGS=\"--signing-key /путь/к/ключу/вуза\""
	@echo "  make verify     проверить комплект: комплектность, целостность, авторство"
	@echo "  make test       проверить импорты, синтаксис JS и валидность сценария демо"
	@echo "  make clean      убрать __pycache__ и артефакты сборки"
	@echo "  make clean-sessions  удалить записанные сессии (спросит подтверждение)"
	@echo ""
	@echo "  python:         $(PY)"
	@echo "  каталог сессий: $(SESSIONS)"
	@echo "                  задаётся PROCTOR_SESSIONS_DIR или --sessions-dir"
	@echo "  ключ подписи:   $(if $(PROCTOR_SIGNING_KEY),$(PROCTOR_SIGNING_KEY),не задан — подпись докажет только целостность)"

# ---------------------------------------------------------------------------
setup:
	@bash scripts/setup.sh $(ARGS)

run:
	@bash scripts/run-dev.sh $(ARGS)

run-mock:
	@bash scripts/run-dev.sh --mock $(ARGS)

# РАЗВЕДКА ИСТОЧНИКОВ LMS. Отдельная точка входа и отдельный процесс: оболочка
# прокторинга про неё не знает и знать не должна. Правила разбора записей при
# этом общие — tools/discover переиспользует shell/state.js, то есть тот самый
# слой, который работает на экзамене.
#
# URL обязателен и намеренно не имеет значения по умолчанию: «разведать LMS,
# которую мы сами себе подставили» — бессмысленная операция.
discover:
	@if [ -z "$(URL)" ]; then \
		echo "Нужен адрес страницы входа LMS:"; \
		echo "  make discover URL=https://lms.вуз.edu.kz/login/index.php"; \
		echo ""; \
		echo "Инструмент НАСТРОЙКИ, не экзамен: ничего не блокируется,"; \
		echo "собранный профиль НЕ применяется — это черновик для человека."; \
		exit 2; \
	fi
	@bash scripts/discover-origins.command "$(URL)" $(ARGS)

models:
	@bash scripts/fetch_models.sh

# ---------------------------------------------------------------------------
report:
	@PROCTOR_ROOT="$(ROOT)" PROCTOR_SESSIONS="$(SESSIONS)" $(PY) -c "$$REPORT_PY"

# Пакет доказательств — то, что проктор физически забирает: один файл
# <сессия>.proctor.zip рядом с каталогом сессии. Внутри отчёт, подпись,
# публичный ключ, журнал, кадры и manifest.json со списком sha256.
package:
	@PROCTOR_ROOT="$(ROOT)" PROCTOR_SESSIONS="$(SESSIONS)" \
		PROCTOR_ARGS="$(ARGS)" $(PY) -c "$$PACKAGE_PY"

# Проверку делает scripts/verify_report.py — инструмент экзаменатора. Он
# принимает и пакет, и каталог сессии, и один report.html, и печатает ДВА
# раздельных вердикта: целостность и авторство. Пакету отдаём предпочтение:
# только по нему проверяется комплектность (manifest + sha256).
# Врезка VERIFY_JSONL_PY нужна для деградированного режима, когда storage/db.py
# недоступен и доказательства пишутся в events.jsonl: такую базу verify_report
# не читает, а проверять её всё равно надо.
verify:
	@sess="$$(PROCTOR_ROOT="$(ROOT)" PROCTOR_SESSIONS="$(SESSIONS)" $(PY) -c "$$LATEST_PY")"; \
	if [ -z "$$sess" ]; then \
		echo "В $(SESSIONS) нет ни одной сессии. Сначала проведите прогон: make run"; \
		echo "Если доказательства писались в другой каталог, укажите его:"; \
		echo "  PROCTOR_SESSIONS_DIR=/путь make verify"; \
		exit 1; \
	fi; \
	echo "Сессия: $$sess"; \
	pkg="$$sess.proctor.zip"; \
	if [ -f "$$pkg" ] && [ -f scripts/verify_report.py ]; then \
		echo "Пакет:  $$pkg"; \
		PYTHONPATH="$(ROOT)/sidecar" $(PY) scripts/verify_report.py "$$pkg" $(ARGS); \
	elif [ -f "$$sess/evidence.sqlite" ] && [ -f scripts/verify_report.py ]; then \
		echo "Пакета рядом нет, проверяю каталог сессии (комплектность не проверить)."; \
		echo "Собрать пакет: make package"; \
		PYTHONPATH="$(ROOT)/sidecar" $(PY) scripts/verify_report.py "$$sess" $(ARGS); \
	elif [ -f "$$sess/events.jsonl" ]; then \
		echo "SQLite-базы нет, проверяю журнал деградированного режима events.jsonl"; \
		PROCTOR_SESSION="$$sess" $(PY) -c "$$VERIFY_JSONL_PY"; \
	else \
		echo "В каталоге сессии нет ни evidence.sqlite, ни events.jsonl — проверять нечего."; \
		exit 1; \
	fi

test:
	@echo "=== Python: импорт модулей и валидность сценария ==="
	@PROCTOR_ROOT="$(ROOT)" PYTHONPATH="$(ROOT)/sidecar" $(PY) -c "$$TEST_PY"
	@echo ""
	@echo "=== Сценарий демо глазами мок-сайдкара ==="
	@if [ -f sidecar/tools/mock_sidecar.py ]; then \
		out="$$(PYTHONPATH="$(ROOT)/sidecar" $(PY) sidecar/tools/mock_sidecar.py \
			--scenario scripts/demo_scenario.json --list 2>/dev/null | head -2)"; \
		case "$$out" in \
			*demo_scenario.json*) printf '%s\n' "$$out" | sed 's/^/  /'; echo "  ок     мок принимает наш сценарий";; \
			*) echo "  ОШИБКА мок не принял scripts/demo_scenario.json и взял встроенный сценарий"; exit 1;; \
		esac; \
	else \
		echo "  нет    sidecar/tools/mock_sidecar.py — make run-mock пойдёт через встроенный сценарий ядра"; \
	fi
	@echo ""
	@echo "=== JS: синтаксис ==="
	@if command -v node >/dev/null 2>&1; then \
		rc=0; \
		for f in $(JS_FILES); do \
			if [ -f "$$f" ]; then \
				if node --check "$$f" 2>/dev/null; then echo "  ок     $$f"; \
				else echo "  ОШИБКА $$f"; node --check "$$f" || true; rc=1; fi; \
			else echo "  нет    $$f"; fi; \
		done; \
		exit $$rc; \
	else \
		echo "  node не установлен — проверка пропущена"; \
	fi
	@echo ""
	@echo "=== JS: слой правил разведки источников (tools/discover) ==="
	@if command -v node >/dev/null 2>&1; then \
		node tools/discover/check.js | tail -3; \
	else \
		echo "  node не установлен — проверка пропущена"; \
	fi
	@echo ""
	@echo "=== Shell: синтаксис ==="
	@rc=0; \
	for f in $(SH_FILES); do \
		if [ -f "$$f" ]; then \
			if bash -n "$$f" 2>/dev/null; then echo "  ок     $$f"; \
			else echo "  ОШИБКА $$f"; bash -n "$$f" || true; rc=1; fi; \
		else echo "  нет    $$f"; fi; \
	done; \
	exit $$rc

# ---------------------------------------------------------------------------
clean:
	@echo "Убираю мусор сборки. Каталоги sessions/ и evidence/ не трогаю."
	@find . -type d -name '__pycache__' -not -path './node_modules/*' -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name '*.pyc' -not -path './node_modules/*' -delete 2>/dev/null || true
	@rm -rf .pytest_cache dist
	@echo "Готово. Чтобы удалить записанные сессии: make clean-sessions"

# Отдельная цель и явное подтверждение: в sessions/ лежит доказательная база,
# её нельзя удалять «попутно» вместе с кэшами.
clean-sessions:
	@echo "ВНИМАНИЕ: будут удалены все записанные сессии и доказательства:"
	@echo "  $(ROOT)/sessions/*"
	@echo "  $(ROOT)/evidence/*"
	@printf 'Удалить? Введите «да» для подтверждения: '; \
	read answer; \
	if [ "$$answer" = "да" ]; then \
		rm -rf sessions/* evidence/*; \
		echo "Удалено."; \
	else \
		echo "Отменено, ничего не удалено."; \
	fi

# ===========================================================================
# Python-врезки. Многострочный define + export — иначе на GNU Make 3.81
# (системный make на macOS) не получится передать скрипт целиком.
# ===========================================================================

# --------------------------------------------------------------- make report
define REPORT_PY
"""Собрать HTML-отчёт по последней сессии в sessions/."""
import os
import sys
from pathlib import Path

root = Path(os.environ.get("PROCTOR_ROOT", ".")).resolve()
sys.path.insert(0, str(root / "sidecar"))

base = Path(os.environ.get("PROCTOR_SESSIONS") or (root / "sessions"))
sessions = sorted((p for p in base.glob("*") if p.is_dir()),
                  key=lambda p: p.stat().st_mtime)
if not sessions:
    print(f"В {base} нет ни одной сессии. Сначала проведите прогон: make run")
    print("Если доказательства писались в другой каталог:")
    print("  PROCTOR_SESSIONS_DIR=/путь make report")
    raise SystemExit(1)

session_dir = sessions[-1]
print(f"Сессия: {session_dir}")

db = None
for name in ("evidence.sqlite", "evidence.db", "events.jsonl"):
    candidate = session_dir / name
    if candidate.is_file():
        db = candidate
        break
if db is None:
    print("В каталоге сессии нет доказательной базы (evidence.sqlite / events.jsonl).")
    raise SystemExit(1)
print(f"База:   {db.name}")

try:
    from storage import report as report_mod
except Exception as exc:
    print(f"Модуль sidecar/storage/report.py недоступен: {exc}")
    print("Отчёт собирается им. Поставьте зависимости секции REPORT:")
    print("  .venv/bin/python3 -m pip install jinja2 cryptography")
    raise SystemExit(1)

out = session_dir / "report.html"
# Ключ учреждения задаётся так же, как сайдкару. Без него метка подписи
# остаётся self_signed, и отчёт честно пишет, что авторство не доказано.
key = (os.environ.get("PROCTOR_SIGNING_KEY") or "").strip()
cfg = {"report": {"key_path": key, "authority": "institution" if key else "self_signed"}}
try:
    path = report_mod.build_report(str(session_dir), str(db), str(out), cfg)
except Exception as exc:
    print(f"Не удалось собрать отчёт: {type(exc).__name__}: {exc}")
    raise SystemExit(1)

path = Path(str(path) if path else out)
print(f"Отчёт готов: {path}")
print(f"Открыть:     open '{path}'")

# sign_report сам берёт keys/report_ed25519.key и кладёт рядом с отчётом
# <отчёт>.sig и <отчёт>.pub — публичный ключ едет вместе с отчётом,
# преподавателю не нужно ничего искать.
sign = getattr(report_mod, "sign_report", None)
if sign is None:
    print("Подпись не поставлена: в storage/report.py нет sign_report")
else:
    try:
        sig = sign(str(path), key or None,
                   "institution" if key else "self_signed")
    except Exception as exc:
        print(f"Подпись не поставлена ({type(exc).__name__}: {exc}).")
        print("Нужен ключ keys/report_ed25519.key и пакет cryptography. Отчёт при этом валиден.")
    else:
        print(f"Подпись:     {sig}")
        print(f"Ключ рядом:  {path.name}.pub")
        if key:
            print(f"Подписано ключом учреждения: {key}")
            print("Подпись доказывает целостность И авторство.")
        else:
            print("Подписано ключом с этой машины (self-signed): подпись доказывает")
            print("только целостность при передаче, но НЕ авторство — приватный ключ")
            print("лежит здесь же. Ключ вуза: PROCTOR_SIGNING_KEY=/путь make report")
        print("Собрать пакет: make package")
        print("Проверить:     make verify")
endef
export REPORT_PY

# --------------------------------------------------------------- make package
define PACKAGE_PY
"""Собрать пакет доказательств по последней сессии.

Пакет — это то, что проктор физически забирает: один файл
<сессия>.proctor.zip рядом с каталогом сессии. Цель нужна для двух случаев:
сайдкар останавливали жёстко и пакет не собрался сам, либо каталог сессии
принесли отдельно и комплект надо запаковать на машине преподавателя.
"""
import json
import os
import sys
from pathlib import Path

root = Path(os.environ.get("PROCTOR_ROOT", ".")).resolve()
sys.path.insert(0, str(root / "sidecar"))

base = Path(os.environ.get("PROCTOR_SESSIONS") or (root / "sessions"))
sessions = sorted((p for p in base.glob("*") if p.is_dir()),
                  key=lambda p: p.stat().st_mtime)
if not sessions:
    print(f"В {base} нет ни одной сессии. Сначала проведите прогон: make run")
    print("Если доказательства писались в другой каталог:")
    print("  PROCTOR_SESSIONS_DIR=/путь make package")
    raise SystemExit(1)

session_dir = sessions[-1]
print(f"Сессия: {session_dir}")

try:
    from storage import handover as handover_mod
except Exception as exc:
    print(f"Модуль sidecar/storage/handover.py недоступен: {exc}")
    raise SystemExit(1)


def read(name):
    try:
        data = json.loads((session_dir / name).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


meta = read("meta.json")
summary = read("summary.json")

# Ключ учреждения: та же переменная, что у сайдкара. ARGS="--signing-key X"
# тоже понимаем — так цель вызывают из командной строки чаще всего.
key = (os.environ.get("PROCTOR_SIGNING_KEY") or "").strip()
argv = (os.environ.get("PROCTOR_ARGS") or "").split()
for index, token in enumerate(argv):
    if token in ("--signing-key", "--sign-key") and index + 1 < len(argv):
        key = argv[index + 1]
    elif token.startswith("--signing-key="):
        key = token.split("=", 1)[1]
authority = "institution" if key else "self_signed"

# Genesis и голова цепочки читаются из базы: код сверки обязан быть выводим
# из содержимого, а не взят из meta.json, который лежит рядом и не защищён.
chain = {"genesis": str(meta.get("genesis_hash") or "")}
db_file = session_dir / "evidence.sqlite"
if db_file.is_file():
    try:
        from storage import db as db_mod
        store = db_mod.EvidenceStore(None, db_path=str(db_file))
        try:
            rows = store.list_sessions()
            if rows:
                chain["genesis"] = str(rows[0].get("genesis_hash") or chain["genesis"])
            stats = store.stats()
            chain["last_hash"] = stats.get("chain_head", "")
            chain["records"] = stats.get("records", 0)
            chain["events_checked"] = stats.get("events", 0)
        finally:
            store.close()
    except Exception as exc:
        print(f"Базу прочитать не удалось ({exc}) — манифест будет без состава цепочки.")

code = handover_mod.session_code(chain.get("genesis", ""),
                                 str(meta.get("student_id") or ""))
result = handover_mod.build_package(
    session_dir, None, meta, summary, chain,
    meta.get("handover") if isinstance(meta.get("handover"), dict) else {},
    authority, code, key or None, True)

for line in str(result.message or "").splitlines():
    print(line)
if not result.ok:
    print("Отчёт и журнал при этом на месте — забирайте каталог сессии целиком.")
    raise SystemExit(1)
if authority != "institution":
    print("Подписано ключом с этой машины: пакет доказывает целостность, но не")
    print("авторство. Ключ вуза: make package ARGS=\"--signing-key /путь\"")
endef
export PACKAGE_PY

# ---------------------------------------------------- последняя сессия (хелпер)
define LATEST_PY
import os
from pathlib import Path

root = Path(os.environ.get("PROCTOR_ROOT", ".")).resolve()
# Каталог сессий задаёт проктор (PROCTOR_SESSIONS_DIR / --sessions-dir).
# Искать всегда в ./sessions значило бы не найти ничего в тот самый момент,
# когда доказательства как раз и ушли на сетевой диск вуза.
base = Path(os.environ.get("PROCTOR_SESSIONS") or (root / "sessions"))
dirs = sorted((p for p in base.glob("*") if p.is_dir()),
              key=lambda p: p.stat().st_mtime)
print(dirs[-1] if dirs else "")
endef
export LATEST_PY

# --------------------------------------------------------------- make verify
# ------------------------------------------- make verify (деградированный режим)
define VERIFY_JSONL_PY
"""Проверка хеш-цепочки журнала events.jsonl.

Журнал в этом формате пишется только когда storage/db.py недоступен:
каждая строка — {seq, prev_hash, hash, payload}, где
hash = sha256(prev_hash + канонический JSON payload). Канонический —
значит sorted_keys и ensure_ascii=False, иначе цепочка развалится
на ровном месте от порядка ключей.

Правка любого байта payload или перестановка строк ломают цепочку начиная
с этой записи — её номер и печатается.
"""
import hashlib
import json
import os
from pathlib import Path

path = Path(os.environ["PROCTOR_SESSION"]) / "events.jsonl"
zero = "0" * 64
prev = zero
count = 0

with path.open(encoding="utf-8") as fh:
    for index, line in enumerate(fh):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception as exc:
            print(f"  ЦЕПОЧКА НАРУШЕНА на записи #{index}: строка не читается как JSON ({exc})")
            raise SystemExit(1)
        stored_prev = str(rec.get("prev_hash") or zero)
        stored_hash = str(rec.get("hash") or "")
        if stored_prev != prev:
            print(f"  ЦЕПОЧКА НАРУШЕНА на записи #{index}: prev_hash не совпадает "
                  f"с хешем предыдущей записи (порядок записей изменён или запись удалена)")
            raise SystemExit(1)
        body = json.dumps(rec.get("payload"), ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256((stored_prev + body).encode("utf-8")).hexdigest()
        if digest != stored_hash:
            print(f"  ЦЕПОЧКА НАРУШЕНА на записи #{index}: содержимое записи изменено "
                  f"после сохранения (пересчитанный хеш не совпал)")
            raise SystemExit(1)
        prev = stored_hash
        count += 1

print(f"  Цепочка целостна: {count} записей, хеш последней {prev[:32]}…")
print("  Журнал не правился после экзамена.")
endef
export VERIFY_JSONL_PY

# ----------------------------------------------------------------- make test
define TEST_PY
"""Быстрая проверка проекта: импорты, контракт, валидность сценария демо."""
import importlib
import json
import math
import os
import sys
from pathlib import Path

root = Path(os.environ.get("PROCTOR_ROOT", ".")).resolve()
problems = []

MODULES = [
    ("protocol", True), ("config", True), ("session", True),
    ("capture", True), ("main", True),
    ("detectors.objects", False), ("detectors.face_mesh", False),
    ("detectors.identity", False), ("detectors.audio", False),
    ("engine.events", False), ("engine.risk", False),
    ("engine.fusion", False), ("engine.calibration", False),
    ("env_checks", False), ("storage.db", False), ("storage.report", False),
    # Передача доказательств обязательна: без неё проктор не получает ни
    # пакета, ни кода сверки, то есть ответ на главный вопрос заказчика
    # («как экзаменатор получит доказательства») снова становится «никак».
    ("storage.handover", True),
]

print("Модули сайдкара:")
for name, required in MODULES:
    try:
        importlib.import_module(name)
    except Exception as exc:
        if required:
            print(f"  ОШИБКА {name}: {type(exc).__name__}: {exc}")
            problems.append(f"не импортируется {name}")
        else:
            print(f"  нет    {name} ({type(exc).__name__}) — канал деградирует, это допустимо")
    else:
        print(f"  ок     {name}")

from protocol import (  # noqa: E402
    RISK_LOCK, RISK_PAUSE, RISK_WARN, RISK_WEIGHTS,
    Channel, EventKind, ProctorEvent, Severity,
)

print()
print("Сценарий демо scripts/demo_scenario.json:")
scenario_path = root / "scripts" / "demo_scenario.json"
try:
    steps = json.loads(scenario_path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"  ОШИБКА файл не читается: {exc}")
    problems.append("сценарий демо не парсится")
    steps = []

if not isinstance(steps, list):
    print("  ОШИБКА ожидался массив шагов")
    problems.append("сценарий демо не массив")
    steps = []


def severity_for_weight(weight):
    if weight >= 55:
        return Severity.CRITICAL
    if weight >= 35:
        return Severity.HIGH
    if weight >= 20:
        return Severity.MEDIUM
    if weight >= 8:
        return Severity.LOW
    return Severity.INFO


COMMANDS = {"snapshot", "reset_risk", "export_report"}
STATUS_KEYS = {"fps", "face_present", "face_count", "gaze", "phone",
               "identity_ok", "audio_ok", "risk", "state"}
EVENT_KEYS = {"kind", "severity", "channel", "confidence", "duration",
              "message", "detail", "evidence"}
TOP_KEYS = {"step", "delay", "type", "demo"}

total_delay = 0.0
events = 0
for index, step in enumerate(steps):
    where = f"шаг #{index + 1}"
    if not isinstance(step, dict):
        problems.append(f"{where}: не объект")
        continue
    total_delay += float(step.get("delay", 0.0) or 0.0)
    kind_of_step = step.get("type")
    extra = set(step) - TOP_KEYS
    if kind_of_step == "event":
        unknown = extra - EVENT_KEYS
        if unknown:
            problems.append(f"{where}: лишние ключи события {sorted(unknown)}")
        try:
            kind = EventKind(step["kind"])
            severity = Severity(step["severity"])
            channel = Channel(step["channel"])
        except Exception as exc:
            problems.append(f"{where}: {exc}")
            continue
        expected = severity_for_weight(RISK_WEIGHTS.get(kind, 10.0))
        if severity is not expected:
            problems.append(f"{where}: {kind.value} severity={severity.value}, "
                            f"по весу {RISK_WEIGHTS.get(kind)} ожидается {expected.value}")
        if not str(step.get("message", "")).strip():
            problems.append(f"{where}: {kind.value} без текста message (он идёт в отчёт)")
        try:
            ProctorEvent(kind=kind, severity=severity, channel=channel,
                         confidence=float(step.get("confidence", 1.0)),
                         duration=float(step.get("duration", 0.0)),
                         message=str(step.get("message", "")),
                         detail=dict(step.get("detail") or {})).to_dict()
        except Exception as exc:
            problems.append(f"{where}: ProctorEvent не собирается: {exc}")
        events += 1
    elif kind_of_step == "status":
        unknown = extra - STATUS_KEYS
        if unknown:
            problems.append(f"{where}: лишние ключи status {sorted(unknown)}")
    elif kind_of_step == "control":
        if step.get("name") not in COMMANDS:
            problems.append(f"{where}: control.name={step.get('name')!r} вне набора протокола {sorted(COMMANDS)}")
    else:
        problems.append(f"{where}: неизвестный type={kind_of_step!r}")

demo_steps = sorted({int(s.get("step", 0)) for s in steps if isinstance(s, dict)})
print(f"  сообщений: {len(steps)} (событий {events}), шагов демо: {demo_steps}")
print(f"  длительность прогона: {total_delay:.1f} с")
if demo_steps != list(range(1, 9)):
    problems.append(f"шаги демо {demo_steps}, а в docs/DEMO.md их ровно 8 (1..8)")

# Траектория риска: модель та же, что в сайдкаре (сумма весов с полураспадом).
half_life = 90.0
items = []
clock = 0.0
peak = 0.0
levels = []
for step in steps:
    if not isinstance(step, dict):
        continue
    clock += float(step.get("delay", 0.0) or 0.0)
    if step.get("type") == "control" and step.get("name") == "reset_risk":
        items = []
        continue
    if step.get("type") != "event":
        continue
    try:
        kind = EventKind(step["kind"])
    except Exception:
        continue
    weight = RISK_WEIGHTS.get(kind, 10.0) * max(min(float(step.get("confidence", 1.0)), 1.0), 0.2)
    items.append((clock, weight))
    score = min(sum(w * math.pow(0.5, (clock - ts) / half_life) for ts, w in items), 100.0)
    peak = max(peak, score)
    if score >= RISK_LOCK:
        levels.append("lock")
    elif score >= RISK_PAUSE:
        levels.append("pause")
    elif score >= RISK_WARN:
        levels.append("warn")

print(f"  пик risk-score: {peak:.1f}; пройдены уровни: "
      f"warn={'да' if 'warn' in levels else 'нет'}, "
      f"pause={'да' if 'pause' in levels else 'нет'}, "
      f"lock={'да' if 'lock' in levels else 'нет'}")
for needed in ("warn", "pause", "lock"):
    if needed not in levels:
        problems.append(f"сценарий ни разу не доводит риск до уровня {needed} — эскалацию не показать")

print()
print("Согласованность реестров:")

# 1. Коды инцидентов: один реестр на систему.
#
# Канонический — EVENT_CODES в sidecar/engine/events.py: именно он уходит в
# журнал, в хеш-цепочку и в отчёт, и по нему студент подаёт апелляцию.
# В shell/renderer/hud.js живёт таблица ADVICE с подсказками для студента, и у
# неё есть своя колонка кодов — как запасной вариант для события без detail.
# Эти две таблицы однажды разошлись по ВСЕМ 32 видам (HUD показывал OBJ_201
# там, где в журнале лежит VIS_101), и апелляция по названному с экрана коду не
# находила в журнале ничего. Проверяем вид в вид.
try:
    from engine.events import EVENT_CODES
except Exception as exc:
    print(f"  нет    реестр кодов не импортируется ({type(exc).__name__})")
else:
    import re as _re
    hud_path = root / "shell" / "renderer" / "hud.js"
    if not hud_path.is_file():
        print("  нет    shell/renderer/hud.js — сверить коды HUD не с чем")
    else:
        text = hud_path.read_text(encoding="utf-8")
        block = _re.search(r"var ADVICE = \{(.*?)\n  \};", text, _re.S)
        if block is None:
            problems.append("в hud.js не найдена таблица ADVICE: сверить коды нечем")
        else:
            hud_codes = dict(_re.findall(
                r"^\s*([A-Z_]+):\s*\{\s*code:\s*'([A-Z0-9_]+)'",
                block.group(1), _re.M))
            canon = {k.name: v for k, v in EVENT_CODES.items()}
            bad = sorted(f"{name}: HUD {code}, EVENT_CODES {canon[name]}"
                         for name, code in hud_codes.items()
                         if name in canon and canon[name] != code)
            orphan = sorted(n for n in hud_codes if n not in canon)
            if bad:
                problems.append("коды HUD расходятся с EVENT_CODES: " + "; ".join(bad))
            if orphan:
                problems.append("в hud.js есть виды без кода в EVENT_CODES: "
                                + ", ".join(orphan))
            if not bad and not orphan:
                print(f"  ок     коды HUD совпадают с EVENT_CODES ({len(hud_codes)} видов)")

# 2. Лестницы наблюдения и кластеры поведения: одно определение «что считать
#    одним наблюдением», а не два независимых.
try:
    from storage.report import ladder_cluster_coherence
except Exception as exc:
    print(f"  нет    кластеры поведения не импортируются ({type(exc).__name__})")
else:
    drift = ladder_cluster_coherence()
    for item in drift:
        problems.append(item)
    if not drift:
        print("  ок     ступени лестниц лежат в одном кластере поведения")

# 3. Разметка лестницы печатается по-русски. Ключи `escalation` уходят в
#    раскрытие таблицы инцидентов, и без подписи это английские имена посреди
#    русского документа — см. аудит _detail_audit.
try:
    from storage.report import DETAIL_LABELS
except Exception:
    pass
else:
    need = ("escalation", "ladder", "ladder_ru", "run", "step", "steps",
            "weight", "top_weight", "refines", "refined_by", "refined_by_code",
            "superseded_by", "peak_step", "subject", "level")
    missing = sorted(k for k in need if k not in DETAIL_LABELS)
    if missing:
        problems.append("у ключей разметки лестницы нет русской подписи "
                        f"(DETAIL_LABELS): {', '.join(missing)}")
    else:
        print("  ок     разметка лестницы подписана по-русски")

print()
print("Профиль экзамена: барьер против неутверждённого черновика")

# Инструмент разведки (tools/discover) собирает ЧЕРНОВИК профиля по
# фактическим запросам страницы LMS. Ревью показало, что такой черновик
# применялся как обычный профиль: заглушки `<ФИО и должность проктора>` ядро
# не замечало, `dropped_origins` был пуст, и экзамен ехал на машинном белом
# списке. Барьер стоит в `approved_by_human` и в вердикте готовности; проверяем
# именно его, потому что сломать его можно одной строкой и молча.
try:
    from config import (ExamProfile, PROFILE_DRAFT_SENTINEL,
                        PROFILE_HUMAN_FIELDS, load_exam_profile,
                        profile_has_draft_marker, profile_unfilled_fields)
except Exception as exc:
    problems.append(f"config не отдаёт проверку утверждения профиля: {exc}")
else:
    import json as _json
    import tempfile as _tempfile

    draft_rules = {
        "institution": "<название вуза>",
        "exam_id": "<код экзамена из расписания>",
        "exam_url": "https://lms.astanait.edu.kz/login/index.php",
        "allowed_origins": ["lms.astanait.edu.kz"],
        "allow_search": False,
        "issued_by": "<ФИО и должность проктора>",
        "issued_at": "<дата выдачи, ISO 8601>",
        "notes": f"{PROFILE_DRAFT_SENTINEL}. Собран инструментом разведки.",
    }
    unfilled = profile_unfilled_fields(draft_rules)
    if sorted(unfilled) != sorted(PROFILE_HUMAN_FIELDS):
        problems.append("заглушки `<...>` в полях проктора не опознаются: "
                        f"нашлось {unfilled}")
    else:
        print(f"  ок     заглушки в полях проктора опознаны ({len(unfilled)} поля)")
    if not profile_has_draft_marker(draft_rules):
        problems.append("метка-страж черновика в notes не опознаётся")
    else:
        print("  ок     метка-страж черновика в notes опознана")

    with _tempfile.TemporaryDirectory() as tmp:
        draft_path = Path(tmp) / "exam-profile.draft.json"
        draft_path.write_text(_json.dumps(draft_rules, ensure_ascii=False),
                              encoding="utf-8")
        loaded = load_exam_profile(path=str(draft_path))
        if loaded.approved_by_human:
            problems.append("машинный черновик профиля считается утверждённым "
                            "человеком: барьер не работает")
        else:
            print("  ок     машинный черновик НЕ считается утверждённым человеком")
        if "ЧЕРНОВИК" not in loaded.header_line().upper():
            problems.append("шапка отчёта не называет профиль черновиком: "
                            f"{loaded.header_line()!r}")
        else:
            print("  ок     шапка отчёта называет профиль черновиком")
        rec = loaded.record()
        if rec.get("approved_by_human") is not False or not rec.get("unfilled_fields"):
            problems.append("в записи цепочки нет следа, что профиль не утверждён")
        else:
            print("  ок     в цепочку едут approved_by_human и unfilled_fields")

        # Тот же файл, заполненный человеком, обязан проходить: барьер должен
        # сниматься одним осознанным действием, а не требовать новой сборки.
        approved = dict(draft_rules)
        approved.update({
            "institution": "Astana IT University",
            "exam_id": "SE-2026-01",
            "issued_by": "Иванова А.Б., старший проктор",
            "issued_at": "2026-10-16T09:00:00+05:00",
            "notes": "Белый список проверен глазами 16.10.2026.",
        })
        ok_path = Path(tmp) / "exam-profile.json"
        ok_path.write_text(_json.dumps(approved, ensure_ascii=False), encoding="utf-8")
        loaded_ok = load_exam_profile(path=str(ok_path))
        if not loaded_ok.approved_by_human:
            problems.append("заполненный человеком профиль всё равно считается "
                            f"черновиком: {loaded_ok.unfilled_fields}")
        else:
            print("  ок     заполненный человеком профиль проходит барьер")

    # Пустой профиль — не «неутверждённый»: правила просто не задавались, и у
    # этого состояния своя строка в шапке.
    if not ExamProfile().approved_by_human:
        problems.append("отсутствие профиля ошибочно считается неутверждённым "
                        "черновиком: это разные состояния")
    else:
        print("  ок     отсутствие профиля и черновик — разные состояния")

print()
if problems:
    print("НЕ ПРОШЛО:")
    for item in problems:
        print(f"  - {item}")
    raise SystemExit(1)
print("Python-часть и сценарий демо в порядке.")
endef
export TEST_PY

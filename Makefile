# Точки входа проекта. Всё, что нужно команде и жюри, запускается отсюда.
#
#   make setup      — собрать окружение (один раз на машине)
#   make run        — запустить систему: сайдкар + оболочка
#   make run-mock   — демо без камеры и моделей (план Б на сцене)
#   make models     — подготовить веса детектора объектов (нужен интернет, однократно)
#   make report     — собрать HTML-отчёт по последней сессии
#   make verify     — проверить хеш-цепочку доказательной базы и подпись отчёта
#   make test       — быстрая проверка: всё импортируется, парсится, сценарий валиден
#   make clean      — убрать мусор сборки (доказательства НЕ трогает)
#
# Дополнительные аргументы прокидываются через ARGS:
#   make run ARGS="--no-kiosk --port 8800"
#   make setup ARGS="--vision --audio"

SHELL := /bin/bash
.DEFAULT_GOAL := help

ROOT := $(CURDIR)
ARGS ?=

# Интерпретатор: .venv, затем системный. Переопределяется через PROCTOR_PYTHON.
PY := $(shell if [ -n "$$PROCTOR_PYTHON" ]; then echo "$$PROCTOR_PYTHON"; \
	elif [ -x "$(CURDIR)/.venv/bin/python3" ]; then echo "$(CURDIR)/.venv/bin/python3"; \
	else echo python3; fi)

JS_FILES := shell/main.js shell/lockdown.js shell/ipc.js shell/preload.js \
	shell/renderer/app.js shell/renderer/hud.js shell/renderer/exam.js \
	shell/renderer/calibration.js shell/renderer/telemetry.js

SH_FILES := scripts/setup.sh scripts/run-dev.sh scripts/fetch_models.sh

.PHONY: help setup run run-mock models report verify test clean clean-sessions

# ---------------------------------------------------------------------------
help:
	@echo "Локальная система прокторинга — доступные цели:"
	@echo ""
	@echo "  make setup      собрать .venv, поставить зависимости, проверить версии"
	@echo "                  ARGS=\"--vision --audio --identity --all --skip-npm\""
	@echo "  make run        запустить сайдкар и оболочку, Ctrl+C гасит оба"
	@echo "                  ARGS=\"--no-kiosk --headless --port 8800\""
	@echo "  make run-mock   демо без камеры: события по scripts/demo_scenario.json"
	@echo "  make models     подготовить models/yolov8n.onnx (однократно, нужен интернет)"
	@echo "  make report     собрать отчёт по последней сессии в sessions/"
	@echo "  make verify     проверить хеш-цепочку доказательств и подпись отчёта"
	@echo "  make test       проверить импорты, синтаксис JS и валидность сценария демо"
	@echo "  make clean      убрать __pycache__ и артефакты сборки"
	@echo "  make clean-sessions  удалить записанные сессии (спросит подтверждение)"
	@echo ""
	@echo "  python: $(PY)"

# ---------------------------------------------------------------------------
setup:
	@bash scripts/setup.sh $(ARGS)

run:
	@bash scripts/run-dev.sh $(ARGS)

run-mock:
	@bash scripts/run-dev.sh --mock $(ARGS)

models:
	@bash scripts/fetch_models.sh

# ---------------------------------------------------------------------------
report:
	@PROCTOR_ROOT="$(ROOT)" $(PY) -c "$$REPORT_PY"

# Проверку подписи и SQLite-цепочки делает scripts/verify_report.py — это
# инструмент преподавателя, он же показывает номер сломанной записи.
# Врезка ниже нужна только для деградированного режима, когда storage/db.py
# недоступен и доказательства пишутся в events.jsonl: такую базу verify_report
# не читает, а проверять её всё равно надо.
verify:
	@sess="$$(PROCTOR_ROOT="$(ROOT)" $(PY) -c "$$LATEST_PY")"; \
	if [ -z "$$sess" ]; then \
		echo "В sessions/ нет ни одной сессии. Сначала проведите прогон: make run"; \
		exit 1; \
	fi; \
	echo "Сессия: $$sess"; \
	if [ -f "$$sess/evidence.sqlite" ] && [ -f scripts/verify_report.py ]; then \
		PYTHONPATH="$(ROOT)/sidecar" $(PY) scripts/verify_report.py --db "$$sess/evidence.sqlite" $(ARGS); \
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

sessions = sorted((p for p in (root / "sessions").glob("*") if p.is_dir()),
                  key=lambda p: p.stat().st_mtime)
if not sessions:
    print("В sessions/ нет ни одной сессии. Сначала проведите прогон: make run")
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
try:
    path = report_mod.build_report(str(session_dir), str(db), str(out))
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
        sig = sign(str(path))
    except Exception as exc:
        print(f"Подпись не поставлена ({type(exc).__name__}: {exc}).")
        print("Нужен ключ keys/report_ed25519.key и пакет cryptography. Отчёт при этом валиден.")
    else:
        print(f"Подпись:     {sig}")
        print(f"Ключ рядом:  {path.name}.pub")
        print("Проверить:   make verify")
endef
export REPORT_PY

# ---------------------------------------------------- последняя сессия (хелпер)
define LATEST_PY
import os
from pathlib import Path

root = Path(os.environ.get("PROCTOR_ROOT", ".")).resolve()
dirs = sorted((p for p in (root / "sessions").glob("*") if p.is_dir()),
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
if problems:
    print("НЕ ПРОШЛО:")
    for item in problems:
        print(f"  - {item}")
    raise SystemExit(1)
print("Python-часть и сценарий демо в порядке.")
endef
export TEST_PY

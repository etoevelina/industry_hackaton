"""
Сборка автономного HTML-отчёта по сессии прокторинга + подпись Ed25519.

Отчёт — не скриншот демки, а артефакт, который преподаватель открывает с
флешки без интернета и может проверить:

  * все картинки вшиты в HTML как base64, CSS инлайном, внешних запросов нет;
  * числа считаются по тем же весам (`protocol.RISK_WEIGHTS`), что и в рантайме;
  * данные берутся из `events.payload` — ровно из тех байтов, по которым считан
    hash-chain, поэтому отчёт и цепочка говорят об одном и том же;
  * результат `verify_chain()` и голова цепочки печатаются в самом отчёте;
  * рядом с отчётом кладутся `report.html.sig` (подпись Ed25519) и
    `report.html.pub` (публичный ключ) — проверка одной командой через
    `scripts/verify_report.py`.

INTEGRITY SCORE. Итоговая цифра — не «вероятность списывания», а «насколько
можно доверять результату». Считается так:

    штраф_вида     = вес_вида * средняя_уверенность * sqrt(число_срабатываний)
    штраф          = сумма штрафов видов
    integrity      = 100 * exp(-штраф / SCALE)        # SCALE = 120

sqrt даёт убывающую отдачу: десять «взгляд вниз» не обнуляют доверие, но и не
игнорируются. Экспонента держит оценку в диапазоне 0..100 и не даёт отрицательных
значений. Разложение по каналам точное: потерянные баллы делятся
пропорционально вкладу канала в штраф, поэтому сумма вкладов равна
`100 - integrity` — именно это рисует SVG-диаграмма.

Отдельно показывается `risk-score` из рантайма: он мгновенный и затухающий
(half-life), поэтому к концу спокойной сессии почти нулевой. Эти две метрики
дополняют друг друга, и в отчёте это написано прямо.
"""
from __future__ import annotations

import base64
import html
import importlib
import importlib.util
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent          # sidecar/storage
_SIDECAR = _HERE.parent                          # sidecar
_ROOT = _SIDECAR.parent                          # корень репозитория
for _p in (str(_SIDECAR), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: Масштаб экспоненты integrity score (подобран так, чтобы одна подмена
#: личности роняла доверие примерно до 60, а четыре «взгляд вниз» — до 88).
INTEGRITY_SCALE = 120.0

#: Половина времени жизни вклада события в risk-score, сек (как в рантайме).
DEFAULT_HALF_LIFE = 90.0

#: Лимит на суммарный объём вшитых картинок, чтобы отчёт оставался открываемым.
EMBED_BUDGET_BYTES = 24 * 1024 * 1024
THUMB_WIDTH = 260
CLIP_EMBED_LIMIT = 3 * 1024 * 1024

TEMPLATE_NAME = "report.html.j2"

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_LABELS = {
    "info": "информация", "low": "низкая", "medium": "средняя",
    "high": "высокая", "critical": "критическая",
}
SEVERITY_COLORS = {
    "info": "#64748b", "low": "#2563eb", "medium": "#d97706",
    "high": "#dc2626", "critical": "#7f1d1d",
}
CHANNEL_LABELS = {
    "vision": "Объекты в кадре", "gaze": "Взгляд и голова", "identity": "Личность",
    "audio": "Аудио", "environment": "Окружение (ОС)", "shell": "Оболочка экзамена",
    "fusion": "Связки сигналов", "system": "Служебное",
}
CHANNEL_COLORS = {
    "vision": "#2563eb", "gaze": "#0891b2", "identity": "#7c3aed", "audio": "#db2777",
    "environment": "#ca8a04", "shell": "#475569", "fusion": "#dc2626", "system": "#94a3b8",
}
#: Человекочитаемые названия инцидентов — подпись для таблицы, если в событии
#: не оказалось `message`. Новых видов событий здесь не вводится.
KIND_LABELS = {
    "PHONE_IN_FRAME": "Телефон в кадре",
    "PHONE_RAISED": "Телефон поднят к лицу",
    "PHONE_AIMED_AT_SCREEN": "Телефон направлен на экран",
    "FORBIDDEN_OBJECT": "Посторонний предмет в кадре",
    "NO_FACE": "Лицо не в кадре",
    "SECOND_FACE": "Второе лицо в кадре",
    "IDENTITY_MISMATCH": "Личность не совпадает с эталоном",
    "LIVENESS_FAIL": "Признаки неживого изображения",
    "GAZE_DOWN": "Взгляд направлен вниз",
    "GAZE_SIDE": "Взгляд направлен в сторону",
    "GAZE_OFF_SCREEN": "Взгляд вне области экрана",
    "HEAD_TURNED": "Голова отвернута от экрана",
    "VOICE_OTHER": "Посторонний голос",
    "SPEECH_WITHOUT_LIP_MOTION": "Речь без движения губ",
    "VIRTUAL_CAMERA": "Виртуальная камера",
    "REMOTE_ACCESS_SOFTWARE": "Софт удалённого доступа",
    "VIRTUAL_MACHINE": "Виртуальная машина",
    "SCREEN_RECORDING": "Запись экрана",
    "MULTIPLE_DISPLAYS": "Подключено несколько экранов",
    "BLACKLISTED_PROCESS": "Запрещённый процесс",
    "WINDOW_BLUR": "Потеря фокуса окна экзамена",
    "FULLSCREEN_EXIT": "Выход из полноэкранного режима",
    "SHORTCUT_BLOCKED": "Заблокированное сочетание клавиш",
    "CLIPBOARD_PASTE": "Вставка из буфера обмена",
    "DEVTOOLS_ATTEMPT": "Попытка открыть инструменты разработчика",
    "PASTE_BURST": "Вставка большого блока текста",
    "TYPING_ANOMALY": "Аномалия ритма набора",
    "FUSION_GAZE_THEN_ANSWER": "Связка: взгляд ушёл — сразу ответ",
    "FUSION_BLUR_THEN_ANSWER": "Связка: потеря фокуса — сразу ответ",
    "FUSION_PHONE_THEN_ANSWER": "Связка: телефон — сразу ответ",
    "SESSION_STARTED": "Сессия начата",
    "SESSION_ENDED": "Сессия завершена",
    "CALIBRATION_DONE": "Калибровка завершена",
    "SENSOR_LOST": "Потерян датчик (камера/микрофон)",
}
#: Расшифровка полей detail для разбора связок.
DETAIL_LABELS = {
    "question_id": "Вопрос", "zone": "Зона взгляда", "gaze_zone": "Зона взгляда",
    "duration": "Длительность, с", "gaze_duration": "Взгляд отведён, с",
    "delay": "Задержка до ответа, с", "delay_ms": "Задержка до ответа, мс",
    "gap_sec": "Пауза, с", "chars": "Символов введено", "length": "Длина, символов",
    "mean_ms": "Средний интервал набора, мс", "std_ms": "Разброс интервала, мс",
    "baseline_ms": "Базовый интервал студента, мс", "ratio": "Отношение к базовой линии",
    "speed_ratio": "Во сколько раз быстрее обычного", "conf": "Уверенность",
    "confidence": "Уверенность", "label": "Метка детектора", "yaw": "Поворот головы, град",
    "pitch": "Наклон головы, град", "gaze_yaw": "Взгляд по горизонтали, град",
    "gaze_pitch": "Взгляд по вертикали, град", "similarity": "Близость к эталону",
    "threshold": "Порог", "area_ratio": "Доля кадра", "bbox": "Рамка (x,y,w,h)",
    "process": "Процесс", "device": "Устройство", "count": "Количество",
    "face_count": "Лиц в кадре", "rms": "Громкость (RMS)", "is_owner": "Голос владельца",
    "source": "Источник", "stage": "Этап", "reason": "Причина",
    "time_to_answer_ms": "Время на ответ, мс", "trigger": "Триггер",
    "linked_event": "Связанное событие", "window_sec": "Окно связки, с",
    "paste": "Вставка", "typing_stats": "Статистика набора", "student_id": "Студент",
    "exam_id": "Экзамен", "capabilities": "Активные каналы", "difficulty": "Сложность",
}
SERVICE_KINDS = {"SESSION_STARTED", "SESSION_ENDED", "CALIBRATION_DONE"}


# ---------------------------------------------------------------------------
# Загрузка соседних модулей максимально терпимо к способу импорта
# ---------------------------------------------------------------------------
def _import_first(*names: str) -> Any:
    for name in names:
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None


def _load_db_module() -> Any:
    """Модуль хранилища: как пакет, как плоский модуль или прямо по файлу."""
    mod = _import_first("storage.db", "sidecar.storage.db", "db")
    if mod is not None and hasattr(mod, "EvidenceStore"):
        return mod
    try:
        spec = importlib.util.spec_from_file_location("_evidence_db", _HERE / "db.py")
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    except Exception:
        pass
    return None


_PROTO = _import_first("protocol", "sidecar.protocol")


def _risk_weights() -> dict[str, float]:
    """Веса из protocol.py. Нет протокола — нейтральный вес, отчёт всё равно соберётся."""
    weights: dict[str, float] = {}
    table = getattr(_PROTO, "RISK_WEIGHTS", None) if _PROTO else None
    if isinstance(table, dict):
        for kind, value in table.items():
            key = getattr(kind, "value", kind)
            try:
                weights[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    return weights


def _thresholds() -> tuple[float, float, float]:
    warn = float(getattr(_PROTO, "RISK_WARN", 30.0) or 30.0) if _PROTO else 30.0
    pause = float(getattr(_PROTO, "RISK_PAUSE", 60.0) or 60.0) if _PROTO else 60.0
    lock = float(getattr(_PROTO, "RISK_LOCK", 90.0) or 90.0) if _PROTO else 90.0
    return warn, pause, lock


def _fusion_kinds() -> set[str]:
    kinds = set()
    enum = getattr(_PROTO, "EventKind", None) if _PROTO else None
    if enum is not None:
        for item in enum:
            value = str(getattr(item, "value", item))
            if value.startswith("FUSION_"):
                kinds.add(value)
    if not kinds:
        kinds = {k for k in KIND_LABELS if k.startswith("FUSION_")}
    return kinds


# ---------------------------------------------------------------------------
# Утилиты форматирования
# ---------------------------------------------------------------------------
def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clock(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    except Exception:
        return "--:--:--"


def _full_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return "—"


def _offset(ts: float, start: float) -> str:
    delta = max(_f(ts) - _f(start), 0.0)
    return f"+{int(delta // 60):02d}:{int(delta % 60):02d}"


def _dur(seconds: float) -> str:
    total = int(max(_f(seconds), 0.0))
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h} ч {m:02d} мин {s:02d} с" if h else f"{m} мин {s:02d} с"


def _num(value: float, digits: int = 1) -> str:
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def _channel_label(channel: str) -> str:
    return CHANNEL_LABELS.get(channel, channel or "—")


# ---------------------------------------------------------------------------
# Чтение данных сессии
# ---------------------------------------------------------------------------
@dataclass
class ReportData:
    session_dir: Path
    db_path: Path | None
    meta: dict[str, Any]
    summary: dict[str, Any]
    events: list[dict[str, Any]]
    chain: dict[str, Any]
    sources: list[str]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_jsonl_events(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Фолбэк-формат сайдкара (events.jsonl), если SQLite недоступен."""
    events: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return events, meta, summary
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            continue
        rtype = payload.get("type")
        if rtype == "event" and isinstance(payload.get("event"), dict):
            event = dict(payload["event"])
            event["_seq"] = record.get("seq")
            event["_hash"] = record.get("hash", "")
            events.append(event)
        elif rtype == "session_open" and isinstance(payload.get("meta"), dict):
            meta = dict(payload["meta"])
        elif rtype == "session_close" and isinstance(payload.get("summary"), dict):
            summary = dict(payload["summary"])
    return events, meta, summary


def load_report_data(session_dir: str | Path, db_path: str | Path | None = None) -> ReportData:
    """Собрать всё, что нужно отчёту: мета, сводка, события, состояние цепочки."""
    sdir = Path(str(session_dir)).resolve()
    sources: list[str] = []
    meta = _read_json(sdir / "meta.json")
    summary = _read_json(sdir / "summary.json")
    if meta:
        sources.append("meta.json")
    if summary:
        sources.append("summary.json")

    db = Path(str(db_path)).resolve() if db_path else None
    if db is None:
        candidate = sdir / "evidence.sqlite"
        db = candidate if candidate.is_file() else None

    events: list[dict[str, Any]] = []
    chain: dict[str, Any] = {"ok": False, "reason": "проверка не выполнялась",
                             "checked": 0, "events_checked": 0, "broken_at": -1,
                             "last_hash": "", "genesis": "", "available": False}

    db_mod = _load_db_module()
    if db is not None and db.is_file() and db_mod is not None:
        store = None
        try:
            store = db_mod.EvidenceStore(None, db_path=str(db))
            sessions = store.list_sessions()
            session_id = str(meta.get("session_id") or (sessions[0]["session_id"] if sessions else ""))
            row = next((s for s in sessions if str(s.get("session_id")) == session_id), None)
            if row is None and sessions:
                row = sessions[0]
                session_id = str(row.get("session_id") or "")
            if row is not None:
                # приоритет у данных из цепочки: meta.json/summary.json лежат рядом
                # с отчётом и не защищены хешем, поэтому они только дополняют.
                meta = {**meta, **(row.get("meta_parsed") or {})}
                stored_summary = row.get("summary_parsed") or {}
                summary = {**summary, **stored_summary} if stored_summary else summary
                chain["genesis"] = str(row.get("genesis_hash") or "")
            events = store.load_events(session_id or None)
            verified = store.verify_chain_detailed()
            chain.update(verified)
            chain["available"] = True
            chain.update(store.stats(session_id or None))
            sources.append(f"SQLite: {db.name}")
        except Exception as exc:
            chain["reason"] = f"не удалось прочитать базу: {exc}"
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

    if not events:
        jsonl = sdir / "events.jsonl"
        if jsonl.is_file():
            events, jmeta, jsummary = _read_jsonl_events(jsonl)
            meta = {**jmeta, **meta}
            summary = {**jsummary, **summary} if summary else dict(jsummary)
            if events:
                sources.append("events.jsonl (резервный журнал)")
                chain.setdefault("reason", "")
                if not chain.get("available"):
                    chain["reason"] = ("журнал прочитан из резервного файла events.jsonl, "
                                       "SQLite-цепочка недоступна")

    events.sort(key=lambda e: _f(e.get("ts")))
    return ReportData(session_dir=sdir, db_path=db, meta=meta, summary=summary,
                      events=events, chain=chain, sources=sources)


# ---------------------------------------------------------------------------
# Расчёты
# ---------------------------------------------------------------------------
def compute_integrity(events: list[dict[str, Any]],
                      weights: dict[str, float] | None = None,
                      scale: float = INTEGRITY_SCALE) -> dict[str, Any]:
    """Integrity score и его точное разложение по каналам и видам инцидентов."""
    weights = weights if weights is not None else _risk_weights()
    kinds: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = str(event.get("kind") or "")
        if not kind or kind in SERVICE_KINDS:
            continue
        weight = float(weights.get(kind, 10.0))
        if weight <= 0:
            continue
        row = kinds.setdefault(kind, {
            "kind": kind, "label": _kind_label(kind),
            "channel": str(event.get("channel") or "system"),
            "weight": weight, "count": 0, "conf_sum": 0.0,
            "severity": str(event.get("severity") or "info"),
        })
        row["count"] += 1
        row["conf_sum"] += min(max(_f(event.get("confidence"), 1.0), 0.2), 1.0)
        if SEVERITY_ORDER.get(str(event.get("severity")), 0) > SEVERITY_ORDER.get(row["severity"], 0):
            row["severity"] = str(event.get("severity"))

    penalty = 0.0
    for row in kinds.values():
        mean_conf = row["conf_sum"] / max(row["count"], 1)
        row["mean_conf"] = round(mean_conf, 3)
        row["penalty"] = row["weight"] * mean_conf * math.sqrt(row["count"])
        penalty += row["penalty"]

    integrity = 100.0 * math.exp(-penalty / max(scale, 1.0)) if penalty > 0 else 100.0
    lost = 100.0 - integrity

    channels: dict[str, dict[str, Any]] = {}
    for row in kinds.values():
        ch = row["channel"] or "system"
        node = channels.setdefault(ch, {
            "channel": ch, "label": _channel_label(ch), "color": CHANNEL_COLORS.get(ch, "#94a3b8"),
            "penalty": 0.0, "count": 0, "kinds": [],
        })
        node["penalty"] += row["penalty"]
        node["count"] += row["count"]
        node["kinds"].append(row)

    for node in channels.values():
        node["lost"] = round(lost * node["penalty"] / penalty, 2) if penalty > 0 else 0.0
        node["share"] = round(100.0 * node["penalty"] / penalty, 1) if penalty > 0 else 0.0
        node["kinds"].sort(key=lambda r: r["penalty"], reverse=True)
        for row in node["kinds"]:
            row["lost"] = round(lost * row["penalty"] / penalty, 2) if penalty > 0 else 0.0

    ordered = sorted(channels.values(), key=lambda n: n["penalty"], reverse=True)
    return {
        "score": round(integrity, 1),
        "lost": round(lost, 1),
        "penalty": round(penalty, 2),
        "scale": scale,
        "channels": ordered,
        "kinds": sorted(kinds.values(), key=lambda r: r["penalty"], reverse=True),
    }


def integrity_verdict(score: float, has_critical: bool) -> dict[str, str]:
    """Словесный вердикт по integrity score. Решение всё равно за преподавателем."""
    if score >= 85 and not has_critical:
        return {"level": "ok", "title": "Существенных нарушений не зафиксировано",
                "text": "Поведение в пределах нормы. Отдельные срабатывания объясняются "
                        "обычными движениями и не образуют связок.",
                "color": "#15803d"}
    if score >= 65:
        return {"level": "attention", "title": "Требуется выборочная проверка",
                "text": "Зафиксированы единичные инциденты. Рекомендуется просмотреть "
                        "отмеченные моменты ниже и принять решение по существу.",
                "color": "#ca8a04"}
    if score >= 40:
        return {"level": "doubt", "title": "Серьёзные сомнения в самостоятельности работы",
                "text": "Инциденты повторяются и подкреплены доказательствами. "
                        "Работу следует разобрать с студентом по пунктам таймлайна.",
                "color": "#ea580c"}
    return {"level": "fail", "title": "Результат нельзя считать достоверным",
            "text": "Зафиксированы грубые нарушения условий экзамена. "
                    "Итоговое решение принимает преподаватель, факты приведены ниже.",
            "color": "#b91c1c"}


def risk_series(events: list[dict[str, Any]], t0: float, t1: float,
                weights: dict[str, float] | None = None,
                half_life: float = DEFAULT_HALF_LIFE, max_score: float = 100.0,
                points: int = 240) -> list[tuple[float, float]]:
    """Восстановить кривую risk-score: те же веса и то же затухание, что в рантайме."""
    weights = weights if weights is not None else _risk_weights()
    items: list[tuple[float, float]] = []
    for event in events:
        kind = str(event.get("kind") or "")
        if kind in SERVICE_KINDS:
            continue
        weight = float(weights.get(kind, 10.0)) * min(max(_f(event.get("confidence"), 1.0), 0.2), 1.0)
        if weight > 0:
            items.append((_f(event.get("ts")), weight))
    span = max(t1 - t0, 1.0)
    step = span / max(points - 1, 1)
    hl = max(half_life, 1.0)
    out: list[tuple[float, float]] = []
    for i in range(points):
        t = t0 + i * step
        total = 0.0
        for ts, weight in items:
            if ts <= t:
                total += weight * math.pow(0.5, (t - ts) / hl)
        out.append((t, min(total, max_score)))
    return out


# ---------------------------------------------------------------------------
# Вшивание файлов-доказательств
# ---------------------------------------------------------------------------
class _Embedder:
    """base64-вшивание с бюджетом по объёму: отчёт обязан остаться открываемым."""

    def __init__(self, session_dir: Path, budget: int = EMBED_BUDGET_BYTES) -> None:
        self.session_dir = session_dir
        self.budget = budget
        self.used = 0
        self.skipped = 0
        self._cv: Any = None
        self._np: Any = None
        self._tried = False

    def _deps(self) -> tuple[Any, Any]:
        if not self._tried:
            self._tried = True
            try:
                import cv2  # type: ignore
                import numpy  # type: ignore
                self._cv, self._np = cv2, numpy
            except Exception:
                self._cv, self._np = None, None
        return self._cv, self._np

    def resolve(self, rel: str) -> Path | None:
        if not rel:
            return None
        path = Path(str(rel))
        if not path.is_absolute():
            path = self.session_dir / path
        return path if path.is_file() else None

    def image(self, rel: str, width: int = THUMB_WIDTH) -> str:
        """Миниатюра кадра-доказательства как data URI."""
        path = self.resolve(rel)
        if path is None:
            return ""
        cv, np = self._deps()
        blob: bytes | None = None
        if cv is not None and np is not None:
            try:
                image = cv.imread(str(path))
                if image is not None:
                    h, w = image.shape[:2]
                    if w > width:
                        scale = width / float(w)
                        image = cv.resize(image, (width, max(int(h * scale), 2)),
                                          interpolation=cv.INTER_AREA)
                    ok, buf = cv.imencode(".jpg", image, [int(cv.IMWRITE_JPEG_QUALITY), 72])
                    if ok:
                        blob = bytes(buf.tobytes())
            except Exception:
                blob = None
        if blob is None:
            try:
                raw = path.read_bytes()
                blob = raw if len(raw) <= 600 * 1024 else None
            except Exception:
                blob = None
        if blob is None or self.used + len(blob) > self.budget:
            self.skipped += 1
            return ""
        self.used += len(blob)
        return "data:image/jpeg;base64," + base64.b64encode(blob).decode("ascii")

    def clip(self, rel: str) -> tuple[str, str]:
        """(data URI клипа или пусто, имя файла). Крупные клипы не вшиваем."""
        path = self.resolve(rel)
        if path is None:
            return "", ""
        try:
            size = path.stat().st_size
        except Exception:
            return "", ""
        if size > CLIP_EMBED_LIMIT or self.used + size > self.budget:
            self.skipped += 1
            return "", path.name
        try:
            blob = path.read_bytes()
        except Exception:
            return "", path.name
        self.used += len(blob)
        mime = "video/mp4" if path.suffix.lower() == ".mp4" else "video/x-msvideo"
        return f"data:{mime};base64," + base64.b64encode(blob).decode("ascii"), path.name


# ---------------------------------------------------------------------------
# SVG — рисуем руками, без библиотек
# ---------------------------------------------------------------------------
def _polar(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    rad = math.radians(deg)
    return cx + r * math.cos(rad), cy - r * math.sin(rad)


def _arc_path(cx: float, cy: float, r: float, a_from: float, a_to: float,
              width: float) -> str:
    """Дуга толщиной `width` от угла a_from до a_to (градусы, 180 — слева)."""
    r_out, r_in = r + width / 2.0, r - width / 2.0
    x1, y1 = _polar(cx, cy, r_out, a_from)
    x2, y2 = _polar(cx, cy, r_out, a_to)
    x3, y3 = _polar(cx, cy, r_in, a_to)
    x4, y4 = _polar(cx, cy, r_in, a_from)
    large = 1 if abs(a_to - a_from) > 180 else 0
    sweep_out = 1 if a_to < a_from else 0
    sweep_in = 0 if a_to < a_from else 1
    return (f"M {x1:.2f} {y1:.2f} A {r_out:.2f} {r_out:.2f} 0 {large} {sweep_out} {x2:.2f} {y2:.2f} "
            f"L {x3:.2f} {y3:.2f} A {r_in:.2f} {r_in:.2f} 0 {large} {sweep_in} {x4:.2f} {y4:.2f} Z")


def build_score_svg(integrity: dict[str, Any]) -> str:
    """Полукруглый индикатор доверия + стопка вкладов каналов. Чистый SVG."""
    score = _f(integrity.get("score"), 100.0)
    lost = _f(integrity.get("lost"))
    channels = integrity.get("channels") or []
    cx, cy, r, w = 170.0, 190.0, 125.0, 26.0

    zones = ((0, 40, "#fecaca"), (40, 65, "#fed7aa"), (65, 85, "#fef08a"), (85, 100, "#bbf7d0"))
    parts: list[str] = []
    for lo, hi, color in zones:
        a_from = 180.0 - lo * 1.8
        a_to = 180.0 - hi * 1.8
        parts.append(f'<path d="{_arc_path(cx, cy, r, a_from, a_to, w)}" fill="{color}"/>')
    # заполненная часть — тонкая дуга внутри шкалы, чтобы цвета зон остались видны
    a_score = 180.0 - max(min(score, 100.0), 0.0) * 1.8
    parts.append(f'<path d="{_arc_path(cx, cy, r - w / 2 - 7, 180.0, a_score, 8)}" '
                 f'fill="#0f172a"/>')
    nx, ny = _polar(cx, cy, r + w / 2 + 8, a_score)
    bx, by = _polar(cx, cy, r - w / 2 - 14, a_score)
    parts.append(f'<line x1="{bx:.1f}" y1="{by:.1f}" x2="{nx:.1f}" y2="{ny:.1f}" '
                 f'stroke="#0f172a" stroke-width="3"/>')
    parts.append(f'<text x="{cx}" y="{cy - 22}" text-anchor="middle" '
                 f'font-size="54" font-weight="700" fill="#0f172a">{_num(score)}</text>')
    parts.append(f'<text x="{cx - r - w / 2}" y="{cy + 20}" text-anchor="middle" '
                 f'font-size="12" fill="#94a3b8">0</text>')
    parts.append(f'<text x="{cx + r + w / 2}" y="{cy + 20}" text-anchor="middle" '
                 f'font-size="12" fill="#94a3b8">100</text>')
    parts.append(f'<text x="{cx}" y="{cy + 44}" text-anchor="middle" font-size="14" '
                 f'fill="#475569">из 100 — доверие к результату</text>')

    # ---- стопка вкладов каналов в потерянные баллы
    bar_x, bar_y, bar_w, bar_h = 370.0, 54.0, 380.0, 34.0
    parts.append(f'<text x="{bar_x}" y="{bar_y - 16}" font-size="14" fill="#334155" '
                 f'font-weight="600">Из чего сложились потерянные {_num(lost)} баллов</text>')
    if lost <= 0.05 or not channels:
        parts.append(f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="6" '
                     f'fill="#dcfce7" stroke="#86efac"/>')
        parts.append(f'<text x="{bar_x + bar_w / 2}" y="{bar_y + 22}" text-anchor="middle" '
                     f'font-size="13" fill="#166534">инцидентов, влияющих на оценку, нет</text>')
    else:
        x = bar_x
        for node in channels:
            seg = bar_w * (_f(node.get("lost")) / lost) if lost else 0.0
            if seg <= 0.4:
                continue
            parts.append(f'<rect x="{x:.1f}" y="{bar_y}" width="{seg:.1f}" height="{bar_h}" '
                         f'fill="{node.get("color", "#94a3b8")}"><title>'
                         f'{_esc(node.get("label"))}: -{_num(_f(node.get("lost")), 2)}</title></rect>')
            x += seg
        parts.append(f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="4" '
                     f'fill="none" stroke="#cbd5e1"/>')

    y = bar_y + bar_h + 28
    for node in channels[:6]:
        color = node.get("color", "#94a3b8")
        parts.append(f'<rect x="{bar_x}" y="{y - 10}" width="12" height="12" rx="3" fill="{color}"/>')
        parts.append(f'<text x="{bar_x + 20}" y="{y}" font-size="13" fill="#1e293b">'
                     f'{_esc(node.get("label"))}</text>')
        parts.append(f'<text x="{bar_x + 300}" y="{y}" font-size="13" fill="#475569">'
                     f'{_esc(node.get("count"))} шт.</text>')
        parts.append(f'<text x="{bar_x + bar_w}" y="{y}" text-anchor="end" font-size="13" '
                     f'font-weight="600" fill="#0f172a">-{_num(_f(node.get("lost")), 2)}</text>')
        y += 24
    if not channels:
        parts.append(f'<text x="{bar_x}" y="{y}" font-size="13" fill="#64748b">'
                     f'каналы не дали срабатываний</text>')

    body = "\n".join(parts)
    return (f'<svg viewBox="0 0 780 260" width="100%" role="img" '
            f'aria-label="Integrity score {_num(score)} из 100" '
            f'xmlns="http://www.w3.org/2000/svg" font-family="inherit">{body}</svg>')


def _time_ticks(t0: float, t1: float, target: int = 8) -> list[float]:
    span = max(t1 - t0, 1.0)
    for step in (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200):
        if span / step <= target:
            break
    ticks = []
    t = t0
    while t <= t1 + 1e-6:
        ticks.append(t)
        t += step
    if len(ticks) < 2:
        ticks = [t0, t1]
    return ticks


def build_timeline_svg(events: list[dict[str, Any]], t0: float, t1: float) -> str:
    """Полоса сессии с метками инцидентов по severity."""
    width, height = 1000.0, 215.0
    left, right = 56.0, 24.0
    plot = width - left - right
    span = max(t1 - t0, 1.0)
    band_y, band_h = 60.0, 26.0

    def x_of(ts: float) -> float:
        return left + plot * min(max((_f(ts) - t0) / span, 0.0), 1.0)

    parts: list[str] = [
        f'<text x="{left}" y="16" font-size="13" fill="#334155" font-weight="600">'
        f'Таймлайн сессии</text>',
        f'<text x="{left}" y="32" font-size="11" fill="#64748b">'
        f'каждая метка — зафиксированный инцидент, цвет = критичность</text>',
        f'<rect x="{left}" y="{band_y}" width="{plot}" height="{band_h}" rx="6" '
        f'fill="#e2e8f0"/>',
    ]
    for tick in _time_ticks(t0, t1):
        x = x_of(tick)
        parts.append(f'<line x1="{x:.1f}" y1="{band_y}" x2="{x:.1f}" '
                     f'y2="{band_y + band_h + 6}" stroke="#cbd5e1" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{band_y + band_h + 22}" text-anchor="middle" '
                     f'font-size="11" fill="#64748b">{_offset(tick, t0)}</text>')

    # подписи крупных инцидентов раскладываются по двум рядам, чтобы не слипаться
    rows: list[list[float]] = [[], []]
    row_y = (128.0, 160.0)
    label_parts: list[str] = []
    for event in events:
        kind = str(event.get("kind") or "")
        if kind in SERVICE_KINDS:
            continue
        severity = str(event.get("severity") or "info")
        color = SEVERITY_COLORS.get(severity, "#64748b")
        x = x_of(event.get("ts"))
        duration = _f(event.get("duration"))
        if duration > 0.5:
            w = max(plot * (duration / span), 1.5)
            parts.append(f'<rect x="{x:.1f}" y="{band_y + 2}" width="{w:.1f}" '
                         f'height="{band_h - 4}" fill="{color}" fill-opacity="0.35"/>')
        parts.append(f'<line x1="{x:.1f}" y1="{band_y - 6}" x2="{x:.1f}" '
                     f'y2="{band_y + band_h + 4}" stroke="{color}" stroke-width="2"/>')
        parts.append(f'<circle cx="{x:.1f}" cy="{band_y - 10}" r="5" fill="{color}">'
                     f'<title>{_clock(event.get("ts"))} — {_esc(_kind_label(kind))} '
                     f'({_esc(SEVERITY_LABELS.get(severity, severity))})</title></circle>')
        if SEVERITY_ORDER.get(severity, 0) < 3:
            continue
        row = next((i for i in (0, 1)
                    if all(abs(x - taken) > 190 for taken in rows[i])), None)
        if row is None:
            continue
        rows[row].append(x)
        anchor = "start" if x < width - 240 else "end"
        tx = x + (6 if anchor == "start" else -6)
        label = _kind_label(kind)
        if len(label) > 30:
            label = label[:29] + "…"
        y = row_y[row]
        label_parts.append(
            f'<line x1="{x:.1f}" y1="{band_y + band_h + 4}" x2="{x:.1f}" y2="{y - 11:.1f}" '
            f'stroke="{color}" stroke-width="1" stroke-dasharray="2 3"/>'
            f'<text x="{tx:.1f}" y="{y:.1f}" text-anchor="{anchor}" font-size="11" '
            f'fill="{color}" font-weight="600">{_esc(label)}</text>'
            f'<text x="{tx:.1f}" y="{y + 14:.1f}" text-anchor="{anchor}" font-size="10" '
            f'fill="#64748b">{_clock(event.get("ts"))}</text>')
    parts.extend(label_parts)

    legend_x = left
    parts.append(f'<text x="{legend_x}" y="202" font-size="11" fill="#64748b">Критичность:</text>')
    legend_x += 88
    for key in ("low", "medium", "high", "critical"):
        parts.append(f'<circle cx="{legend_x}" cy="198" r="5" fill="{SEVERITY_COLORS[key]}"/>')
        parts.append(f'<text x="{legend_x + 10}" y="202" font-size="11" fill="#475569">'
                     f'{SEVERITY_LABELS[key]}</text>')
        legend_x += 110

    body = "\n".join(parts)
    return (f'<svg viewBox="0 0 {width:.0f} {height:.0f}" width="100%" role="img" '
            f'aria-label="Таймлайн инцидентов сессии" '
            f'xmlns="http://www.w3.org/2000/svg" font-family="inherit">{body}</svg>')


def build_risk_svg(series: list[tuple[float, float]], t0: float, t1: float,
                   thresholds: tuple[float, float, float],
                   half_life: float = DEFAULT_HALF_LIFE) -> str:
    """График восстановленного risk-score с порогами реакции."""
    width, height = 1000.0, 230.0
    left, right, top, bottom = 56.0, 24.0, 34.0, 36.0
    plot_w = width - left - right
    plot_h = height - top - bottom
    span = max(t1 - t0, 1.0)

    def x_of(ts: float) -> float:
        return left + plot_w * min(max((ts - t0) / span, 0.0), 1.0)

    def y_of(value: float) -> float:
        return top + plot_h * (1.0 - min(max(value / 100.0, 0.0), 1.0))

    parts: list[str] = [
        f'<text x="{left}" y="20" font-size="13" fill="#334155" font-weight="600">'
        f'Risk-score во времени (восстановлен по журналу, half-life '
        f'{int(max(half_life, 1.0))} с)</text>',
        f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="#f8fafc" '
        f'stroke="#e2e8f0"/>',
    ]
    for value in (0, 25, 50, 75, 100):
        y = y_of(value)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
                     f'stroke="#e2e8f0"/>')
        parts.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
                     f'fill="#94a3b8">{value}</text>')
    for value, label, color in zip(thresholds, ("предупреждение", "пауза", "блокировка"),
                                   ("#ca8a04", "#ea580c", "#b91c1c")):
        y = y_of(value)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
                     f'stroke="{color}" stroke-width="1" stroke-dasharray="6 4"/>')
        parts.append(f'<text x="{left + plot_w - 4}" y="{y - 5:.1f}" text-anchor="end" '
                     f'font-size="10" fill="{color}">{label} ({_num(value, 0)})</text>')
    for tick in _time_ticks(t0, t1):
        x = x_of(tick)
        parts.append(f'<line x1="{x:.1f}" y1="{top + plot_h}" x2="{x:.1f}" '
                     f'y2="{top + plot_h + 5}" stroke="#cbd5e1"/>')
        parts.append(f'<text x="{x:.1f}" y="{top + plot_h + 20}" text-anchor="middle" '
                     f'font-size="11" fill="#64748b">{_offset(tick, t0)}</text>')

    if series:
        points = " ".join(f"{x_of(ts):.1f},{y_of(value):.1f}" for ts, value in series)
        first_x = x_of(series[0][0])
        last_x = x_of(series[-1][0])
        base_y = y_of(0)
        parts.append(f'<polygon points="{first_x:.1f},{base_y:.1f} {points} '
                     f'{last_x:.1f},{base_y:.1f}" fill="#1d4ed8" fill-opacity="0.12"/>')
        parts.append(f'<polyline points="{points}" fill="none" stroke="#1d4ed8" '
                     f'stroke-width="2"/>')
        peak_ts, peak = max(series, key=lambda item: item[1])
        if peak > 1.0:
            px, py = x_of(peak_ts), y_of(peak)
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="#1d4ed8"/>')
            anchor = "start" if px < width - 160 else "end"
            dx = 8 if anchor == "start" else -8
            parts.append(f'<text x="{px + dx:.1f}" y="{max(py - 12, top + 12):.1f}" '
                         f'text-anchor="{anchor}" font-size="11" fill="#1d4ed8" '
                         f'font-weight="600">пик {_num(peak)} ({_clock(peak_ts)})</text>')

    body = "\n".join(parts)
    return (f'<svg viewBox="0 0 {width:.0f} {height:.0f}" width="100%" role="img" '
            f'aria-label="График risk-score" xmlns="http://www.w3.org/2000/svg" '
            f'font-family="inherit">{body}</svg>')


# ---------------------------------------------------------------------------
# HTML-фрагменты
# ---------------------------------------------------------------------------
def _detail_rows(detail: dict[str, Any], limit: int = 14) -> str:
    rows: list[str] = []
    for key, value in list(detail.items())[:limit]:
        label = DETAIL_LABELS.get(str(key), str(key))
        if isinstance(value, float):
            shown = _num(value, 3)
        elif isinstance(value, bool):
            shown = "да" if value else "нет"
        elif isinstance(value, (dict, list)):
            shown = json.dumps(value, ensure_ascii=False)
        else:
            shown = str(value)
        if len(shown) > 220:
            shown = shown[:217] + "…"
        rows.append(f'<div class="kv"><span class="k">{_esc(label)}</span>'
                    f'<span class="v">{_esc(shown)}</span></div>')
    return "".join(rows) or '<div class="muted">подробности не записаны</div>'


def _evidence_cell(event: dict[str, Any], embed: _Embedder,
                   empty: str = '<span class="muted">—</span>') -> str:
    evidence = event.get("evidence") or {}
    if not isinstance(evidence, dict):
        return empty
    frame = str(evidence.get("frame_path") or "")
    clip = str(evidence.get("clip_path") or "")
    bits: list[str] = []
    uri = embed.image(frame) if frame else ""
    if uri:
        bits.append(f'<img class="thumb" src="{uri}" alt="кадр-доказательство" '
                    f'title="{_esc(frame)}">')
    elif frame:
        bits.append(f'<span class="muted">кадр: {_esc(Path(frame).name)}</span>')
    if clip:
        bits.append(f'<span class="muted">клип: {_esc(Path(clip).name)}</span>')
    if not bits:
        return empty
    return "".join(bits)


def build_header_html(meta: dict[str, Any], summary: dict[str, Any],
                      integrity: dict[str, Any], verdict: dict[str, str],
                      stats: dict[str, Any]) -> str:
    started = _f(summary.get("started_at") or meta.get("started_at"))
    ended = _f(summary.get("ended_at"))
    duration = _f(summary.get("duration_sec")) or max(ended - started, 0.0)
    rows = [
        ("Студент", (meta.get("student_name") or summary.get("student_name") or "—")),
        ("Идентификатор студента", (meta.get("student_id") or summary.get("student_id") or "—")),
        ("Экзамен", (meta.get("exam_id") or summary.get("exam_id") or "—")),
        ("Сессия", (meta.get("session_id") or summary.get("session_id") or "—")),
        ("Начало", _full_time(started) if started else "—"),
        ("Окончание", _full_time(ended) if ended else "сессия не закрыта штатно"),
        ("Длительность", _dur(duration) if duration else "—"),
        ("Причина завершения", (summary.get("end_reason") or "—")),
    ]
    cells = "".join(
        f'<div class="meta-item"><div class="meta-k">{_esc(k)}</div>'
        f'<div class="meta-v">{_esc(v)}</div></div>' for k, v in rows)
    chips = "".join(
        f'<span class="chip chip-{_esc(key)}">{_esc(SEVERITY_LABELS.get(key, key))}: '
        f'{_esc(value)}</span>'
        for key, value in (stats.get("by_severity") or {}).items() if value)
    return (
        f'<div class="verdict" style="--vc:{verdict["color"]}">'
        f'<div class="verdict-score">{_num(_f(integrity.get("score"), 100.0))}</div>'
        f'<div class="verdict-body"><div class="verdict-title">{_esc(verdict["title"])}</div>'
        f'<div class="verdict-text">{_esc(verdict["text"])}</div>'
        f'<div class="chips">{chips}</div></div></div>'
        f'<div class="meta-grid">{cells}</div>'
    )


def build_incidents_html(events: list[dict[str, Any]], t0: float,
                         embed: _Embedder) -> str:
    rows: list[str] = []
    for event in events:
        kind = str(event.get("kind") or "")
        if kind in SERVICE_KINDS:
            continue
        severity = str(event.get("severity") or "info")
        message = str(event.get("message") or "").strip() or _kind_label(kind)
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        extra = ""
        if detail:
            extra = (f'<details><summary>подробности детектора</summary>'
                     f'<div class="kv-list">{_detail_rows(detail)}</div></details>')
        duration = _f(event.get("duration"))
        rows.append(
            f'<tr>'
            f'<td class="nowrap"><div class="t-main">{_clock(event.get("ts"))}</div>'
            f'<div class="t-sub">{_offset(event.get("ts"), t0)}</div></td>'
            f'<td><span class="dot" style="background:'
            f'{CHANNEL_COLORS.get(str(event.get("channel")), "#94a3b8")}"></span>'
            f'{_esc(_channel_label(str(event.get("channel") or "")))}</td>'
            f'<td><div class="kind">{_esc(_kind_label(kind))}</div>'
            f'<div class="msg">{_esc(message)}</div>{extra}</td>'
            f'<td class="nowrap"><span class="sev sev-{_esc(severity)}">'
            f'{_esc(SEVERITY_LABELS.get(severity, severity))}</span></td>'
            f'<td class="num">{_num(_f(event.get("confidence"), 1.0) * 100, 0)}%</td>'
            f'<td class="num">{(_num(duration) + " с") if duration else "—"}</td>'
            f'<td>{_evidence_cell(event, embed)}</td>'
            f'</tr>')
    if not rows:
        return ('<p class="muted">За сессию не зафиксировано ни одного инцидента. '
                'Журнал содержит только служебные записи о начале и завершении.</p>')
    return (
        '<div class="scroll"><table class="tbl"><thead><tr>'
        '<th>Время</th><th>Канал</th><th>Инцидент и описание</th><th>Критичность</th>'
        '<th>Доверие</th><th>Длит.</th><th>Доказательство</th>'
        '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
    )


def build_fusion_html(events: list[dict[str, Any]], t0: float, embed: _Embedder) -> str:
    """Раздел связок: главное отличие системы, поэтому с подробным разбором."""
    fusion_kinds = _fusion_kinds()
    items = [e for e in events if str(e.get("kind") or "") in fusion_kinds]
    intro = (
        '<p class="lead">Связка — это не одно срабатывание, а совпадение сигналов разной '
        'природы на одной временной шкале. По отдельности «взгляд ушёл в сторону» и '
        '«быстро введён длинный ответ» не доказывают ничего; вместе, с точными '
        'интервалами, они образуют проверяемый факт. Ниже — разбор каждой связки с '
        'таймингами, как они были зафиксированы в момент экзамена.</p>'
    )
    if not items:
        return intro + ('<p class="muted">Связок за сессию не зафиксировано: совпадений '
                        'поведения ввода и CV-сигналов в окне корреляции не было.</p>')
    cards: list[str] = []
    for event in items:
        kind = str(event.get("kind") or "")
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        severity = str(event.get("severity") or "high")
        thumb = _evidence_cell(event, embed, empty="")
        raw = json.dumps(event.get("detail") or {}, ensure_ascii=False, indent=2,
                         sort_keys=True)
        cards.append(
            f'<article class="fusion-card">'
            f'<header><span class="sev sev-{_esc(severity)}">'
            f'{_esc(SEVERITY_LABELS.get(severity, severity))}</span>'
            f'<h3>{_esc(_kind_label(kind))}</h3>'
            f'<span class="fusion-time">{_clock(event.get("ts"))} '
            f'({_offset(event.get("ts"), t0)} от начала)</span></header>'
            f'<p class="fusion-msg">{_esc(str(event.get("message") or _kind_label(kind)))}</p>'
            f'<div class="fusion-body"><div class="kv-list">{_detail_rows(detail, limit=20)}</div>'
            f'<div class="fusion-ev">{thumb}</div></div>'
            f'<div class="fusion-foot">Уверенность связки: '
            f'{_num(_f(event.get("confidence"), 1.0) * 100, 0)}% · '
            f'запись цепочки #{_esc(event.get("_seq", "—"))} · '
            f'hash {_esc(str(event.get("_hash") or "")[:16])}…</div>'
            f'<details><summary>сырые данные связки (как записаны в журнал)</summary>'
            f'<pre>{_esc(raw)}</pre></details>'
            f'</article>')
    return intro + "".join(cards)


def build_integrity_html(chain: dict[str, Any], signature: dict[str, Any],
                         db_path: Path | None, report_name: str) -> str:
    ok = bool(chain.get("ok"))
    if ok:
        status = (f'<div class="chain-ok">Цепочка целостна: пересчитано '
                  f'{_esc(chain.get("checked", 0))} записей, расхождений нет.</div>')
    else:
        broken = chain.get("broken_at", -1)
        where = f" Первое расхождение — запись #{_esc(broken)}." if _f(broken, -1) >= 0 else ""
        status = (f'<div class="chain-bad">Цепочка НЕ целостна. '
                  f'{_esc(chain.get("reason") or "причина не определена")}.{where}</div>')
    rows = [
        ("Файл журнала", db_path.name if db_path else "—"),
        ("Записей в цепочке", chain.get("records", chain.get("checked", 0))),
        ("Из них инцидентов", chain.get("events_checked", chain.get("events", 0))),
        ("Genesis-хеш сессии", chain.get("genesis") or "—"),
        ("Хеш последней записи", chain.get("last_hash") or chain.get("chain_head") or "—"),
    ]
    if signature.get("available"):
        rows.extend([
            ("Алгоритм подписи", signature.get("algorithm", "Ed25519")),
            ("Публичный ключ", signature.get("public_key", "—")),
            ("Подпись головы цепочки", signature.get("chain_signature", "—")),
            ("Файл подписи отчёта", signature.get("sig_name", f"{report_name}.sig")),
            ("Файл публичного ключа", signature.get("pub_name", f"{report_name}.pub")),
        ])
    sign_note = (
        '<p>Подпись отчёта лежит рядом с ним отдельным файлом — подписать можно только '
        'уже готовый файл; публичный ключ тоже приложен. Приватный ключ остаётся на '
        'машине, где проходил экзамен, в комплект отчёта не входит и не нужен для '
        'проверки. Подпись головы цепочки выше вшита в сам отчёт: её можно сверить '
        'с хешем последней записи журнала.</p>'
        if signature.get("available") else
        f'<p class="warn">Подпись не сформирована: {_esc(signature.get("reason") or "нет пакета cryptography")}. '
        f'Hash-chain при этом работает и проверяется — подделать содержимое журнала '
        f'по-прежнему нельзя, но подтвердить авторство файла отчёта нечем.</p>'
    )
    table = "".join(
        f'<div class="kv"><span class="k">{_esc(k)}</span>'
        f'<span class="v mono">{_esc(v)}</span></div>' for k, v in rows)
    return (
        f'{status}'
        f'<p class="lead">Каждая запись журнала содержит <span class="mono">'
        f'hash = sha256(prev_hash + canonical_json(запись))</span>, а начало цепочки выведено '
        f'из метаданных сессии. Изменение любого байта, удаление, вставка или перестановка '
        f'записей ломают пересчёт и указывают номер первой испорченной записи.</p>'
        f'<div class="kv-list kv-wide">{table}</div>'
        f'{sign_note}'
        f'<p class="lead">Проверка одной командой:</p>'
        f'<pre>python3 scripts/verify_report.py {_esc(report_name)}</pre>'
    )


def build_limits_html(meta: dict[str, Any], events: list[dict[str, Any]],
                      chain: dict[str, Any]) -> str:
    """Честный дисклеймер: что система НЕ контролировала в этой сессии."""
    capabilities: dict[str, Any] = {}
    for event in events:
        if str(event.get("kind")) == "SESSION_STARTED":
            detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
            caps = detail.get("capabilities")
            if isinstance(caps, dict):
                capabilities = caps
            break
    names = {"vision": "видеоканал (объекты в кадре)", "gaze": "взгляд и поза головы",
             "identity": "непрерывная сверка личности", "audio": "аудиоканал",
             "env": "проверки окружения ОС"}
    off = [names[key] for key, value in capabilities.items()
           if key in names and not value]
    on = [names[key] for key, value in capabilities.items() if key in names and value]

    platform = sys.platform
    if platform == "darwin":
        plat_items = [
            "на macOS из пользовательского пространства не блокируются Cmd+Tab, "
            "Mission Control и системный снимок экрана — такие попытки только фиксируются;",
            "защита окна от записи экрана на macOS слабее, чем на Windows: "
            "возможна съёмка сторонним софтом с разрешением на запись экрана.",
        ]
    elif platform == "win32":
        plat_items = [
            "клавиши Win, Ctrl+Alt+Del и аппаратный PrtScn не перехватываются без "
            "драйвера уровня ядра — такие события только фиксируются;",
            "окно экзамена защищено от снимков средствами ОС, но не от съёмки внешней камерой.",
        ]
    else:
        plat_items = [
            "на этой платформе перехват системных сочетаний клавиш не реализован — "
            "подобные действия только фиксируются в журнале.",
        ]

    common = [
        "бумажная шпаргалка и любые записи вне поля зрения веб-камеры не детектируются;",
        "второй человек, находящийся вне кадра и не издающий звуков, не детектируется;",
        "телефон, физически вне поля зрения камеры (под столом, за монитором), "
        "виден только косвенно — по направлению взгляда;",
        "реалистичный deepfake в реальном времени выходит за рамки модели угроз;",
        "студент с правами администратора может воздействовать на сам клиент: "
        "такие попытки фиксируются (потеря датчика, обрыв связи), но не предотвращаются;",
        "аппаратный KVM и HDMI-сплиттер не отличимы от одного монитора;",
        "risk-score и integrity score — не вероятность списывания, а мера того, "
        "сколько зафиксировано отклонений от условий экзамена. Решение принимает "
        "преподаватель, а не система.",
    ]
    off_html = ""
    if off:
        off_html = ('<p class="warn">В этой сессии были отключены или недоступны каналы: '
                    + _esc(", ".join(off)) + '. Соответствующие нарушения зафиксированы '
                    'быть не могли.</p>')
    on_html = ""
    if on:
        on_html = f'<p class="muted">Активные каналы сессии: {_esc(", ".join(on))}.</p>'
    if not chain.get("available"):
        on_html += ('<p class="warn">Журнал прочитан не из SQLite-цепочки: '
                    f'{_esc(chain.get("reason") or "база недоступна")}.</p>')
    items = "".join(f"<li>{_esc(text)}</li>" for text in plat_items + common)
    return (f'{off_html}{on_html}'
            f'<p class="lead">Что система не контролировала (граница модели угроз, '
            f'заявленная разработчиком, а не обнаруженная проверяющим):</p>'
            f'<ul class="limits">{items}</ul>')


def build_breakdown_html(integrity: dict[str, Any], summary: dict[str, Any]) -> str:
    rows: list[str] = []
    for node in integrity.get("channels") or []:
        kinds = "".join(
            f'<div class="subitem"><span class="dot" style="background:{node["color"]}"></span>'
            f'{_esc(row["label"])} — {_esc(row["count"])} шт., вес {_num(row["weight"], 0)}, '
            f'вклад -{_num(_f(row.get("lost")), 2)}</div>'
            for row in node.get("kinds") or [])
        rows.append(
            f'<tr><td><span class="dot" style="background:{node["color"]}"></span>'
            f'{_esc(node["label"])}</td>'
            f'<td class="num">{_esc(node["count"])}</td>'
            f'<td class="num">{_num(_f(node.get("share")), 1)}%</td>'
            f'<td class="num strong">-{_num(_f(node.get("lost")), 2)}</td></tr>'
            f'<tr class="subrow"><td colspan="4">{kinds}</td></tr>')
    table = (
        '<div class="scroll"><table class="tbl"><thead><tr><th>Канал</th>'
        '<th>Срабатываний</th><th>Доля штрафа</th><th>Потеряно баллов</th></tr></thead>'
        '<tbody>' + "".join(rows) + '</tbody></table></div>'
        if rows else '<p class="muted">Штрафов нет: оценка доверия не снижалась.</p>'
    )
    final_risk = _f(summary.get("final_risk"))
    final_action = str(summary.get("final_action") or "none")
    actions = {"none": "действий не требовалось", "warn": "выдано предупреждение",
               "pause": "тест приостанавливался", "lock": "сессия заблокирована"}
    return (
        f'<p class="lead">Integrity score = 100 · exp(−штраф / {int(INTEGRITY_SCALE)}), где '
        f'штраф вида = вес · средняя уверенность · √(число срабатываний). Корень даёт '
        f'убывающую отдачу: повторы учитываются, но одно и то же нарушение не обнуляет '
        f'оценку. Суммарный штраф сессии — {_num(_f(integrity.get("penalty")), 2)}.</p>'
        f'{table}'
        f'<p class="muted">Мгновенный risk-score на момент завершения: '
        f'{_num(final_risk)} ({_esc(actions.get(final_action, final_action))}). Он затухает '
        f'со временем и показывает ситуацию «здесь и сейчас», тогда как integrity score '
        f'оценивает сессию целиком и не затухает.</p>'
    )


# ---------------------------------------------------------------------------
# Подпись Ed25519
# ---------------------------------------------------------------------------
def _ed25519() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519  # type: ignore
        return ed25519
    except Exception:
        return None


def _default_key_path() -> Path:
    return _ROOT / "keys" / "report_ed25519.key"


def ensure_keys(key_path: str | Path | None = None) -> tuple[Path, Path, Any, Any]:
    """Вернуть (приватный путь, публичный путь, приватный ключ, публичный ключ).

    Ключи генерируются при первом запуске в `keys/` (вне git). Приватный файл
    получает права 0600. Нет `cryptography` -> (пути, None, None).
    """
    ed = _ed25519()
    path = Path(str(key_path)) if key_path else _default_key_path()
    if path.is_dir():
        path = path / "report_ed25519.key"
    pub_path = path.with_suffix(path.suffix + ".pub") if path.suffix else Path(str(path) + ".pub")
    if ed is None:
        return path, pub_path, None, None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            private = ed.Ed25519PrivateKey.from_private_bytes(
                bytes.fromhex(path.read_text(encoding="utf-8").strip()))
        else:
            private = ed.Ed25519PrivateKey.generate()
            from cryptography.hazmat.primitives import serialization  # type: ignore
            raw = private.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption())
            path.write_text(raw.hex() + "\n", encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        public = private.public_key()
        from cryptography.hazmat.primitives import serialization  # type: ignore
        pub_hex = public.public_bytes(encoding=serialization.Encoding.Raw,
                                      format=serialization.PublicFormat.Raw).hex()
        pub_path.write_text(pub_hex + "\n", encoding="utf-8")
        return path, pub_path, private, public
    except Exception:
        return path, pub_path, None, None


def _public_hex(public: Any) -> str:
    try:
        from cryptography.hazmat.primitives import serialization  # type: ignore
        return public.public_bytes(encoding=serialization.Encoding.Raw,
                                   format=serialization.PublicFormat.Raw).hex()
    except Exception:
        return ""


def sign_report(path: str | Path, key_path: str | Path | None = None) -> str:
    """Подписать файл отчёта Ed25519. Возвращает путь к файлу подписи ('' если нечем).

    Рядом с отчётом появляются `<отчёт>.sig` (JSON: алгоритм, sha256, подпись)
    и `<отчёт>.pub` (публичный ключ в hex) — этого достаточно для проверки
    на любой машине без доступа к приватному ключу.
    """
    report = Path(str(path))
    if not report.is_file():
        return ""
    _priv_path, _pub_path, private, public = ensure_keys(key_path)
    if private is None:
        return ""
    import hashlib
    blob = report.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    signature = private.sign(blob)
    sig_path = report.with_name(report.name + ".sig")
    payload = {
        "algorithm": "Ed25519",
        "file": report.name,
        "sha256": digest,
        "signature": signature.hex(),
        "public_key": _public_hex(public),
        "signed_at": datetime.now().isoformat(timespec="seconds"),
    }
    sig_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report.with_name(report.name + ".pub").write_text(
        _public_hex(public) + "\n", encoding="utf-8")
    return str(sig_path)


def verify_report(path: str | Path, sig_path: str | Path | None = None,
                  pub_key_path: str | Path | None = None) -> bool:
    """Проверить подпись отчёта. Любая проблема -> False (подробности в verify_report_detailed)."""
    return bool(verify_report_detailed(path, sig_path, pub_key_path)["ok"])


def verify_report_detailed(path: str | Path, sig_path: str | Path | None = None,
                           pub_key_path: str | Path | None = None) -> dict[str, Any]:
    """Проверка подписи с человекочитаемой причиной отказа."""
    import hashlib
    out: dict[str, Any] = {"ok": False, "reason": "", "sha256": "", "public_key": "",
                           "signed_at": "", "sig_path": "", "pub_path": ""}
    report = Path(str(path))
    if not report.is_file():
        out["reason"] = f"файл отчёта не найден: {report}"
        return out
    sig = Path(str(sig_path)) if sig_path else report.with_name(report.name + ".sig")
    out["sig_path"] = str(sig)
    if not sig.is_file():
        out["reason"] = f"файл подписи не найден: {sig.name}"
        return out
    ed = _ed25519()
    if ed is None:
        out["reason"] = "пакет cryptography не установлен, подпись проверить нечем"
        return out

    raw = sig.read_text(encoding="utf-8").strip()
    data: dict[str, Any]
    try:
        parsed = json.loads(raw)
        data = parsed if isinstance(parsed, dict) else {}
    except Exception:
        data = {"signature": raw}
    sig_hex = str(data.get("signature") or "").strip()
    out["sha256"] = str(data.get("sha256") or "")
    out["signed_at"] = str(data.get("signed_at") or "")

    pub = Path(str(pub_key_path)) if pub_key_path else report.with_name(report.name + ".pub")
    out["pub_path"] = str(pub)
    pub_hex = ""
    if pub.is_file():
        pub_hex = pub.read_text(encoding="utf-8").strip()
    elif data.get("public_key"):
        pub_hex = str(data["public_key"]).strip()
        out["pub_path"] = f"{sig.name} (ключ внутри подписи)"
    if not pub_hex:
        out["reason"] = "публичный ключ не найден"
        return out
    out["public_key"] = pub_hex

    blob = report.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    if out["sha256"] and out["sha256"] != digest:
        out["reason"] = "файл отчёта изменён: sha256 не совпадает с записанным в подписи"
        out["sha256_actual"] = digest
        return out
    try:
        public = ed.Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        public.verify(bytes.fromhex(sig_hex), blob)
    except Exception as exc:
        out["reason"] = f"подпись недействительна: {exc}"
        return out
    out["ok"] = True
    out["reason"] = "подпись действительна"
    return out


def _sign_chain_head(chain: dict[str, Any], key_path: str | Path | None) -> dict[str, Any]:
    """Подписать голову цепочки до рендера — подпись попадает внутрь HTML."""
    info: dict[str, Any] = {"available": False, "reason": "", "algorithm": "Ed25519"}
    head = str(chain.get("last_hash") or chain.get("chain_head") or "")
    if _ed25519() is None:
        info["reason"] = "пакет cryptography не установлен"
        return info
    _priv, _pub, private, public = ensure_keys(key_path)
    if private is None:
        info["reason"] = "не удалось подготовить ключи в каталоге keys/"
        return info
    try:
        signature = private.sign(head.encode("utf-8")) if head else b""
    except Exception as exc:
        info["reason"] = f"подписать голову цепочки не удалось: {exc}"
        return info
    info.update({
        "available": True,
        "public_key": _public_hex(public),
        "chain_signature": signature.hex(),
        "chain_head": head,
    })
    return info


# ---------------------------------------------------------------------------
# Рендер
# ---------------------------------------------------------------------------
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*(?:\|\s*safe\s*)?\}\}")

_BUILTIN_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ page_title }}</title><style>{{ css }}</style></head>
<body><main class="page">
<header class="head"><div class="brand">Отчёт о прокторинге</div>
<h1>{{ page_title }}</h1><div class="sub">{{ generated_at }}</div></header>
{{ header_html }}
<section><h2>Integrity score и вклад каналов</h2>{{ score_svg }}{{ breakdown_html }}</section>
<section><h2>Таймлайн сессии</h2>{{ timeline_svg }}{{ risk_svg }}</section>
<section class="fusion"><h2>Связки сигналов (fusion)</h2>{{ fusion_html }}</section>
<section><h2>Инциденты</h2>{{ incidents_html }}</section>
<section><h2>Целостность записи</h2>{{ integrity_html }}</section>
<section><h2>Ограничения системы</h2>{{ limits_html }}</section>
<footer class="foot">{{ footer_html }}</footer>
</main></body></html>
"""

_CSS = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:#eef2f7;color:#0f172a;font:15px/1.55 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
.page{max-width:1080px;margin:0 auto;padding:28px 20px 56px}
.head{margin-bottom:22px}
.brand{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:#64748b}
h1{margin:6px 0 4px;font-size:27px;line-height:1.25}
h2{margin:0 0 14px;font-size:19px}
h3{margin:0;font-size:16px}
.sub{color:#64748b;font-size:13px}
.toc{display:flex;flex-wrap:wrap;gap:14px;margin-top:12px;font-size:13px}
.toc a{color:#1d4ed8;text-decoration:none;border-bottom:1px solid #bfdbfe}
section{background:#fff;border:1px solid #dbe2ea;border-radius:12px;padding:20px;
margin-bottom:18px;box-shadow:0 1px 2px rgba(15,23,42,.04)}
section.fusion{border-color:#fca5a5;box-shadow:0 2px 10px rgba(185,28,28,.08)}
section.fusion h2{color:#991b1b}
.verdict{display:flex;gap:18px;align-items:center;background:#fff;border:1px solid #dbe2ea;
border-left:6px solid var(--vc);border-radius:12px;padding:18px 20px;margin-bottom:14px}
.verdict-score{font-size:46px;font-weight:700;color:var(--vc);line-height:1;min-width:92px;
text-align:center}
.verdict-title{font-size:19px;font-weight:650;margin-bottom:4px}
.verdict-text{color:#475569;font-size:14px}
.chips{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:12px;padding:2px 9px;border-radius:999px;background:#f1f5f9;color:#334155;
border:1px solid #e2e8f0}
.chip-high{background:#fee2e2;border-color:#fecaca;color:#991b1b}
.chip-critical{background:#fecaca;border-color:#fca5a5;color:#7f1d1d}
.chip-medium{background:#fef3c7;border-color:#fde68a;color:#92400e}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:1px;
background:#dbe2ea;border:1px solid #dbe2ea;border-radius:12px;overflow:hidden;margin-bottom:18px}
.meta-item{background:#fff;padding:11px 14px}
.meta-k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#64748b}
.meta-v{font-size:15px;margin-top:2px;word-break:break-word}
.lead{color:#334155;font-size:14px;margin:0 0 14px}
.muted{color:#64748b;font-size:13px}
.warn{color:#9a3412;background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;
padding:9px 12px;font-size:13px}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table.tbl{width:100%;border-collapse:collapse;font-size:13.5px;min-width:720px}
table.tbl th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
color:#64748b;border-bottom:1px solid #dbe2ea;padding:8px 10px;white-space:nowrap}
table.tbl td{border-bottom:1px solid #eef2f7;padding:10px;vertical-align:top}
table.tbl tr.subrow td{border-bottom:1px solid #dbe2ea;padding-top:0}
.subitem{color:#475569;font-size:12.5px;padding:2px 0 2px 2px}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.strong{font-weight:650}
.nowrap{white-space:nowrap}
.t-main{font-variant-numeric:tabular-nums}
.t-sub{color:#94a3b8;font-size:11px}
.kind{font-weight:600}
.msg{color:#475569;font-size:13px;margin-top:2px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
.sev{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid}
.sev-info{background:#f1f5f9;border-color:#e2e8f0;color:#475569}
.sev-low{background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8}
.sev-medium{background:#fffbeb;border-color:#fde68a;color:#92400e}
.sev-high{background:#fef2f2;border-color:#fecaca;color:#b91c1c}
.sev-critical{background:#fee2e2;border-color:#fca5a5;color:#7f1d1d}
img.thumb{display:block;width:150px;max-width:100%;border-radius:6px;border:1px solid #cbd5e1;
margin-bottom:4px}
details{margin-top:6px}
summary{cursor:pointer;color:#1d4ed8;font-size:12.5px}
pre{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;overflow-x:auto;
font-size:12px;line-height:1.5}
.kv-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:6px 18px;
margin:8px 0}
.kv-wide{grid-template-columns:1fr}
.kv{display:flex;gap:10px;justify-content:space-between;border-bottom:1px dotted #dbe2ea;
padding:4px 0;font-size:13px}
.kv .k{color:#64748b}
.kv .v{text-align:right;word-break:break-all}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.chain-ok{background:#f0fdf4;border:1px solid #86efac;color:#166534;border-radius:8px;
padding:11px 14px;font-weight:600;margin-bottom:14px}
.chain-bad{background:#fef2f2;border:1px solid #fca5a5;color:#991b1b;border-radius:8px;
padding:11px 14px;font-weight:600;margin-bottom:14px}
.fusion-card{border:1px solid #fecaca;border-radius:10px;padding:14px 16px;margin-bottom:14px;
background:#fffafa}
.fusion-card header{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.fusion-time{color:#64748b;font-size:12px;margin-left:auto;font-variant-numeric:tabular-nums}
.fusion-msg{margin:0 0 10px;font-size:14.5px}
.fusion-body{display:flex;gap:18px;flex-wrap:wrap}
.fusion-body .kv-list{flex:1 1 320px}
.fusion-ev{flex:0 0 160px}
.fusion-foot{margin-top:8px;color:#64748b;font-size:12px}
ul.limits{margin:0;padding-left:20px;color:#334155;font-size:14px}
ul.limits li{margin-bottom:6px}
.foot{color:#64748b;font-size:12px;text-align:center;padding-top:6px}
@media print{body{background:#fff}section{break-inside:avoid;box-shadow:none}
details{display:none}}
"""


def _render(fragments: dict[str, str], template_dir: Path) -> str:
    """Jinja2, если есть; иначе string.Template по тому же шаблону; иначе встроенный."""
    template_path = template_dir / TEMPLATE_NAME
    source = ""
    if template_path.is_file():
        try:
            source = template_path.read_text(encoding="utf-8")
        except Exception:
            source = ""
    if not source:
        source = _BUILTIN_TEMPLATE

    try:
        import jinja2  # type: ignore
        env = jinja2.Environment(autoescape=False, keep_trailing_newline=True)
        return env.from_string(source).render(**fragments)
    except Exception:
        pass
    from string import Template
    converted = _PLACEHOLDER_RE.sub(lambda m: "${" + m.group(1) + "}", source)
    return Template(converted).safe_substitute(**fragments)


def build_report(session_dir: str | Path, db_path: str | Path | None = None,
                 out_html: str | Path | None = None,
                 config: dict[str, Any] | None = None,
                 sign: bool | None = None) -> str:
    """Собрать автономный HTML-отчёт по сессии. Возвращает путь к файлу.

    `sign=None` — подписать, если установлен `cryptography` (ключи создаются
    в `keys/` при первом запуске). `sign=False` — только HTML.
    """
    data = load_report_data(session_dir, db_path)
    cfg = config or {}
    report_cfg = cfg.get("report") if isinstance(cfg.get("report"), dict) else {}
    key_path = (report_cfg.get("key_path") or cfg.get("report_key_path") or None)

    out = Path(str(out_html)) if out_html else (data.session_dir / "report.html")
    out.parent.mkdir(parents=True, exist_ok=True)

    weights = _risk_weights()
    events = data.events
    meaningful = [e for e in events if str(e.get("kind")) not in SERVICE_KINDS]

    started = _f(data.summary.get("started_at") or data.meta.get("started_at"))
    if not started:
        started = _f(events[0].get("ts")) if events else time.time()
    ended = _f(data.summary.get("ended_at"))
    if not ended:
        ended = _f(events[-1].get("ts")) if events else started + 1.0
    if ended <= started:
        ended = started + max(_f(data.summary.get("duration_sec")), 60.0)

    integrity = compute_integrity(meaningful, weights)
    has_critical = any(str(e.get("severity")) == "critical" for e in meaningful)
    verdict = integrity_verdict(_f(integrity.get("score"), 100.0), has_critical)

    risk_cfg = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
    half_life = _f(risk_cfg.get("half_life") or cfg.get("risk_half_life"), DEFAULT_HALF_LIFE)
    series = risk_series(meaningful, started, ended, weights, half_life)

    by_severity: dict[str, int] = {}
    for event in meaningful:
        key = str(event.get("severity") or "info")
        by_severity[key] = by_severity.get(key, 0) + 1
    ordered_sev = {k: by_severity[k] for k in
                   sorted(by_severity, key=lambda s: -SEVERITY_ORDER.get(s, 0))}

    embed = _Embedder(data.session_dir)
    signature = _sign_chain_head(data.chain, key_path) if sign is not False else {
        "available": False, "reason": "подпись отключена конфигурацией"}
    signature.setdefault("sig_name", out.name + ".sig")
    signature.setdefault("pub_name", out.name + ".pub")

    student = str(data.meta.get("student_name") or data.meta.get("student_id") or "студент")
    exam = str(data.meta.get("exam_id") or "")
    page_title = f"{student}" + (f" — {exam}" if exam else "") + " — отчёт о прокторинге"

    fragments = {
        "page_title": _esc(page_title),
        "css": _CSS,
        "generated_at": _esc(f"Отчёт сформирован {_full_time(time.time())} · "
                             f"источники данных: {', '.join(data.sources) or 'нет'}"),
        "header_html": build_header_html(data.meta, data.summary, integrity, verdict,
                                        {"by_severity": ordered_sev}),
        "score_svg": build_score_svg(integrity),
        "breakdown_html": build_breakdown_html(integrity, data.summary),
        "timeline_svg": build_timeline_svg(meaningful, started, ended),
        "risk_svg": build_risk_svg(series, started, ended, _thresholds(), half_life),
        "fusion_html": build_fusion_html(events, started, embed),
        "incidents_html": build_incidents_html(events, started, embed),
        "integrity_html": build_integrity_html(data.chain, signature, data.db_path, out.name),
        "limits_html": build_limits_html(data.meta, events, data.chain),
    }
    fragments["footer_html"] = _esc(
        f"Инцидентов: {len(meaningful)} · записей в цепочке: "
        f"{data.chain.get('records', data.chain.get('checked', 0))} · "
        f"вшито доказательств: {round(embed.used / 1024)} КБ"
        + (f" (пропущено крупных: {embed.skipped})" if embed.skipped else "")
        + " · локальная система прокторинга, данные наружу не передавались"
    )

    html_text = _render(fragments, _HERE / "templates")
    out.write_text(html_text, encoding="utf-8")

    if sign is not False:
        try:
            sign_report(out, key_path)
        except Exception:
            pass
    return str(out)


__all__ = [
    "build_report", "sign_report", "verify_report", "verify_report_detailed",
    "load_report_data", "compute_integrity", "integrity_verdict", "risk_series",
    "ensure_keys", "build_score_svg", "build_timeline_svg", "build_risk_svg",
    "INTEGRITY_SCALE",
]

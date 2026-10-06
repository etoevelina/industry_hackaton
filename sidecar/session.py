"""
Состояние экзаменационной сессии и её каталог на диске.

Сессия — это: конечный автомат (idle / calibrating / running / paused / locked),
метаданные студента, каталог `sessions/<ts>_<student_id>/` с подкаталогом
`evidence/`, счётчики событий и сводка для отчёта.

Переходы валидируются: оболочка не может «продолжить» заблокированную сессию
или начать вторую поверх активной — это осознанно, чтобы доказательная база
не смешивалась между сессиями.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from protocol import ProctorEvent, Severity  # noqa: E402

_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")

#: Порядок строгости — нужен для «максимальной severity» в сводке.
SEVERITY_ORDER: dict[str, int] = {
    Severity.INFO.value: 0,
    Severity.LOW.value: 1,
    Severity.MEDIUM.value: 2,
    Severity.HIGH.value: 3,
    Severity.CRITICAL.value: 4,
}


class SessionState(str, Enum):
    IDLE = "idle"                # сессии нет
    CALIBRATING = "calibrating"  # идёт калибровка взгляда/личности/голоса
    RUNNING = "running"          # экзамен идёт, доказательства пишутся
    PAUSED = "paused"            # приостановлен прокторингом или оболочкой
    LOCKED = "locked"            # закрыт по риску, нужен отчёт и разбор


#: Разрешённые переходы автомата.
#: Блокировка достижима из любого живого состояния, включая калибровку: если
#: нарушение поймано, пока студент калибруется, сессию обязательно нужно закрыть.
#: Из LOCKED выход только в IDLE — снять блокировку может лишь session_end.
ALLOWED_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.IDLE: {SessionState.CALIBRATING, SessionState.RUNNING},
    SessionState.CALIBRATING: {SessionState.RUNNING, SessionState.PAUSED,
                               SessionState.LOCKED, SessionState.IDLE},
    SessionState.RUNNING: {SessionState.CALIBRATING, SessionState.PAUSED,
                           SessionState.LOCKED, SessionState.IDLE},
    SessionState.PAUSED: {SessionState.RUNNING, SessionState.LOCKED, SessionState.IDLE},
    SessionState.LOCKED: {SessionState.IDLE},
}


class SessionStateError(RuntimeError):
    """Недопустимый переход состояния сессии."""


def safe_id(value: str, limit: int = 48) -> str:
    """Сделать из произвольной строки безопасное имя каталога."""
    cleaned = _SAFE_RE.sub("_", (value or "").strip()) or "anon"
    return cleaned[:limit]


class Session:
    """Состояние сессии + её каталог. Потокобезопасна (внутренний lock)."""

    def __init__(
        self,
        sessions_root: str | Path,
        evidence_subdir: str = "evidence",
        db_filename: str = "evidence.sqlite",
        report_filename: str = "report.html",
    ) -> None:
        self.sessions_root = Path(sessions_root)
        self.evidence_subdir = evidence_subdir
        self.db_filename = db_filename
        self.report_filename = report_filename

        self._lock = threading.RLock()
        self._state = SessionState.IDLE

        self.student_id: str = ""
        self.student_name: str = ""
        self.exam_id: str = ""
        self.session_id: str = ""
        self.dir: Path | None = None
        self.started_at: float = 0.0
        self.ended_at: float = 0.0
        self.end_reason: str = ""
        self.history: list[dict[str, Any]] = []

        self.events_total: int = 0
        self.by_kind: dict[str, int] = {}
        self.by_severity: dict[str, int] = {}
        self.max_severity: str = Severity.INFO.value
        self.final_risk: float = 0.0
        self.final_action: str = "none"
        self._evidence_seq: int = 0

    # ---------------------------------------------------------------- состояние
    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    @property
    def active(self) -> bool:
        """Сессия существует (не idle)."""
        return self.state is not SessionState.IDLE

    @property
    def recording(self) -> bool:
        """Нужно ли писать доказательства прямо сейчас."""
        return self.state in (SessionState.RUNNING, SessionState.PAUSED,
                              SessionState.CALIBRATING, SessionState.LOCKED)

    def can(self, target: SessionState) -> bool:
        with self._lock:
            if target is self._state:
                return True
            return target in ALLOWED_TRANSITIONS.get(self._state, set())

    def transition(self, target: SessionState, reason: str = "") -> SessionState:
        """Перевести автомат. Недопустимый переход -> SessionStateError."""
        with self._lock:
            if target is self._state:
                return self._state
            if target not in ALLOWED_TRANSITIONS.get(self._state, set()):
                raise SessionStateError(
                    f"переход {self._state.value} -> {target.value} запрещён"
                )
            prev, self._state = self._state, target
            self.history.append({
                "ts": time.time(), "from": prev.value, "to": target.value, "reason": reason,
            })
            self._write_meta()
            return target

    # ------------------------------------------------------------ жизненный цикл
    def start(
        self,
        student_id: str,
        exam_id: str = "",
        student_name: str = "",
        calibrating: bool = False,
    ) -> Path:
        """Создать каталог сессии и перейти в running (или calibrating)."""
        with self._lock:
            if self.active:
                raise SessionStateError("сессия уже активна, сначала session_end")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.student_id = student_id or "anon"
            self.student_name = student_name or ""
            self.exam_id = exam_id or ""
            self.session_id = f"{stamp}_{safe_id(self.student_id)}"
            self.dir = self.sessions_root / self.session_id
            (self.dir / self.evidence_subdir).mkdir(parents=True, exist_ok=True)

            self.started_at = time.time()
            self.ended_at = 0.0
            self.end_reason = ""
            self.events_total = 0
            self.by_kind = {}
            self.by_severity = {}
            self.max_severity = Severity.INFO.value
            self.final_risk = 0.0
            self.final_action = "none"
            self._evidence_seq = 0
            self.history = []

            target = SessionState.CALIBRATING if calibrating else SessionState.RUNNING
            prev, self._state = self._state, target
            self.history.append({
                "ts": self.started_at, "from": prev.value, "to": target.value,
                "reason": "session_start",
            })
            self._write_meta()
            return self.dir

    def end(self, reason: str = "") -> dict[str, Any]:
        """Закрыть сессию, записать сводку, вернуться в idle."""
        with self._lock:
            if not self.active:
                return self.summary()
            self.ended_at = time.time()
            self.end_reason = reason or ""
            prev, self._state = self._state, SessionState.IDLE
            self.history.append({
                "ts": self.ended_at, "from": prev.value, "to": SessionState.IDLE.value,
                "reason": reason or "session_end",
            })
            summary = self.summary()
            self._write_json("summary.json", summary)
            self._write_meta()
            return summary

    # ------------------------------------------------------------------ события
    def note_event(self, event: ProctorEvent) -> None:
        """Учесть событие в счётчиках сессии."""
        with self._lock:
            self.events_total += 1
            kind = event.kind.value if hasattr(event.kind, "value") else str(event.kind)
            sev = event.severity.value if hasattr(event.severity, "value") else str(event.severity)
            self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
            self.by_severity[sev] = self.by_severity.get(sev, 0) + 1
            if SEVERITY_ORDER.get(sev, 0) > SEVERITY_ORDER.get(self.max_severity, 0):
                self.max_severity = sev

    def set_risk(self, score: float, action: str) -> None:
        with self._lock:
            self.final_risk = round(float(score), 2)
            self.final_action = str(action)

    # -------------------------------------------------------------------- пути
    @property
    def evidence_dir(self) -> Path | None:
        return None if self.dir is None else self.dir / self.evidence_subdir

    @property
    def db_path(self) -> Path | None:
        return None if self.dir is None else self.dir / self.db_filename

    @property
    def report_path(self) -> Path | None:
        return None if self.dir is None else self.dir / self.report_filename

    def evidence_file(self, prefix: str, ext: str = "jpg") -> tuple[Path, str] | None:
        """Уникальный путь под доказательство.

        Возвращает (абсолютный путь, путь относительно каталога сессии) —
        в `Evidence.frame_path` по протоколу кладётся относительный.
        """
        with self._lock:
            if self.dir is None:
                return None
            self._evidence_seq += 1
            name = f"{int(time.time() * 1000)}_{self._evidence_seq:04d}_{safe_id(prefix, 32)}.{ext}"
            rel = f"{self.evidence_subdir}/{name}"
            return self.dir / rel, rel

    # ------------------------------------------------------------------ сводки
    def meta(self) -> dict[str, Any]:
        """Метаданные для EvidenceStore.open_session()."""
        with self._lock:
            return {
                "session_id": self.session_id,
                "student_id": self.student_id,
                "student_name": self.student_name,
                "exam_id": self.exam_id,
                "started_at": self.started_at,
                "started_at_iso": _iso(self.started_at),
                "state": self._state.value,
                "session_dir": str(self.dir) if self.dir else "",
                "evidence_dir": str(self.evidence_dir) if self.dir else "",
                "db_path": str(self.db_path) if self.dir else "",
            }

    def summary(self) -> dict[str, Any]:
        with self._lock:
            duration = (self.ended_at or time.time()) - self.started_at if self.started_at else 0.0
            data = self.meta()
            data.update({
                "ended_at": self.ended_at,
                "ended_at_iso": _iso(self.ended_at) if self.ended_at else "",
                "duration_sec": round(max(duration, 0.0), 2),
                "end_reason": self.end_reason,
                "events_total": self.events_total,
                "events_by_kind": dict(self.by_kind),
                "events_by_severity": dict(self.by_severity),
                "max_severity": self.max_severity,
                "final_risk": self.final_risk,
                "final_action": self.final_action,
                "state_history": list(self.history),
            })
            return data

    # --------------------------------------------------------------------- диск
    def _write_meta(self) -> None:
        self._write_json("meta.json", self.meta())

    def _write_json(self, name: str, payload: dict[str, Any]) -> None:
        if self.dir is None:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:  # диск недоступен — сессия всё равно должна идти
            pass


def _iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except Exception:
        return ""


__all__ = ["Session", "SessionState", "SessionStateError", "ALLOWED_TRANSITIONS",
           "SEVERITY_ORDER", "safe_id"]

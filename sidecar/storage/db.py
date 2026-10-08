"""
Хранилище доказательной базы: SQLite + hash-chain.

Зачем так. Отчёт прокторинга имеет смысл только если его нельзя тихо
переписать. Поэтому каждая запись журнала связана хешем с предыдущей:

    hash_n = sha256(prev_hash || canonical_json(body_n))

Любое изменение байта в теле записи, подмена порядка записей, удаление или
вставка записи ломают цепочку, и `verify_chain()` возвращает номер первой
битой записи. Начало цепочки (genesis) выводится из метаданных сессии:

    genesis = sha256("qih-proctor-chain/v1" | session_id | canonical_json(meta))

то есть подменить студента или время начала, не сломав всё остальное, нельзя.

Канонический JSON детерминирован: `sort_keys=True`, `separators=(",", ":")`,
`ensure_ascii=False`. Хешируемое тело хранится в базе ровно той строкой, по
которой считался хеш, — проверка не зависит от версии Python и порядка dict.

Таблицы
-------
`sessions` — одна строка на сессию: кто, когда, genesis, голова цепочки,
             канонические `meta` (участвует в genesis) и `summary`.
`chain`    — сама цепочка: seq, тип записи, prev_hash, hash. Для записей
             `session_open` / `session_close` / `control` тело лежит здесь
             (`body`), для событий и наблюдений — в `events.payload` и
             `observations.payload` (не дублируем байты, по которым считается
             хеш: иначе правка «не того» экземпляра осталась бы незамеченной).
`events`   — развёрнутые поля инцидента для выборок и отчёта + `payload`
             (хешируемое тело). Развёрнутые колонки при проверке сверяются
             с payload, поэтому правка `events.message` тоже ловится.
`observations`
           — СЫРЫЕ наблюдения и телеметрия ввода: то, из чего движок событий
             складывал инциденты, и то, что раньше жило только в памяти
             процесса и умирало вместе с ним. Записи так же связаны хешем,
             как и события: журнал один, отдельного «неподписанного» слоя нет.

             Зачем. Отчёт отвечает не только на вопрос «что система сочла
             инцидентом», но и на вопрос «что она вообще видела». Без сырого
             слоя апелляция упирается в готовый вывод движка: пересчитать
             оценку по другому профилю весов можно, а проверить, не потерял ли
             движок контекст, — нет.

             Приватность — часть контракта таблицы, а не пожелание. Телеметрия
             ввода пишется БЕЗ символов: только интервалы между нажатиями,
             КЛАССЫ клавиш (буква / цифра / управляющая / навигация) и длины
             блоков. Восстановить по такой записи текст ответа студента
             нельзя — а именно это и было бы слежкой за содержанием работы, а
             не контролем условий экзамена. Фильтр стоит на входе в сайдкаре
             (`main.py`), и то, что сюда не попало, не попадёт и в отчёт.

Потокобезопасность: соединение на поток (`threading.local`), все записи
сериализованы через `RLock` (prev_hash — общий ресурс), база в режиме WAL
с `busy_timeout`. Модуль использует только stdlib.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("sidecar.storage.db")

#: Версия схемы пишется в PRAGMA user_version — отдельная таблица не нужна.
#: 2 — добавлена таблица `observations` (сырые наблюдения и телеметрия ввода).
SCHEMA_VERSION = 2

#: Домен хеширования: защищает от переноса genesis между системами.
GENESIS_DOMAIN = "qih-proctor-chain/v1"

#: Типы записей цепочки.
REC_SESSION_OPEN = "session_open"
REC_EVENT = "event"
REC_SESSION_CLOSE = "session_close"
#: Сырое наблюдение или пачка телеметрии ввода. Тело — в `observations.payload`.
REC_OBSERVATION = "observation"
#: Действие над экзаменом или над самим журналом: политика блокировки, решение
#: проктора, сброс риска, режим аутентификации канала. Тело — в `chain.body`.
REC_CONTROL = "control"

#: Типы записей, тело которых лежит НЕ в chain.body, а в своей таблице.
#: Байты, по которым считался хеш, не дублируются: иначе правка «не того»
#: экземпляра осталась бы незамеченной.
_BODY_TABLES: dict[str, str] = {
    REC_EVENT: "events",
    REC_OBSERVATION: "observations",
}

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    student_id     TEXT NOT NULL DEFAULT '',
    student_name   TEXT NOT NULL DEFAULT '',
    exam_id        TEXT NOT NULL DEFAULT '',
    started_at     REAL NOT NULL DEFAULT 0,
    started_at_iso TEXT NOT NULL DEFAULT '',
    ended_at       REAL NOT NULL DEFAULT 0,
    ended_at_iso   TEXT NOT NULL DEFAULT '',
    duration_sec   REAL NOT NULL DEFAULT 0,
    state          TEXT NOT NULL DEFAULT 'open',
    session_dir    TEXT NOT NULL DEFAULT '',
    genesis_hash   TEXT NOT NULL,
    chain_head     TEXT NOT NULL DEFAULT '',
    records        INTEGER NOT NULL DEFAULT 0,
    events_total   INTEGER NOT NULL DEFAULT 0,
    final_risk     REAL NOT NULL DEFAULT 0,
    final_action   TEXT NOT NULL DEFAULT 'none',
    meta           TEXT NOT NULL,
    summary        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chain (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    rec_type   TEXT NOT NULL,
    ref_id     TEXT NOT NULL DEFAULT '',
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL,
    body       TEXT,
    written_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    event_id   TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT '',
    severity   TEXT NOT NULL DEFAULT '',
    channel    TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    duration   REAL NOT NULL DEFAULT 0,
    message    TEXT NOT NULL DEFAULT '',
    frame_path TEXT NOT NULL DEFAULT '',
    clip_path  TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    seq        INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    obs_id     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL DEFAULT 0,
    window_sec REAL NOT NULL DEFAULT 0,
    samples    INTEGER NOT NULL DEFAULT 0,
    payload    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chain_session ON chain(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_obs_name ON observations(name);
"""


class EvidenceStoreError(RuntimeError):
    """Ошибка хранилища доказательств."""


# ---------------------------------------------------------------------------
# Канонизация и хеши
# ---------------------------------------------------------------------------
def _json_default(obj: Any) -> Any:
    """Привести к JSON то, что туда само не ложится (numpy, Enum, Path, bytes)."""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    # numpy-скаляры и всё, что умеет item()/float()
    for attr in ("item", "tolist"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return str(obj)


def canonical_json(payload: Any) -> str:
    """Детерминированная сериализация: sorted keys, без пробелов, UTF-8 как есть."""
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def chain_hash(prev_hash: str, body: str) -> str:
    """hash = sha256(prev_hash + canonical_json(body))."""
    return hashlib.sha256(((prev_hash or "") + body).encode("utf-8")).hexdigest()


def genesis_from_canonical(session_id: str, meta_canonical: str) -> str:
    """Genesis-хеш по уже канонизированным метаданным сессии."""
    blob = "|".join((GENESIS_DOMAIN, session_id, meta_canonical))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def genesis_hash(session_id: str, meta: dict[str, Any]) -> str:
    """Genesis-хеш сессии: привязывает цепочку к студенту, экзамену и времени."""
    return genesis_from_canonical(session_id, canonical_json(meta or {}))


def _as_dict(event: Any) -> dict[str, Any]:
    """ProctorEvent -> dict. Утиная типизация: модуль не зависит от protocol.py."""
    to_dict = getattr(event, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if isinstance(event, dict):
        return dict(event)
    if is_dataclass(event) and not isinstance(event, type):
        return asdict(event)
    raise EvidenceStoreError(f"не умею сериализовать событие типа {type(event)!r}")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _loads(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------
class EvidenceStore:
    """SQLite + hash-chain: каждая запись хранит hash(prev_hash + payload).

    Жизненный цикл: `open_session(meta)` -> N раз `append(event)` ->
    `close_session(summary)`. Чтение для отчёта: `list_sessions()`,
    `load_events()`, `verify_chain_detailed()`.
    """

    def __init__(self, config: dict[str, Any] | None = None,
                 db_path: str | Path | None = None) -> None:
        cfg = config or {}
        storage = cfg.get("storage") if isinstance(cfg.get("storage"), dict) else {}
        self._cfg: dict[str, Any] = dict(cfg)
        self._storage: dict[str, Any] = dict(storage or {})

        root = (self._storage.get("sessions_path") or cfg.get("sessions_path")
                or cfg.get("sessions_dir") or "sessions")
        self.sessions_root = Path(str(root))
        self.db_filename = str(self._storage.get("db_filename")
                               or cfg.get("db_filename") or "evidence.sqlite")

        self._lock = threading.RLock()
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []

        raw_db = db_path or self._storage.get("db_path") or cfg.get("db_path")
        self.db_path: Path | None = Path(str(raw_db)) if raw_db else None

        self.session_id: str = ""
        self.session_dir: str = ""
        self._genesis: str = ""
        self._prev_hash: str = ""
        self._records: int = 0
        self._events: int = 0
        #: Счётчик сырых наблюдений. В таблице `sessions` своей колонки не имеет
        #: намеренно: добавить её значило бы поменять состав `meta`/`summary`,
        #: а из них выводится genesis и код сверки у ВСЕХ будущих сессий.
        #: Фактическое число всегда считается по таблице (`stats()`).
        self._observations: int = 0
        self._closed: bool = False

    # ------------------------------------------------------------- доступность
    def available(self) -> bool:
        """sqlite3 — stdlib, хранилище доступно всегда."""
        return True

    @property
    def genesis(self) -> str:
        """genesis-хеш открытой сессии. Пусто — сессия не открыта.

        Нужен снаружи затем, что из genesis выводится код сверки сессии
        (`storage/handover.py`): код обязан быть детерминированным от
        содержимого цепочки, а не случайным, иначе проверить его нечем.
        """
        with self._lock:
            return self._genesis

    # ----------------------------------------------------------- соединение
    def _ensure_db(self) -> None:
        if self.db_path is None:
            raise EvidenceStoreError("путь к базе не задан")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn(create=True)
        with self._lock:
            conn.executescript(_DDL)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _conn(self, create: bool = False) -> sqlite3.Connection:
        """Соединение текущего потока. Разные потоки — разные соединения."""
        if self.db_path is None:
            raise EvidenceStoreError("хранилище не открыто: сначала open_session()")
        if not create and not self.db_path.is_file():
            raise EvidenceStoreError(f"база не найдена: {self.db_path}")
        key = str(self.db_path)
        conn = getattr(self._local, "conn", None)
        if conn is not None and getattr(self._local, "key", "") == key:
            return conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        conn = sqlite3.connect(key, timeout=15.0, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL",
                       "PRAGMA busy_timeout=15000"):
            try:
                conn.execute(pragma)
            except sqlite3.Error:
                pass  # WAL недоступен на сетевой ФС — работаем в journal-режиме
        self._local.conn = conn
        self._local.key = key
        with self._lock:
            self._conns.append(conn)
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Запись в одной транзакции под общим локом: цепочка не расходится."""
        with self._lock:
            conn = self._conn(create=True)
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            else:
                conn.execute("COMMIT")

    def close(self) -> None:
        """Закрыть все соединения (освобождает файл БД)."""
        with self._lock:
            for conn in self._conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._conns.clear()
        self._local = threading.local()

    # --------------------------------------------------------------- сессия
    def open_session(self, meta: dict[str, Any]) -> str:
        """Открыть сессию, создать БД и записать genesis-запись цепочки.

        Возвращает `session_id`. Путь к базе берётся из `meta["db_path"]`,
        иначе `meta["session_dir"]/<db_filename>`, иначе из конфига.
        """
        meta = dict(meta or {})
        session_id = _s(meta.get("session_id")) or f"session-{int(time.time())}"
        meta.setdefault("session_id", session_id)

        raw_db = meta.get("db_path")
        if raw_db:
            db_path = Path(str(raw_db))
        elif meta.get("session_dir"):
            db_path = Path(str(meta["session_dir"])) / self.db_filename
        else:
            db_path = self.sessions_root / session_id / self.db_filename

        with self._lock:
            self.db_path = db_path
            self._ensure_db()
            meta_canon = canonical_json(meta)
            row = self._conn().execute(
                "SELECT genesis_hash, chain_head, records, events_total, meta, summary "
                "FROM sessions WHERE session_id=?", (session_id,)).fetchone()

            if row is None:
                self._genesis = genesis_from_canonical(session_id, meta_canon)
                self._prev_hash = self._genesis
                self._records = 0
                self._events = 0
                self._observations = 0
                with self._tx() as conn:
                    conn.execute(
                        "INSERT INTO sessions(session_id, student_id, student_name, exam_id,"
                        " started_at, started_at_iso, state, session_dir, genesis_hash,"
                        " chain_head, meta)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (session_id, _s(meta.get("student_id")), _s(meta.get("student_name")),
                         _s(meta.get("exam_id")), _f(meta.get("started_at"), time.time()),
                         _s(meta.get("started_at_iso")), "open", _s(meta.get("session_dir")),
                         self._genesis, self._genesis, meta_canon),
                    )
            else:
                # Строка сессии уже есть. Продолжать цепочку можно ТОЛЬКО если
                # это буквально та же сессия и она ещё не закрыта.
                #
                # Почему это проверка, а не формальность. На общей папке
                # каталог сессии доступен всем, у кого есть право записи, —
                # ровно то право, которое `check_writable` доказывает при
                # старте. Прежний код на существующую строку молча продолжал
                # цепочку, а `close_session` перезаписывал `sessions.summary`.
                # Посторонний процесс мог «подхватить» сессию жертвы, дописать
                # в её журнал и закрыть его: запись `session_close` расходилась
                # со сводкой, проверка давала код 1, и ЖЕРТВА выглядела
                # фальсификатором. Атака не в свою пользу, а в чужой вред, и
                # стоила она одного права записи в чужой каталог.
                stored_meta = _s(row["meta"])
                if _s(row["summary"]):
                    raise EvidenceStoreError(
                        f"сессия «{session_id}» в этом журнале уже ЗАКРЫТА: дописывать "
                        f"в закрытую цепочку нельзя. Если это чужой каталог на общей "
                        f"папке — не трогайте его; если свой, начните новую сессию")
                if stored_meta and stored_meta != meta_canon:
                    raise EvidenceStoreError(
                        f"сессия «{session_id}» в этом журнале уже существует С ДРУГИМИ "
                        f"метаданными: совпадение идентификаторов при разном содержимом "
                        f"означает либо чужую сессию в этом каталоге, либо сдвиг часов. "
                        f"Цепочка не продолжена, чужие доказательства не затронуты")
                self._genesis = _s(row["genesis_hash"])
                self._prev_hash = _s(row["chain_head"]) or self._genesis
                self._records = int(row["records"] or 0)
                self._events = int(row["events_total"] or 0)
                counted = self._conn().execute(
                    "SELECT COUNT(*) AS n FROM observations WHERE session_id=?",
                    (session_id,)).fetchone()
                self._observations = int((counted["n"] if counted else 0) or 0)

            self.session_id = session_id
            self.session_dir = _s(meta.get("session_dir"))
            self._closed = False
            self._append_record(REC_SESSION_OPEN, session_id, {
                "type": REC_SESSION_OPEN,
                "session_id": session_id,
                "meta": meta,
                "ts": time.time(),
            })
        return session_id

    def append(self, event: Any) -> str:
        """Записать инцидент в цепочку. Возвращает hash записи."""
        data = _as_dict(event)
        with self._lock:
            if self.db_path is None or not self.session_id:
                # доказательство не должно пропасть из-за незакрытого протокола
                self.open_session({"session_id": f"orphan-{int(time.time())}",
                                   "note": "сессия не была открыта явно"})
            body = canonical_json(data)
            evidence = data.get("evidence") or {}
            if not isinstance(evidence, dict):
                evidence = {}
            row = {
                "event_id": _s(data.get("id")),
                "kind": _s(data.get("kind")),
                "severity": _s(data.get("severity")),
                "channel": _s(data.get("channel")),
                "ts": _f(data.get("ts")),
                "confidence": _f(data.get("confidence"), 1.0),
                "duration": _f(data.get("duration")),
                "message": _s(data.get("message")),
                "frame_path": _s(evidence.get("frame_path")),
                "clip_path": _s(evidence.get("clip_path")),
            }
            _seq, digest = self._append_record(
                REC_EVENT, row["event_id"], data, body=body, event_row=row)
            return digest

    def append_observation(self, obs: Any) -> str:
        """Записать СЫРОЕ наблюдение или пачку телеметрии. Возвращает hash записи.

        Тело кладётся в `observations.payload` и входит в ту же хеш-цепочку, что
        и события: подменить сырой слой, не сломав цепочку, нельзя.

        Фильтрация содержимого — забота вызывающего (см. `main.py`): сюда не
        должны приходить символы, набранный текст и имена клавиш. Хранилище не
        знает, что приватно, и не имеет права это угадывать.
        """
        data = _as_dict(obs)
        with self._lock:
            if self.db_path is None or not self.session_id:
                self.open_session({"session_id": f"orphan-{int(time.time())}",
                                   "note": "сессия не была открыта явно"})
            body = canonical_json(data)
            row = {
                "obs_id": _s(data.get("id")),
                "source": _s(data.get("source")),
                "name": _s(data.get("name")),
                "ts": _f(data.get("ts")),
                "window_sec": _f(data.get("window_sec")),
                "samples": int(_f(data.get("samples"))),
            }
            _seq, digest = self._append_record(
                REC_OBSERVATION, row["obs_id"], data, body=body, obs_row=row)
            return digest

    def append_control(self, name: str, payload: dict[str, Any]) -> str:
        """Записать действие над экзаменом или журналом. Возвращает hash записи.

        Сюда идут политика блокировки, решение проктора, сброс накопленного
        риска и режим аутентификации канала. Такая запись НЕИЗГЛАДИМА: удалить
        её из цепочки, не сломав все последующие хеши, невозможно — а именно
        это и требуется от записи «риск был обнулён, вот кем и когда».
        """
        body_obj = {
            "type": REC_CONTROL,
            "control": str(name or ""),
            "session_id": self.session_id,
            "ts": time.time(),
            "payload": dict(payload or {}),
        }
        with self._lock:
            if self.db_path is None or not self.session_id:
                self.open_session({"session_id": f"orphan-{int(time.time())}",
                                   "note": "сессия не была открыта явно"})
            _seq, digest = self._append_record(REC_CONTROL, str(name or ""), body_obj)
            return digest

    def close_session(self, summary: dict[str, Any]) -> None:
        """Записать итоговую запись цепочки и сводку сессии."""
        summary = dict(summary or {})
        with self._lock:
            if self.db_path is None or not self.session_id:
                return
            session_id = _s(summary.get("session_id")) or self.session_id
            summary.setdefault("session_id", session_id)
            summary_canon = canonical_json(summary)
            _seq, digest = self._append_record(REC_SESSION_CLOSE, session_id, {
                "type": REC_SESSION_CLOSE,
                "session_id": session_id,
                "summary": summary,
                "ts": time.time(),
            })
            with self._tx() as conn:
                conn.execute(
                    "UPDATE sessions SET ended_at=?, ended_at_iso=?, duration_sec=?,"
                    " state=?, final_risk=?, final_action=?, events_total=?, summary=?"
                    " WHERE session_id=?",
                    (_f(summary.get("ended_at"), time.time()), _s(summary.get("ended_at_iso")),
                     _f(summary.get("duration_sec")), "closed", _f(summary.get("final_risk")),
                     _s(summary.get("final_action")) or "none",
                     int(_f(summary.get("events_total"))) or self._events,
                     summary_canon, session_id),
                )
            self._closed = True
            self._prev_hash = digest
            # Сбрасываем WAL в основной файл и удаляем -wal/-shm. В режиме WAL
            # все записи сессии физически лежат в evidence.sqlite-wal, а сам
            # evidence.sqlite остаётся заголовком в 4 КБ. SQLite сливает их сам,
            # но только когда закроется последнее соединение, то есть при выходе
            # процесса. До этого момента каталог сессии не самодостаточен:
            # скопировали на флешку один evidence.sqlite рядом с report.html —
            # и преподаватель получил пустую базу вместо доказательной цепочки.
            # Делаем checkpoint сразу по закрытии сессии: дальше каталог можно
            # копировать как есть, даже если сайдкар ещё работает или его убили.
            try:
                self._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as exc:
                log.warning("не удалось слить WAL в основной файл базы: %s", exc)

    # ------------------------------------------------------- запись в цепочку
    def _append_record(self, rec_type: str, ref_id: str, body_obj: Any,
                       body: str | None = None,
                       event_row: dict[str, Any] | None = None,
                       obs_row: dict[str, Any] | None = None) -> tuple[int, str]:
        """Добавить запись цепочки.

        Тело события живёт в `events.payload`, тело наблюдения — в
        `observations.payload`, всё остальное — в `chain.body`.
        """
        canon = body if body is not None else canonical_json(body_obj)
        prev = self._prev_hash or self._genesis
        if not prev:
            raise EvidenceStoreError("цепочка не инициализирована: нет genesis")
        digest = chain_hash(prev, canon)
        now = time.time()
        store_body = None if rec_type in _BODY_TABLES else canon
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO chain(session_id, rec_type, ref_id, prev_hash, hash, body, written_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (self.session_id, rec_type, _s(ref_id), prev, digest, store_body, now),
            )
            seq = int(cur.lastrowid or 0)
            if rec_type == REC_EVENT:
                r = event_row or {}
                conn.execute(
                    "INSERT INTO events(seq, session_id, event_id, kind, severity, channel,"
                    " ts, confidence, duration, message, frame_path, clip_path, payload)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (seq, self.session_id, r.get("event_id", ""), r.get("kind", ""),
                     r.get("severity", ""), r.get("channel", ""), r.get("ts", 0.0),
                     r.get("confidence", 0.0), r.get("duration", 0.0), r.get("message", ""),
                     r.get("frame_path", ""), r.get("clip_path", ""), canon),
                )
                self._events += 1
            elif rec_type == REC_OBSERVATION:
                r = obs_row or {}
                conn.execute(
                    "INSERT INTO observations(seq, session_id, obs_id, source, name,"
                    " ts, window_sec, samples, payload) VALUES(?,?,?,?,?,?,?,?,?)",
                    (seq, self.session_id, r.get("obs_id", ""), r.get("source", ""),
                     r.get("name", ""), r.get("ts", 0.0), r.get("window_sec", 0.0),
                     int(r.get("samples", 0) or 0), canon),
                )
                self._observations += 1
            self._records += 1
            conn.execute(
                "UPDATE sessions SET chain_head=?, records=?, events_total=?"
                " WHERE session_id=?",
                (digest, self._records, self._events, self.session_id),
            )
        self._prev_hash = digest
        return seq, digest

    # ------------------------------------------------------------- проверка
    def verify_chain(self) -> tuple[bool, int]:
        """Пересчитать всю цепочку.

        Возвращает `(True, число проверенных записей)` либо
        `(False, seq первой битой записи)`. Подробности — `verify_chain_detailed()`.
        """
        result = self.verify_chain_detailed()
        if result["ok"]:
            return True, int(result["checked"])
        return False, int(result["broken_at"])

    def verify_chain_detailed(self, session_id: str | None = None) -> dict[str, Any]:
        """Полная проверка с человекочитаемой причиной.

        Что проверяется:
          1. первая запись сессии ссылается на genesis, выведенный из её meta;
          2. `prev_hash` каждой записи равен хешу предыдущей;
          3. `hash == sha256(prev_hash + payload)` — тело не менялось;
          4. развёрнутые колонки `events` и `observations` совпадают с
             хешируемым payload;
          5. `sessions.meta` / `sessions.summary` совпадают с телами записей
             открытия и закрытия;
          6. нет записей `events` / `observations` без своей записи в `chain`.
        """
        out: dict[str, Any] = {
            "ok": True, "checked": 0, "events_checked": 0, "observations_checked": 0,
            "controls_checked": 0, "broken_at": -1,
            "reason": "", "last_hash": "", "sessions": [], "db_path": str(self.db_path or ""),
        }
        if self.db_path is None or not self.db_path.is_file():
            out.update(ok=False, broken_at=0, reason="файл базы доказательств не найден")
            return out
        try:
            conn = self._conn()
        except Exception as exc:
            out.update(ok=False, broken_at=0, reason=f"база недоступна: {exc}")
            return out

        meta_rows = {
            _s(r["session_id"]): r for r in conn.execute("SELECT * FROM sessions")
        }
        payloads = {
            int(r["seq"]): r for r in conn.execute(
                "SELECT seq, session_id, event_id, kind, severity, channel, ts, confidence,"
                " duration, message, payload FROM events")
        }
        # Таблица наблюдений появилась в схеме 2. База, записанная прежней
        # версией, остаётся проверяемой: цепочка в ней просто не содержит
        # записей `observation`, и отсутствие таблицы — не повреждение.
        obs_payloads: dict[int, sqlite3.Row] = {}
        if _has_table(conn, "observations"):
            obs_payloads = {
                int(r["seq"]): r for r in conn.execute(
                    "SELECT seq, session_id, obs_id, source, name, ts, window_sec,"
                    " samples, payload FROM observations")
            }

        if session_id:
            rows = conn.execute(
                "SELECT * FROM chain WHERE session_id=? ORDER BY seq ASC",
                (session_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM chain ORDER BY seq ASC").fetchall()

        expected: dict[str, str] = {}
        seen_sessions: list[str] = []
        used_event_seq: set[int] = set()
        used_obs_seq: set[int] = set()
        #: Сессии, в которых уже встретилась запись закрытия. Нужна, чтобы
        #: поймать дописку ПОСЛЕ `session_close`.
        closed_sessions: set[str] = set()
        #: Сколько записей цепочки насчитали по каждой сессии — для сверки с
        #: колонкой `sessions.records`, которую раньше не сверял никто.
        counted: dict[str, int] = {}

        def fail(seq: int, reason: str) -> dict[str, Any]:
            out.update(ok=False, broken_at=int(seq), reason=reason)
            return out

        for row in rows:
            seq = int(row["seq"])
            sid = _s(row["session_id"])
            rec_type = _s(row["rec_type"])
            out["checked"] += 1
            counted[sid] = counted.get(sid, 0) + 1

            session_row = meta_rows.get(sid)
            if session_row is None:
                return fail(seq, f"запись #{seq} ссылается на неизвестную сессию «{sid}»")

            # ПОРЯДОК ТИПОВ ЗАПИСЕЙ. Сами по себе хеши его не задают: цепочка,
            # в которую инцидент дописан ПОСЛЕ `session_close`, пересчитывается
            # без расхождений, потому что каждая запись честно ссылается на
            # предыдущую. Проверять порядок нужно отдельно, и это не педантизм:
            # дописка после закрытия — способ вписать инцидент в журнал ДРУГОГО
            # студента, и единственным её следом оставалась колонка
            # `events_total`, которую обновлял тот же UPDATE.
            if sid in closed_sessions:
                return fail(seq, f"запись #{seq} типа «{rec_type}» добавлена ПОСЛЕ "
                                 f"закрытия сессии «{sid}»: журнал дописан задним "
                                 f"числом. Хеши при этом сходятся — связность цепочки "
                                 f"порядок записей не задаёт, поэтому он проверяется "
                                 f"отдельно")
            if sid not in expected and rec_type != REC_SESSION_OPEN:
                return fail(seq, f"первая запись сессии «{sid}» имеет тип «{rec_type}», "
                                 f"а должна быть «{REC_SESSION_OPEN}»: начало журнала "
                                 f"отрезано")
            if rec_type == REC_SESSION_CLOSE:
                closed_sessions.add(sid)

            if sid not in expected:
                seen_sessions.append(sid)
                genesis = genesis_from_canonical(sid, _s(session_row["meta"]))
                if genesis != _s(session_row["genesis_hash"]):
                    return fail(seq, "метаданные сессии изменены: genesis не сходится")
                bad_col = _session_meta_mismatch(session_row)
                if bad_col:
                    return fail(seq, f"в таблице sessions изменено поле «{bad_col}»: "
                                     f"оно не совпадает с защищёнными метаданными сессии")
                expected[sid] = genesis

            if _s(row["prev_hash"]) != expected[sid]:
                return fail(seq, f"запись #{seq}: prev_hash не совпадает с хешем предыдущей "
                                 f"(удаление, вставка или перестановка записей)")

            if rec_type == REC_EVENT:
                ev = payloads.get(seq)
                if ev is None:
                    return fail(seq, f"запись #{seq}: тело события отсутствует в таблице events")
                used_event_seq.add(seq)
                body = _s(ev["payload"])
                out["events_checked"] += 1
            elif rec_type == REC_OBSERVATION:
                ob = obs_payloads.get(seq)
                if ob is None:
                    return fail(seq, f"запись #{seq}: тело наблюдения отсутствует "
                                     f"в таблице observations")
                used_obs_seq.add(seq)
                body = _s(ob["payload"])
                out["observations_checked"] += 1
            else:
                if row["body"] is None:
                    return fail(seq, f"запись #{seq}: тело записи «{rec_type}» отсутствует")
                body = _s(row["body"])
                if rec_type == REC_CONTROL:
                    out["controls_checked"] += 1

            digest = chain_hash(_s(row["prev_hash"]), body)
            if digest != _s(row["hash"]):
                return fail(seq, f"запись #{seq}: тело записи изменено — хеш не совпадает")

            if rec_type == REC_EVENT:
                ev = payloads[seq]
                parsed = _loads(body)
                if not isinstance(parsed, dict):
                    return fail(seq, f"запись #{seq}: payload события не разбирается как JSON")
                mismatch = _event_columns_mismatch(ev, parsed)
                if mismatch:
                    return fail(seq, f"запись #{seq}: колонка «{mismatch}» расходится "
                                     f"с подписанным телом события")
            elif rec_type == REC_OBSERVATION:
                ob = obs_payloads[seq]
                parsed = _loads(body)
                if not isinstance(parsed, dict):
                    return fail(seq, f"запись #{seq}: payload наблюдения не разбирается "
                                     f"как JSON")
                mismatch = _observation_columns_mismatch(ob, parsed)
                if mismatch:
                    return fail(seq, f"запись #{seq}: колонка «{mismatch}» расходится "
                                     f"с подписанным телом наблюдения")
            else:
                parsed = _loads(body)
                if not isinstance(parsed, dict):
                    return fail(seq, f"запись #{seq}: тело записи не разбирается как JSON")
                if rec_type == REC_SESSION_OPEN:
                    if canonical_json(parsed.get("meta") or {}) != _s(session_row["meta"]):
                        return fail(seq, "метаданные сессии в таблице sessions изменены")
                elif rec_type == REC_SESSION_CLOSE:
                    stored = _s(session_row["summary"])
                    if stored and canonical_json(parsed.get("summary") or {}) != stored:
                        return fail(seq, "итоговая сводка сессии изменена")
                    bad_col = _session_summary_mismatch(session_row)
                    if bad_col:
                        return fail(seq, f"в таблице sessions изменено поле «{bad_col}»: "
                                         f"оно не совпадает с защищённой сводкой сессии")

            expected[sid] = _s(row["hash"])
            out["last_hash"] = _s(row["hash"])

        orphans = sorted(set(payloads) - used_event_seq)
        if orphans and not session_id:
            return fail(orphans[0], f"событие #{orphans[0]} не связано с цепочкой "
                                    f"(запись добавлена в обход журнала)")

        obs_orphans = sorted(set(obs_payloads) - used_obs_seq)
        if obs_orphans and not session_id:
            return fail(obs_orphans[0], f"наблюдение #{obs_orphans[0]} не связано с цепочкой "
                                        f"(запись добавлена в обход журнала)")

        for sid in seen_sessions:
            # Колонка `records` раньше не сверялась нигде: её возвращала одна
            # `UPDATE`, и дописанная запись просто увеличивала её вместе с
            # `events_total`. Сверяем с фактическим числом записей цепочки.
            declared_records = int(_f(meta_rows[sid]["records"]))
            if declared_records and declared_records != counted.get(sid, 0):
                return fail(-1, f"в таблице sessions у «{sid}» записано "
                                f"{declared_records} записей, а в цепочке их "
                                f"{counted.get(sid, 0)}: журнал или счётчик правили")
            head = _s(meta_rows[sid]["chain_head"])
            if head and head != expected.get(sid):
                out["sessions"].append({"session_id": sid, "head_ok": False})
                return fail(-1, f"голова цепочки сессии «{sid}» не совпадает с последней записью")
            out["sessions"].append({"session_id": sid, "head_ok": True,
                                    "chain_head": expected.get(sid, "")})

        if out["checked"] == 0:
            out.update(ok=False, broken_at=0, reason="цепочка пуста: записей нет")
        return out

    def last_hash(self, session_id: str | None = None) -> str:
        """Хеш последней записи цепочки (голова цепочки)."""
        if self.db_path is None or not self.db_path.is_file():
            return ""
        try:
            conn = self._conn()
        except Exception:
            return ""
        if session_id:
            row = conn.execute("SELECT hash FROM chain WHERE session_id=?"
                               " ORDER BY seq DESC LIMIT 1", (session_id,)).fetchone()
        else:
            row = conn.execute("SELECT hash FROM chain ORDER BY seq DESC LIMIT 1").fetchone()
        return _s(row["hash"]) if row else ""

    # --------------------------------------------------------------- чтение
    def list_sessions(self) -> list[dict[str, Any]]:
        """Все сессии базы, свежие первыми. meta/summary разобраны из JSON."""
        if self.db_path is None or not self.db_path.is_file():
            return []
        try:
            conn = self._conn()
        except Exception:
            return []
        rows = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC").fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            data = {k: row[k] for k in row.keys()}
            data["meta_parsed"] = _loads(row["meta"]) or {}
            data["summary_parsed"] = _loads(row["summary"]) or {}
            out.append(data)
        return out

    def load_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """События в порядке цепочки. Возвращает разобранный payload + seq/hash.

        Отчёт строится именно по payload: это те байты, по которым считался
        хеш, поэтому подмена развёрнутых колонок на отчёт не влияет (и ловится
        `verify_chain`).
        """
        if self.db_path is None or not self.db_path.is_file():
            return []
        try:
            conn = self._conn()
        except Exception:
            return []
        sql = ("SELECT e.seq AS seq, e.payload AS payload, c.hash AS hash,"
               " c.prev_hash AS prev_hash, e.session_id AS session_id"
               " FROM events e LEFT JOIN chain c ON c.seq = e.seq")
        args: tuple[Any, ...] = ()
        if session_id:
            sql += " WHERE e.session_id=?"
            args = (session_id,)
        sql += " ORDER BY e.seq ASC"
        out: list[dict[str, Any]] = []
        for row in conn.execute(sql, args):
            payload = _loads(row["payload"])
            if not isinstance(payload, dict):
                continue
            payload["_seq"] = int(row["seq"])
            payload["_hash"] = _s(row["hash"])
            payload["_session_id"] = _s(row["session_id"])
            out.append(payload)
        return out

    def load_observations(self, session_id: str | None = None,
                          source: str | None = None,
                          limit: int | None = None) -> list[dict[str, Any]]:
        """Сырые наблюдения в порядке цепочки: разобранный payload + seq/hash.

        `source` фильтрует слой (`telemetry`, `vision`, `gaze`, …). `limit`
        ограничивает выборку: отчёт показывает сводку, а не весь сырой поток.
        """
        if self.db_path is None or not self.db_path.is_file():
            return []
        try:
            conn = self._conn()
        except Exception:
            return []
        if not _has_table(conn, "observations"):
            return []
        sql = ("SELECT o.seq AS seq, o.payload AS payload, c.hash AS hash,"
               " o.session_id AS session_id FROM observations o"
               " LEFT JOIN chain c ON c.seq = o.seq")
        where: list[str] = []
        args: list[Any] = []
        if session_id:
            where.append("o.session_id=?")
            args.append(session_id)
        if source:
            where.append("o.source=?")
            args.append(source)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY o.seq ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        out: list[dict[str, Any]] = []
        for row in conn.execute(sql, tuple(args)):
            payload = _loads(row["payload"])
            if not isinstance(payload, dict):
                continue
            payload["_seq"] = int(row["seq"])
            payload["_hash"] = _s(row["hash"])
            payload["_session_id"] = _s(row["session_id"])
            out.append(payload)
        return out

    def observation_summary(self, session_id: str | None = None) -> dict[str, Any]:
        """Сводка сырого слоя для отчёта: сколько записей, по каким слоям, когда.

        Отчёт не печатает сырой поток целиком — он печатает ровно это: что
        слой есть, какого он объёма и за какой отрезок времени. Проверяющий,
        которому нужны сами наблюдения, берёт их из базы.
        """
        empty = {"available": False, "total": 0, "sources": [], "names": [],
                 "first_ts": 0.0, "last_ts": 0.0}
        if self.db_path is None or not self.db_path.is_file():
            return empty
        try:
            conn = self._conn()
        except Exception:
            return empty
        if not _has_table(conn, "observations"):
            return empty
        where = " WHERE session_id=?" if session_id else ""
        args: tuple[Any, ...] = (session_id,) if session_id else ()
        total = conn.execute(
            f"SELECT COUNT(*) AS n, MIN(ts) AS t0, MAX(ts) AS t1"
            f" FROM observations{where}", args).fetchone()
        sources = [
            {"source": _s(r["source"]), "count": int(r["n"] or 0),
             "samples": int(_f(r["samples"]))}
            for r in conn.execute(
                f"SELECT source, COUNT(*) AS n, SUM(samples) AS samples"
                f" FROM observations{where} GROUP BY source ORDER BY n DESC", args)
        ]
        names = [
            {"name": _s(r["name"]), "count": int(r["n"] or 0)}
            for r in conn.execute(
                f"SELECT name, COUNT(*) AS n FROM observations{where}"
                f" GROUP BY name ORDER BY n DESC LIMIT 40", args)
        ]
        return {
            "available": True,
            "total": int((total["n"] if total else 0) or 0),
            "first_ts": _f(total["t0"] if total else 0.0),
            "last_ts": _f(total["t1"] if total else 0.0),
            "sources": sources,
            "names": names,
        }

    def load_controls(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Записи `control` цепочки: политика, решения проктора, сбросы риска.

        Читаются из `chain.body`, то есть из тех самых байтов, по которым
        считался хеш. Отчёт берёт политику блокировки отсюда, а не из конфига:
        конфиг лежит рядом и правится, цепочка — нет.
        """
        if self.db_path is None or not self.db_path.is_file():
            return []
        try:
            conn = self._conn()
        except Exception:
            return []
        sql = ("SELECT seq, session_id, ref_id, hash, body, written_at FROM chain"
               " WHERE rec_type=?")
        args: list[Any] = [REC_CONTROL]
        if session_id:
            sql += " AND session_id=?"
            args.append(session_id)
        sql += " ORDER BY seq ASC"
        out: list[dict[str, Any]] = []
        for row in conn.execute(sql, tuple(args)):
            body = _loads(row["body"])
            if not isinstance(body, dict):
                continue
            body["_seq"] = int(row["seq"])
            body["_hash"] = _s(row["hash"])
            body["_session_id"] = _s(row["session_id"])
            out.append(body)
        return out

    def chain_records(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Служебный просмотр цепочки (для CLI проверки)."""
        if self.db_path is None or not self.db_path.is_file():
            return []
        try:
            conn = self._conn()
        except Exception:
            return []
        sql = ("SELECT seq, session_id, rec_type, ref_id, prev_hash, hash, written_at"
               " FROM chain")
        args: tuple[Any, ...] = ()
        if session_id:
            sql += " WHERE session_id=?"
            args = (session_id,)
        sql += " ORDER BY seq ASC"
        return [{k: r[k] for k in r.keys()} for r in conn.execute(sql, args)]

    def stats(self, session_id: str | None = None) -> dict[str, Any]:
        """Счётчики для отчёта: записей в цепочке, событий, наблюдений, голова."""
        empty = {"records": 0, "events": 0, "observations": 0, "controls": 0,
                 "chain_head": ""}
        if self.db_path is None or not self.db_path.is_file():
            return dict(empty)
        try:
            conn = self._conn()
        except Exception:
            return dict(empty)
        where = " WHERE session_id=?" if session_id else ""
        args: tuple[Any, ...] = (session_id,) if session_id else ()
        records = conn.execute(f"SELECT COUNT(*) AS n FROM chain{where}", args).fetchone()["n"]
        events = conn.execute(f"SELECT COUNT(*) AS n FROM events{where}", args).fetchone()["n"]
        observations = 0
        if _has_table(conn, "observations"):
            observations = int(conn.execute(
                f"SELECT COUNT(*) AS n FROM observations{where}", args).fetchone()["n"] or 0)
        ctl_where = " AND session_id=?" if session_id else ""
        controls = conn.execute(
            f"SELECT COUNT(*) AS n FROM chain WHERE rec_type=?{ctl_where}",
            (REC_CONTROL, *args)).fetchone()["n"]
        return {"records": int(records), "events": int(events),
                "observations": observations, "controls": int(controls or 0),
                "chain_head": self.last_hash(session_id)}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """Есть ли таблица. Нужно для баз, записанных прежней версией схемы."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _observation_columns_mismatch(row: sqlite3.Row, payload: dict[str, Any]) -> str:
    """Сверить развёрнутые колонки наблюдения с хешируемым payload."""
    for name, stored, expected in (
        ("obs_id", _s(row["obs_id"]), _s(payload.get("id"))),
        ("source", _s(row["source"]), _s(payload.get("source"))),
        ("name", _s(row["name"]), _s(payload.get("name"))),
    ):
        if stored != expected:
            return name
    for name, key in (("ts", "ts"), ("window_sec", "window_sec")):
        if abs(_f(row[name]) - _f(payload.get(key))) > 1e-6:
            return name
    if int(_f(row["samples"])) != int(_f(payload.get("samples"))):
        return "samples"
    return ""


def _session_meta_mismatch(row: sqlite3.Row) -> str:
    """Развёрнутые колонки сессии против защищённых genesis-метаданных."""
    meta = _loads(row["meta"])
    if not isinstance(meta, dict):
        return "meta"
    for column, key in (("student_id", "student_id"), ("student_name", "student_name"),
                        ("exam_id", "exam_id"), ("session_dir", "session_dir")):
        if _s(row[column]) != _s(meta.get(key)):
            return column
    if "started_at" in meta and abs(_f(row["started_at"]) - _f(meta.get("started_at"))) > 1e-6:
        return "started_at"
    return ""


def _session_summary_mismatch(row: sqlite3.Row) -> str:
    """Развёрнутые колонки итогов против защищённой сводки сессии."""
    summary = _loads(row["summary"])
    if not isinstance(summary, dict):
        return ""
    if _s(row["final_action"]) != (_s(summary.get("final_action")) or "none"):
        return "final_action"
    for column, key in (("ended_at", "ended_at"), ("duration_sec", "duration_sec"),
                        ("final_risk", "final_risk")):
        if key in summary and abs(_f(row[column]) - _f(summary.get(key))) > 1e-6:
            return column
    if "ended_at_iso" in summary and _s(row["ended_at_iso"]) != _s(summary.get("ended_at_iso")):
        return "ended_at_iso"
    if summary.get("events_total") is not None:
        expected = int(_f(summary.get("events_total")))
        if expected and int(_f(row["events_total"])) != expected:
            return "events_total"
    return ""


def _event_columns_mismatch(row: sqlite3.Row, payload: dict[str, Any]) -> str:
    """Сверить развёрнутые колонки события с хешируемым payload."""
    checks: tuple[tuple[str, Any, Any], ...] = (
        ("event_id", _s(row["event_id"]), _s(payload.get("id"))),
        ("kind", _s(row["kind"]), _s(payload.get("kind"))),
        ("severity", _s(row["severity"]), _s(payload.get("severity"))),
        ("channel", _s(row["channel"]), _s(payload.get("channel"))),
        ("message", _s(row["message"]), _s(payload.get("message"))),
    )
    for name, stored, expected in checks:
        if stored != expected:
            return name
    for name, key in (("ts", "ts"), ("confidence", "confidence"), ("duration", "duration")):
        if abs(_f(row[name]) - _f(payload.get(key))) > 1e-6:
            return name
    return ""


__all__ = [
    "EvidenceStore", "EvidenceStoreError", "canonical_json", "chain_hash",
    "genesis_hash", "genesis_from_canonical", "SCHEMA_VERSION", "GENESIS_DOMAIN",
    "REC_SESSION_OPEN", "REC_EVENT", "REC_SESSION_CLOSE", "REC_OBSERVATION",
    "REC_CONTROL",
]

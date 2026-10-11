"""SQLite persistence: state, append-only audit log and notification
outbox (PRD §11.3, §12; NFR-008, NFR-009).

WAL mode with ``synchronous=FULL``: an order intent is on disk before
the request leaves the process (FR-047). The database is the durable
record; there are no JSON state files and a damaged database is never
treated as empty (fixes LIM-14).

All calls are synchronous and short. They run on the event loop thread,
which makes every single call atomic with respect to other coroutines.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Optional, Union

from hlbot.domain.states import (
    ORDER_TERMINAL,
    SIGNAL_TERMINAL,
    TRADE_TERMINAL,
    IllegalTransition,
    check_transition,
)
from hlbot.logging_setup import get_correlation_id
from hlbot.redact import redact
from hlbot.timeutil import Clock

logger = logging.getLogger("hlbot.db")

Row = dict[str, Any]

_TABLES = frozenset(
    {
        "kv",
        "config_versions",
        "account_snapshots",
        "messages",
        "signals",
        "plans",
        "risk_assessments",
        "trades",
        "orders",
        "fills",
        "position_snapshots",
        "funding_payments",
        "day_state",
        "control_state",
        "conditions",
        "audit_events",
        "outbox",
    }
)


class DatabaseError(Exception):
    pass


class DatabaseCorrupt(DatabaseError):
    """The database failed its integrity check. Restore from backup;
    the bot never starts fresh on its own."""


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads(value: Optional[str], default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def _migrations() -> list[tuple]:
    found = []
    for entry in resources.files("hlbot.db").joinpath("migrations").iterdir():
        if entry.name.endswith(".sql"):
            found.append((int(entry.name.split("_", 1)[0]), entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found)


class Database:
    def __init__(self, path: Union[str, Path], clock: Optional[Clock] = None, *, readonly: bool = False):
        self.path = Path(path)
        self.clock = clock or Clock()
        self.readonly = readonly
        self._depth = 0
        if readonly:
            if not self.path.exists():
                raise DatabaseError(f"database not found: {self.path}")
            self.conn = self._open_readonly()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.path.exists() and self.path.stat().st_size > 0
            self.conn = sqlite3.connect(str(self.path), isolation_level=None)
            if existed:
                self._integrity_check()
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            try:
                self.path.chmod(0o600)
            except OSError:  # pragma: no cover - platform dependent
                pass
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.row_factory = sqlite3.Row

    def _open_readonly(self) -> sqlite3.Connection:
        """A connection that cannot write. A WAL-mode database whose
        ``-shm`` file does not exist (the bot is stopped) cannot always
        be opened with ``mode=ro``, because the reader has to create that
        file; whether it works depends on the SQLite build. Fall back to
        a normal connection with ``query_only`` set, which still refuses
        every write."""
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, isolation_level=None)
        try:
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            return conn
        except sqlite3.OperationalError:
            conn.close()
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _integrity_check(self) -> None:
        try:
            result = self.conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise DatabaseCorrupt(f"{self.path} is not a readable database: {exc}") from exc
        if not result or result[0] != "ok":
            raise DatabaseCorrupt(f"{self.path} failed its integrity check: {result[0] if result else '?'}")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ schema
    def migrate(self) -> int:
        """Apply pending forward-only migrations. Returns the schema version."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        applied = {row[0] for row in self.conn.execute("SELECT version FROM schema_migrations")}
        version = max(applied, default=0)
        for number, name, sql in _migrations():
            if number in applied:
                continue
            self.conn.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")
            self.conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (number, name, self.clock.iso()),
            )
            version = number
            logger.info("applied migration %s", name)
        if not self.one("SELECT 1 FROM control_state WHERE id = 1"):
            self.conn.execute(
                "INSERT INTO control_state (id, mode, reason, set_by, set_at) VALUES (1, 'RUNNING', NULL, 'init', ?)",
                (self.clock.iso(),),
            )
        return version

    def schema_version(self) -> int:
        row = self.one("SELECT MAX(version) AS v FROM schema_migrations")
        return int(row["v"] or 0) if row else 0

    # ------------------------------------------------------------ primitives
    @contextmanager
    def tx(self) -> Iterator[None]:
        """One atomic, durable transaction. Re-entrant."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._depth = 0

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Row]:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return default if row is None or row[0] is None else row[0]

    def insert(self, table: str, **fields: Any) -> int:
        assert table in _TABLES, table
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cursor = self.conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(fields.values()))
        return int(cursor.lastrowid or 0)

    def update(self, table: str, row_id: int, **fields: Any) -> None:
        assert table in _TABLES, table
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self.conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", (*fields.values(), row_id))

    def get(self, table: str, row_id: int) -> Optional[Row]:
        assert table in _TABLES, table
        return self.one(f"SELECT * FROM {table} WHERE id = ?", (row_id,))

    # ------------------------------------------------------------ kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, dumps(value), self.clock.iso()),
        )

    # ------------------------------------------------------------ audit
    def audit(
        self,
        kind: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        signal_id: Optional[int] = None,
        trade_id: Optional[int] = None,
        order_id: Optional[int] = None,
        correlation: Optional[str] = None,
    ) -> int:
        """Append one audit event. Payloads pass through redaction.

        Everything that belongs to one signal shares the correlation id
        ``sig-<signal id>``, whichever task or process run wrote it, so
        the whole chain from message to fill can be followed (FR-090).
        Events not tied to a signal use the ambient correlation id."""
        if correlation is None:
            root = signal_id
            if root is None and trade_id is not None:
                root = self.scalar("SELECT signal_id FROM trades WHERE id = ?", (trade_id,))
            if root is None and order_id is not None:
                root = self.scalar(
                    "SELECT t.signal_id FROM orders o JOIN trades t ON t.id = o.trade_id WHERE o.id = ?", (order_id,)
                )
            correlation = f"sig-{root}" if root is not None else get_correlation_id()
        return self.insert(
            "audit_events",
            at=self.clock.iso(),
            kind=kind,
            correlation_id=correlation,
            signal_id=signal_id,
            trade_id=trade_id,
            order_id=order_id,
            payload=dumps(redact(payload or {})),
        )

    # ------------------------------------------------------------ state machines
    def set_signal_state(self, signal_id: int, new: str, reason: Optional[str] = None, **fields: Any) -> bool:
        with self.tx():
            row = self.get("signals", signal_id)
            if row is None:
                raise DatabaseError(f"signal {signal_id} not found")
            current = row["state"]
            if current == new:
                self.update("signals", signal_id, updated_at=self.clock.iso(), **fields)
                return False
            if current in SIGNAL_TERMINAL:
                raise IllegalTransition("signal", current, new)
            check_transition("signal", current, new)
            self.update("signals", signal_id, state=new, reason=reason, updated_at=self.clock.iso(), **fields)
            self.audit("signal.state", {"from": current, "to": new, "reason": reason}, signal_id=signal_id)
            return True

    def set_order_state(self, order_id: int, new: str, **fields: Any) -> bool:
        """Move an order to ``new``. A terminal order is immutable: a later
        contradictory report is recorded as an audit event and ignored
        (PRD §12.3). Returns True when the state changed."""
        with self.tx():
            row = self.get("orders", order_id)
            if row is None:
                raise DatabaseError(f"order {order_id} not found")
            current = row["state"]
            if current == new:
                self.update("orders", order_id, updated_at=self.clock.iso(), **fields)
                return False
            if current in ORDER_TERMINAL:
                self.audit(
                    "order.contradiction",
                    {"state": current, "reported": new, "fields": fields},
                    trade_id=row["trade_id"],
                    order_id=order_id,
                )
                return False
            check_transition("order", current, new)
            self.update("orders", order_id, state=new, updated_at=self.clock.iso(), **fields)
            self.audit(
                "order.state",
                {
                    "from": current,
                    "to": new,
                    "role": row["role"],
                    "cloid": row["cloid"],
                    **{k: v for k, v in fields.items() if k in ("exchange_status", "error_text", "oid", "filled_size")},
                },
                trade_id=row["trade_id"],
                order_id=order_id,
            )
            return True

    def set_trade_state(self, trade_id: int, new: str, **fields: Any) -> bool:
        with self.tx():
            row = self.get("trades", trade_id)
            if row is None:
                raise DatabaseError(f"trade {trade_id} not found")
            current = row["state"]
            if current == new and new != "REDUCING":
                self.update("trades", trade_id, updated_at=self.clock.iso(), **fields)
                return False
            if current in TRADE_TERMINAL:
                raise IllegalTransition("trade", current, new)
            check_transition("trade", current, new)
            self.update("trades", trade_id, state=new, updated_at=self.clock.iso(), **fields)
            self.audit(
                "trade.state",
                {"from": current, "to": new, **{k: v for k, v in fields.items() if k in ("close_reason", "outcome")}},
                signal_id=row["signal_id"],
                trade_id=trade_id,
            )
            return True

    # ------------------------------------------------------------ maintenance
    def backup(self, destination: Union[str, Path]) -> Path:
        """Consistent online backup (NFR-008)."""
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
            # One self-contained file: no -wal / -shm companions to lose
            # or forget when the backup is copied elsewhere.
            target.execute("PRAGMA journal_mode=DELETE").fetchone()
            target.commit()
        finally:
            target.close()
        # Some SQLite builds leave the shared-memory index behind. It is a
        # rebuildable cache, never data; an empty write-ahead log likewise.
        shm = dest.with_name(dest.name + "-shm")
        wal = dest.with_name(dest.name + "-wal")
        shm.unlink(missing_ok=True)
        if wal.exists() and wal.stat().st_size == 0:
            wal.unlink()
        try:
            dest.chmod(0o600)
        except OSError:  # pragma: no cover
            pass
        return dest

    def prune_snapshots(self, keep_days: int = 14) -> None:
        from datetime import timedelta

        cutoff = (self.clock.now() - timedelta(days=keep_days)).isoformat(timespec="milliseconds")
        self.conn.execute("DELETE FROM position_snapshots WHERE at < ?", (cutoff,))
        self.conn.execute("DELETE FROM account_snapshots WHERE snapshot_at < ?", (cutoff,))

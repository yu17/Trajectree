"""The transactional ledger: every mutation is one SQLite transaction recorded as a changeset."""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import apsw

from trajectree.db.changes import materialize
from trajectree.db.schema import LEDGER_TABLES
from trajectree.ids import new_id
from trajectree.models import Actor, Change, Txn
from trajectree.timeutil import from_db, to_db, utcnow

_TXN_COLUMNS: tuple[str, ...] = (
    "id",
    "ts",
    "actor_kind",
    "actor_ref",
    "model",
    "backend",
    "tool",
    "tool_version",
    "session_id",
    "run_id",
    "proposed_by_run",
    "intent",
    "changeset",
    "review",
    "review_ts",
    "reverts_txn",
    "reverted_by",
)
_TXN_SELECT = f"SELECT {', '.join(_TXN_COLUMNS)} FROM txn"


@dataclass(frozen=True)
class TxnHandle:
    id: str
    conn: apsw.Connection


def review_for(actor: Actor, auto_accept: bool) -> str:
    """Human writes and explicitly auto-accepted writes are accepted; the rest await review."""
    return "accepted" if auto_accept or actor.kind == "human" else "pending"


def _json_or_null(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value)


def _txn_from_row(row: tuple[Any, ...]) -> Txn:
    data = dict(zip(_TXN_COLUMNS, row))
    data["ts"] = from_db(data["ts"])
    if data["review_ts"] is not None:
        data["review_ts"] = from_db(data["review_ts"])
    return Txn(**data)


class Ledger:
    def __init__(self, conn: apsw.Connection) -> None:
        self.conn = conn
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self, actor: Actor, intent: str, *, auto_accept: bool = False) -> Iterator[TxnHandle]:
        """Run the block as one SQLite transaction and record its changeset as a txn row.

        A block that changes no ledger table commits without writing a txn row.
        Any exception rolls the whole transaction back and is re-raised.
        """
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            session: apsw.Session | None = None
            try:
                session = apsw.Session(self.conn, "main")
                for table in LEDGER_TABLES:
                    session.attach(table)
                handle = TxnHandle(id=new_id(), conn=self.conn)
                yield handle
                changeset = session.changeset()
                if changeset:
                    self._record(handle.id, actor, intent, auto_accept, changeset)
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
            finally:
                if session is not None:
                    session.close()

    def _record(self, txn_id: str, actor: Actor, intent: str, auto_accept: bool, changeset: bytes) -> None:
        self.conn.execute(
            "INSERT INTO txn (id, ts, actor_kind, actor_ref, model, backend, tool, tool_version,"
            " session_id, run_id, proposed_by_run, intent, changeset, review)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                txn_id,
                to_db(utcnow()),
                actor.kind,
                actor.ref,
                actor.model,
                actor.backend,
                actor.tool,
                actor.tool_version,
                actor.session_id,
                actor.run_id,
                actor.proposed_by_run,
                intent,
                changeset,
                review_for(actor, auto_accept),
            ),
        )
        for seq, (table_name, op, pk, old, new) in enumerate(materialize(self.conn, changeset)):
            self.conn.execute(
                "INSERT INTO change (txn_id, seq, table_name, op, pk, old, new) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (txn_id, seq, table_name, op, json.dumps(pk), _json_or_null(old), _json_or_null(new)),
            )

    def get_txn(self, txn_id: str) -> Txn | None:
        with self._lock:
            row = self.conn.execute(f"{_TXN_SELECT} WHERE id = ?", (txn_id,)).fetchone()
        return None if row is None else _txn_from_row(row)

    def list_txns(self, *, limit: int = 50, before: str | None = None) -> list[Txn]:
        """Transactions newest first; `before` pages to ids older than the given one."""
        with self._lock:
            if before is None:
                rows = self.conn.execute(f"{_TXN_SELECT} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = self.conn.execute(
                    f"{_TXN_SELECT} WHERE id < ? ORDER BY id DESC LIMIT ?", (before, limit)
                ).fetchall()
        return [_txn_from_row(row) for row in rows]

    def changes_for(self, txn_id: str) -> list[Change]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT txn_id, seq, table_name, op, pk, old, new FROM change WHERE txn_id = ? ORDER BY seq",
                (txn_id,),
            ).fetchall()
        return [
            Change(
                txn_id=txn_id_,
                seq=seq,
                table_name=table_name,
                op=op,
                pk=json.loads(pk),
                old=None if old is None else json.loads(old),
                new=None if new is None else json.loads(new),
            )
            for txn_id_, seq, table_name, op, pk, old, new in rows
        ]

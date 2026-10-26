"""The transactional ledger: every mutation is one SQLite transaction recorded as a changeset."""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

import apsw

from trajectree.db.changes import _row_dict, materialize
from trajectree.db.schema import LEDGER_TABLES, table_columns
from trajectree.ids import new_id
from trajectree.models import Actor, Change, Conflict, Txn
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


class RevertConflict(Exception):
    """Reverting would overwrite rows changed since; nothing was applied."""

    def __init__(self, conflicts: list[Conflict]) -> None:
        super().__init__(f"revert hit {len(conflicts)} conflict(s)")
        self.conflicts = conflicts


class AlreadyReverted(Exception):
    """The transaction has already been reverted."""


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


def _conflict_from(conn: apsw.Connection, reason: int, change: apsw.TableChange) -> Conflict:
    """Copy a conflicting change out of the apply callback, keyed like `materialize`.

    A foreign-key conflict is reported once for the whole changeset and carries
    no row (apsw gives `name`, `op` and `pk_columns` as None), so it becomes a
    conflict with only its reason set.
    """
    name = apsw.mapping_session_conflict[reason]
    if change.name is None:
        return Conflict(reason=name, table_name="", op="", pk=[])
    columns = table_columns(conn, change.name)
    key_source = change.new if change.op == "INSERT" else change.old
    return Conflict(
        reason=name,
        table_name=change.name,
        op=change.op,
        pk=[key_source[i] for i in sorted(change.pk_columns)],
        old=_row_dict(columns, change.old),
        new=_row_dict(columns, change.new),
        conflict_row=_row_dict(columns, change.conflict),
    )


class Ledger:
    def __init__(self, conn: apsw.Connection) -> None:
        self.conn = conn
        self._lock = threading.RLock()

    @contextmanager
    def transaction(
        self, actor: Actor, intent: str, *, auto_accept: bool = False, reverts_txn: str | None = None
    ) -> Iterator[TxnHandle]:
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
                    self._record(handle.id, actor, intent, auto_accept, changeset, reverts_txn)
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
            finally:
                if session is not None:
                    session.close()

    def _record(
        self,
        txn_id: str,
        actor: Actor,
        intent: str,
        auto_accept: bool,
        changeset: bytes,
        reverts_txn: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO txn (id, ts, actor_kind, actor_ref, model, backend, tool, tool_version,"
            " session_id, run_id, proposed_by_run, intent, changeset, review, reverts_txn)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                reverts_txn,
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

    @contextmanager
    def locked(self) -> Iterator[apsw.Connection]:
        """Hold the ledger lock for a block of reads; re-entrant inside `transaction`."""
        with self._lock:
            yield self.conn

    def list_txns(
        self,
        *,
        limit: int = 50,
        before: str | None = None,
        review: str | None = None,
        actor_kinds: list[str] | None = None,
        run_id: str | None = None,
    ) -> list[Txn]:
        """Transactions newest first; `before` pages to ids older than the given one.

        `review` keeps only that review state, `actor_kinds` only those actor kinds.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if before is not None:
            clauses.append("id < ?")
            params.append(before)
        if review is not None:
            clauses.append("review = ?")
            params.append(review)
        if actor_kinds is not None:
            clauses.append(f"actor_kind IN ({', '.join('?' * len(actor_kinds))})" if actor_kinds else "0")
            params.extend(actor_kinds)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.conn.execute(
                f"{_TXN_SELECT}{where} ORDER BY id DESC LIMIT ?", (*params, limit)
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

    def revert(
        self,
        txn_id: str,
        actor: Actor,
        intent: str | None = None,
        *,
        mark_review: Literal["rejected"] | None = None,
    ) -> Txn:
        """Apply the inverse of `txn_id` as a new transaction linked to it both ways.

        Raises `KeyError` for an unknown id, `AlreadyReverted` if it was reverted
        before, and `RevertConflict` (with nothing applied) if any row it touched
        has changed since. A pending original is marked `rejected`, as is any
        original when `mark_review` is given; the marking runs in the same
        transaction, so a failed revert leaves the review untouched.
        """
        conflicts: list[Conflict] = []

        def on_conflict(reason: int, change: apsw.TableChange) -> int:
            conflicts.append(_conflict_from(self.conn, reason, change))
            return apsw.SQLITE_CHANGESET_ABORT

        try:
            with self.transaction(actor, intent or f"revert {txn_id}", reverts_txn=txn_id) as handle:
                # Checked inside the block so the lock and write transaction cover check and apply.
                original = self.get_txn(txn_id)
                if original is None:
                    raise KeyError(txn_id)
                if original.reverted_by is not None:
                    raise AlreadyReverted(txn_id)
                inverse = apsw.Changeset.invert(original.changeset)
                apsw.Changeset.apply(inverse, self.conn, conflict=on_conflict)
                self.conn.execute("UPDATE txn SET reverted_by = ? WHERE id = ?", (handle.id, txn_id))
                # Reverting a write still awaiting review is a rejection of it.
                review = mark_review or ("rejected" if original.review == "pending" else None)
                if review is not None:
                    self.conn.execute(
                        "UPDATE txn SET review = ?, review_ts = ? WHERE id = ?",
                        (review, to_db(utcnow()), txn_id),
                    )
        except apsw.Error as exc:
            if conflicts:
                raise RevertConflict(conflicts) from exc
            raise
        reverted = self.get_txn(handle.id)
        assert reverted is not None
        return reverted

    def set_review(self, txn_id: str, state: Literal["accepted", "rejected"], actor: Actor) -> Txn:
        """Accept a transaction, or reject it by reverting it; returns the reviewed transaction.

        Reviewing again with the same state returns the transaction unchanged.
        Accepting a reverted transaction raises `AlreadyReverted`; rejecting runs
        the revert and the review update as one transaction, or, for one already
        reverted whose review is still pending, only marks it rejected.
        """
        with self._lock:
            txn = self.get_txn(txn_id)
            if txn is None:
                raise KeyError(txn_id)
            if state == "accepted" and txn.reverted_by is not None:
                raise AlreadyReverted(txn_id)
            if txn.review == state:
                return txn
            if state == "rejected" and txn.reverted_by is not None and txn.review == "pending":
                # Already undone, still awaiting review: rejecting it only records the review.
                self.conn.execute(
                    "UPDATE txn SET review = ?, review_ts = ? WHERE id = ?", (state, to_db(utcnow()), txn_id)
                )
            elif state == "rejected":
                self.revert(txn_id, actor, mark_review="rejected")
            else:
                self.conn.execute(
                    "UPDATE txn SET review = ?, review_ts = ? WHERE id = ?", (state, to_db(utcnow()), txn_id)
                )
            reviewed = self.get_txn(txn_id)
        assert reviewed is not None
        return reviewed

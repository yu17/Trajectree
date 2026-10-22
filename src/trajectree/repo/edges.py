"""Edge repository."""

import apsw

from trajectree.db.ledger import Ledger
from trajectree.ids import new_id
from trajectree.models import Actor, Edge, EdgeIn
from trajectree.repo import DuplicateEdge, NotFound
from trajectree.repo._rows import dumps_props, row_to_model, select_row, select_rows
from trajectree.timeutil import to_db, utcnow


def get_edge(conn: apsw.Connection, edge_id: str) -> Edge | None:
    row = select_row(conn, "edge", edge_id)
    return row_to_model(conn, "edge", row, Edge) if row else None


def list_edges(conn: apsw.Connection, *, event_ids: list[str] | None = None) -> list[Edge]:
    where: list[str] = []
    params: list[str] = []
    if event_ids is not None:
        if not event_ids:
            return []
        marks = ", ".join("?" * len(event_ids))
        where.append(
            f"((src_type = 'event' AND src_id IN ({marks}))"
            f" OR (dst_type = 'event' AND dst_id IN ({marks})))"
        )
        params.extend(event_ids)
        params.extend(event_ids)
    rows = select_rows(conn, "edge", where, params, "created_at, id")
    return [row_to_model(conn, "edge", r, Edge) for r in rows]


def create_edge(
    ledger: Ledger, actor: Actor, data: EdgeIn, *, intent: str | None = None
) -> Edge:
    edge_id = new_id()
    now = to_db(utcnow())
    default_intent = f"link {data.src_type}:{data.src_id} -{data.rel}-> {data.dst_type}:{data.dst_id}"
    try:
        with ledger.transaction(actor, intent or default_intent) as h:
            # EdgeIn restricts the types to project|event|todo|resource, so the table names are safe
            for kind, ref in ((data.src_type, data.src_id), (data.dst_type, data.dst_id)):
                if h.conn.execute(f"SELECT 1 FROM {kind} WHERE id = ?", (ref,)).fetchone() is None:
                    raise NotFound(ref)
            h.conn.execute(
                "INSERT INTO edge (id, src_type, src_id, dst_type, dst_id, rel, props,"
                " valid_from, valid_to, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    edge_id, data.src_type, data.src_id, data.dst_type, data.dst_id, data.rel,
                    dumps_props(data.props),
                    to_db(data.valid_from) if data.valid_from else None,
                    to_db(data.valid_to) if data.valid_to else None,
                    now,
                ),
            )
    except apsw.ConstraintError as exc:
        if "edge." not in str(exc):
            raise
        raise DuplicateEdge(edge_id) from exc
    edge = get_edge(ledger.conn, edge_id)
    assert edge is not None
    return edge


def delete_edge(ledger: Ledger, actor: Actor, edge_id: str, *, intent: str | None = None) -> None:
    with ledger.transaction(actor, intent or f"unlink edge {edge_id}") as h:
        if select_row(h.conn, "edge", edge_id) is None:
            raise NotFound(edge_id)
        h.conn.execute("DELETE FROM edge WHERE id = ?", (edge_id,))

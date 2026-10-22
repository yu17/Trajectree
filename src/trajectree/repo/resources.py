"""Resource repository."""

from pathlib import PurePosixPath
from typing import Any

import apsw

from trajectree.db.ledger import Ledger
from trajectree.ids import new_id
from trajectree.models import Actor, Project, Resource, ResourceIn
from trajectree.repo import NotFound
from trajectree.repo._rows import dumps_props, row_to_model, select_row, select_rows, update_fields
from trajectree.repo.projects import get_project
from trajectree.timeutil import to_db, utcnow


def get_resource(conn: apsw.Connection, resource_id: str) -> Resource | None:
    row = select_row(conn, "resource", resource_id)
    return row_to_model(conn, "resource", row, Resource) if row else None


def find_resource(
    conn: apsw.Connection, project_id: str, kind: str, locator: str
) -> Resource | None:
    rows = select_rows(
        conn,
        "resource",
        ["project_id = ?", "kind = ?", "locator = ?"],
        [project_id, kind, locator],
        "id",
    )
    return row_to_model(conn, "resource", rows[0], Resource) if rows else None


def list_resources(
    conn: apsw.Connection,
    *,
    project_ids: list[str] | None = None,
    kinds: list[str] | None = None,
) -> list[Resource]:
    where: list[str] = []
    params: list[Any] = []
    if project_ids is not None:
        where.append(f"project_id IN ({', '.join('?' * len(project_ids))})")
        params.extend(project_ids)
    if kinds is not None:
        where.append(f"kind IN ({', '.join('?' * len(kinds))})")
        params.extend(kinds)
    rows = select_rows(conn, "resource", where, params, "project_id, kind, locator")
    return [row_to_model(conn, "resource", r, Resource) for r in rows]


def upsert_resource(
    ledger: Ledger, actor: Actor, data: ResourceIn, *, intent: str | None = None
) -> Resource:
    with ledger.locked() as conn:
        existing = find_resource(conn, data.project_id, data.kind, data.locator)
        if existing is not None and existing.title == data.title and existing.props == data.props:
            return existing
    now = to_db(utcnow())
    label = f"{data.kind} {data.locator}"
    if existing is None:
        resource_id = new_id()
        with ledger.transaction(actor, intent or f"register resource {label}") as h:
            if h.conn.execute("SELECT 1 FROM project WHERE id = ?", (data.project_id,)).fetchone() is None:
                raise NotFound(data.project_id)
            h.conn.execute(
                "INSERT INTO resource (id, project_id, kind, locator, title, props,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resource_id, data.project_id, data.kind, data.locator, data.title,
                    dumps_props(data.props), now, now,
                ),
            )
    else:
        resource_id = existing.id
        with ledger.transaction(actor, intent or f"update resource {label}") as h:
            update_fields(
                h.conn,
                "resource",
                resource_id,
                {"title": data.title, "props": dumps_props(data.props), "updated_at": now},
            )
    resource = get_resource(ledger.conn, resource_id)
    assert resource is not None
    return resource


def project_for_path(conn: apsw.Connection, path: str) -> Project | None:
    target = PurePosixPath(path)
    best: Resource | None = None
    for res in list_resources(conn, kinds=["folder", "git_local"]):
        root = PurePosixPath(res.locator)
        if target == root or target.is_relative_to(root):
            if best is None or len(res.locator) > len(best.locator):
                best = res
    return get_project(conn, best.project_id) if best else None

"""Todo repository."""

from datetime import datetime
from typing import Any

import apsw

from trajectree.db.ledger import Ledger
from trajectree.ids import new_id
from trajectree.models import Actor, Todo, TodoIn, TodoPatch
from trajectree.repo import NotFound
from trajectree.repo._rows import dumps_props, row_to_model, select_row, select_rows, update_fields
from trajectree.timeutil import to_db, utcnow


def get_todo(conn: apsw.Connection, todo_id: str) -> Todo | None:
    row = select_row(conn, "todo", todo_id)
    return row_to_model(conn, "todo", row, Todo) if row else None


def list_todos(
    conn: apsw.Connection,
    *,
    project_ids: list[str] | None = None,
    states: list[str] | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
) -> list[Todo]:
    where: list[str] = []
    params: list[Any] = []
    if project_ids is not None:
        where.append(f"project_id IN ({', '.join('?' * len(project_ids))})")
        params.extend(project_ids)
    if states is not None:
        where.append(f"state IN ({', '.join('?' * len(states))})")
        params.extend(states)
    if due_from is not None:
        where.append("due >= ?")
        params.append(to_db(due_from))
    if due_to is not None:
        where.append("due <= ?")
        params.append(to_db(due_to))
    rows = select_rows(conn, "todo", where, params, "due IS NULL, due, created_at")
    return [row_to_model(conn, "todo", r, Todo) for r in rows]


def create_todo(
    ledger: Ledger, actor: Actor, data: TodoIn, *, intent: str | None = None
) -> Todo:
    todo_id = new_id()
    now = to_db(utcnow())
    with ledger.transaction(actor, intent or f"add todo: {data.title}") as h:
        if (
            data.project_id is not None
            and h.conn.execute("SELECT 1 FROM project WHERE id = ?", (data.project_id,)).fetchone() is None
        ):
            raise NotFound(data.project_id)
        h.conn.execute(
            "INSERT INTO todo (id, project_id, title, body_md, due, state, priority, props,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                todo_id, data.project_id, data.title, data.body_md,
                to_db(data.due) if data.due else None, data.state, data.priority,
                dumps_props(data.props), now, now,
            ),
        )
    todo = get_todo(ledger.conn, todo_id)
    assert todo is not None
    return todo


def update_todo(
    ledger: Ledger, actor: Actor, todo_id: str, patch: TodoPatch, *, intent: str | None = None
) -> Todo:
    fields: dict[str, Any] = patch.model_dump(exclude_unset=True)
    if fields.get("due") is not None:
        fields["due"] = to_db(fields["due"])
    if "props" in fields:
        fields["props"] = dumps_props(fields["props"])
    fields["updated_at"] = to_db(utcnow())
    with ledger.transaction(actor, intent or f"update todo {todo_id}") as h:
        if select_row(h.conn, "todo", todo_id) is None:
            raise NotFound(todo_id)
        project_id = fields.get("project_id")
        if (
            project_id is not None
            and h.conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None
        ):
            raise NotFound(project_id)
        update_fields(h.conn, "todo", todo_id, fields)
    todo = get_todo(ledger.conn, todo_id)
    assert todo is not None
    return todo

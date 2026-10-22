"""Project repository."""

import apsw

from trajectree.db.ledger import Ledger
from trajectree.ids import new_id
from trajectree.models import Actor, Project, ProjectIn, ProjectPatch
from trajectree.repo import DuplicateSlug, NotFound, UnknownProject
from trajectree.repo._rows import dumps_props, row_to_model, select_row, select_rows, update_fields
from trajectree.timeutil import to_db, utcnow


def get_project(conn: apsw.Connection, project_id: str) -> Project | None:
    row = select_row(conn, "project", project_id)
    return row_to_model(conn, "project", row, Project) if row else None


def get_project_by_slug(conn: apsw.Connection, slug: str) -> Project | None:
    rows = select_rows(conn, "project", ["slug = ?"], [slug], "id")
    return row_to_model(conn, "project", rows[0], Project) if rows else None


def list_projects(conn: apsw.Connection, *, status: str | None = None) -> list[Project]:
    where, params = ([], []) if status is None else (["status = ?"], [status])
    rows = select_rows(conn, "project", where, params, "lane_order, created_at")
    return [row_to_model(conn, "project", r, Project) for r in rows]


def create_project(
    ledger: Ledger, actor: Actor, data: ProjectIn, *, intent: str | None = None
) -> Project:
    project_id = new_id()
    now = to_db(utcnow())
    try:
        with ledger.transaction(actor, intent or f"create project {data.slug}") as h:
            h.conn.execute(
                "INSERT INTO project (id, slug, title, kind, status, lane_order, summary_md,"
                " recurrence, props, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id, data.slug, data.title, data.kind, data.status, data.lane_order,
                    data.summary_md, data.recurrence, dumps_props(data.props), now, now,
                ),
            )
    except apsw.ConstraintError as exc:
        raise DuplicateSlug(data.slug) from exc
    project = get_project(ledger.conn, project_id)
    assert project is not None
    return project


def update_project(
    ledger: Ledger, actor: Actor, project_id: str, patch: ProjectPatch, *, intent: str | None = None
) -> Project:
    fields = patch.model_dump(exclude_unset=True)
    if "props" in fields:
        fields["props"] = dumps_props(fields["props"])
    fields["updated_at"] = to_db(utcnow())
    if "summary_md" in fields:
        fields["summary_at"] = fields["updated_at"]
    try:
        with ledger.transaction(actor, intent or f"update project {project_id}") as h:
            if select_row(h.conn, "project", project_id) is None:
                raise NotFound(project_id)
            update_fields(h.conn, "project", project_id, fields)
    except apsw.ConstraintError as exc:
        raise DuplicateSlug(str(patch.slug)) from exc
    project = get_project(ledger.conn, project_id)
    assert project is not None
    return project


def unknown_project(conn: apsw.Connection, name: str) -> UnknownProject:
    known = ", ".join(sorted(p.slug for p in list_projects(conn)))
    return UnknownProject(f"unknown project '{name}'; known projects: {known}")


def resolve_project_ref(conn: apsw.Connection, ref: str) -> Project:
    project = get_project_by_slug(conn, ref) or get_project(conn, ref)
    if project is None:
        raise unknown_project(conn, ref)
    return project

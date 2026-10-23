"""Report repository: decks and memos, saved as ledger transactions."""

import json
from typing import Any

import apsw

from trajectree.db.ledger import Ledger
from trajectree.ids import new_id
from trajectree.models import Actor, Report, ReportIn, ReportPatch
from trajectree.repo import NotFound
from trajectree.repo._rows import dumps_props, select_row, select_rows, update_fields
from trajectree.db.schema import table_columns
from trajectree.timeutil import to_db, utcnow


def _to_report(conn: apsw.Connection, row: tuple[Any, ...]) -> Report:
    data = dict(zip(table_columns(conn, "report"), row))
    data["scope"] = json.loads(data["scope"])
    data["props"] = json.loads(data["props"])
    return Report.model_validate(data)


def get_report(conn: apsw.Connection, report_id: str) -> Report | None:
    row = select_row(conn, "report", report_id)
    return None if row is None else _to_report(conn, row)


def list_reports(conn: apsw.Connection, *, limit: int = 100) -> list[Report]:
    rows = select_rows(conn, "report", [], [], f"updated_at DESC, id DESC LIMIT {int(limit)}")
    return [_to_report(conn, r) for r in rows]


def create_report(
    ledger: Ledger, actor: Actor, data: ReportIn, *, intent: str | None = None
) -> Report:
    report_id = new_id()
    now = to_db(utcnow())
    with ledger.transaction(actor, intent or f"create report: {data.title}") as h:
        h.conn.execute(
            "INSERT INTO report (id, title, kind, scope, source_md, props, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_id, data.title, data.kind, data.scope.model_dump_json(), data.source_md,
                dumps_props(data.props), now, now,
            ),
        )
    report = get_report(ledger.conn, report_id)
    assert report is not None
    return report


def update_report(
    ledger: Ledger, actor: Actor, report_id: str, patch: ReportPatch, *, intent: str | None = None
) -> Report:
    fields: dict[str, Any] = patch.model_dump(exclude_unset=True, mode="json")
    for key in ("scope", "props"):
        if key in fields:
            fields[key] = json.dumps(fields[key])
    fields["updated_at"] = to_db(utcnow())
    with ledger.transaction(actor, intent or f"update report {report_id}") as h:
        if select_row(h.conn, "report", report_id) is None:
            raise NotFound(report_id)
        update_fields(h.conn, "report", report_id, fields)
    report = get_report(ledger.conn, report_id)
    assert report is not None
    return report

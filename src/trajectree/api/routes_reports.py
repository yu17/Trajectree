"""Reports: list, owner saves (each a human transaction), agent drafts and exports."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from trajectree.api.deps import actor_from_request, get_ledger
from trajectree.api.routes_runs import _enqueue
from trajectree.db.ledger import Ledger
from trajectree.models import Report, ReportIn, ReportPatch, Run
from trajectree.repo import NotFound
from trajectree.repo.reports import create_report, get_report, list_reports, update_report
from trajectree.repo.review import pending_txns
from trajectree.reports.export import ExportFormat, MarpUnavailable, export_report
from trajectree.runner.kinds import ReportRequest

router = APIRouter()

MEDIA_TYPES: dict[str, str] = {
    "md": "text/markdown",
    "html": "text/html",
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class ReportOut(Report):
    pending_txn: str | None = None


def _get(conn, report_id: str) -> Report:
    report = get_report(conn, report_id)
    if report is None:
        raise NotFound(f"report {report_id} not found")
    return report


@router.get("/reports", response_model=list[ReportOut])
def reports_list(limit: int = Query(100, ge=1, le=500), ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        pending = pending_txns(conn, "report")
        return [ReportOut(**r.model_dump(), pending_txn=pending.get(r.id)) for r in list_reports(conn, limit=limit)]


@router.post("/reports", response_model=Report, status_code=201)
def reports_create(body: ReportIn, request: Request, ledger: Ledger = Depends(get_ledger)):
    return create_report(ledger, actor_from_request(request), body, intent=f"new report {body.title}")


@router.post("/reports/draft", response_model=Run, status_code=202)
def reports_draft(body: ReportRequest, request: Request):
    return _enqueue(request, body)


@router.get("/reports/{report_id}", response_model=Report)
def reports_get(report_id: str, ledger: Ledger = Depends(get_ledger)):
    with ledger.locked() as conn:
        return _get(conn, report_id)


@router.patch("/reports/{report_id}", response_model=Report)
def reports_patch(report_id: str, body: ReportPatch, request: Request, ledger: Ledger = Depends(get_ledger)):
    nulls = sorted(k for k in body.model_fields_set if getattr(body, k) is None)
    if nulls:
        raise HTTPException(status_code=422, detail=f"cannot be null: {', '.join(nulls)}")
    # one lock across read and write, so the intent names the title the save leaves
    with ledger.locked() as conn:
        title = body.title or _get(conn, report_id).title
        return update_report(ledger, actor_from_request(request), report_id, body, intent=f"update report {title}")


@router.get("/reports/{report_id}/export")
def reports_export(
    report_id: str,
    request: Request,
    fmt: ExportFormat = Query("html", alias="format"),
    ledger: Ledger = Depends(get_ledger),
):
    with ledger.locked() as conn:
        report = _get(conn, report_id)
    # marp-cli may take a while: it runs with no lock held
    try:
        path = export_report(request.app.state.settings, report, fmt)
    except MarpUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # The page is agent-written: a sandboxed origin keeps its scripts away from the owner's session.
    headers = {"Content-Security-Policy": "sandbox allow-scripts"} if fmt == "html" else None
    return FileResponse(
        path,
        media_type=MEDIA_TYPES[fmt],
        filename=path.name,
        content_disposition_type="inline",
        headers=headers,
    )

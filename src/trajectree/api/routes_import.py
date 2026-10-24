from __future__ import annotations

import posixpath
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from trajectree.api.deps import actor_from_request, get_ledger, get_settings
from trajectree.config import Settings
from trajectree.connectors.local_git import discover_remotes
from trajectree.connectors.probe import ProbeReport, probe_folder, probe_state_reader
from trajectree.connectors.remote import normalize_remote
from trajectree.db.ledger import Ledger
from trajectree.importing import IMPORT_SOURCES, ImportRecord, SourceChoice
from trajectree.models import Project, ProjectPatch, Resource, ResourceIn, Run
from trajectree.repo import NotFound
from trajectree.repo.projects import get_project, update_project
from trajectree.repo.runs import update_run
from trajectree.repo.resources import list_resources, upsert_resource
from trajectree.repo.state import get_state, set_state
from trajectree.runner.kinds import StartingPointRequest
from trajectree.runner.service import RunService
from trajectree.timeutil import utcnow

router = APIRouter()


class ProbeRequest(BaseModel):
    path: str


class StartBlockChoice(BaseModel):
    runner: str | None = None
    model: str  # required until model resolution (Task 9) supplies a default


class ImportRequest(BaseModel):
    sources: dict[str, SourceChoice]
    remotes: list[str] = []
    start_block: StartBlockChoice | None = None
    starts: dict[str, date] = {}

    @field_validator("sources")
    @classmethod
    def _known_sources(cls, sources: dict[str, SourceChoice]) -> dict[str, SourceChoice]:
        for source in sources:
            if source not in IMPORT_SOURCES:
                raise ValueError(f"unknown source '{source}'")
        return sources


class ImportResult(BaseModel):
    project: Project
    resources: list[Resource]
    starting_point_run: Run | None = None


@router.post("/import/probe", response_model=ProbeReport)
def probe(body: ProbeRequest, ledger: Ledger = Depends(get_ledger), settings: Settings = Depends(get_settings)):
    if not body.path.startswith("/"):
        raise HTTPException(status_code=422, detail="path must be absolute")
    path = _inside_mapped_roots(settings, body.path)
    return probe_folder(probe_state_reader(ledger), settings, path, now=utcnow())


def _inside_mapped_roots(settings: Settings, path: str) -> str:
    """`path` normalized; 422 when a path map is configured and no mapped host root contains it."""
    path = posixpath.normpath(path)
    target = PurePosixPath(path)
    if settings.path_map and not any(target.is_relative_to(PurePosixPath(host)) for host, _ in settings.path_map):
        raise HTTPException(status_code=422, detail=f"path {path} is outside every mapped folder")
    return path


def _validate(body: ImportRequest) -> dict[str, str]:
    """Check the request on its own; returns each picked remote URL normalized."""
    picked = {s for s, c in body.sources.items() if c.picked}
    for source in body.starts:
        if source not in picked or source == "remote":
            raise HTTPException(status_code=422, detail=f"starts names '{source}', which is not a picked history source")
    if body.start_block is not None and not body.starts:
        raise HTTPException(status_code=422, detail="start_block needs starts for at least one source")
    urls: dict[str, str] = {}
    for url in body.remotes:
        normal = normalize_remote(url)
        if normal is None:
            raise HTTPException(status_code=422, detail=f"unrecognised remote '{url}'")
        urls[url] = normal
    return urls


@router.post("/projects/{project_id}/import", response_model=ImportResult)
def import_project(
    project_id: str,
    body: ImportRequest,
    request: Request,
    ledger: Ledger = Depends(get_ledger),
    settings: Settings = Depends(get_settings),
):
    picked_remotes = list(dict.fromkeys(_validate(body).values()))
    with ledger.locked() as conn:
        project = get_project(conn, project_id)
        if project is None:
            raise NotFound(project_id)
        folders = list_resources(conn, project_ids=[project_id], kinds=["folder"])
    existing = ImportRecord.model_validate(project.props["import"]) if "import" in project.props else None
    for source, choice in body.sources.items():
        prior = existing.sources.get(source) if existing else None
        if choice.picked and prior is not None and prior.picked:
            raise HTTPException(status_code=409, detail=f"already imported: {source}")
    if not folders:
        raise HTTPException(status_code=422, detail="project has no folder resource; register the folder first")
    folder = _inside_mapped_roots(settings, min(folders, key=lambda r: len(r.locator)).locator)
    runs: RunService = request.app.state.runs
    start_request: StartingPointRequest | None = None
    if body.start_block is not None:
        # Everything enqueue would reject is checked before the first write.
        try:
            start_request = StartingPointRequest(
                project=project_id, starts=body.starts, runner=body.start_block.runner, model=body.start_block.model
            )
            runs.check(start_request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    actor = actor_from_request(request)

    sources = dict(existing.sources) if existing else {}
    for source, choice in body.sources.items():
        prior = sources.get(source)
        if prior is None or not prior.picked:
            sources[source] = choice
    if picked_remotes and "remote" not in sources:
        sources["remote"] = SourceChoice(picked=True)

    if "git" in body.sources and body.sources["git"].picked:
        upsert_resource(ledger, actor, ResourceIn(project_id=project_id, kind="git_local", locator=folder, title=folder.rsplit("/", 1)[-1]))
    for url in picked_remotes:
        upsert_resource(
            ledger, actor, ResourceIn(project_id=project_id, kind="git_remote", locator=url, title=urlparse(url).path.strip("/"))
        )

    # Remotes the checkout has that the owner did not pick are remembered as declined, so ingest
    # never suggests them. git runs here, outside the lock.
    anchor = Resource(id="import", project_id=project_id, kind="git_local", locator=folder)
    seen = [url for _, url in discover_remotes(settings, [anchor])]
    with ledger.locked() as conn:
        suggested = get_state(conn, "remote:suggested")
        suggested = suggested if isinstance(suggested, dict) else {}
        known = suggested.get(project_id)
        known = known if isinstance(known, list) else []
        merged = [*known, *(u for u in dict.fromkeys([*picked_remotes, *seen]) if u not in known)]
        if merged != known:
            suggested[project_id] = merged
            with conn:
                set_state(conn, "remote:suggested", suggested)

    record = ImportRecord(
        sources=sources,
        recorded_at=existing.recorded_at if existing else utcnow(),
        start_run_id=existing.start_run_id if existing else None,
    )

    def save(rec: ImportRecord) -> Project:
        props = {**project.props, "import": rec.model_dump(mode="json")}
        return update_project(ledger, actor, project_id, ProjectPatch(props=props), intent="record import choices")

    run: Run | None = None
    if start_request is not None:
        # Enqueue before the record is saved, so a saved record always has the run the owner asked
        # for; a failure here (a race with the checks above) leaves the record unsaved and the
        # import retryable. Resources and suggestions written above are idempotent.
        try:
            run = runs.enqueue(start_request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"starting-point run not queued, nothing recorded: {exc}") from exc
        record = record.model_copy(update={"start_run_id": run.id})
    try:
        updated = save(record)
    except Exception:
        if run is not None:  # do not leave a run for an import that was not recorded
            with ledger.locked() as conn:
                with conn:
                    update_run(conn, run.id, status="cancelled", finished=utcnow())
        raise
    with ledger.locked() as conn:
        resources = list_resources(conn, project_ids=[project_id])
    return ImportResult(project=updated, resources=resources, starting_point_run=run)

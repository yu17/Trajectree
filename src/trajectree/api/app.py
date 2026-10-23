from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from trajectree.api.deps import COOKIE_NAME, require_token, token_matches
from trajectree.api.mcp_mount import mount_mcp, owner_or_token_resolver
from trajectree.api.routes_hooks import router as hooks_router
from trajectree.api.routes_context import router as context_router
from trajectree.api.routes_entities import router as entities_router
from trajectree.api.routes_ledger import router as ledger_router
from trajectree.api.routes_search import router as search_router
from trajectree.api.routes_resources import router as resources_router
from trajectree.api.routes_routines import router as routines_router
from trajectree.api.routes_runs import router as runs_router
from trajectree.api.routes_signals import router as signals_router
from trajectree.api.routes_timeline import router as timeline_router
from trajectree.api.routes_tokens import router as tokens_router
from trajectree.config import Settings, ensure_token
from trajectree.db.connection import open_db
from trajectree.db.ledger import AlreadyReverted, Ledger, RevertConflict
from trajectree.ingest.job import IngestBusy
from trajectree.ingest.scheduler import ingest_loop
from trajectree.mcpserver.server import build_mcp
from trajectree.repo import DuplicateEdge, DuplicateRoutine, DuplicateSlug, InvalidRange, NotFound, UnknownProject, UnknownSession
from trajectree.runner.seeds import ensure_inbox_project, ensure_seed_routines
from trajectree.runner.service import RunService
from trajectree.runner.spec import AlreadyApplied, RunsPaused
from trajectree.scheduler import Scheduler
from trajectree.timeutil import utcnow

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Plain routers (no prefix, no auth). create_app includes each into a fresh
# /api router that enforces require_token. Later tasks append theirs here.
PROTECTED_ROUTERS: list[APIRouter] = [entities_router, timeline_router, ledger_router, search_router, resources_router, context_router, signals_router, runs_router, tokens_router, routines_router]


def _seed(ledger: Ledger, settings: Settings) -> None:
    """The inbox project, then the seed routines that deliver to it."""
    ensure_inbox_project(ledger)
    with ledger.locked() as conn:
        ensure_seed_routines(conn, settings, now=utcnow())


def _error_handler(status: int):
    def handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status)

    return handler


def create_app(settings: Settings, *, ledger: Ledger | None = None) -> FastAPI:
    token = ensure_token(settings)
    if ledger is None:
        ledger = Ledger(open_db(settings.db_path))

    # One MCPServer per app: its session manager's run() can be entered only once.
    mcp = build_mcp(ledger, settings, resolve=owner_or_token_resolver(ledger, token, live_window_s=settings.live_window_s))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            stop = asyncio.Event()
            # Runs left `running` by a previous process can never finish: fail them first.
            await asyncio.to_thread(app.state.runs.recover)
            await asyncio.to_thread(_seed, ledger, settings)
            # Exactly one worker per slot: each slot runs serially.
            tasks = [
                asyncio.create_task(app.state.runs.worker(stop, slot="interactive")),
                asyncio.create_task(app.state.runs.worker(stop, slot="scheduled")),
            ]
            if settings.ingest_interval_s > 0:
                tasks.append(
                    asyncio.create_task(
                        ingest_loop(ledger, settings, interval_s=settings.ingest_interval_s, stop=stop)
                    )
                )
                app.state.ingest_stop, app.state.ingest_task = stop, tasks[-1]
            if settings.scheduler_interval_s > 0:
                tasks.append(
                    asyncio.create_task(
                        app.state.scheduler.loop(stop, interval_s=settings.scheduler_interval_s)
                    )
                )
            try:
                yield
            finally:
                stop.set()
                for task in tasks:
                    try:
                        # wait_for cancels the task on timeout; a pass already in its
                        # worker thread finishes on its own.
                        await asyncio.wait_for(task, 10)
                    except TimeoutError:
                        pass

    app = FastAPI(title="Trajectree", lifespan=lifespan)
    app.state.settings = settings
    app.state.ledger = ledger
    app.state.token = token
    app.state.mcp = mcp
    app.state.runs = RunService(ledger, settings)
    app.state.scheduler = Scheduler(ledger, settings, app.state.runs)

    app.add_exception_handler(NotFound, _error_handler(404))
    app.add_exception_handler(DuplicateSlug, _error_handler(409))
    app.add_exception_handler(DuplicateRoutine, lambda r, e: JSONResponse({"detail": f"routine {e} exists"}, status_code=409))
    app.add_exception_handler(DuplicateEdge, _error_handler(409))
    app.add_exception_handler(InvalidRange, _error_handler(422))
    app.add_exception_handler(IngestBusy, _error_handler(409))
    app.add_exception_handler(UnknownProject, _error_handler(422))
    app.add_exception_handler(UnknownSession, _error_handler(422))
    app.add_exception_handler(RunsPaused, lambda r, e: JSONResponse({"detail": "runs are paused"}, status_code=409))
    app.add_exception_handler(
        AlreadyApplied, lambda r, e: JSONResponse({"detail": str(e)}, status_code=409)
    )

    def revert_conflict(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RevertConflict)
        return JSONResponse(
            {"detail": "conflict", "conflicts": [c.model_dump(mode="json") for c in exc.conflicts]},
            status_code=409,
        )

    def already_reverted(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": "already reverted"}, status_code=409)

    app.add_exception_handler(RevertConflict, revert_conflict)
    app.add_exception_handler(AlreadyReverted, already_reverted)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": version("trajectree")}

    api_router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])
    for router in PROTECTED_ROUTERS:
        api_router.include_router(router)
    app.include_router(api_router)
    app.include_router(hooks_router)
    mount_mcp(app, mcp, ledger=ledger, owner_token=token)

    index = STATIC_DIR / "index.html"

    @app.get("/", include_in_schema=False)
    def root(token: str | None = None):
        if token is not None:
            if not token_matches(token, app.state.token):
                raise HTTPException(status_code=401, detail="Unauthorized")
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                COOKIE_NAME, token, max_age=365 * 24 * 3600,
                httponly=True, samesite="lax", path="/",
            )
            return response
        if index.exists():
            return FileResponse(index)
        return PlainTextResponse("Trajectree API")

    if index.exists():
        static_root = STATIC_DIR.resolve()

        @app.get("/{path:path}", include_in_schema=False)
        def spa_fallback(path: str):
            # Registered before the mount so unknown client-side routes get
            # index.html instead of the mount's 404; real asset files win.
            if any(path == p or path.startswith(p + "/") for p in ("api", "mcp")):
                raise HTTPException(status_code=404, detail="Not Found")
            candidate = (static_root / path).resolve()
            if candidate.is_file() and candidate.is_relative_to(static_root):
                return FileResponse(candidate)
            return FileResponse(index)

        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app

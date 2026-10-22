from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from trajectree.api.deps import COOKIE_NAME, require_token, token_matches
from trajectree.api.routes_entities import router as entities_router
from trajectree.config import Settings, ensure_token
from trajectree.db.connection import open_db
from trajectree.db.ledger import Ledger
from trajectree.repo import DuplicateEdge, DuplicateSlug, NotFound

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Plain routers (no prefix, no auth). create_app includes each into a fresh
# /api router that enforces require_token. Later tasks append theirs here.
PROTECTED_ROUTERS: list[APIRouter] = [entities_router]


def _error_handler(status: int):
    def handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status)

    return handler


def create_app(settings: Settings, *, ledger: Ledger | None = None) -> FastAPI:
    token = ensure_token(settings)
    if ledger is None:
        ledger = Ledger(open_db(settings.db_path))

    app = FastAPI(title="Trajectree")
    app.state.settings = settings
    app.state.ledger = ledger
    app.state.token = token

    app.add_exception_handler(NotFound, _error_handler(404))
    app.add_exception_handler(DuplicateSlug, _error_handler(409))
    app.add_exception_handler(DuplicateEdge, _error_handler(409))
    app.add_exception_handler(ValueError, _error_handler(422))

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": version("trajectree")}

    api_router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])
    for router in PROTECTED_ROUTERS:
        api_router.include_router(router)
    app.include_router(api_router)

    index = STATIC_DIR / "index.html"

    @app.get("/", include_in_schema=False)
    def root(token: str | None = None):
        if token is not None:
            if not token_matches(token, app.state.token):
                raise HTTPException(status_code=401, detail="Unauthorized")
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                COOKIE_NAME, token, httponly=True, samesite="lax", path="/"
            )
            return response
        if index.exists():
            return FileResponse(index)
        return PlainTextResponse("Trajectree API")

    if index.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app

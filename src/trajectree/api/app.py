from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from trajectree.api.deps import COOKIE_NAME, require_token, token_matches
from trajectree.config import Settings, ensure_token
from trajectree.db.connection import open_db
from trajectree.db.ledger import Ledger

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Later tasks: write a module-level `router = APIRouter()` of plain routes (no
# prefix, no auth) and append it to PROTECTED_ROUTERS. create_app includes each
# into a fresh /api router that enforces require_token.
_placeholder = APIRouter()


@_placeholder.get("/projects")
def list_projects_placeholder() -> list:
    return []  # replaced in Task 10


PROTECTED_ROUTERS: list[APIRouter] = [_placeholder]


def create_app(settings: Settings, *, ledger: Ledger | None = None) -> FastAPI:
    token = ensure_token(settings)
    if ledger is None:
        ledger = Ledger(open_db(settings.db_path))

    app = FastAPI(title="Trajectree")
    app.state.settings = settings
    app.state.ledger = ledger
    app.state.token = token

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

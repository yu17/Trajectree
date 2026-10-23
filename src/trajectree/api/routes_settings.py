from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


class Mounts(BaseModel):
    claude_home: str | None
    backends_root: str | None
    path_map: list[list[str]]
    data_dir: str
    cfg_dir: str
    exports_dir: str


@router.get("/mounts", response_model=Mounts)
def mounts(request: Request) -> Mounts:
    s = request.app.state.settings
    return Mounts(
        claude_home=str(s.claude_home) if s.claude_home else None,
        backends_root=str(s.backends_root) if s.backends_root else None,
        path_map=[[host, container] for host, container in s.path_map],
        data_dir=str(s.data_dir),
        cfg_dir=str(s.cfg_root),
        exports_dir=str(s.exports_dir),
    )

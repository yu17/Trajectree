"""What a confined run may read and write (the Landlock rules `confine` applies)."""

from __future__ import annotations

from pathlib import Path

import apsw
from pydantic import BaseModel

from trajectree.config import Settings
from trajectree.connectors.paths import to_local
from trajectree.repo.resources import list_resources
from trajectree.runner.confine import system_ro_paths


class ConfinePaths(BaseModel):
    ro: list[Path]
    rw: list[Path]


def runner_cfg_dir(settings: Settings, runner: str, backend: str) -> Path:
    """The one config directory a runner logs in from (and may write)."""
    if runner == "codex":
        return settings.codex_home
    if runner.startswith("ccx:"):
        return settings.backends_cfg_dir / backend
    return settings.claude_config_dir


def run_paths(
    conn: apsw.Connection,
    settings: Settings,
    *,
    runner: str,
    backend: str,
    run_dir: Path,
    tmp_dir: Path,
    cwd: Path | None = None,
) -> ConfinePaths:
    """Read: system dirs and every registered project folder. Write: the run's own dirs and its runner's config."""
    folders: list[Path] = []
    for resource in list_resources(conn, kinds=["folder"]):  # all projects, not only the run's scope
        local = to_local(settings, resource.locator)
        if local.is_dir() and local not in folders:
            folders.append(local)
    ro = [*system_ro_paths(), *settings.confine_extra_ro, *folders]
    rw = [Path("/dev"), tmp_dir, run_dir, runner_cfg_dir(settings, runner, backend)]
    if cwd is not None and cwd.is_dir() and not any(cwd.is_relative_to(p) for p in [*ro, *rw]):
        ro.append(cwd)
    return ConfinePaths(ro=ro, rw=rw)

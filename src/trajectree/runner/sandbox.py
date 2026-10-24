"""What a confined run may read and write (the Landlock rules `confine` applies)."""

from __future__ import annotations

import logging
from pathlib import Path

import apsw
from pydantic import BaseModel

from trajectree.config import Settings
from trajectree.connectors.paths import to_local
from trajectree.repo.resources import list_resources
from trajectree.repo.review import pending_txns
from trajectree.runner.confine import system_ro_paths

log = logging.getLogger("trajectree.runner")


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


def registered_folders(conn: apsw.Connection) -> list[str]:
    """Locators of every accepted `folder` resource, of all projects. Call under the ledger lock.

    A pending folder (proposed by an agent, not yet reviewed) is left out: otherwise one run
    could widen what every later run may read."""
    unreviewed = pending_txns(conn, "resource")
    return [r.locator for r in list_resources(conn, kinds=["folder"]) if r.id not in unreviewed]


def _protected_roots(settings: Settings) -> list[Path]:
    roots = [settings.data_dir, settings.cfg_root, settings.claude_home, settings.backends_root]
    return [root.resolve() for root in roots if root is not None]


def _overlaps(path: Path, roots: list[Path]) -> bool:
    return any(path.is_relative_to(root) or root.is_relative_to(path) for root in roots)


def run_paths(
    settings: Settings,
    *,
    folders: list[str],
    runner: str,
    backend: str,
    run_dir: Path,
    tmp_dir: Path,
    cwd: Path | None = None,
) -> ConfinePaths:
    """Read: system dirs and the registered project `folders`. Write: the run's own dirs and its
    runner's config. Touches the filesystem, so call it with no lock held.

    Every read grant beyond the system dirs is resolved (symlinks followed) and dropped if it
    equals, lies inside or contains the daemon's data, its config, or the host's Claude homes."""
    rw = [Path("/dev"), tmp_dir, run_dir, runner_cfg_dir(settings, runner, backend)]
    protected = _protected_roots(settings)
    granted = [p.resolve() for p in rw]
    extra: list[Path] = []

    def grant(candidate: Path) -> None:
        resolved = candidate.resolve()
        if not resolved.is_dir() or resolved in extra:
            return
        if _overlaps(resolved, protected):
            log.warning("not granting %s to a run: it overlaps the daemon's data or config", resolved)
            return
        extra.append(resolved)

    for path in settings.confine_extra_ro:
        grant(path)
    for locator in folders:
        grant(to_local(settings, locator))
    if cwd is not None:
        resolved = cwd.resolve()
        # A cwd inside the run's own dirs (runs_dir/<id>) is already writable.
        if not any(resolved.is_relative_to(p) for p in [*granted, *extra]):
            grant(resolved)  # if refused, the run is still confined; it just cannot read its cwd
    return ConfinePaths(ro=[*system_ro_paths(), *extra], rw=rw)

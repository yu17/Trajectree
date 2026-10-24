from pathlib import Path

import pytest

from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.projects import create_project
from trajectree.repo.resources import upsert_resource
from trajectree.runner.confine import system_ro_paths
from trajectree.runner.sandbox import run_paths


def _folder(ledger, human, slug, path):
    p = create_project(ledger, human, ProjectIn(slug=slug, title=slug.upper(), kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=str(path)))
    return p


def _paths(ledger, settings, tmp_path, runner="claude", backend="claude", **kw):
    return run_paths(ledger.conn, settings, runner=runner, backend=backend,
                     run_dir=settings.runs_dir / "r1", tmp_dir=tmp_path / "tmp-r1", **kw)


def test_ro_has_every_registered_folder(ledger, human, settings, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _folder(ledger, human, "a", a)
    _folder(ledger, human, "b", b)
    paths = _paths(ledger, settings, tmp_path)
    assert a in paths.ro and b in paths.ro
    assert paths.ro[:len(system_ro_paths())] == system_ro_paths()
    assert all(p in paths.ro for p in settings.confine_extra_ro)


def test_folder_goes_through_path_map(ledger, human, settings, tmp_path):
    local = tmp_path / "projects" / "x"
    local.mkdir(parents=True)
    _folder(ledger, human, "x", "/home/me/x")
    s = settings.model_copy(update={"path_map": [("/home/me", str(tmp_path / "projects"))]})
    assert local in _paths(ledger, s, tmp_path).ro


def test_missing_folder_is_skipped(ledger, human, settings, tmp_path):
    _folder(ledger, human, "gone", tmp_path / "gone")
    paths = _paths(ledger, settings, tmp_path)
    assert tmp_path / "gone" not in paths.ro


def test_uncovered_cwd_is_readable(ledger, human, settings, tmp_path):
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    assert cwd in _paths(ledger, settings, tmp_path, cwd=cwd).ro
    run_cwd = settings.runs_dir / "r1"
    run_cwd.mkdir(parents=True)
    assert run_cwd not in _paths(ledger, settings, tmp_path, cwd=run_cwd).ro  # already writable


@pytest.mark.parametrize("runner,backend,cfg", [
    ("claude", "claude", lambda s: s.claude_config_dir),
    ("codex", "codex", lambda s: s.codex_home),
    ("ccx:kimi", "kimi", lambda s: s.backends_cfg_dir / "kimi"),
])
def test_rw_is_run_scoped(ledger, settings, tmp_path, runner, backend, cfg):
    paths = _paths(ledger, settings, tmp_path, runner=runner, backend=backend)
    assert paths.rw == [Path("/dev"), tmp_path / "tmp-r1", settings.runs_dir / "r1", cfg(settings)]


@pytest.mark.parametrize("runner,backend", [("claude", "claude"), ("codex", "codex"), ("ccx:kimi", "kimi")])
def test_never_whole_data_or_cfg(ledger, human, settings, tmp_path, runner, backend):
    claude_home, backends_root = tmp_path / "claude-home", tmp_path / "backends-root"
    claude_home.mkdir()
    backends_root.mkdir()
    s = settings.model_copy(update={"claude_home": claude_home, "backends_root": backends_root})
    folder = tmp_path / "proj"
    folder.mkdir()
    _folder(ledger, human, "a", folder)
    paths = _paths(ledger, s, tmp_path, runner=runner, backend=backend)
    for p in [*paths.ro, *paths.rw]:
        assert p not in (s.data_dir, s.cfg_root)
        assert not p.is_relative_to(claude_home) and not p.is_relative_to(backends_root)

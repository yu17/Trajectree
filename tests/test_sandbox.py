from pathlib import Path

import pytest

from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.projects import create_project
from trajectree.repo.resources import upsert_resource
from trajectree.runner.confine import system_ro_paths
from trajectree.runner.sandbox import registered_folders, run_paths


def _folder(ledger, actor, slug, path):
    p = create_project(ledger, actor, ProjectIn(slug=slug, title=slug.upper(), kind="research"))
    upsert_resource(ledger, actor, ResourceIn(project_id=p.id, kind="folder", locator=str(path)))
    return p


def _paths(ledger, settings, tmp_path, runner="claude", backend="claude", **kw):
    return run_paths(settings, folders=registered_folders(ledger.conn), runner=runner, backend=backend,
                     run_dir=settings.runs_dir / "r1", tmp_dir=tmp_path / "tmp-r1", **kw)


def _granted(paths):
    """The read grants beyond the system dirs."""
    n = len(system_ro_paths())
    assert paths.ro[:n] == system_ro_paths()
    return paths.ro[n:]


def test_ro_has_every_registered_folder(ledger, human, settings, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _folder(ledger, human, "a", a)
    _folder(ledger, human, "b", b)
    granted = _granted(_paths(ledger, settings, tmp_path))
    assert a.resolve() in granted and b.resolve() in granted
    assert all(p.resolve() in granted for p in settings.confine_extra_ro)


def test_accepted_folder_is_granted_resolved(ledger, human, settings, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    _folder(ledger, human, "a", tmp_path / "alias")
    granted = _granted(_paths(ledger, settings, tmp_path))
    assert real.resolve() in granted and tmp_path / "alias" not in granted


def test_folder_goes_through_path_map(ledger, human, settings, tmp_path):
    local = tmp_path / "projects" / "x"
    local.mkdir(parents=True)
    _folder(ledger, human, "x", "/home/me/x")
    s = settings.model_copy(update={"path_map": [("/home/me", str(tmp_path / "projects"))]})
    assert local.resolve() in _paths(ledger, s, tmp_path).ro


def test_missing_folder_is_skipped(ledger, human, settings, tmp_path):
    _folder(ledger, human, "gone", tmp_path / "gone")
    paths = _paths(ledger, settings, tmp_path)
    assert (tmp_path / "gone").resolve() not in paths.ro


def test_pending_folder_is_not_granted(ledger, human, agent, settings, tmp_path):
    proposed = tmp_path / "proposed"
    proposed.mkdir()
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, agent, ResourceIn(project_id=p.id, kind="folder", locator=str(proposed)))
    assert ledger.list_txns()[0].review == "pending"
    assert proposed.resolve() not in _paths(ledger, settings, tmp_path).ro


@pytest.mark.parametrize("where", ["root", "data_parent", "inside_cfg", "symlink_into_data"])
def test_folder_overlapping_protected_roots_is_dropped(ledger, human, settings, tmp_path, where, caplog):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.cfg_root.mkdir(parents=True, exist_ok=True)
    (settings.cfg_root / "claude").mkdir(exist_ok=True)
    if where == "root":
        bad = Path("/")
    elif where == "data_parent":
        bad = settings.data_dir.parent
    elif where == "inside_cfg":
        bad = settings.cfg_root / "claude"
    else:
        bad = tmp_path / "link"
        bad.symlink_to(settings.data_dir)
    good = tmp_path / "good"
    good.mkdir()
    _folder(ledger, human, "bad", bad)
    _folder(ledger, human, "good", good)
    granted = _granted(_paths(ledger, settings, tmp_path))
    assert granted == [*(p.resolve() for p in settings.confine_extra_ro), good.resolve()]   # the rest still granted
    assert "not granting" in caplog.text


def test_cwd_overlapping_protected_roots_is_dropped(ledger, settings, tmp_path):
    paths = _paths(ledger, settings, tmp_path, cwd=settings.data_dir.parent)
    assert _granted(paths) == [p.resolve() for p in settings.confine_extra_ro]
    assert paths.rw[2] == settings.runs_dir / "r1"   # the run is still confined with its own dirs


def test_uncovered_cwd_is_readable(ledger, settings, tmp_path):
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    assert cwd.resolve() in _paths(ledger, settings, tmp_path, cwd=cwd).ro
    run_cwd = settings.runs_dir / "r1"
    run_cwd.mkdir(parents=True)
    assert run_cwd.resolve() not in _paths(ledger, settings, tmp_path, cwd=run_cwd).ro  # already writable


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
    for slug, folder in [("a", tmp_path / "proj"), ("b", claude_home), ("c", backends_root / "kimi")]:
        folder.mkdir(exist_ok=True)
        _folder(ledger, human, slug, folder)
    paths = _paths(ledger, s, tmp_path, runner=runner, backend=backend)
    assert (tmp_path / "proj").resolve() in paths.ro
    for p in [*paths.ro, *paths.rw]:
        assert p not in (s.data_dir, s.cfg_root)
        assert not p.is_relative_to(claude_home) and not p.is_relative_to(backends_root)


@pytest.mark.parametrize("where", ["at", "above", "below"])
def test_codex_sessions_root_is_never_granted(ledger, human, settings, tmp_path, where):
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    (sessions / "2026").mkdir(parents=True)
    (tmp_path / "proj").mkdir()
    s = settings.model_copy(update={"codex_sessions_root": sessions})
    _folder(ledger, human, "a", {"at": sessions, "above": codex_home, "below": sessions / "2026"}[where])
    _folder(ledger, human, "b", tmp_path / "proj")
    paths = _paths(ledger, s, tmp_path)
    granted = _granted(paths)
    assert (tmp_path / "proj").resolve() in granted
    for p in granted:
        assert not p.is_relative_to(sessions) and not sessions.is_relative_to(p)


@pytest.mark.parametrize("runner,backend", [("claude", "claude"), ("codex", "codex"), ("ccx:kimi", "kimi")])
def test_daemon_secrets_are_never_granted(ledger, human, settings, tmp_path, runner, backend):
    from trajectree.runner.credentials import OAUTH_FILE, store_oauth_token

    token = store_oauth_token(settings, "oat-secret")
    assert token == settings.secrets_dir / OAUTH_FILE
    # even a folder registered at, above or below the secrets dir
    for slug, folder in [("a", settings.secrets_dir), ("b", settings.cfg_root), ("c", tmp_path / "proj")]:
        folder.mkdir(exist_ok=True)
        _folder(ledger, human, slug, folder)
    paths = _paths(ledger, settings, tmp_path, runner=runner, backend=backend, cwd=settings.secrets_dir)
    for p in [*paths.ro, *paths.rw]:
        assert not token.resolve().is_relative_to(p.resolve()), p
        assert not p.resolve().is_relative_to(settings.secrets_dir.resolve()), p

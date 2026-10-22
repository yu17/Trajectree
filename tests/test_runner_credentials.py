import json
import stat
from types import SimpleNamespace

import pytest

from trajectree.runner.credentials import (
    cli_version,
    load_oauth_token,
    profile_has_credentials,
    reset_version_cache,
    runner_status,
    store_oauth_token,
    sync_backends,
)


def test_store_and_load_oauth_token(settings):
    path = store_oauth_token(settings, " sk-ant-oat01-abc \n")
    assert load_oauth_token(settings) == "sk-ant-oat01-abc"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads((settings.claude_config_dir / ".claude.json").read_text()) == {"hasCompletedOnboarding": True}
    assert (settings.claude_config_dir / "settings.json").read_text() == "{}"
    assert load_oauth_token(settings.model_copy(update={"cfg_dir": settings.cfg_root / "nope"})) is None
    with pytest.raises(ValueError):
        store_oauth_token(settings, "   ")


def test_sync_backends_copies_only_config(settings, tmp_path):
    root = tmp_path / "backends"
    for name, key in (("kimi", "k"), ("empty", "")):
        d = root / name
        (d / "projects" / "x").mkdir(parents=True)
        (d / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_BASE_URL": "https://x"}}))
        (d / "history.jsonl").write_text("secret\n")
    (root / "kimi" / ".claude.json").write_text('{"hasCompletedOnboarding": true}')
    (root / "notaprofile").mkdir()
    s = settings.model_copy(update={"backends_root": root})
    assert sync_backends(s) == ["empty", "kimi"]
    copied = s.backends_cfg_dir / "kimi"
    assert sorted(p.name for p in copied.iterdir()) == [".claude.json", "settings.json"]
    assert stat.S_IMODE((copied / "settings.json").stat().st_mode) == 0o600
    assert profile_has_credentials(copied / "settings.json") and not profile_has_credentials(s.backends_cfg_dir / "empty" / "settings.json")
    assert sync_backends(settings) == []


def test_runner_status_and_version_cache(settings):
    reset_version_cache()
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="2.1.309 (Claude Code)\n", stderr="")

    which = lambda b: "/usr/bin/x" if b == settings.claude_bin else None
    st = runner_status(settings, which=which, run=run)
    assert st.claude.model_dump() == {"available": True, "version": "2.1.309", "authenticated": False} and st.codex.available is False
    runner_status(settings, which=which, run=run)
    assert len(calls) == 1
    assert cli_version("definitely-missing-binary-xyz") is None


def test_sync_backends_skips_unreadable_profile(settings, tmp_path):
    root = tmp_path / "backends"
    for name in ("aaa-locked", "zzz-ok"):
        (root / name).mkdir(parents=True)
        (root / name / "settings.json").write_text('{"apiKeyHelper": "x"}')
    locked = root / "aaa-locked" / "settings.json"
    locked.chmod(0o000)
    s = settings.model_copy(update={"backends_root": root})
    try:
        assert sync_backends(s) == ["zzz-ok"]
    finally:
        locked.chmod(0o600)
    assert (s.backends_cfg_dir / "zzz-ok" / "settings.json").exists()
    assert not (s.backends_cfg_dir / "aaa-locked").exists()

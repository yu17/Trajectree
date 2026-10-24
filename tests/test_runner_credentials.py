import json
import os
import stat
from types import SimpleNamespace

import pytest

from trajectree.runner.credentials import (
    cli_version,
    load_oauth_token,
    migrate_oauth_token,
    profile_has_credentials,
    reset_version_cache,
    runner_status,
    store_oauth_token,
    sync_backends,
)


def test_store_and_load_oauth_token(settings):
    path = store_oauth_token(settings, " sk-ant-oat01-abc \n")
    assert path == settings.secrets_dir / "claude_oauth_token"
    assert not (settings.claude_config_dir / "oauth_token").exists()   # never where Claude runs write
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


def test_sync_backends_keeps_only_env_model_and_api_key_helper(settings, tmp_path):
    root = tmp_path / "backends"
    full = {"env": {"ANTHROPIC_AUTH_TOKEN": "k"}, "model": "kimi-k2", "apiKeyHelper": "/bin/key",
            "permissions": {"allow": ["Bash(*)"]}, "hooks": {"Stop": []}, "statusLine": {"type": "command", "command": "x"}}
    for name, data in (("full", full), ("envonly", {k: full[k] for k in ("env", "permissions", "hooks", "statusLine")})):
        (root / name).mkdir(parents=True)
        (root / name / "settings.json").write_text(json.dumps(data))
        (root / name / ".claude.json").write_text('{"hasCompletedOnboarding": true, "x": 1}')
    for name, text in (("broken", "{not json"), ("alist", "[1, 2]")):
        (root / name).mkdir(parents=True)
        (root / name / "settings.json").write_text(text)
    s = settings.model_copy(update={"backends_root": root})
    assert sync_backends(s) == ["envonly", "full"]
    out = s.backends_cfg_dir
    assert json.loads((out / "full" / "settings.json").read_text()) == {"env": {"ANTHROPIC_AUTH_TOKEN": "k"}, "model": "kimi-k2", "apiKeyHelper": "/bin/key"}
    assert json.loads((out / "envonly" / "settings.json").read_text()) == {"env": {"ANTHROPIC_AUTH_TOKEN": "k"}}
    assert (out / "full" / ".claude.json").read_text() == '{"hasCompletedOnboarding": true, "x": 1}'   # copied as is
    assert not (out / "broken").exists() and not (out / "alist").exists()


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


def test_connector_tokens_roundtrip_and_validation(settings):
    import pytest

    from trajectree.runner.credentials import CONNECTOR_NAMES, load_connector_token, store_connector_token

    assert CONNECTOR_NAMES == ("github", "gitlab", "chatmaid")
    assert load_connector_token(settings, "github") is None
    path = store_connector_token(settings, "github", "  ghp_x \n")
    assert path == settings.connectors_dir / "github_token" and path.stat().st_mode & 0o777 == 0o600
    assert load_connector_token(settings, "github") == "ghp_x"
    with pytest.raises(ValueError):
        store_connector_token(settings, "github", "  ")
    with pytest.raises(ValueError):
        store_connector_token(settings, "../x", "t")


def test_migrates_a_regular_legacy_token_file(settings):
    settings.claude_config_dir.mkdir(parents=True)
    old = settings.claude_config_dir / "oauth_token"
    old.write_text("sk-ant-old\n")
    migrate_oauth_token(settings)
    assert load_oauth_token(settings) == "sk-ant-old" and not os.path.lexists(old)
    new = settings.secrets_dir / "claude_oauth_token"
    assert stat.S_IMODE(new.stat().st_mode) == 0o600 and stat.S_IMODE(settings.secrets_dir.stat().st_mode) == 0o700
    migrate_oauth_token(settings)   # nothing left to move
    assert load_oauth_token(settings) == "sk-ant-old"


def test_symlink_at_the_legacy_path_is_never_taken(settings):
    owner = settings.data_dir / "token"
    owner.parent.mkdir(parents=True)
    owner.write_text("owner-token")
    settings.claude_config_dir.mkdir(parents=True)
    old = settings.claude_config_dir / "oauth_token"
    old.symlink_to(owner)   # planted by a Claude run before the upgrade
    migrate_oauth_token(settings)
    assert load_oauth_token(settings) is None and not os.path.lexists(old) and owner.read_text() == "owner-token"
    store_oauth_token(settings, "sk-ant-real")
    old.symlink_to(owner)   # planted after the migration
    migrate_oauth_token(settings)
    assert load_oauth_token(settings) == "sk-ant-real" and not os.path.lexists(old)


def test_load_refuses_a_symlinked_token(settings):
    store_oauth_token(settings, "sk-ant-real")
    owner = settings.data_dir / "token"
    owner.parent.mkdir(parents=True)
    owner.write_text("owner-token")
    path = settings.secrets_dir / "claude_oauth_token"
    path.unlink()
    path.symlink_to(owner)
    assert load_oauth_token(settings) is None


def test_app_start_migrates_the_legacy_token(settings, ledger):
    from trajectree.api.app import create_app

    settings.claude_config_dir.mkdir(parents=True)
    (settings.claude_config_dir / "oauth_token").write_text("sk-ant-old")
    create_app(settings, ledger=ledger)
    assert load_oauth_token(settings) == "sk-ant-old"
    assert not (settings.claude_config_dir / "oauth_token").exists()


def test_store_oauth_token_does_not_write_through_planted_symlinks(settings, tmp_path):
    target = tmp_path / "elsewhere"
    settings.claude_config_dir.mkdir(parents=True)
    (settings.claude_config_dir / "settings.json").symlink_to(target)   # dangling: exists() is False
    store_oauth_token(settings, "sk-ant-x")
    assert not target.exists()


def test_sync_backends_replaces_a_planted_symlink(settings, tmp_path):
    owner = tmp_path / "owner-token"
    owner.write_text("owner-token")
    root = tmp_path / "backends"
    (root / "kimi").mkdir(parents=True)
    (root / "kimi" / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "k"}}))
    s = settings.model_copy(update={"backends_root": root})
    dest = s.backends_cfg_dir / "kimi"
    dest.mkdir(parents=True)
    (dest / "settings.json").symlink_to(owner)   # a ccx:kimi run can write its profile dir
    assert sync_backends(s) == ["kimi"]
    assert owner.read_text() == "owner-token" and not (dest / "settings.json").is_symlink()

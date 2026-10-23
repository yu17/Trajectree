import json
from tests.test_cli import env_for
from trajectree.cli import main as cli
import hashlib, stat
from trajectree.cli.hooks import merge_hooks, remove_hooks, token_label

EXISTING = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify"}]}],
                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "lint"}]}]},
            "statusLine": {"type": "command", "command": "x"}}

def test_merge_preserves_other_hooks_and_is_idempotent():
    url = "http://127.0.0.1:8742/api/hooks/claude"
    once = merge_hooks(EXISTING, url, "trj_1")
    twice = merge_hooks(once, url, "trj_2")
    for ev in ("SessionStart", "Stop", "SessionEnd"):
        ours = [h for g in twice["hooks"][ev] for h in g["hooks"] if h.get("url") == url]
        assert len(ours) == 1 and ours[0]["headers"]["Authorization"] == "Bearer trj_2" and ours[0]["type"] == "http" and ours[0]["timeout"] == 3
    assert twice["hooks"]["Stop"][0]["hooks"][0]["command"] == "notify" and twice["hooks"]["PreToolUse"] == EXISTING["hooks"]["PreToolUse"]
    assert twice["statusLine"] == EXISTING["statusLine"] and EXISTING["hooks"].get("SessionStart") is None   # input untouched
    assert remove_hooks(twice, url) == EXISTING

def test_install_writes_backup_rotates_token_and_authenticates(app, client, anon, tmp_path, capsys):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps(EXISTING))
    env = {**env_for(app), "HOME": str(tmp_path)}
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(f)], client=anon, env=env) == 0
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(f)], client=anon, env=env) == 0
    out = capsys.readouterr().out
    assert "Bearer ***" in out and "trj_" not in out and out.count("hooks: installed (3 events)") == 2
    assert (tmp_path / "settings.json.trajectree.bak").exists()
    written = json.loads(f.read_text())
    secret = written["hooks"]["SessionStart"][-1]["hooks"][0]["headers"]["Authorization"].split()[1]
    assert written["hooks"]["SessionStart"][-1]["hooks"][0]["url"] == "http://testserver/api/hooks/claude"
    assert [t["label"] for t in client.get("/api/tokens").json()] == [token_label(f)]   # rotated, not accumulated
    payload = {"session_id": "s1", "cwd": "/x", "hook_event_name": "SessionStart", "transcript_path": "/t"}
    assert anon.post("/api/hooks/claude", json=payload, headers={"Authorization": f"Bearer {secret}"}).status_code == 200
    assert cli.main(["hooks", "show", "--settings-file", str(f)], client=anon, env=env) == 0
    assert capsys.readouterr().out == "SessionStart: present\nStop: present\nSessionEnd: present\ntoken: present\n"
    assert cli.main(["hooks", "uninstall", "--settings-file", str(f)], client=anon, env=env) == 0
    assert json.loads(f.read_text()) == EXISTING and client.get("/api/tokens").json() == []

def test_install_aborts_without_confirmation(app, client, anon, tmp_path, capsys):
    f = tmp_path / "settings.json"
    env = {**env_for(app), "HOME": str(tmp_path)}
    old = client.post("/api/tokens", json={"label": "hooks", "scope": {"projects": [], "tools": ["hooks"], "write": False}, "ttl_s": None}).json()
    assert cli.main(["hooks", "install", "--settings-file", str(f)], client=anon, env=env) == 1   # stdin is not a tty
    assert "aborted" in capsys.readouterr().out and not f.exists()
    assert [t["id"] for t in client.get("/api/tokens").json()] == [old["id"]]   # old token survives the abort

def test_install_default_path_and_bad_json(app, anon, tmp_path, capsys):
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    (cfg / "settings.json").write_text("{not json")
    assert cli.main(["hooks", "install", "--yes"], client=anon, env={**env_for(app), "HOME": str(tmp_path)}) == 1
    assert "cannot parse" in capsys.readouterr().err

def test_backup_keeps_the_original_across_installs(app, anon, tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps(EXISTING))
    env = {**env_for(app), "HOME": str(tmp_path)}
    for _ in range(2):
        assert cli.main(["hooks", "install", "--yes", "--settings-file", str(f)], client=anon, env=env) == 0
    assert json.loads((tmp_path / "settings.json.trajectree.bak").read_text()) == EXISTING

def test_malformed_structure_is_rejected_before_minting(app, client, anon, tmp_path, capsys):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"hooks": {"Stop": "x"}}))
    env = {**env_for(app), "HOME": str(tmp_path)}
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(f)], client=anon, env=env) == 1
    assert "cannot parse" in capsys.readouterr().err and client.get("/api/tokens").json() == []

def _secret(path):
    return json.loads(path.read_text())["hooks"]["Stop"][-1]["hooks"][0]["headers"]["Authorization"].split()[1]

def test_token_label_is_per_resolved_settings_path(tmp_path):
    f = tmp_path / "a" / "settings.json"
    expected = "hooks:" + hashlib.sha256(str(f.resolve()).encode()).hexdigest()[:8]
    assert token_label(f) == expected and token_label(tmp_path / "a" / ".." / "a" / "settings.json") == expected
    assert token_label(tmp_path / "b" / "settings.json") != expected

def test_two_settings_files_keep_their_own_tokens(app, client, anon, tmp_path, capsys):
    env = {**env_for(app), "HOME": str(tmp_path)}
    one, two = tmp_path / "one" / "settings.json", tmp_path / "two" / "settings.json"
    for f in (one, two):
        assert cli.main(["hooks", "install", "--yes", "--settings-file", str(f)], client=anon, env=env) == 0
    assert sorted(t["label"] for t in client.get("/api/tokens").json()) == sorted([token_label(one), token_label(two)])
    payload = {"session_id": "s1", "cwd": "/x", "hook_event_name": "SessionStart", "transcript_path": "/t"}
    for f in (one, two):
        assert anon.post("/api/hooks/claude", json=payload, headers={"Authorization": f"Bearer {_secret(f)}"}).status_code == 200
    capsys.readouterr()
    assert cli.main(["hooks", "uninstall", "--settings-file", str(one)], client=anon, env=env) == 0
    assert [t["label"] for t in client.get("/api/tokens").json()] == [token_label(two)]
    assert anon.post("/api/hooks/claude", json=payload, headers={"Authorization": f"Bearer {_secret(two)}"}).status_code == 200
    capsys.readouterr()
    assert cli.main(["hooks", "show", "--settings-file", str(one)], client=anon, env=env) == 0
    assert capsys.readouterr().out.endswith("token: absent\n")
    assert cli.main(["hooks", "show", "--settings-file", str(two)], client=anon, env=env) == 0
    assert capsys.readouterr().out.endswith("token: present\n")

def test_fresh_install_is_private_and_existing_mode_is_kept(app, anon, tmp_path):
    env = {**env_for(app), "HOME": str(tmp_path)}
    fresh = tmp_path / "fresh" / "settings.json"
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(fresh)], client=anon, env=env) == 0
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600
    existing = tmp_path / "existing.json"
    existing.write_text(json.dumps(EXISTING))
    existing.chmod(0o644)
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(existing)], client=anon, env=env) == 0
    assert stat.S_IMODE(existing.stat().st_mode) == 0o644 and _secret(existing).startswith("trj_")
    assert not any(p.name.endswith(".tmp") for p in tmp_path.rglob("*"))

def test_uninstall_without_our_hooks_leaves_file_alone(app, anon, tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps(EXISTING))
    before = f.read_bytes()
    env = {**env_for(app), "HOME": str(tmp_path)}
    assert cli.main(["hooks", "uninstall", "--settings-file", str(f)], client=anon, env=env) == 0
    assert f.read_bytes() == before and not (tmp_path / "settings.json.trajectree.bak").exists()

def test_install_follows_symlink_and_rotates_legacy_tokens(app, client, anon, tmp_path, capsys):
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    legacy = client.post("/api/tokens", json={"label": "hooks", "scope": {"projects": [], "tools": ["hooks"], "write": False}}).json()
    env = {**env_for(app), "HOME": str(tmp_path)}
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(link)], client=anon, env=env) == 0
    assert link.is_symlink() and json.loads(real.read_text())["hooks"]
    assert (tmp_path / "real.json.trajectree.bak").exists()
    assert f"rotated legacy token {legacy['id']}" in capsys.readouterr().out
    assert [t["label"] for t in client.get("/api/tokens").json()] == [token_label(real)]

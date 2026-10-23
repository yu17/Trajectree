import json
from tests.test_cli import env_for
from trajectree.cli import main as cli
from trajectree.cli.hooks import merge_hooks, remove_hooks

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
    assert [t["label"] for t in client.get("/api/tokens").json()] == ["hooks"]   # rotated, not accumulated
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

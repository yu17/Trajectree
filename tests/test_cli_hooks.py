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
    assert remove_hooks(twice) == EXISTING

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

def test_show_reports_a_handler_for_another_url_as_stale(app, anon, tmp_path, capsys):
    f = tmp_path / "settings.json"
    old = "http://127.0.0.1:8742/api/hooks/claude"
    f.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "http", "url": old}]}]}}))
    env = {**env_for(app), "HOME": str(tmp_path)}
    assert cli.main(["hooks", "show", "--settings-file", str(f)], client=anon, env=env) == 0
    out = capsys.readouterr().out
    assert f"Stop: stale ({old}); run: trajectree hooks install\n" in out
    assert "SessionStart: absent\n" in out


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

def test_hooks_install_replaces_other_port(app, anon, tmp_path):
    f = tmp_path / "settings.json"
    stale = "http://127.0.0.1:8742/api/hooks/claude"
    other = "http://elsewhere/api/hooks/other"
    f.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "http", "url": stale}, {"type": "http", "url": other}]}]}}))
    env = {**env_for(app), "HOME": str(tmp_path)}
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(f)], client=anon, env=env) == 0
    written = json.loads(f.read_text())
    for ev in ("SessionStart", "Stop", "SessionEnd"):
        urls = [h["url"] for g in written["hooks"][ev] for h in g["hooks"]]
        assert urls.count("http://testserver/api/hooks/claude") == 1 and stale not in urls
    assert other in [h["url"] for g in written["hooks"]["Stop"] for h in g["hooks"]]

def test_hooks_uninstall_removes_any_port(app, anon, tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "http", "url": "http://127.0.0.1:8742/api/hooks/claude"}]}]}}))
    env = {**env_for(app), "HOME": str(tmp_path), "TRAJECTREE_URL": "http://127.0.0.1:40210"}
    assert cli.main(["hooks", "uninstall", "--settings-file", str(f)], client=anon, env=env) == 0
    assert json.loads(f.read_text()) == {}


# ---- Codex hooks. Every test points CODEX_HOME and HOME at tmp_path; none may touch the real ~/.codex. ----
import http.server, threading, io
from trajectree.cli import hooks as hk

OWNER = {"type": "command", "command": "owner-hook", "timeout": 5}


import pytest

SHAPES = pytest.mark.parametrize("wrapped", [False, True], ids=["flat", "wrapper"])


def _ev(doc):
    """The event -> groups mapping of a Codex hooks document, flat or wrapped."""
    return doc["hooks"] if "hooks" in doc else doc


def _codex(tmp_path, *, feature=True, wrapped=False):
    home = tmp_path / "codex"
    home.mkdir()
    events = {ev: [{"matcher": None, "hooks": [OWNER]}] for ev in ("PermissionRequest", "SessionStart", "Stop", "UserPromptSubmit")}
    (home / "hooks.json").write_text(json.dumps({"hooks": events} if wrapped else events))
    (home / "config.toml").write_text("[features]\nhooks = true\n" if feature else "[features]\n")
    env_home = tmp_path / "home"
    env_home.mkdir()
    return home, {"HOME": str(env_home), "CODEX_HOME": str(home)}


def test_codex_home_never_falls_back_to_the_real_home(tmp_path):
    assert hk.codex_home({}) is None and hk.codex_home({"HOME": str(tmp_path)}) is None
    (tmp_path / ".codex").mkdir()
    assert hk.codex_home({"HOME": str(tmp_path)}) == tmp_path / ".codex"
    assert hk.codex_home({"CODEX_HOME": str(tmp_path / "nope")}) is None


@SHAPES
def test_codex_install_appends_last_group_writes_token_file_and_show_reports_untrusted(app, client, anon, tmp_path, capsys, wrapped):
    home, extra = _codex(tmp_path, wrapped=wrapped)
    claude = tmp_path / "settings.json"
    env = {**env_for(app), **extra}
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(claude)], client=anon, env=env) == 0
    out = capsys.readouterr().out
    assert "codex hooks: installed (3 events); Codex will ask to trust them on its next start" in out
    assert "enable them" not in out and "trj_" not in out
    doc = json.loads((home / "hooks.json").read_text())
    assert ("hooks" in doc) == wrapped
    evs = _ev(doc)
    for ev in ("SessionStart", "Stop"):
        assert len(evs[ev]) == 2 and evs[ev][0]["hooks"] == [OWNER]
        h = evs[ev][-1]["hooks"][0]
        assert h["type"] == "command" and h["async"] is True and h["timeout"] == 3
    assert len(evs["SessionEnd"]) == 1 and len(evs["PermissionRequest"]) == 1
    token_file = tmp_path / "home" / ".config" / "trajectree" / "codex-hook-token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    secret = token_file.read_text().strip()
    assert secret.startswith("trj_") and secret not in json.dumps(doc) and str(token_file) in evs["Stop"][-1]["hooks"][0]["command"]
    assert any(t["label"].startswith("hooks-codex:") for t in client.get("/api/tokens").json())
    assert cli.main(["hooks", "show", "--settings-file", str(claude)], client=anon, env=env) == 0
    shown = capsys.readouterr().out
    assert "codex SessionStart: present (not yet trusted)" in shown and secret not in shown
    # Codex keys trust by snake_case event name; a CamelCase key is not a trust entry.
    (home / "config.toml").write_text(f'[features]\nhooks = true\n\n[hooks.state."{home / "hooks.json"}:SessionStart:1:0"]\ntrusted_hash = "sha256:x"\n')
    assert cli.main(["hooks", "show", "--settings-file", str(claude)], client=anon, env=env) == 0
    assert "codex SessionStart: present (not yet trusted)" in capsys.readouterr().out
    (home / "config.toml").write_text(f'[features]\nhooks = true\n\n[hooks.state."{home / "hooks.json"}:session_start:1:0"]\ntrusted_hash = "sha256:x"\n')
    assert cli.main(["hooks", "show", "--settings-file", str(claude)], client=anon, env=env) == 0
    shown = capsys.readouterr().out
    assert "codex SessionStart: present (trusted)" in shown and "codex Stop: present (not yet trusted)" in shown


def test_codex_reinstall_replaces_in_place_without_shifting_owner_groups(app, anon, tmp_path, capsys):
    home, extra = _codex(tmp_path)
    env = {**env_for(app), **extra}
    claude = tmp_path / "s.json"
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(claude)], client=anon, env=env) == 0
    doc = json.loads((home / "hooks.json").read_text())
    later = {"hooks": [{"type": "command", "command": "later"}]}
    doc["Stop"].append(later)
    (home / "hooks.json").write_text(json.dumps(doc))
    capsys.readouterr()
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(claude)], client=anon, env=env) == 0
    out = capsys.readouterr().out
    after = json.loads((home / "hooks.json").read_text())
    assert after["Stop"][0] == doc["Stop"][0] and after["Stop"][2] == later
    assert "trajectree hooks post" in after["Stop"][1]["hooks"][0]["command"] and len(after["Stop"]) == 3
    assert "shift" not in out and "re-trust" not in out


def test_codex_hooks_file_symlink_is_written_through(app, client, anon, tmp_path, capsys):
    home, extra = _codex(tmp_path)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "codex-hooks.json"
    (home / "hooks.json").rename(real)
    (home / "hooks.json").symlink_to(real)
    env = {**env_for(app), **extra}
    claude = tmp_path / "s.json"
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(claude)], client=anon, env=env) == 0
    link = home / "hooks.json"
    assert link.is_symlink() and link.resolve() == real
    assert "trajectree hooks post" in json.dumps(json.loads(real.read_text()))
    assert (dotfiles / "codex-hooks.json.trajectree.bak").exists()
    # the token label and the trust key keep the path Codex reads, the link
    assert any(t["label"] == hk.codex_token_label(link) for t in client.get("/api/tokens").json())
    assert cli.main(["hooks", "uninstall", "--settings-file", str(claude)], client=anon, env=env) == 0
    assert link.is_symlink() and "trajectree hooks post" not in real.read_text()
    assert not any(t["label"] == hk.codex_token_label(link) for t in client.get("/api/tokens").json())


def test_codex_token_file_is_private_even_with_a_stale_temp(app, anon, tmp_path):
    home, extra = _codex(tmp_path)
    env = {**env_for(app), **extra}
    cfg = tmp_path / "home" / ".config" / "trajectree"
    cfg.mkdir(parents=True)
    stale = cfg / "codex-hook-token.tmp"
    stale.write_text("old")
    stale.chmod(0o644)
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(tmp_path / "s.json")], client=anon, env=env) == 0
    assert stat.S_IMODE((cfg / "codex-hook-token").stat().st_mode) == 0o600 and not stale.exists()


def test_malformed_codex_file_stops_the_whole_install(app, client, anon, tmp_path, capsys):
    home, extra = _codex(tmp_path)
    (home / "hooks.json").write_text(json.dumps({"Stop": "x"}))
    env = {**env_for(app), **extra}
    claude = tmp_path / "s.json"
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(claude)], client=anon, env=env) == 1
    assert "cannot parse" in capsys.readouterr().err
    assert not claude.exists() and client.get("/api/tokens").json() == []
    assert not (tmp_path / "home" / ".config" / "trajectree" / "codex-hook-token").exists()


def test_codex_install_uses_wrapper_shape_when_the_file_does(app, anon, tmp_path):
    home, extra = _codex(tmp_path)
    (home / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [OWNER]}]}}))
    env = {**env_for(app), **extra}
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(tmp_path / "s.json")], client=anon, env=env) == 0
    doc = json.loads((home / "hooks.json").read_text())
    assert list(doc) == ["hooks"] and len(doc["hooks"]["Stop"]) == 2 and len(doc["hooks"]["SessionStart"]) == 1


@SHAPES
def test_codex_uninstall_removes_only_ours_and_warns_on_later_groups(app, client, anon, tmp_path, capsys, wrapped):
    home, extra = _codex(tmp_path, wrapped=wrapped)
    original = _ev(json.loads((home / "hooks.json").read_text()))
    claude = tmp_path / "settings.json"
    env = {**env_for(app), **extra}
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(claude)], client=anon, env=env) == 0
    doc = _ev(json.loads((home / "hooks.json").read_text()))
    doc["Stop"].append({"hooks": [{"type": "command", "command": "later"}]})
    doc = {"hooks": doc} if wrapped else doc
    (home / "hooks.json").write_text(json.dumps(doc))
    capsys.readouterr()
    assert cli.main(["hooks", "uninstall", "--settings-file", str(claude)], client=anon, env=env) == 0
    out = capsys.readouterr().out
    assert "codex hooks: removed; groups after Trajectree's shifted: re-trust Stop" in out
    after = _ev(json.loads((home / "hooks.json").read_text()))
    assert after["Stop"] == original["Stop"] + [{"hooks": [{"type": "command", "command": "later"}]}]
    assert after["SessionStart"] == original["SessionStart"] and "SessionEnd" not in after
    assert not (tmp_path / "home" / ".config" / "trajectree" / "codex-hook-token").exists()
    assert client.get("/api/tokens").json() == []


def test_codex_install_without_feature_flag_prints_the_flag_line(app, anon, tmp_path, capsys):
    home, extra = _codex(tmp_path, feature=False)
    env = {**env_for(app), **extra}
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(tmp_path / "s.json")], client=anon, env=env) == 0
    assert f"codex hooks: enable them with [features] hooks = true in {home / 'config.toml'}" in capsys.readouterr().out


def test_claude_install_skips_codex_when_env_has_no_codex_home(app, anon, tmp_path, capsys):
    env = {**env_for(app), "HOME": str(tmp_path)}
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(tmp_path / "s.json")], client=anon, env=env) == 0
    assert "codex" not in capsys.readouterr().out


def _stub():
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get("Authorization"), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_hooks_post_forwards_stdin_with_token(tmp_path, capsys):
    srv, seen = _stub()
    token_file = tmp_path / "tok"
    token_file.write_text("trj_secret\n")
    env = {"HOME": str(tmp_path), "TRAJECTREE_URL": f"http://127.0.0.1:{srv.server_port}"}
    body = '{"session_id": "s1", "hook_event_name": "Stop"}'
    assert cli.main(["hooks", "post", "--token-file", str(token_file)], env=env, stdin=io.StringIO(body)) == 0
    srv.shutdown()
    assert seen == [("/api/hooks/codex", "Bearer trj_secret", body.encode())]
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    down = {"HOME": str(tmp_path), "TRAJECTREE_URL": "http://127.0.0.1:1"}
    assert cli.main(["hooks", "post", "--token-file", str(token_file)], env=down, stdin=io.StringIO(body)) == 0
    assert cli.main(["hooks", "post", "--token-file", str(tmp_path / "missing")], env=down, stdin=io.StringIO(body)) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "trj_secret" not in captured.err

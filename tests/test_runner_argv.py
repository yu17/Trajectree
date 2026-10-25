import json
from pathlib import Path
import pytest
from trajectree.runner.argv import claude_argv, codex_argv, mask, mcp_config_json, run_env, run_secrets
from trajectree.runner.spec import MissingCredentials, RunSpec, ToolPolicy

def spec(**over):
    base = dict(run_id="r1", kind="ask", runner="claude", backend="claude", model=None, prompt="Q?", system_prompt=None,
                cwd=Path("/tmp/w"), tools=ToolPolicy(tools=[], allowed=["mcp__trajectree__*"]), usd_cap=0.5, timeout_s=120,
                mcp_url="http://127.0.0.1:8742/mcp", mcp_token="trj_secret", json_schema=None)
    return RunSpec(**{**base, **over})

def test_claude_argv_fixed_shape():
    s = spec(model="sonnet", json_schema={"type": "object"}, system_prompt="Be brief.",
             tools=ToolPolicy(tools=["Read", "Grep", "Glob", "Bash"], allowed=["Read", "Grep", "Glob", "Bash(git log *)", "mcp__trajectree__*"]))
    argv = claude_argv(s, binary="claude", mcp_config_path=Path("/data/runs/r1/mcp.json"))
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert "Q?" not in argv and not any("trj_secret" in a for a in argv)   # prompt on stdin, token in the config file
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk" and argv[argv.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in argv and argv[argv.index("--mcp-config") + 1] == "/data/runs/r1/mcp.json"
    cfg = json.loads(mcp_config_json(s))
    assert cfg["mcpServers"]["trajectree"] == {"type": "http", "url": "http://127.0.0.1:8742/mcp", "headers": {"Authorization": "Bearer trj_secret"}}
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,Bash"
    i = argv.index("--allowedTools")
    assert argv[i + 1:i + 6] == ["Read", "Grep", "Glob", "Bash(git log *)", "mcp__trajectree__*"]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.5" and argv[argv.index("--model") + 1] == "sonnet"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}
    assert argv[argv.index("--append-system-prompt") + 1] == "Be brief." and "--max-turns" not in argv
    assert "--disallowedTools" not in argv

def test_claude_argv_no_tools_and_optional_flags_absent():
    argv = claude_argv(spec(), binary="/opt/claude", mcp_config_path=Path("/m.json"))
    assert argv[argv.index("--tools") + 1] == "" and "--model" not in argv and "--json-schema" not in argv and "--append-system-prompt" not in argv
    assert "--disallowedTools" not in argv

def test_claude_argv_disallowed_tools():
    s = spec(tools=ToolPolicy(tools=["Read"], allowed=["Read"], disallowed=["Read(//cfg/**)", "Read(//data/**)"]))
    argv = claude_argv(s, binary="claude", mcp_config_path=Path("/m.json"))
    i = argv.index("--disallowedTools")
    assert argv[i + 1:i + 3] == ["Read(//cfg/**)", "Read(//data/**)"]

def test_codex_argv():
    argv = codex_argv(spec(runner="codex", backend="codex", model="gpt-5", system_prompt="Sys."), binary="codex",
                      schema_path=Path("/tmp/s.json"), last_message_path=Path("/tmp/last.md"))
    assert argv[:3] == ["codex", "exec", "--json"] and "--ephemeral" in argv
    assert 'mcp_servers.trajectree.url="http://127.0.0.1:8742/mcp"' in argv
    assert 'mcp_servers.trajectree.bearer_token_env_var="TRAJECTREE_RUN_TOKEN"' in argv
    assert argv[argv.index("-o") + 1] == "/tmp/last.md" and argv[argv.index("--output-schema") + 1] == "/tmp/s.json"
    assert argv[-1] == "Sys.\n\nQ?" and argv[argv.index("-m") + 1] == "gpt-5"

def test_codex_argv_uses_danger_full_access():
    argv = codex_argv(spec(runner="codex", backend="codex"), binary="codex", schema_path=None,
                      last_message_path=Path("/tmp/last.md"))
    i = argv.index("--sandbox")
    assert argv[i:i + 2] == ["--sandbox", "danger-full-access"] and "read-only" not in argv

def test_run_env_is_built_from_scratch(settings):
    base = {"PATH": "/usr/bin", "TRAJECTREE_DATA_DIR": "/data", "ANTHROPIC_API_KEY": "leak", "LANG": "C.UTF-8", "HTTPS_PROXY": "http://p"}
    s = settings.model_copy(update={"run_env_passthrough": ["HTTPS_PROXY", "TRAJECTREE_DATA_DIR", "ANTHROPIC_API_KEY"]})
    env = run_env(spec(), s, base=base, oauth_token="oat")
    assert env["PATH"] == "/usr/bin" and env["LANG"] == "C.UTF-8" and env["TERM"] == "dumb" and env["HTTPS_PROXY"] == "http://p"
    assert env["CLAUDE_CONFIG_DIR"] == str(s.claude_config_dir) and "HOME" not in env   # the service sets a per-run HOME
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oat" and env["DISABLE_AUTOUPDATER"] == "1"
    assert "TRAJECTREE_DATA_DIR" not in env and "ANTHROPIC_API_KEY" not in env and "TRAJECTREE_RUN_TOKEN" not in env

def test_run_env_ccx_and_codex(settings):
    prof = settings.backends_cfg_dir / "kimi"
    prof.mkdir(parents=True)
    (prof / "settings.json").write_text('{"env": {"ANTHROPIC_AUTH_TOKEN": "k"}}')
    env = run_env(spec(runner="ccx:kimi", backend="kimi"), settings, base={"PATH": "/bin"}, oauth_token=None)
    assert env["CLAUDE_CONFIG_DIR"] == str(prof) and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    env = run_env(spec(runner="codex", backend="codex"), settings, base={"PATH": "/bin"}, oauth_token=None)
    assert env["TRAJECTREE_RUN_TOKEN"] == "trj_secret" and env["CODEX_HOME"] == str(settings.codex_home)

def test_run_env_missing_credentials(settings):
    with pytest.raises(MissingCredentials, match="trajectree login"):
        run_env(spec(), settings, base={"PATH": "/bin"}, oauth_token=None)
    with pytest.raises(MissingCredentials, match="backends sync"):
        run_env(spec(runner="ccx:nope", backend="nope"), settings, base={"PATH": "/bin"}, oauth_token=None)

def test_mask():
    assert mask(["--mcp-config", '{"Authorization": "Bearer trj_s"}', "x"], ["trj_s", ""]) == ["--mcp-config", '{"Authorization": "Bearer ***"}', "x"]

def test_mcp_config_path_is_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    argv = claude_argv(spec(), binary="claude", mcp_config_path=Path("runs/x/mcp.json"))
    assert argv[argv.index("--mcp-config") + 1].startswith("/")

def test_codex_argv_leaves_code_mode_host_on():
    # The image ships codex-code-mode-host; turning it off only changes the per-run error.
    argv = codex_argv(spec(runner="codex", backend="codex"), binary="codex", schema_path=None,
                      last_message_path=Path("/tmp/last.md"))
    assert not any("code_mode_host" in a for a in argv)

def test_run_env_sets_unix_socket_for_ccx(settings, tmp_path):
    import socket
    prof = settings.backends_cfg_dir / "lmstudio"
    prof.mkdir(parents=True)
    (prof / "settings.json").write_text('{"env": {"ANTHROPIC_AUTH_TOKEN": "k"}}')
    d = tmp_path / "sockets"
    d.mkdir()
    s = settings.model_copy(update={"sockets_dir": d, "run_env_passthrough": ["ANTHROPIC_UNIX_SOCKET"]})
    base = {"PATH": "/bin", "ANTHROPIC_UNIX_SOCKET": "/elsewhere.sock"}
    ccx = spec(runner="ccx:lmstudio", backend="lmstudio")
    assert "ANTHROPIC_UNIX_SOCKET" not in run_env(ccx, s, base=base, oauth_token=None)
    socks = [socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) for _ in range(3)]
    for sock, name in zip(socks, ("lmstudio", "claude", "codex")):  # sockets named after the other runners too
        sock.bind(str(d / f"{name}.sock"))
    with socks[0], socks[1], socks[2]:
        assert run_env(ccx, s, base=base, oauth_token=None)["ANTHROPIC_UNIX_SOCKET"] == str(d / "lmstudio.sock")
        assert "ANTHROPIC_UNIX_SOCKET" not in run_env(spec(), s, base=base, oauth_token="oat")
        assert "ANTHROPIC_UNIX_SOCKET" not in run_env(spec(runner="codex", backend="codex"), s, base=base, oauth_token=None)


def test_run_env_exports_the_profile_env_with_a_socket(settings, tmp_path):
    # With a socket set, Claude Code ignores the profile's settings.json env ("Not logged in").
    import socket
    prof = settings.backends_cfg_dir / "lmstudio"
    prof.mkdir(parents=True)
    (prof / "settings.json").write_text(json.dumps({"env": {
        "ANTHROPIC_AUTH_TOKEN": "k", "ANTHROPIC_BASE_URL": "http://127.0.0.1:40090", "ANTHROPIC_MODEL": "qwen",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "65536", "ANTHROPIC_MAX_TOKENS": 5,
        # The confine wrapper starts unconfined, so nothing that could load code into it gets through.
        "LD_PRELOAD": "/cfg/backends/lmstudio/evil.so", "LD_LIBRARY_PATH": "/x", "NODE_OPTIONS": "--require /x",
        "PATH": "/evil", "LANG": "x", "DISABLE_AUTOUPDATER": "0", "CLAUDE_CONFIG_DIR": "/x",
        "ANTHROPIC_UNIX_SOCKET": "/y.sock", "TRAJECTREE_RUN_TOKEN": "t"}}))
    d = tmp_path / "sockets"
    d.mkdir()
    # A passthrough name never overrides the profile, as settings.json wins on a run without a socket.
    s = settings.model_copy(update={"sockets_dir": d, "run_env_passthrough": ["ANTHROPIC_MODEL"]})
    base = {"PATH": "/bin", "LANG": "C.UTF-8", "ANTHROPIC_MODEL": "from-daemon"}
    ccx = spec(runner="ccx:lmstudio", backend="lmstudio")
    assert "ANTHROPIC_AUTH_TOKEN" not in run_env(ccx, s, base=base, oauth_token=None)  # settings.json serves it
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(str(d / "lmstudio.sock"))
        env = run_env(ccx, s, base=base, oauth_token=None)
    assert (env["ANTHROPIC_AUTH_TOKEN"], env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_MODEL"]) == ("k", "http://127.0.0.1:40090", "qwen")
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "65536" and "ANTHROPIC_MAX_TOKENS" not in env
    assert not {"LD_PRELOAD", "LD_LIBRARY_PATH", "NODE_OPTIONS", "TRAJECTREE_RUN_TOKEN"} & set(env)
    assert (env["PATH"], env["LANG"], env["DISABLE_AUTOUPDATER"]) == ("/bin", "C.UTF-8", "1")
    assert env["CLAUDE_CONFIG_DIR"] == str(prof) and env["ANTHROPIC_UNIX_SOCKET"] == str(d / "lmstudio.sock")


def test_run_secrets_takes_credentials_not_token_counts():
    env = {"ANTHROPIC_AUTH_TOKEN": "a", "ANTHROPIC_API_KEY": "b", "ANTHROPIC_CUSTOM_HEADERS": "X: c",
           "TRAJECTREE_RUN_TOKEN": "d", "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "65536", "ANTHROPIC_MODEL": "m", "EMPTY_KEY": ""}
    assert sorted(run_secrets(env)) == ["X: c", "a", "b", "d"]

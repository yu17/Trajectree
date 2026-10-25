from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from trajectree.config import Settings
from trajectree.runner.credentials import profile_has_credentials, read_profile
from trajectree.runner.sockets import backend_socket
from trajectree.runner.spec import MissingCredentials, RunSpec

MCP_SERVER_NAME = "trajectree"
# ANTHROPIC_UNIX_SOCKET too: only a ccx run gets one, and only its own backend's.
_NEVER_PASSTHROUGH = {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_UNIX_SOCKET"}
# Variables a profile's env never overrides in a run (the service sets HOME and TMPDIR).
_RUNNER_OWNED = {"PATH", "HOME", "TMPDIR", "TERM", "CLAUDE_CONFIG_DIR", "ANTHROPIC_UNIX_SOCKET"}
# A profile key exported into a run's env, so it is masked like the run's own secrets.
PROFILE_SECRET_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


def mcp_config_json(spec: RunSpec) -> str:
    cfg = {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "type": "http",
                "url": spec.mcp_url,
                "headers": {"Authorization": f"Bearer {spec.mcp_token}"},
            }
        }
    }
    return json.dumps(cfg, separators=(",", ":"))


def claude_argv(spec: RunSpec, *, binary: str, mcp_config_path: Path) -> list[str]:
    """The headless argv. The prompt goes on stdin and the run token stays in the config file."""
    argv = [
        binary, "-p",
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--setting-sources", "user",
        "--strict-mcp-config",
        "--mcp-config", str(mcp_config_path.absolute()),
        "--tools", ",".join(spec.tools.tools),
        "--allowedTools", *spec.tools.allowed,
        "--max-budget-usd", str(spec.usd_cap),
    ]
    if spec.tools.disallowed:
        argv += ["--disallowedTools", *spec.tools.disallowed]
    if spec.model:
        argv += ["--model", spec.model]
    if spec.json_schema is not None:
        argv += ["--json-schema", json.dumps(spec.json_schema, separators=(",", ":"))]
    if spec.system_prompt:
        argv += ["--append-system-prompt", spec.system_prompt]
    return argv


def codex_argv(spec: RunSpec, *, binary: str, schema_path: Path | None, last_message_path: Path) -> list[str]:
    argv = [
        binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
        "--sandbox", "danger-full-access",  # the Landlock wrapper is the isolation (see runner/confine.py)
        "-C", str(spec.cwd),
        "-c", f'mcp_servers.{MCP_SERVER_NAME}.url="{spec.mcp_url}"',
        "-c", f'mcp_servers.{MCP_SERVER_NAME}.bearer_token_env_var="TRAJECTREE_RUN_TOKEN"',
        "-o", str(last_message_path),
    ]
    if schema_path is not None:
        argv += ["--output-schema", str(schema_path)]
    if spec.model:
        argv += ["-m", spec.model]
    prompt = f"{spec.system_prompt}\n\n{spec.prompt}" if spec.system_prompt else spec.prompt
    return argv + [prompt]


def run_env(spec: RunSpec, settings: Settings, *, base: Mapping[str, str], oauth_token: str | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "LANG"):
        if key in base:
            env[key] = base[key]
    env["TERM"] = "dumb"
    env["DISABLE_AUTOUPDATER"] = "1"
    if spec.runner == "claude":
        if not oauth_token:
            raise MissingCredentials("no Claude OAuth token stored; run: trajectree login")
        env["CLAUDE_CONFIG_DIR"] = str(settings.claude_config_dir)
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    elif spec.runner.startswith("ccx:"):
        profile = settings.backends_cfg_dir / spec.backend
        if not profile_has_credentials(profile / "settings.json"):
            raise MissingCredentials(f"backend profile {spec.backend!r} has no credentials; run: trajectree backends sync")
        env["CLAUDE_CONFIG_DIR"] = str(profile)
        # Claude Code sends its API traffic here; the profile's base URL still names host and path.
        if (sock := backend_socket(settings, spec.backend)) is not None:
            # With a socket set, Claude Code 2.1.310 ignores the settings.json env ("Not logged in"),
            # so the profile's variables go in the process env.
            env.update(_profile_env(profile / "settings.json"))
            env["ANTHROPIC_UNIX_SOCKET"] = str(sock)
    elif spec.runner == "codex":
        env["CODEX_HOME"] = str(settings.codex_home)
        env["TRAJECTREE_RUN_TOKEN"] = spec.mcp_token
    for name in settings.run_env_passthrough:
        if name in base and not name.startswith("TRAJECTREE_") and name not in _NEVER_PASSTHROUGH:
            env[name] = base[name]
    return env


def _profile_env(settings_json: Path) -> dict[str, str]:
    """A backend profile's string env values, minus the ones the runner sets itself."""
    data = read_profile(settings_json) or {}
    env = data.get("env")
    if not isinstance(env, dict):
        return {}
    return {
        k: v for k, v in env.items()
        if isinstance(k, str) and isinstance(v, str) and k not in _RUNNER_OWNED and not k.startswith("TRAJECTREE_")
    }


def mask_text(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def mask(argv: list[str], secrets: list[str]) -> list[str]:
    return [mask_text(arg, secrets) for arg in argv]

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from trajectree.config import Settings
from trajectree.runner.credentials import profile_has_credentials
from trajectree.runner.spec import MissingCredentials, RunSpec

MCP_SERVER_NAME = "trajectree"


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


def claude_argv(spec: RunSpec, *, binary: str) -> list[str]:
    argv = [
        binary, "-p", spec.prompt,
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--setting-sources", "user",
        "--strict-mcp-config",
        "--mcp-config", mcp_config_json(spec),
        "--tools", ",".join(spec.tools.tools),
        "--allowedTools", *spec.tools.allowed,
        "--max-budget-usd", str(spec.usd_cap),
    ]
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
        "--sandbox", "read-only",
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
    env["HOME"] = str(settings.run_home)
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
    elif spec.runner == "codex":
        env["CODEX_HOME"] = str(settings.codex_home)
        env["TRAJECTREE_RUN_TOKEN"] = spec.mcp_token
    for name in settings.run_env_passthrough:
        if name in base:
            env[name] = base[name]
    return env


def mask(argv: list[str], secrets: list[str]) -> list[str]:
    out = []
    for arg in argv:
        for secret in secrets:
            if secret:
                arg = arg.replace(secret, "***")
        out.append(arg)
    return out

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel

from trajectree.config import Settings
from trajectree.runner.safe_io import MiB, read_regular, write_private

OAUTH_FILE = "claude_oauth_token"  # in settings.secrets_dir
LEGACY_OAUTH_FILE = "oauth_token"  # in settings.claude_config_dir, which Claude runs may write
CONNECTOR_NAMES = ("github", "gitlab", "chatmaid")
_BACKEND_KEYS = ("env", "model", "apiKeyHelper")
_TOKEN_MAX_BYTES = 64 * 1024
PROFILE_MAX_BYTES = MiB

_versions: dict[str, str | None] = {}

log = logging.getLogger("trajectree.runner")


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def ensure_cfg_dirs(settings: Settings) -> None:
    for d in (
        settings.cfg_root,
        settings.secrets_dir,
        settings.claude_config_dir,
        settings.run_home,
        settings.backends_cfg_dir,
        settings.run_home / ".ssh",  # ssh's accept-new writes known_hosts under HOME=<run_home>
    ):
        _private_dir(d)


def _read_token(path: Path) -> str | None:
    try:
        data = read_regular(path, max_bytes=_TOKEN_MAX_BYTES)
    except OSError:
        return None
    token = data.decode("utf-8", errors="replace").strip() if data is not None else ""
    return token or None


def store_oauth_token(settings: Settings, token: str) -> Path:
    """Store the Claude OAuth token in the secrets dir, which no run may read or write."""
    token = token.strip()
    if not token:
        raise ValueError("empty token")
    ensure_cfg_dirs(settings)
    path = settings.secrets_dir / OAUTH_FILE
    write_private(path, token.encode())
    for name, content in (
        ("settings.json", "{}"),
        (".claude.json", json.dumps({"hasCompletedOnboarding": True})),
    ):
        target = settings.claude_config_dir / name
        if not os.path.lexists(target):
            write_private(target, content.encode())
    return path


def migrate_oauth_token(settings: Settings) -> None:
    """Move a token stored by an earlier version inside the Claude config dir (which Claude runs
    may write) to the secrets dir. Only a regular file is taken: a run may have planted a
    symlink there. The old entry is removed either way; a token already in the secrets dir wins."""
    old = settings.claude_config_dir / LEGACY_OAUTH_FILE
    if not os.path.lexists(old):
        return
    new = settings.secrets_dir / OAUTH_FILE
    token = _read_token(old)
    if token is None:
        log.warning("ignoring %s: not a regular token file", old)
    elif not new.exists():
        _private_dir(settings.cfg_root)
        _private_dir(settings.secrets_dir)
        write_private(new, token.encode())
    try:
        old.unlink()
    except OSError as exc:
        log.warning("could not remove %s: %s", old, exc.strerror or type(exc).__name__)


def load_oauth_token(settings: Settings) -> str | None:
    return _read_token(settings.secrets_dir / OAUTH_FILE)


def store_connector_token(settings: Settings, name: str, token: str) -> Path:
    """Write a connector's API token to `<cfg_root>/connectors/<name>_token` (0600)."""
    if name not in CONNECTOR_NAMES:
        raise ValueError(f"unknown connector {name!r}")
    token = token.strip()
    if not token:
        raise ValueError("empty token")
    _private_dir(settings.cfg_root)
    _private_dir(settings.connectors_dir)
    path = settings.connectors_dir / f"{name}_token"
    write_private(path, token.encode())
    return path


def load_connector_token(settings: Settings, name: str) -> str | None:
    if name not in CONNECTOR_NAMES:
        return None
    try:
        token = (settings.connectors_dir / f"{name}_token").read_text().strip()
    except OSError:
        return None
    return token or None


def read_profile(settings_json: Path) -> dict | None:
    """A synced backend profile's settings, read safely: ccx runs may write their profile dir."""
    try:
        raw = read_regular(settings_json, max_bytes=PROFILE_MAX_BYTES)
        data = json.loads(raw) if raw is not None else None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def profile_has_credentials(settings_json: Path) -> bool:
    data = read_profile(settings_json)
    if data is None:
        return False
    env = data.get("env")
    if isinstance(env, dict) and (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")):
        return True
    return bool(data.get("apiKeyHelper"))


def sync_backends(settings: Settings) -> list[str]:
    root = settings.backends_root
    if root is None or not root.is_dir():
        return []
    names: list[str] = []
    for profile in sorted(root.iterdir()):
        src = profile / "settings.json"
        if not profile.is_dir() or not src.is_file():
            continue
        dest = settings.backends_cfg_dir / profile.name
        onboarding = profile / ".claude.json"
        try:
            # Read everything first so an unreadable file leaves no half-copied profile.
            source = json.loads(src.read_bytes())
            if not isinstance(source, dict):
                continue
            # Only what selects the backend: hooks, permissions, status lines and the rest of
            # the owner's settings must not run or apply inside the daemon's runs.
            kept = {key: source[key] for key in _BACKEND_KEYS if key in source}
            settings_bytes = json.dumps(kept, indent=2).encode()
            onboarding_bytes = onboarding.read_bytes() if onboarding.is_file() else None
            _private_dir(settings.backends_cfg_dir)
            _private_dir(dest)
            write_private(dest / "settings.json", settings_bytes)
            if onboarding_bytes is not None:
                write_private(dest / ".claude.json", onboarding_bytes)
        except (OSError, ValueError):
            continue
        names.append(profile.name)
    return sorted(names)


class RunnerInfo(BaseModel):
    available: bool
    version: str | None
    authenticated: bool


class BackendInfo(BaseModel):
    name: str
    credentials: bool


class RunnerStatus(BaseModel):
    claude: RunnerInfo
    codex: RunnerInfo
    backends: list[BackendInfo]


def reset_version_cache() -> None:
    _versions.clear()


def cli_version(binary: str, *, run=subprocess.run) -> str | None:
    if binary in _versions:
        return _versions[binary]
    version: str | None = None
    try:
        proc = run([binary, "--version"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            tokens = proc.stdout.split()
            version = tokens[0] if tokens else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        version = None
    _versions[binary] = version
    return version


def runner_status(settings: Settings, *, which=shutil.which, run=subprocess.run) -> RunnerStatus:
    claude_ok = which(settings.claude_bin) is not None
    codex_ok = which(settings.codex_bin) is not None
    backends: list[BackendInfo] = []
    if settings.backends_cfg_dir.is_dir():
        for d in sorted(settings.backends_cfg_dir.iterdir()):
            if d.is_dir():
                backends.append(
                    BackendInfo(name=d.name, credentials=profile_has_credentials(d / "settings.json"))
                )
    return RunnerStatus(
        claude=RunnerInfo(
            available=claude_ok,
            version=cli_version(settings.claude_bin, run=run) if claude_ok else None,
            authenticated=load_oauth_token(settings) is not None,
        ),
        codex=RunnerInfo(
            available=codex_ok,
            version=cli_version(settings.codex_bin, run=run) if codex_ok else None,
            authenticated=(settings.codex_home / "auth.json").exists(),
        ),
        backends=backends,
    )

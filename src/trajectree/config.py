from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

log = logging.getLogger("trajectree.config")


class Settings(BaseModel):
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8742
    claude_home: Path | None = None
    backends_root: Path | None = None
    codex_sessions_root: Path | None = None  # the host's ~/.codex/sessions
    sockets_dir: Path | None = None  # <profile>.sock: a ccx backend reached through a host socket
    path_map: list[tuple[str, str]] = []
    ingest_interval_s: int = 300
    live_window_s: int = 900
    stale_settle_h: int = 24
    scan_since_days: int = 365
    cfg_dir: Path | None = None
    claude_bin: str = "claude"
    codex_bin: str = "codex"
    run_env_passthrough: list[str] = []
    confine_extra_ro: list[Path] = []  # dev/tests only: more read-only dirs for confined runs
    tz: str = "UTC"
    scheduler_interval_s: int = 30
    scheduler_grace_s: int = 3600
    digest_max_chars: int = 12000
    folders_scan_every_h: int = 24
    folders_big_dir_mb: int = 500
    remote_every_m: int = 30
    slurm_host: str | None = None
    slurm_user: str | None = None
    slurm_key: Path | None = None
    slurm_every_m: int = 15
    ssh_bin: str = "ssh"
    gitlab_host: str | None = None  # the one host polled as GitLab (and sent the GitLab token)
    chatmaid_url: str | None = None
    public_url: str | None = None  # where the owner reaches the daemon; only the ready line shows it
    marp_bin: str | None = None  # None: look up `marp` on PATH when exporting
    chrome_bin: str | None = None

    @property
    def connectors_dir(self) -> Path:
        return self.cfg_root / "connectors"

    @property
    def cfg_root(self) -> Path:
        return self.cfg_dir or self.data_dir / "cfg"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def prompts_dir(self) -> Path:
        return self.data_dir / "prompts"

    @property
    def secrets_dir(self) -> Path:
        """Daemon-owned runner secrets; outside every directory a run may read or write."""
        return self.cfg_root / "secrets"

    @property
    def claude_config_dir(self) -> Path:
        return self.cfg_root / "claude"

    @property
    def backends_cfg_dir(self) -> Path:
        return self.cfg_root / "backends"

    @property
    def codex_home(self) -> Path:
        return self.cfg_root / "codex"

    @property
    def run_home(self) -> Path:
        return self.cfg_root / "home"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "trajectree.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def reports_dir(self) -> Path:
        return self.exports_dir / "reports"


def _optional_dir(var: str, default: str) -> Path | None:
    """An env-configured directory: empty means none; unset means `default` if it exists."""
    if var in os.environ:
        value = os.environ[var]
        return Path(value).expanduser() if value else None
    path = Path(default).expanduser()
    return path if path.is_dir() else None


def _parse_path_map(raw: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in raw.split(";"):
        if not item.strip():
            continue
        host, sep, local = item.partition("=")
        if not sep or not host.strip() or not local.strip():
            raise ValueError(f"TRAJECTREE_PATH_MAP entry must be host=container: {item!r}")
        pairs.append((host.strip(), local.strip()))
    return pairs


_INT_ENV = {
    "TRAJECTREE_INGEST_EVERY": "ingest_interval_s",
    "TRAJECTREE_LIVE_WINDOW": "live_window_s",
    "TRAJECTREE_STALE_SETTLE_H": "stale_settle_h",
    "TRAJECTREE_SCAN_SINCE_DAYS": "scan_since_days",
    "TRAJECTREE_SCHEDULER_EVERY": "scheduler_interval_s",
    "TRAJECTREE_SCHEDULER_GRACE_S": "scheduler_grace_s",
    "TRAJECTREE_DIGEST_MAX_CHARS": "digest_max_chars",
    "TRAJECTREE_FOLDERS_EVERY_H": "folders_scan_every_h",
    "TRAJECTREE_BIG_DIR_MB": "folders_big_dir_mb",
    "TRAJECTREE_REMOTE_EVERY_M": "remote_every_m",
    "TRAJECTREE_SLURM_EVERY_M": "slurm_every_m",
}


_STR_ENV = {
    "TRAJECTREE_CLAUDE_BIN": "claude_bin",
    "TRAJECTREE_CODEX_BIN": "codex_bin",
    "TRAJECTREE_TZ": "tz",
    "TRAJECTREE_SSH_BIN": "ssh_bin",
}


def _valid_tz(value: str) -> str:
    """`value` when it names an IANA time zone; otherwise "UTC", with a warning."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):  # OSError: e.g. a directory such as "America"
        log.warning("TRAJECTREE_TZ=%r is not a known time zone; using UTC", value)
        return "UTC"
    return value


def load_settings() -> Settings:
    data_dir = Path(
        os.environ.get("TRAJECTREE_DATA_DIR", "~/.local/share/trajectree")
    ).expanduser()
    values: dict[str, object] = {"data_dir": data_dir}
    if "TRAJECTREE_HOST" in os.environ:
        values["host"] = os.environ["TRAJECTREE_HOST"]
    if "TRAJECTREE_PORT" in os.environ:
        values["port"] = int(os.environ["TRAJECTREE_PORT"])
    values["claude_home"] = _optional_dir("TRAJECTREE_CLAUDE_HOME", "~/.claude")
    values["backends_root"] = _optional_dir(
        "TRAJECTREE_BACKENDS_ROOT", "~/.config/claude-backends"
    )
    values["codex_sessions_root"] = _optional_dir("TRAJECTREE_CODEX_SESSIONS", "~/.codex/sessions")
    if "TRAJECTREE_PATH_MAP" in os.environ:
        values["path_map"] = _parse_path_map(os.environ["TRAJECTREE_PATH_MAP"])
    if os.environ.get("TRAJECTREE_CFG_DIR"):
        values["cfg_dir"] = Path(os.environ["TRAJECTREE_CFG_DIR"]).expanduser()
    if os.environ.get("TRAJECTREE_SOCKETS_DIR"):
        values["sockets_dir"] = Path(os.environ["TRAJECTREE_SOCKETS_DIR"]).expanduser()
    for var, field in _STR_ENV.items():
        if var in os.environ:
            values[field] = os.environ[var]
    if os.environ.get("TRAJECTREE_GITLAB_HOST", "").strip():
        values["gitlab_host"] = os.environ["TRAJECTREE_GITLAB_HOST"].strip().lower()
    if "tz" in values:
        values["tz"] = _valid_tz(str(values["tz"]))
    if os.environ.get("TRAJECTREE_CHATMAID_URL"):
        values["chatmaid_url"] = os.environ["TRAJECTREE_CHATMAID_URL"].strip()
    if os.environ.get("TRAJECTREE_PUBLIC_URL", "").strip():
        values["public_url"] = os.environ["TRAJECTREE_PUBLIC_URL"].strip()
    if os.environ.get("TRAJECTREE_MARP_BIN"):
        values["marp_bin"] = os.environ["TRAJECTREE_MARP_BIN"]
    if os.environ.get("TRAJECTREE_CHROME_BIN"):
        values["chrome_bin"] = os.environ["TRAJECTREE_CHROME_BIN"]
    if os.environ.get("TRAJECTREE_SLURM_HOST"):
        values["slurm_host"] = os.environ["TRAJECTREE_SLURM_HOST"]
    if os.environ.get("TRAJECTREE_SLURM_USER"):
        values["slurm_user"] = os.environ["TRAJECTREE_SLURM_USER"]
    if os.environ.get("TRAJECTREE_SLURM_KEY"):
        values["slurm_key"] = Path(os.environ["TRAJECTREE_SLURM_KEY"]).expanduser()
    if "TRAJECTREE_RUN_ENV_PASSTHROUGH" in os.environ:
        values["run_env_passthrough"] = [
            item.strip()
            for item in os.environ["TRAJECTREE_RUN_ENV_PASSTHROUGH"].split(",")
            if item.strip()
        ]
    if os.environ.get("TRAJECTREE_CONFINE_EXTRA_RO"):
        values["confine_extra_ro"] = [
            Path(item).expanduser() for item in os.environ["TRAJECTREE_CONFINE_EXTRA_RO"].split(":") if item
        ]
    for var, field in _INT_ENV.items():
        if var in os.environ:
            values[field] = int(os.environ[var])
    return Settings(**values)


def ensure_token(settings: Settings) -> str:
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = settings.token_path
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
        path.chmod(0o600)
    return path.read_text().strip()

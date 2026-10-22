from __future__ import annotations

import os
import secrets
from pathlib import Path

from pydantic import BaseModel


class Settings(BaseModel):
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8742
    claude_home: Path | None = None
    backends_root: Path | None = None
    path_map: list[tuple[str, str]] = []
    ingest_interval_s: int = 300
    live_window_s: int = 900
    stale_settle_h: int = 24
    scan_since_days: int = 365

    @property
    def db_path(self) -> Path:
        return self.data_dir / "trajectree.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


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
}


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
    if "TRAJECTREE_PATH_MAP" in os.environ:
        values["path_map"] = _parse_path_map(os.environ["TRAJECTREE_PATH_MAP"])
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

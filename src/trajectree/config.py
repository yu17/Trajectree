from __future__ import annotations

import os
import secrets
from pathlib import Path

from pydantic import BaseModel


class Settings(BaseModel):
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8742

    @property
    def db_path(self) -> Path:
        return self.data_dir / "trajectree.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


def load_settings() -> Settings:
    data_dir = Path(
        os.environ.get("TRAJECTREE_DATA_DIR", "~/.local/share/trajectree")
    ).expanduser()
    values: dict[str, object] = {"data_dir": data_dir}
    if "TRAJECTREE_HOST" in os.environ:
        values["host"] = os.environ["TRAJECTREE_HOST"]
    if "TRAJECTREE_PORT" in os.environ:
        values["port"] = int(os.environ["TRAJECTREE_PORT"])
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

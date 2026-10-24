"""Local backends reached through a host socket: `<sockets_dir>/<profile>.sock`, forwarded on
the host to the TCP address the profile names (deploy/systemd/trajectree-backend@.socket)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from trajectree.config import Settings


def backend_socket(settings: Settings, name: str) -> Path | None:
    """The socket for backend profile `name`, when the sockets dir holds one by that name."""
    if settings.sockets_dir is None:
        return None
    path = settings.sockets_dir / f"{name}.sock"
    try:
        return path if stat.S_ISSOCK(os.stat(path).st_mode) else None
    except OSError:
        return None

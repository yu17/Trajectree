"""Path helpers: host-to-container path mapping and Claude Code's project-dir naming."""

import re
from pathlib import Path, PurePosixPath

from trajectree.config import Settings


def to_local(settings: Settings, host_path: str) -> Path:
    """Map a host path through `settings.path_map` (longest component-wise prefix wins)."""
    target = PurePosixPath(host_path)
    best: tuple[PurePosixPath, str] | None = None
    for host, local in settings.path_map:
        root = PurePosixPath(host)
        if target.is_relative_to(root) and (best is None or len(root.parts) > len(best[0].parts)):
            best = (root, local)
    if best is None:
        return Path(host_path)
    root, local = best
    return Path(local) / target.relative_to(root)


def encode_project_dir(cwd: str) -> str:
    """Claude Code's transcript folder name for `cwd`: non-alphanumerics become `-` (lossy)."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)

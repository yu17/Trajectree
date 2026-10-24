"""Daemon-side file access inside directories a confined run may write.

A run can replace any file in its run dir or its runner's config dir with a symlink (to the
owner token, say), a FIFO or a huge file. The daemon reads and writes those files unconfined,
so it never follows a symlink there, never blocks on a special file and never reads more
than a bound. Only the last path component is protected: the directory itself must be one
the run cannot replace (its parent is not writable by any run).
"""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from pathlib import Path

MiB = 1 << 20


class RefusedFile(OSError):
    """The path exists but is not a regular file of at most the allowed size."""


def read_regular(path: Path, *, max_bytes: int) -> bytes | None:
    """The content of `path` when it is a regular file of at most `max_bytes`.

    Returns None when nothing is there. Raises `RefusedFile` for a symlink, a FIFO, a device,
    a directory or a file larger than `max_bytes` (refused whole, never truncated, so a
    caller never parses a cut-off document)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RefusedFile(errno.ELOOP, "symlink refused", str(path)) from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RefusedFile(errno.EINVAL, "not a regular file", str(path))
        if st.st_size > max_bytes:
            raise RefusedFile(errno.EFBIG, "file too large", str(path))
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, min(MiB, max_bytes + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:  # grew after the fstat
                raise RefusedFile(errno.EFBIG, "file too large", str(path))
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_private(path: Path, data: bytes) -> None:
    """Replace `path` with a new 0600 file holding `data`.

    The file is written beside `path` and renamed over it, so a symlink planted at `path` is
    replaced, never written through; a directory planted there makes this raise OSError."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # created 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            os.fchmod(f.fileno(), 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

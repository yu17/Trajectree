"""Confine a process with Landlock, then exec the runner command.

Every agent run starts as `python -m trajectree.runner.confine --ro ... --rw ... -- argv`.
Everything not listed is unreadable and unwritable for argv and its children. If
Landlock cannot be applied the wrapper exits 97 without running argv: a run never
proceeds unconfined.

Stdlib only and no trajectree imports: it starts before any restriction and must be cheap.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

CONFINE_FAILED_EXIT = 97

# x86_64 syscall numbers (the same on aarch64: 444-446 are in the common table).
_SYS_CREATE_RULESET, _SYS_ADD_RULE, _SYS_RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_EXECUTE, _WRITE_FILE, _READ_FILE, _READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_TRUNCATE, _IOCTL_DEV = 1 << 14, 1 << 15
_SCOPE_ABSTRACT_UNIX_SOCKET, _SCOPE_SIGNAL = 1 << 0, 1 << 1

_RO = _EXECUTE | _READ_FILE | _READ_DIR
_FILE_ONLY = _EXECUTE | _WRITE_FILE | _READ_FILE | _TRUNCATE | _IOCTL_DEV


class LandlockUnavailable(OSError):
    """Landlock is missing, disabled or refused the restriction."""


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _check(ret: int, what: str) -> int:
    if ret < 0:
        err = ctypes.get_errno()
        raise LandlockUnavailable(err, f"{what}: {os.strerror(err)}")
    return ret


def landlock_abi() -> int:
    """The kernel's Landlock ABI version; raises LandlockUnavailable if there is none."""
    return _check(_libc().syscall(_SYS_CREATE_RULESET, None, 0, _CREATE_RULESET_VERSION),
                  "landlock abi")


def _handled_fs(abi: int) -> int:
    handled = (1 << 13) - 1  # ABI 1: EXECUTE .. MAKE_SYM
    if abi >= 2:
        handled |= 1 << 13  # REFER
    if abi >= 3:
        handled |= _TRUNCATE
    if abi >= 5:
        handled |= _IOCTL_DEV
    return handled


def system_ro_paths() -> list[Path]:
    """What a process needs to start at all: system dirs and this interpreter's prefixes."""
    fixed = "/usr /lib /lib64 /bin /sbin /etc /opt /app /proc /sys".split()
    return [Path(p) for p in fixed] + [
        Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
    ]


def restrict(ro: Sequence[Path], rw: Sequence[Path]) -> int:
    """Restrict the calling process (and its future children); returns the Landlock ABI."""
    libc = _libc()
    abi = landlock_abi()
    handled = _handled_fs(abi)
    if abi >= 6:  # scope abstract unix sockets and signals to the sandbox
        attr = struct.pack("QQQ", handled, 0, _SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL)
    else:
        attr = struct.pack("QQ", handled, 0)
    ruleset = _check(libc.syscall(_SYS_CREATE_RULESET, attr, len(attr), 0), "create ruleset")
    try:
        for paths, access in ((ro, _RO), (rw, handled)):
            for path in paths:
                _add_rule(libc, ruleset, os.path.realpath(path), access & handled, handled)
        _check(libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "no_new_privs")
        _check(libc.syscall(_SYS_RESTRICT_SELF, ruleset, 0), "restrict self")
    finally:
        os.close(ruleset)
    return abi


def _add_rule(libc: ctypes.CDLL, ruleset: int, path: str, access: int, handled: int) -> None:
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError):
        return  # nonexistent paths grant nothing
    try:
        if not os.path.isdir(path):
            access &= _FILE_ONLY & handled
        rule = struct.pack("=Qi", access, fd)
        _check(libc.syscall(_SYS_ADD_RULE, ruleset, _RULE_PATH_BENEATH, rule, 0),
               f"add rule {path}")
    finally:
        os.close(fd)


def confine_argv(ro: Sequence[Path], rw: Sequence[Path], argv: Sequence[str], *,
                 python: str = sys.executable) -> list[str]:
    """The command line that runs argv under this wrapper."""
    return [python, "-m", "trajectree.runner.confine",
            "--ro", *map(str, ro), "--rw", *map(str, rw), "--", *argv]


def _parse(args: list[str]) -> tuple[list[Path], list[Path], list[str]]:
    if "--" not in args:
        raise ValueError("usage: confine --ro PATH... --rw PATH... -- COMMAND...")
    sep = args.index("--")
    ro: list[Path] = []
    rw: list[Path] = []
    target = None
    for item in args[:sep]:
        if item == "--ro":
            target = ro
        elif item == "--rw":
            target = rw
        elif target is None:
            raise ValueError(f"unexpected argument {item!r}")
        else:
            target.append(Path(item))
    cmd = args[sep + 1:]
    if not cmd:
        raise ValueError("no command given")
    return ro, rw, cmd


def main(argv: list[str]) -> NoReturn:
    try:
        ro, rw, cmd = _parse(argv)
        restrict(ro, rw)
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        print(f"trajectree-confine: landlock unavailable: {reason}", file=sys.stderr)
        raise SystemExit(CONFINE_FAILED_EXIT) from None
    try:
        os.execvp(cmd[0], cmd)
    except OSError as exc:  # already confined; report the exec failure as itself
        print(f"trajectree-confine: cannot run {cmd[0]}: {exc.strerror}", file=sys.stderr)
        raise SystemExit(127) from None


if __name__ == "__main__":
    main(sys.argv[1:])

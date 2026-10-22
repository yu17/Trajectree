from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel

from trajectree.runner.argv import mask

STDERR_TAIL = 2000


class RunOutcome(BaseModel):
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr_tail: str
    log_path: str
    duration_ms: int


def _mask_text(text: str, secrets: list[str]) -> str:
    return mask([text], secrets)[0]


def _signal_group(proc, sig: int, fallback) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        fallback()


def launch(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout_s: int,
    log_path: Path,
    secrets: list[str],
    popen=subprocess.Popen,
) -> RunOutcome:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    header = f"# argv: {shlex.join(mask(argv, secrets))}\n# cwd: {cwd}\n"
    start = time.monotonic()
    timed_out = False
    exit_code: int | None
    try:
        proc = popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError:
        log_path.write_text(header)
        return RunOutcome(
            exit_code=None, timed_out=False, stdout="", stderr_tail=f"{argv[0]}: not found",
            log_path=str(log_path), duration_ms=int((time.monotonic() - start) * 1000),
        )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _signal_group(proc, signal.SIGTERM, proc.terminate)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL, proc.kill)
        _signal_group(proc, signal.SIGKILL, proc.kill)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A grandchild escaped the process group and still holds the pipes.
            stdout, stderr = "", ""
    exit_code = proc.returncode
    duration_ms = int((time.monotonic() - start) * 1000)
    stdout = stdout or ""
    stderr = stderr or ""
    log_path.write_text(
        header
        + "--- stdout ---\n" + _mask_text(stdout, secrets) + "\n"
        + "--- stderr ---\n" + _mask_text(stderr, secrets) + "\n"
    )
    return RunOutcome(
        exit_code=exit_code, timed_out=timed_out, stdout=stdout,
        stderr_tail=_mask_text(stderr, secrets)[-STDERR_TAIL:],
        log_path=str(log_path), duration_ms=duration_ms,
    )

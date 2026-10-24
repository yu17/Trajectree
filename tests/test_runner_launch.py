import json, os, time
from pathlib import Path
from trajectree.runner.launch import launch
FAKE = str(Path(__file__).parent / "bin" / "fake-claude")

def test_launch_captures_and_masks(tmp_path):
    log = tmp_path / "r.log"
    out = launch([FAKE, "-p", "hi", "--mcp-config", '{"h": "Bearer trj_x"}'], env={**os.environ, "FAKE_CLAUDE_MODE": "ok"},
                 cwd=tmp_path / "w", timeout_s=20, log_path=log, secrets=["trj_x"])
    assert out.exit_code == 0 and not out.timed_out and json.loads(out.stdout.strip().splitlines()[-1])["subtype"] == "success"
    text = log.read_text()
    assert "trj_x" not in text and "Bearer ***" in text and "--- stdout ---" in text and out.duration_ms >= 0

def test_launch_feeds_stdin_text(tmp_path):
    got = tmp_path / "prompt.txt"
    out = launch([FAKE, "-p", "--output-format", "json"], env={**os.environ, "FAKE_CLAUDE_MODE": "ok", "FAKE_CLAUDE_PROMPT_FILE": str(got)},
                 cwd=tmp_path, timeout_s=20, log_path=tmp_path / "r.log", secrets=[], stdin_text="Prompt via stdin")
    assert out.exit_code == 0 and got.read_text() == "Prompt via stdin"
    assert "Prompt via stdin" not in (tmp_path / "r.log").read_text().split("--- stdout ---")[0]   # not in the argv header

def test_launch_kills_on_timeout(tmp_path):
    t0 = time.monotonic()
    out = launch([FAKE], env={**os.environ, "FAKE_CLAUDE_MODE": "sleep"}, cwd=tmp_path, timeout_s=1, log_path=tmp_path / "r.log", secrets=[])
    assert out.timed_out and time.monotonic() - t0 < 10

def test_launch_missing_binary(tmp_path):
    out = launch(["/definitely/missing/bin"], env={"PATH": "/bin"}, cwd=tmp_path, timeout_s=5, log_path=tmp_path / "r.log", secrets=[])
    assert out.exit_code is None and "not found" in out.stderr_tail

def test_launch_timeout_does_not_block_on_orphaned_grandchild(tmp_path):
    t0 = time.monotonic()
    out = launch([FAKE], env={**os.environ, "FAKE_CLAUDE_MODE": "orphan"}, cwd=tmp_path, timeout_s=1, log_path=tmp_path / "r.log", secrets=[])
    assert out.timed_out and time.monotonic() - t0 < 10

def test_launch_masks_stderr_tail(tmp_path):
    out = launch([FAKE], env={**os.environ, "FAKE_CLAUDE_MODE": "garbage", "FAKE_CLAUDE_STDERR": "leak trj_x"}, cwd=tmp_path,
                 timeout_s=5, log_path=tmp_path / "r.log", secrets=["trj_x"])
    assert "trj_x" not in out.stderr_tail and "leak ***" in out.stderr_tail

def test_fake_claude_garbage_and_version(tmp_path):
    out = launch([FAKE], env={**os.environ, "FAKE_CLAUDE_MODE": "garbage"}, cwd=tmp_path, timeout_s=5, log_path=tmp_path / "r.log", secrets=[])
    assert out.exit_code == 1 and "boom" in out.stderr_tail
    out = launch([FAKE, "--version"], env={**os.environ}, cwd=tmp_path, timeout_s=5, log_path=tmp_path / "v.log", secrets=[])
    assert out.stdout.startswith("2.1.309")

def _gone(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):   # already reaped
        return True
    return state in ("Z", "X")   # killed, waiting for its new parent to reap it

def test_launch_kills_backgrounded_children_after_exit(tmp_path):
    # The runner exits at once; the child it left behind must not outlive the run.
    out = launch(["/bin/sh", "-c", "sleep 30 >/dev/null 2>&1 </dev/null & echo $!"], env={"PATH": "/bin:/usr/bin"},
                 cwd=tmp_path, timeout_s=10, log_path=tmp_path / "r.log", secrets=[])
    pid = int(out.stdout.strip())
    assert out.exit_code == 0 and not out.timed_out
    deadline = time.monotonic() + 5
    while not _gone(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(pid)

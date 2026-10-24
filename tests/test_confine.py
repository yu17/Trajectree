from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from trajectree.runner import confine
from trajectree.runner.confine import LandlockUnavailable, confine_argv, system_ro_paths


@pytest.fixture(scope="module")
def kernel():
    try:
        return confine.landlock_abi()
    except LandlockUnavailable:
        pytest.skip("Landlock is not available on this kernel")


def run_confined(ro, rw, argv):
    cmd = confine_argv([*system_ro_paths(), *ro], rw, argv, python=sys.executable)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


@pytest.fixture
def tree(tmp_path):
    allowed = tmp_path / "allowed"
    secret = tmp_path / "secret"
    allowed.mkdir()
    secret.mkdir()
    (allowed / "f").write_text("visible")
    (secret / "token").write_text("hidden")
    return tmp_path


def test_denied_file_is_unreadable(kernel, tree):
    r = run_confined([tree / "allowed"], [], ["cat", str(tree / "secret" / "token")])
    assert r.returncode != 0
    assert "Permission denied" in r.stderr


def test_allowed_file_is_readable(kernel, tree):
    r = run_confined([tree / "allowed"], [], ["cat", str(tree / "allowed" / "f")])
    assert r.returncode == 0, r.stderr
    assert r.stdout == "visible"


def test_rw_dir_is_writable_ro_dir_is_not(kernel, tree):
    rw = tree / "rw"
    ro = tree / "ro"
    rw.mkdir()
    ro.mkdir()
    run_confined([ro], [rw], ["sh", "-c", f"echo x > {rw}/a; echo x > {ro}/b"])
    assert (rw / "a").exists()
    assert not (ro / "b").exists()


def test_children_inherit_the_restriction(kernel, tree):
    secret = tree / "secret" / "token"
    r = run_confined([tree / "allowed"], [], ["sh", "-c", f'sh -c "cat {secret}"'])
    assert r.returncode != 0
    assert "Permission denied" in r.stderr


def test_missing_paths_are_skipped(kernel, tree):
    r = run_confined(
        [tree / "allowed", tree / "does-not-exist"], [tree / "nope"],
        ["cat", str(tree / "allowed" / "f")],
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == "visible"


def test_proc_escape_is_denied(kernel, tree):
    secret = tree / "secret" / "token"
    try:
        Path(f"/proc/1/root{secret}").read_text()
    except OSError:
        pytest.skip("/proc/1/root is not readable even without Landlock")
    r = run_confined([tree / "allowed"], [], ["cat", f"/proc/1/root{secret}"])
    assert r.returncode != 0
    assert "Permission denied" in r.stderr


def test_unavailable_exits_97(monkeypatch, capsys):
    def boom():
        raise LandlockUnavailable("not supported")

    monkeypatch.setattr(confine, "landlock_abi", boom)
    with pytest.raises(SystemExit) as exc:
        confine.main(["--ro", "/usr", "--", "true"])
    assert exc.value.code == 97
    assert capsys.readouterr().err.startswith(
        "trajectree-confine: landlock unavailable: not supported"
    )


def test_cwd_shadow_modules_do_not_run_before_restriction(kernel, tmp_path):
    cwd = tmp_path / "untrusted"
    out = tmp_path / "out"
    cwd.mkdir()
    out.mkdir()
    marker = out / "ran"
    (cwd / "struct.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    cmd = confine_argv(system_ro_paths(), [], ["true"], python=sys.executable)
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert not marker.exists()


def test_confine_argv_shape():
    assert confine_argv(
        [Path("/a")], [Path("/b")], ["x", "--y"], python="/py"
    ) == [
        "/py", "-I", "-m", "trajectree.runner.confine",
        "--ro", "/a", "--rw", "/b", "--", "x", "--y",
    ]

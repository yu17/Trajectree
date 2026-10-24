import os
import stat
import time

import pytest

from trajectree.runner.safe_io import RefusedFile, read_regular, write_private


def test_reads_a_regular_file_and_none_when_missing(tmp_path):
    (tmp_path / "f").write_bytes(b"hello")
    assert read_regular(tmp_path / "f", max_bytes=5) == b"hello"
    assert read_regular(tmp_path / "missing", max_bytes=5) is None
    assert read_regular(tmp_path / "f" / "below", max_bytes=5) is None   # a parent that is a file


def test_refuses_symlinks_even_to_regular_files(tmp_path):
    (tmp_path / "secret").write_text("owner-token")
    (tmp_path / "link").symlink_to(tmp_path / "secret")
    with pytest.raises(RefusedFile):
        read_regular(tmp_path / "link", max_bytes=1 << 20)


def test_refuses_a_fifo_promptly(tmp_path):
    os.mkfifo(tmp_path / "fifo")
    t0 = time.monotonic()
    with pytest.raises(RefusedFile):
        read_regular(tmp_path / "fifo", max_bytes=1 << 20)
    assert time.monotonic() - t0 < 1


def test_refuses_a_directory_and_an_oversized_file(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "big").write_bytes(b"x" * 11)
    with pytest.raises(RefusedFile):
        read_regular(tmp_path / "d", max_bytes=10)
    with pytest.raises(RefusedFile):
        read_regular(tmp_path / "big", max_bytes=10)   # refused whole, never truncated


def test_write_private_replaces_a_planted_symlink_instead_of_following_it(tmp_path):
    target = tmp_path / "token"
    target.write_text("owner-token")
    (tmp_path / "settings.json").symlink_to(target)
    write_private(tmp_path / "settings.json", b"{}")
    assert target.read_text() == "owner-token"
    out = tmp_path / "settings.json"
    assert not out.is_symlink() and out.read_bytes() == b"{}" and stat.S_IMODE(out.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json", "token"]   # no temp file left

import os
from datetime import datetime, timezone

from trajectree.connectors.folders import (
    collect_folders,
    folders_due,
    read_folder_states,
    scan_folder,
    write_folders,
)
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.projects import create_project
from trajectree.repo.resources import list_resources, upsert_resource
from trajectree.repo.signals import list_signals
from trajectree.repo.state import get_state, set_state

UTC = timezone.utc
T1 = datetime(2026, 3, 10, 12, 0, tzinfo=UTC).timestamp()
D1 = "2026-03-10"
EPOCH_BEFORE_T1 = datetime(2026, 3, 1, tzinfo=UTC)
NOW = datetime(2026, 3, 12, 8, 0, tzinfo=UTC)


def test_scan_folder_counts_per_day_honours_gitignore_and_excludes(tmp_path):
    root = tmp_path / "p"
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / ".gitignore").write_text("*.log\n")
    (root / "src" / "a.py").write_text("x")
    (root / "src" / "b.log").write_text("x")
    (root / "node_modules" / "x" / "c.js").write_text("x")
    os.utime(root / "src" / "a.py", (T1, T1))
    os.utime(root / ".gitignore", (T1 - 30 * 86400, T1 - 30 * 86400))
    days = scan_folder(root, since=EPOCH_BEFORE_T1)
    assert list(days) == [D1] and days[D1].count == 1 and days[D1].paths == ["src/a.py"]


def test_big_dir_flagged_not_descended(tmp_path):
    root = tmp_path / "p"
    (root / "data").mkdir(parents=True)
    (root / "data" / "blob.bin").write_bytes(b"0" * (2 * 1024 * 1024))
    (root / "keep.txt").write_text("x")
    for f in (root / "data" / "blob.bin", root / "keep.txt"):
        os.utime(f, (T1, T1))
    days = scan_folder(root, since=EPOCH_BEFORE_T1, big_dir_mb=1)
    assert days[D1].big_dirs == ["data"] and days[D1].count == 1 and days[D1].paths == ["keep.txt"]


def test_symlinks_not_followed_and_max_entries(tmp_path):
    root = tmp_path / "p"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "o.txt").write_text("x")
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    for i in range(5):
        f = root / f"f{i}.txt"
        f.write_text("x")
        os.utime(f, (T1, T1))
    assert scan_folder(root, since=EPOCH_BEFORE_T1)[D1].count == 5
    assert scan_folder(root, since=EPOCH_BEFORE_T1, max_entries=3)[D1].count < 5


def test_collect_and_write_folders_signals(ledger, human, settings, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    os.utime(root / "a.txt", (T1, T1))
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=str(root)))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=str(tmp_path / "gone")))
    resources = list_resources(ledger.conn, kinds=["folder"])
    states = read_folder_states(ledger.conn, resources)
    c = collect_folders(settings, resources, states, now=NOW, since_days=30)
    assert len(c.signals) == 1 and len(c.errors) == 1 and c.errors[0].startswith(str(tmp_path / "gone"))
    good = next(r for r in resources if r.locator == str(root))
    assert c.polled == [good.id]
    assert list_signals(ledger.conn, project_ids=[p.id]) == []
    assert write_folders(ledger.conn, c, now=NOW) == 1
    (sig,) = list_signals(ledger.conn, project_ids=[p.id], kinds=["files_changed"])
    assert sig.dedupe_key == f"folder:{good.id}:{D1}"
    assert sig.payload["count"] == 1 and sig.payload["bytes"] == 5 and sig.payload["paths"] == ["a.txt"]
    assert get_state(ledger.conn, f"folders:{good.id}")["last_scan"]
    assert get_state(ledger.conn, "folders:last") is not None
    # second pass is idempotent
    c2 = collect_folders(settings, resources, read_folder_states(ledger.conn, resources), now=NOW, since_days=30)
    write_folders(ledger.conn, c2, now=NOW)
    assert len(list_signals(ledger.conn, project_ids=[p.id], kinds=["files_changed"])) == 1


def test_folders_due(conn):
    assert folders_due(conn, now=NOW, every_h=24)
    assert not folders_due(conn, now=NOW, every_h=0)
    set_state(conn, "folders:last", {"at": NOW.isoformat()})
    assert not folders_due(conn, now=NOW.replace(hour=20), every_h=24)
    assert folders_due(conn, now=NOW.replace(day=14), every_h=24)
    assert not folders_due(conn, now=NOW.replace(day=14), every_h=0)

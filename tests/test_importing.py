from datetime import date, datetime, timezone

from trajectree.importing import (
    SESSION_SOURCE_OF_BACKEND, ImportRecord, SourceChoice, default_since, local_midnight, source_picked, source_start,
)
from trajectree.models import Project

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def project(sources=None) -> Project:
    props = {}
    if sources is not None:
        props["import"] = ImportRecord(sources=sources, recorded_at=NOW).model_dump(mode="json")
    return Project(
        id="p", slug="p", title="P", kind="research", status="active", lane_order=0, summary_md="",
        props=props, created_at=NOW, updated_at=NOW,
    )  # fmt: skip


def start(p, source, tz="UTC"):
    return source_start(p, source, now=NOW, default_days=30, tz=tz)


def test_no_record_means_v1_behaviour():
    p = project()
    assert all(source_picked(p, s) for s in ("git", "claude", "codex", "folder", "remote"))
    assert start(p, "git") == datetime(2026, 9, 22, tzinfo=UTC)


def test_all_means_none():
    assert start(project({"git": SourceChoice(picked=True, since="all")}), "git") is None


def test_date_is_local_midnight():
    p = project({"git": SourceChoice(picked=True, since=date(2025, 1, 15))})
    assert start(p, "git", tz="America/Chicago") == datetime(2025, 1, 15, 6, tzinfo=UTC)


def test_default_start_is_local_midnight_of_the_default_day():
    now = datetime(2026, 10, 22, 20, tzinfo=UTC)  # 05:00 on the 23rd in Tokyo
    day = default_since(now, 30, "Asia/Tokyo")
    assert day == date(2026, 9, 23)
    expected = local_midnight(day, "Asia/Tokyo")
    assert expected == datetime(2026, 9, 22, 15, tzinfo=UTC)
    got = source_start(project(), "git", now=now, default_days=30, tz="Asia/Tokyo")
    assert got == expected


def test_remote_follows_git():
    p = project({"git": SourceChoice(picked=True, since=date(2025, 1, 15)), "remote": SourceChoice(picked=False)})
    assert start(p, "remote") == datetime(2025, 1, 15, tzinfo=UTC)
    assert start(project({"git": SourceChoice(picked=True)}), "remote") == datetime(2026, 9, 22, tzinfo=UTC)


def test_unpicked_source():
    p = project({"git": SourceChoice(picked=True), "claude": SourceChoice(picked=False)})
    assert source_picked(p, "git") and not source_picked(p, "claude") and not source_picked(p, "folder")


def test_session_source_of_backend():
    assert SESSION_SOURCE_OF_BACKEND["codex"] == "codex"
    assert SESSION_SOURCE_OF_BACKEND["claude"] == "claude" and SESSION_SOURCE_OF_BACKEND["deepseek"] == "claude"

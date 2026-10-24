"""Per-project import choices: which sources a project pulled history from, and from when.

The record lives at `project.props["import"]`. A project without one (the `inbox` project,
API-created projects) behaves as before: every source picked, the default start.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ValidationError

from trajectree.models import Project

IMPORT_SOURCES = ("git", "claude", "codex", "folder", "remote")

# The import source a run backend's transcripts belong to: codex, or claude for any other.
SESSION_SOURCE_OF_BACKEND: defaultdict[str, str] = defaultdict(lambda: "claude", {"codex": "codex"})


class SourceChoice(BaseModel):
    picked: bool
    since: date | Literal["all"] | None = None  # None: the default cutoff; "all": everything


class ImportRecord(BaseModel):
    sources: dict[str, SourceChoice]
    recorded_at: AwareDatetime
    start_run_id: str | None = None


def import_record(project: Project) -> ImportRecord | None:
    raw = project.props.get("import")
    if raw is None:
        return None
    try:
        return ImportRecord.model_validate(raw)
    except ValidationError:
        return None


def source_picked(project: Project, source: str) -> bool:
    """True when the project picked `source`; a project with no record picked everything."""
    record = import_record(project)
    if record is None:
        return True
    choice = record.sources.get(source)
    return choice is not None and choice.picked


def source_start(
    project: Project, source: str, *, now: datetime, default_days: int, tz: str
) -> datetime | None:
    """Where history import for `source` starts (UTC); `None` means from the beginning."""
    record = import_record(project)
    choice = record.sources.get("git" if source == "remote" else source) if record else None
    since = choice.since if choice is not None else None
    if since == "all":
        return None
    if isinstance(since, date):
        return datetime.combine(since, time.min, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
    first = (now - timedelta(days=default_days)).astimezone(timezone.utc)
    return first.replace(hour=0, minute=0, second=0, microsecond=0)

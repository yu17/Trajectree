"""Recurrence materializer: turns a routine project's RRULE into planned instance events.

Instances are ordinary events marked `props.instance` (their start) and `props.rrule` (the rule
they were made from). Changing the rule drops the future planned ones of the old rule; past ones
stay as history.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from pydantic import BaseModel

from trajectree.db.ledger import Ledger
from trajectree.models import Actor, EventIn, EventPatch
from trajectree.repo.events import create_event, list_events, update_event
from trajectree.repo.projects import list_projects

HORIZON_DAYS = 56
MATERIALIZER = Actor(kind="connector", ref="recurrence", tool="recurrence")


class MaterializeReport(BaseModel):
    created: int
    dropped: int
    errors: list[str]


def instances(rule: str, *, start: datetime, until: datetime) -> list[datetime]:
    """Occurrences of `rule` in [start, until]; ValueError when the rule cannot be parsed."""
    try:
        parsed = rrulestr(rule, dtstart=start)
        return list(parsed.between(start, until, inc=True))
    except ValueError:
        raise
    except Exception as exc:  # dateutil can raise TypeError/AttributeError on odd input
        raise ValueError(str(exc)) from exc


def _parse_instance(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def materialize(ledger: Ledger, *, now: datetime, tz: str = "UTC") -> MaterializeReport:
    """Create the missing planned instances for every recurring project; take the lock per write.

    Rules are expanded in `tz` from the start of the local day of `now` (a stable anchor, so
    seconds and unset hour/minute never drift between passes); instances are stored in UTC.
    """
    created = dropped = 0
    errors: list[str] = []
    zone = ZoneInfo(tz)
    start = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    until = now + timedelta(days=HORIZON_DAYS)
    with ledger.locked() as conn:
        projects = [p for p in list_projects(conn) if p.recurrence]
        existing = {
            p.id: [e for e in list_events(conn, project_ids=[p.id]) if e.props.get("instance")]
            for p in projects
        }
    for project in projects:
        rule = project.recurrence or ""
        try:
            local = instances(rule, start=start, until=until.astimezone(zone))
            wanted = [w.astimezone(timezone.utc) for w in local if w >= now]
            current: set[datetime] = set()
            for event in existing[project.id]:
                if event.props.get("rrule") == rule:
                    parsed = _parse_instance(event.props.get("instance"))
                    if parsed is not None:
                        current.add(parsed)
                elif event.status == "planned" and event.t_start > now:
                    update_event(
                        ledger,
                        MATERIALIZER,
                        event.id,
                        EventPatch(status="dropped"),
                        intent="recurrence rule changed",
                        auto_accept=True,
                    )
                    dropped += 1
            minutes = project.props.get("instance_minutes", 60)
            for when in wanted:
                if when in current:
                    continue
                create_event(
                    ledger,
                    MATERIALIZER,
                    EventIn(
                        project_id=project.id,
                        kind=project.props.get("instance_kind", "meeting"),
                        title=project.props.get("instance_title", project.title),
                        t_start=when,
                        t_end=when + timedelta(minutes=minutes),
                        status="planned",
                        source="connector",
                        props={"instance": when.isoformat(), "rrule": rule},
                    ),
                    intent="materialize recurrence",
                    auto_accept=True,
                )
                created += 1
        except Exception as exc:  # one bad project never stops the others
            errors.append(f"{project.slug}: {exc}")
    return MaterializeReport(created=created, dropped=dropped, errors=errors)

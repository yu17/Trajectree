from datetime import datetime, timezone
from trajectree.context.digest import activity_digest
from trajectree.models import ProjectIn, SignalIn
from trajectree.repo.projects import create_project
from trajectree.repo.signals import upsert_signals

UTC = timezone.utc
SINCE, UNTIL = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)

def sig(p, kind, day, key, payload):
    return SignalIn(project_id=p.id, kind=kind, t_start=datetime(2026, 1, day, 9, tzinfo=UTC), dedupe_key=key, payload=payload)

def test_activity_digest_formats_each_kind_newest_first(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    assert activity_digest(ledger.conn, project_ids=[p.id], since=SINCE, until=UNTIL) == "_no activity recorded_"
    upsert_signals(ledger.conn, [
        sig(p, "commits", 2, "c", {"count": 4, "subjects": ["one", "two", "three", "four"]}),
        sig(p, "session", 3, "s", {"session_id": "abcdef123456", "title": None, "prompts": 5, "model": "opus"}),
        sig(p, "files_changed", 4, "f", {"count": 5, "paths": ["a.py", "b.py", "c.py", "d.py"]}),
        sig(p, "job_state", 5, "j", {"name": "train", "state": "failed"}),
        sig(p, "remote", 6, "r", {"count": 2, "titles": ["PR 1", "PR 2"]}),
        sig(p, "message", 7, "m", {"channel": "slack", "summary": "ship it"}),
    ])
    lines = activity_digest(ledger.conn, project_ids=[p.id], since=SINCE, until=UNTIL).splitlines()
    assert lines == [
        "- 2026-01-07 message via slack: ship it",
        "- 2026-01-06 2 remote updates: PR 1; PR 2",
        "- 2026-01-05 job train failed",
        "- 2026-01-04 5 files changed (a.py, b.py, c.py)",
        '- 2026-01-03 session "abcdef12" (5 prompts, opus)',
        "- 2026-01-02 4 commits: one; two; three",
    ]

def test_activity_digest_cuts_at_max_chars(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_signals(ledger.conn, [sig(p, "job_state", d, f"j{d}", {"name": "train", "state": "ok"}) for d in range(1, 11)])
    out = activity_digest(ledger.conn, project_ids=[p.id], since=SINCE, until=UNTIL, max_chars=100).splitlines()
    assert out[0] == "- 2026-01-10 job train ok" and out[-1].startswith("(… ") and out[-1].endswith(" more)")
    shown = len(out) - 1
    assert out[-1] == f"(… {10 - shown} more)" and 0 < shown < 10

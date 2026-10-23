from datetime import datetime, timezone

from trajectree.models import RoutinePatch
from trajectree.repo.routines import get_routine_by_name, update_routine
from trajectree.runner.seeds import ensure_seed_routines

NOW = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)


def test_seed_routines_once(conn, settings):
    assert ensure_seed_routines(conn, settings, now=NOW) == ["cross-project-review", "meeting-prep"]
    update_routine(conn, get_routine_by_name(conn, "meeting-prep").id, RoutinePatch(enabled=True), now=NOW)
    assert ensure_seed_routines(conn, settings, now=NOW) == []
    assert get_routine_by_name(conn, "meeting-prep").enabled is True


def test_seed_routine_values(conn, settings):
    settings.tz = "Europe/Berlin"
    ensure_seed_routines(conn, settings, now=NOW)
    review = get_routine_by_name(conn, "cross-project-review")
    prep = get_routine_by_name(conn, "meeting-prep")
    assert (review.trigger, review.tz, review.enabled, review.prompt_ref, review.scope.lookback_days) == (
        "0 7 * * 1", "Europe/Berlin", False, "cross_project_review.md", 14)
    assert (review.cwd_mode, review.tool_policy, review.write_mode, review.delivery) == ("data_dir", [], "direct", "inbox")
    assert (prep.trigger, prep.tz, prep.prompt_ref, prep.scope.lookback_days, prep.write_mode, prep.delivery) == (
        "0 7 * * 1-5", "Europe/Berlin", "meeting_prep.md", 7, "propose", "inbox")
    assert prep.enabled is False and prep.next_fire is None

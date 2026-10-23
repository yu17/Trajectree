"""Seed routines: created disabled on first start, never overwritten once they exist."""

from __future__ import annotations

from datetime import datetime

import apsw

from trajectree.config import Settings
from trajectree.models import RoutineIn, RoutineScope
from trajectree.repo.routines import create_routine, get_routine_by_name

SEED_ROUTINES: list[RoutineIn] = [
    RoutineIn(
        name="cross-project-review",
        trigger="0 7 * * 1",
        enabled=False,
        prompt_ref="cross_project_review.md",
        scope=RoutineScope(lookback_days=14),
        cwd_mode="data_dir",
        tool_policy=[],
        write_mode="direct",
        delivery="inbox",
    ),
    RoutineIn(
        name="meeting-prep",
        trigger="0 7 * * 1-5",
        enabled=False,
        prompt_ref="meeting_prep.md",
        scope=RoutineScope(lookback_days=7),
        write_mode="propose",
        delivery="inbox",
    ),
]


def ensure_seed_routines(conn: apsw.Connection, settings: Settings, *, now: datetime) -> list[str]:
    """Create each seed routine that is missing by name (in `settings.tz`); return the names created."""
    created: list[str] = []
    for seed in SEED_ROUTINES:
        if get_routine_by_name(conn, seed.name) is None:
            data = RoutineIn.model_validate({**seed.model_dump(), "tz": settings.tz})  # validates the tz
            create_routine(conn, data, now=now)
            created.append(seed.name)
    return created

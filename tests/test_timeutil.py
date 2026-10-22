from datetime import datetime, timezone, timedelta
import pytest
from trajectree.timeutil import to_db, from_db


def test_to_db_normalizes_to_utc_z():
    dt = datetime(2026, 10, 21, 19, 0, tzinfo=timezone(timedelta(hours=2)))
    assert to_db(dt) == "2026-10-21T17:00:00Z"


def test_from_db_round_trips():
    assert to_db(from_db("2026-10-21T17:00:00Z")) == "2026-10-21T17:00:00Z"


def test_to_db_rejects_naive():
    with pytest.raises(ValueError):
        to_db(datetime(2026, 10, 21))

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError
from trajectree.models import EventIn, ProjectIn, EdgeIn

UTC = timezone.utc

def test_event_rejects_end_before_start():
    with pytest.raises(ValidationError):
        EventIn(project_id="p", kind="meeting", title="x",
                t_start=datetime(2026,1,2,tzinfo=UTC), t_end=datetime(2026,1,1,tzinfo=UTC))

def test_naive_datetime_rejected():
    with pytest.raises(ValidationError):
        EventIn(project_id="p", kind="note", title="x", t_start=datetime(2026,1,1))

def test_project_slug_pattern():
    with pytest.raises(ValidationError):
        ProjectIn(slug="Not Valid!", title="t", kind="research")
    assert ProjectIn(slug="clmi-v2", title="t", kind="research").slug == "clmi-v2"

def test_edge_type_restricted():
    with pytest.raises(ValidationError):
        EdgeIn(src_type="thing", src_id="a", dst_type="event", dst_id="b", rel="relates_to")

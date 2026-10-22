from ulid import ULID


def new_id() -> str:
    """Return a new 26-character ULID string (sortable by creation time)."""
    return str(ULID())

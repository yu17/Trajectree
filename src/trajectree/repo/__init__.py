"""Repository layer: plain functions over the ledger and read connections."""


class NotFound(Exception):
    """The addressed entity does not exist."""


class DuplicateSlug(Exception):
    """A project with this slug already exists."""


class DuplicateEdge(Exception):
    """An edge with the same endpoints and relation already exists."""


class InvalidRange(ValueError):
    """An update would leave an event ending before it starts."""

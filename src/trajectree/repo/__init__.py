"""Repository layer: plain functions over the ledger and read connections."""


class NotFound(Exception):
    """The addressed entity does not exist."""


class UnknownProject(Exception):
    """A project name given by a caller matches no project; see `projects.unknown_project`."""


class DuplicateSlug(Exception):
    """A project with this slug already exists."""


class DuplicateEdge(Exception):
    """An edge with the same endpoints and relation already exists."""


class DuplicateRoutine(Exception):
    """A routine with this name already exists."""


class InvalidRange(ValueError):
    """An update would leave an event ending before it starts."""

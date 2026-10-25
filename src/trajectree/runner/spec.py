from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from trajectree.models import RunKind


class MissingCredentials(Exception):
    pass


class RunsPaused(Exception):
    pass


class AlreadyApplied(Exception):
    pass


class InspectionActive(Exception):
    """An `inspect` or `inspect_boundaries` run of the project is queued or running."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"an inspection is already running for {slug}")
        self.slug = slug


class NothingToInspect(Exception):
    """The project has no settled uninspected stretch."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"nothing to inspect for {slug}")
        self.slug = slug


class ToolPolicy(BaseModel):
    tools: list[str]
    allowed: list[str]
    disallowed: list[str] = []


class RunSpec(BaseModel):
    run_id: str
    kind: RunKind
    runner: str
    backend: str
    model: str | None
    prompt: str
    system_prompt: str | None
    cwd: Path
    tools: ToolPolicy
    usd_cap: float
    timeout_s: int
    mcp_url: str
    mcp_token: str
    json_schema: dict | None

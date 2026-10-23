"""Scopes and principals: what a caller may touch and who it is."""

from pydantic import BaseModel

from trajectree.models import Actor


class Scope(BaseModel):
    projects: list[str] | None = None
    tools: list[str] | None = None
    write: bool = True

    def allows(self, tool: str, project_id: str | None, *, write: bool) -> bool:
        if write and not self.write:
            return False
        if self.tools is not None and tool not in self.tools:
            return False
        if self.projects is not None:
            if project_id is None:
                # a project-less (inbox) row is outside every project scope for writes
                return not write
            if project_id not in self.projects:
                return False
        return True


class Principal(BaseModel):
    actor: Actor
    scope: Scope
    token_id: str | None = None


def owner_principal(session_ref: str | None = None) -> Principal:
    """Principal for the owner's own session (`session_ref` when known): full scope, no token row."""
    return Principal(
        actor=Actor(kind="session", ref=session_ref or "owner", session_id=session_ref, tool="mcp"),
        scope=Scope(),
        token_id=None,
    )

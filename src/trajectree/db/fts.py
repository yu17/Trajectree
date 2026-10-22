"""FTS5 search index over events, todos and projects, kept in sync by triggers.

Virtual tables cannot be session-attached, so the ledger's changesets never contain search rows.
"""

import apsw
from pydantic import BaseModel

# (source table, title column, body column, kind, project_id column)
_SOURCES: tuple[tuple[str, str, str, str, str], ...] = (
    ("event", "title", "body_md", "event", "project_id"),
    ("todo", "title", "body_md", "todo", "project_id"),
    ("project", "title", "summary_md", "project", "id"),
)

_SPECIAL_CHARS = '"()*:^-'


class SearchHit(BaseModel):
    id: str
    kind: str
    project_id: str | None
    title: str
    snippet: str


def _insert_sql(title: str, body: str, kind: str, pid: str) -> str:
    return (
        "INSERT INTO search_fts(id, kind, project_id, title, body) "
        f"VALUES (new.id, '{kind}', new.{pid}, new.{title}, new.{body});"
    )


def install(conn: apsw.Connection) -> None:
    """Create the search table and its triggers; safe to call on every open."""
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5("
        "id UNINDEXED, kind UNINDEXED, project_id UNINDEXED, title, body)"
    )
    for table, title, body, kind, pid in _SOURCES:
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_fts_ai AFTER INSERT ON {table} BEGIN "
            f"{_insert_sql(title, body, kind, pid)} END"
        )
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_fts_au AFTER UPDATE ON {table} BEGIN "
            "DELETE FROM search_fts WHERE id = old.id; "
            f"{_insert_sql(title, body, kind, pid)} END"
        )
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_fts_ad AFTER DELETE ON {table} BEGIN "
            "DELETE FROM search_fts WHERE id = old.id; END"
        )


def rebuild(conn: apsw.Connection) -> None:
    """Drop every search row and re-index from the source tables."""
    with conn:
        conn.execute("DELETE FROM search_fts")
        for table, title, body, kind, pid in _SOURCES:
            conn.execute(
                "INSERT INTO search_fts(id, kind, project_id, title, body) "
                f"SELECT id, '{kind}', {pid}, {title}, {body} FROM {table}"
            )


def _match_expr(q: str) -> str | None:
    if not q.strip():
        return None
    if any(c in q for c in _SPECIAL_CHARS):
        return '"' + q.replace('"', '""') + '"'
    return q


def search(conn: apsw.Connection, q: str, *, limit: int = 50) -> list[SearchHit]:
    """Ranked full-text hits for `q`, with a bracketed snippet of the body."""
    expr = _match_expr(q)
    if expr is None:
        return []
    rows = conn.execute(
        "SELECT id, kind, project_id, title, snippet(search_fts, 4, '[', ']', '…', 12) "
        "FROM search_fts WHERE search_fts MATCH ? ORDER BY rank LIMIT ?",
        (expr, limit),
    ).fetchall()
    return [SearchHit(id=r[0], kind=r[1], project_id=r[2], title=r[3], snippet=r[4]) for r in rows]

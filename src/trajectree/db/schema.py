"""Phase 1 SQLite schema, versioned migrations and column introspection."""

import weakref

import apsw

from trajectree.db import fts

LEDGER_TABLES: tuple[str, ...] = ("project", "event", "todo", "resource", "edge")

_V1_STATEMENTS: list[str] = [
    """CREATE TABLE project (
        id TEXT PRIMARY KEY,
        slug TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        lane_order INTEGER NOT NULL DEFAULT 0,
        summary_md TEXT NOT NULL DEFAULT '',
        recurrence TEXT,
        props TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE event (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES project(id),
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        body_md TEXT NOT NULL DEFAULT '',
        t_start TEXT NOT NULL,
        t_end TEXT,
        status TEXT NOT NULL DEFAULT 'happened',
        source TEXT NOT NULL DEFAULT 'human',
        props TEXT NOT NULL DEFAULT '{}',
        created_txn TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX event_project_t_start ON event(project_id, t_start)",
    """CREATE TABLE todo (
        id TEXT PRIMARY KEY,
        project_id TEXT REFERENCES project(id),
        title TEXT NOT NULL,
        body_md TEXT NOT NULL DEFAULT '',
        due TEXT,
        state TEXT NOT NULL DEFAULT 'open',
        priority INTEGER NOT NULL DEFAULT 0,
        props TEXT NOT NULL DEFAULT '{}',
        created_at TEXT,
        updated_at TEXT
    )""",
    "CREATE INDEX todo_project_due ON todo(project_id, due)",
    """CREATE TABLE resource (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES project(id),
        kind TEXT NOT NULL,
        locator TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        props TEXT NOT NULL DEFAULT '{}',
        created_at TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE edge (
        id TEXT PRIMARY KEY,
        src_type TEXT NOT NULL,
        src_id TEXT NOT NULL,
        dst_type TEXT NOT NULL,
        dst_id TEXT NOT NULL,
        rel TEXT NOT NULL,
        props TEXT NOT NULL DEFAULT '{}',
        valid_from TEXT,
        valid_to TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(src_type, src_id, dst_type, dst_id, rel)
    )""",
    "CREATE INDEX edge_src_id ON edge(src_id)",
    "CREATE INDEX edge_dst_id ON edge(dst_id)",
    """CREATE TABLE txn (
        id TEXT PRIMARY KEY,
        ts TEXT NOT NULL,
        actor_kind TEXT NOT NULL,
        actor_ref TEXT NOT NULL,
        model TEXT,
        backend TEXT,
        tool TEXT,
        tool_version TEXT,
        session_id TEXT,
        run_id TEXT,
        proposed_by_run TEXT,
        intent TEXT NOT NULL,
        changeset BLOB NOT NULL,
        review TEXT NOT NULL,
        review_ts TEXT,
        reverts_txn TEXT,
        reverted_by TEXT
    )""",
    "CREATE INDEX txn_ts ON txn(ts)",
    """CREATE TABLE change (
        txn_id TEXT NOT NULL REFERENCES txn(id),
        seq INTEGER NOT NULL,
        table_name TEXT NOT NULL,
        op TEXT NOT NULL,
        pk TEXT NOT NULL,
        old TEXT,
        new TEXT,
        PRIMARY KEY (txn_id, seq)
    )""",
    "CREATE INDEX change_table_pk ON change(table_name, pk)",
    "CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
]

# Ordered migration steps; a later schema version appends (version, statements).
MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, _V1_STATEMENTS),
]

SCHEMA_VERSION: int = MIGRATIONS[-1][0]


def migrate(conn: apsw.Connection) -> None:
    """Apply every migration step newer than the database's user_version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        with conn:  # one transaction per step
            for statement in statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
    fts.install(conn)  # idempotent; runs on every open so existing databases get it too


_columns_cache: "weakref.WeakKeyDictionary[apsw.Connection, dict[str, list[str]]]" = (
    weakref.WeakKeyDictionary()
)


def table_columns(conn: apsw.Connection, table: str) -> list[str]:
    """Column names of `table` in table order, cached per connection."""
    per_conn = _columns_cache.setdefault(conn, {})
    if table not in per_conn:
        per_conn[table] = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    return per_conn[table]

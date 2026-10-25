"""Phase 1 SQLite schema, versioned migrations and column introspection."""

import weakref

import apsw

from trajectree.db import fts

LEDGER_TABLES: tuple[str, ...] = ("project", "event", "todo", "resource", "edge", "report")

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

_V2_STATEMENTS: list[str] = [
    "ALTER TABLE project ADD COLUMN summary_at TEXT",
    "CREATE TABLE signal (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, resource_id TEXT, kind TEXT NOT NULL, t_start TEXT NOT NULL, t_end TEXT, payload TEXT NOT NULL DEFAULT '{}', dedupe_key TEXT UNIQUE NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX signal_project_t ON signal(project_id, t_start)",
    "CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE token (id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL, label TEXT NOT NULL, scope TEXT NOT NULL, actor TEXT NOT NULL, expires_at TEXT, created_at TEXT NOT NULL)",
]

_V3_STATEMENTS: list[str] = [
    "CREATE TABLE run (id TEXT PRIMARY KEY, kind TEXT NOT NULL, project_id TEXT, status TEXT NOT NULL, runner TEXT NOT NULL, backend TEXT NOT NULL, model TEXT, session_id TEXT, started TEXT, finished TEXT, tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, tokens_cache INTEGER NOT NULL DEFAULT 0, est_usd REAL NOT NULL DEFAULT 0, turns INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0, result_md TEXT NOT NULL DEFAULT '', proposals TEXT NOT NULL DEFAULT '[]', error TEXT, log_path TEXT, token_id TEXT, props TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)",
    "CREATE INDEX run_created ON run(created_at)",
    "CREATE INDEX run_status ON run(status)",
    "CREATE TABLE session (id TEXT PRIMARY KEY, cwd TEXT NOT NULL, project_id TEXT, transcript_path TEXT, backend TEXT NOT NULL DEFAULT 'claude', started_at TEXT NOT NULL, last_seen TEXT NOT NULL, ended_at TEXT, end_reason TEXT, source TEXT)",
    "CREATE INDEX session_cwd ON session(cwd)",
]

_V4_STATEMENTS: list[str] = [
    """CREATE TABLE routine (
        id TEXT PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        trigger TEXT NOT NULL,
        tz TEXT NOT NULL DEFAULT 'UTC',
        enabled INTEGER NOT NULL DEFAULT 0,
        runner TEXT,
        model TEXT,
        prompt_ref TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT '{}',
        cwd_mode TEXT NOT NULL DEFAULT 'data_dir',
        tool_policy TEXT NOT NULL DEFAULT '[]',
        caps TEXT NOT NULL DEFAULT '{}',
        write_mode TEXT NOT NULL DEFAULT 'propose',
        delivery TEXT NOT NULL DEFAULT 'none',
        next_fire TEXT,
        last_fired TEXT,
        last_run_id TEXT,
        last_error TEXT,
        paused_until TEXT,
        props TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "ALTER TABLE run ADD COLUMN routine_id TEXT",
    "ALTER TABLE run ADD COLUMN slot TEXT NOT NULL DEFAULT 'interactive'",
    "ALTER TABLE run ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1",
    "CREATE INDEX run_routine ON run(routine_id, created_at)",
    "CREATE INDEX run_slot_status ON run(slot, status)",
    """CREATE TABLE report (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        kind TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT '{}',
        source_md TEXT NOT NULL DEFAULT '',
        props TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
]

_V5_STATEMENTS: list[str] = [
    "ALTER TABLE event ADD COLUMN tier TEXT NOT NULL DEFAULT 'block'",
    "UPDATE event SET tier = 'pill' WHERE kind IN ('idea', 'note')",
    # Session signals are rebuilt per work segment: drop the old-key ones and the stored scan
    # state so the first pass re-reads every transcript.
    "DELETE FROM signal WHERE kind = 'session'",
    "DELETE FROM state WHERE key LIKE 'scan:%'",
]

# Ordered migration steps; a later schema version appends (version, statements).
MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, _V1_STATEMENTS),
    (2, _V2_STATEMENTS),
    (3, _V3_STATEMENTS),
    (4, _V4_STATEMENTS),
    (5, _V5_STATEMENTS),
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

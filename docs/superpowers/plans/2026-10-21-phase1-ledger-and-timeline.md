# Phase 1: Ledger and Minimal Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A running Trajectree container where the owner creates projects, events, todos and links, sees them on the lane-model-A timeline, inspects every transaction's diff in a ledger view, and reverts or reviews any transaction.

**Architecture:** One Python daemon (FastAPI) owns a single SQLite file through a `Ledger` that wraps every mutation in a SQLite Session-extension changeset, stores it with author metadata, and can invert and reapply it. A React + d3 single-page app renders the timeline from a JSON API. A thin CLI talks to the same API. Phase 1 has no LLM runs, connectors, or MCP; it builds the substrate the later phases write into.

**Tech Stack:** Python 3.12, uv, apsw >= 3.54.1.0 (Session extension, FTS5), FastAPI, uvicorn, pydantic v2, python-ulid, httpx; React 19, TypeScript, Vite, d3 v7, TanStack Query; pytest, vitest, Playwright; Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-21-trajectree-architecture-design.md` (sections 5, 9, 10, 11, 12, 14; mock at `docs/superpowers/specs/mockups/2026-10-21-timeline-lane-model-a.html`).

Phases 2 to 4 of the spec get their own plans once this one ships.

## Global Constraints

- Python 3.12+ managed by uv; apsw from PyPI wheels (Session extension included); SQLite in WAL mode with foreign keys on.
- Identifiers are ULIDs (26-character Crockford base32 strings). Timestamps are stored as UTC ISO 8601 strings ending in `Z`, second precision; naive datetimes are rejected at the API boundary.
- Every base table has a `props` JSON column; every mutation goes through `Ledger.transaction`; the ledger's own tables (`txn`, `change`) and virtual tables are never attached to a Session.
- Review is per transaction: human actors are `accepted`, agent, session and connector actors are `pending` unless the caller passes `auto_accept=True`.
- Revert aborts on any conflict and reports the conflicting rows; it never partially applies.
- The daemon binds to the loopback interface by default (port 8742) and every `/api` route requires the bearer token generated at first run; the container runs non-root with a read-only root filesystem and `/data` as the only writable volume.
- The timeline follows lane model A: lanes are projects, blocks are events, kind colors are the three validated hues (decision `#2a78d6`, meeting `#eb6834`, milestone `#1baf7a` in light mode; `#3987e5`, `#d95926`, `#199e70` in dark mode), idea and note are neutral pills, planned is dashed, suggested is dotted, pending review shows an amber dot `#fab219`. Text uses ink tokens, never kind colors.
- Agents get no delete or merge capability; in phase 1 the only delete endpoint is for edges and it is owner-only.

## Review Focus

1. Reverting a transaction whose rows were modified afterwards: the revert must fail with the list of conflicting rows and leave the database unchanged. Pinned in Task 5 (`test_revert_conflict_aborts_and_reports`) and Task 11 (`test_revert_conflict_returns_409`).
2. Bad timestamps from the UI or CLI, such as an end before the start or a naive datetime: the API must answer 422, never store them. Pinned in Task 3 (`test_event_rejects_end_before_start`, `test_naive_datetime_rejected`) and Task 10 (`test_event_create_validation_and_window`).
3. The login URL carries the token: it must set the cookie and redirect so the token leaves the address bar, and an invalid token must get 401 without setting anything. Pinned in Task 9 (`test_login_url_sets_cookie_and_redirects`, `test_bad_token_401`).
4. Two writers at once (web and CLI): writes must serialize through the ledger lock with no `database is locked` errors or lost transactions. Pinned in Task 4 (`test_concurrent_transactions_all_recorded`).
5. A lane with thousands of events: packing must only consider visible items and must never place two blocks overlapping in one row. Pinned in Task 14 (`cullVisible drops offscreen items`, `packLane never overlaps within a row`).

---

### Task 1: Repository scaffold, ids, time helpers

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.python-version`, `README.md`, `src/trajectree/__init__.py`, `src/trajectree/ids.py`, `src/trajectree/timeutil.py`, `tests/__init__.py`, `tests/test_ids.py`, `tests/test_timeutil.py`

**Interfaces:**
- Produces: `new_id() -> str` in `ids.py`; `utcnow() -> datetime`, `to_db(dt: datetime) -> str`, `from_db(s: str) -> datetime` in `timeutil.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ids.py
from trajectree.ids import new_id

def test_new_id_is_26_char_ulid():
    assert len(new_id()) == 26

def test_new_ids_sort_in_creation_order():
    a = new_id(); b = new_id()
    assert a < b

# tests/test_timeutil.py
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
```

- [ ] **Step 2: Create `pyproject.toml`** with project name `trajectree`, `requires-python = ">=3.12"`, dependencies `apsw>=3.54.1.0`, `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `python-ulid>=4`, `httpx`; dev group `pytest`, `pytest-asyncio` not needed; console scripts `trajectreed = "trajectree.main:run"` and `trajectree = "trajectree.cli.main:entry"`; pytest `testpaths = ["tests"]`; `.python-version` = `3.12`; `.gitignore` covering `.venv`, `__pycache__`, `data/`, `web/node_modules`, `web/dist`, `src/trajectree/static/`, `.claude/worktrees/`, `.playwright-mcp/`, `test-results/`.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv sync && uv run pytest -q`
Expected: ImportError for `trajectree.ids` and `trajectree.timeutil`.

- [ ] **Step 4: Implement `new_id()` with `ulid.ULID()`; implement `utcnow()`, `to_db()` (raise `ValueError` on naive, convert to UTC, `isoformat(timespec="seconds")` with `Z`), `from_db()`.**

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest -q`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .gitignore .python-version README.md src tests
git commit -m "chore: scaffold trajectree package with ids and time helpers"
```

---

### Task 2: Database connection and schema

**Files:**
- Create: `src/trajectree/db/__init__.py`, `src/trajectree/db/connection.py`, `src/trajectree/db/schema.py`, `tests/conftest.py`, `tests/test_schema.py`

**Interfaces:**
- Produces: `open_db(path: Path | str) -> apsw.Connection` (WAL, `foreign_keys=ON`, `busy_timeout=5000`, runs `migrate`); `migrate(conn: apsw.Connection) -> None`; `SCHEMA_VERSION: int = 1`; `LEDGER_TABLES: tuple[str, ...] = ("project", "event", "todo", "resource", "edge")`; `table_columns(conn, table: str) -> list[str]` (cached per connection via `PRAGMA table_info`).
- Fixture `conn` in `conftest.py`: fresh database in `tmp_path`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_schema.py
import apsw
from trajectree.db.schema import SCHEMA_VERSION, LEDGER_TABLES, table_columns

def test_all_tables_exist(conn):
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"project", "event", "todo", "resource", "edge", "txn", "change", "setting"} <= names

def test_user_version_set(conn):
    assert conn.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION

def test_wal_and_foreign_keys(conn):
    assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert conn.execute("pragma foreign_keys").fetchone()[0] == 1

def test_event_requires_existing_project(conn):
    import pytest
    with pytest.raises(apsw.ConstraintError):
        conn.execute("insert into event(id, project_id, kind, title, t_start, created_at, updated_at) values ('e', 'missing', 'note', 't', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")

def test_table_columns_lists_pk_first(conn):
    assert table_columns(conn, "event")[0] == "id"
    assert set(LEDGER_TABLES) <= {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_schema.py -q`
Expected: ImportError / fixture errors.

- [ ] **Step 3: Write the DDL in `schema.py`** as a list of statements, exactly these tables and columns (all ids `TEXT PRIMARY KEY`, all `props TEXT NOT NULL DEFAULT '{}'`, timestamps `TEXT`):
  - `project(id, slug UNIQUE NOT NULL, title NOT NULL, kind NOT NULL, status NOT NULL DEFAULT 'active', lane_order INTEGER NOT NULL DEFAULT 0, summary_md TEXT NOT NULL DEFAULT '', recurrence TEXT, props, created_at NOT NULL, updated_at NOT NULL)`
  - `event(id, project_id NOT NULL REFERENCES project(id), kind NOT NULL, title NOT NULL, body_md TEXT NOT NULL DEFAULT '', t_start NOT NULL, t_end, status NOT NULL DEFAULT 'happened', source NOT NULL DEFAULT 'human', props, created_txn TEXT, created_at NOT NULL, updated_at NOT NULL)` with index on `(project_id, t_start)`
  - `todo(id, project_id REFERENCES project(id), title NOT NULL, body_md TEXT NOT NULL DEFAULT '', due, state NOT NULL DEFAULT 'open', priority INTEGER NOT NULL DEFAULT 0, props, created_at, updated_at)` with index on `(project_id, due)`
  - `resource(id, project_id NOT NULL REFERENCES project(id), kind NOT NULL, locator NOT NULL, title TEXT NOT NULL DEFAULT '', props, created_at, updated_at)`
  - `edge(id, src_type NOT NULL, src_id NOT NULL, dst_type NOT NULL, dst_id NOT NULL, rel NOT NULL, props, valid_from, valid_to, created_at NOT NULL, UNIQUE(src_type, src_id, dst_type, dst_id, rel))` with indexes on `src_id` and `dst_id`
  - `txn(id, ts NOT NULL, actor_kind NOT NULL, actor_ref NOT NULL, model, backend, tool, tool_version, session_id, run_id, proposed_by_run, intent NOT NULL, changeset BLOB NOT NULL, review NOT NULL, review_ts, reverts_txn, reverted_by)` with index on `ts`
  - `change(txn_id NOT NULL REFERENCES txn(id), seq INTEGER NOT NULL, table_name NOT NULL, op NOT NULL, pk TEXT NOT NULL, old TEXT, new TEXT, PRIMARY KEY(txn_id, seq))` with index on `(table_name, pk)`
  - `setting(key TEXT PRIMARY KEY, value TEXT NOT NULL)`
  `migrate()` runs the DDL when `user_version` is 0 and then sets `user_version = 1`; later versions append migration steps keyed by version.

- [ ] **Step 4: Implement `open_db()` in `connection.py`** (pragmas in this order: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, then `migrate`). Write the `conn` fixture.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_schema.py -q`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add src/trajectree/db tests/conftest.py tests/test_schema.py
git commit -m "feat(db): sqlite connection and phase 1 schema"
```

---

### Task 3: Domain models

**Files:**
- Create: `src/trajectree/models.py`, `tests/test_models.py`

**Interfaces:**
- Produces pydantic models: `Actor(kind: Literal["human","agent","session","connector"], ref: str, model: str|None=None, backend: str|None=None, tool: str|None=None, tool_version: str|None=None, session_id: str|None=None, run_id: str|None=None, proposed_by_run: str|None=None)`; `Project`, `Event`, `Todo`, `Resource`, `Edge`, `Txn`, `Change`, `Conflict(reason: str, table_name: str, op: str, pk: list, old: dict|None, new: dict|None, conflict_row: dict|None)`; input models `ProjectIn`, `EventIn`, `TodoIn`, `EdgeIn` and patch models `ProjectPatch`, `EventPatch`, `TodoPatch` (all optional fields). All datetime fields are `AwareDatetime`. Enumerations: project kind `research|tooling|routine|admin|inbox`, project status `active|paused|done|archived`, event status `happened|planned|suggested|dropped`, source `human|agent|connector`, todo state `open|doing|done|dropped`, txn review `none|pending|accepted|rejected`. Event `kind`, edge `rel` and `src_type`/`dst_type` are free strings; `src_type`/`dst_type` must be one of `project|event|todo|resource`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_models.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement the models.** Slug pattern `^[a-z0-9][a-z0-9-]{0,63}$`. `EventIn` model validator: `t_end is None or t_end >= t_start`. `props: dict[str, Any] = {}` on every entity. `Txn` carries every `txn` column; `Change` carries `txn_id, seq, table_name, op, pk: list, old: dict|None, new: dict|None`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/models.py tests/test_models.py
git commit -m "feat: pydantic domain models with timestamp and slug validation"
```

---

### Task 4: Ledger transactions with changesets

**Files:**
- Create: `src/trajectree/db/ledger.py`, `src/trajectree/db/changes.py`, `tests/test_ledger.py`
- Modify: `tests/conftest.py` (add `ledger` fixture and `human`/`agent` actor fixtures)

**Interfaces:**
- Consumes: `open_db`, `LEDGER_TABLES`, `table_columns` (Task 2); `Actor`, `Txn`, `Change` (Task 3); `new_id`, `utcnow`, `to_db` (Task 1).
- Produces in `ledger.py`: `class Ledger: __init__(self, conn: apsw.Connection)` exposing `conn` as a public attribute; `transaction(self, actor: Actor, intent: str, *, auto_accept: bool = False) -> ContextManager[TxnHandle]` where `TxnHandle` has `id: str` and `conn: apsw.Connection`; `get_txn(self, txn_id: str) -> Txn | None`; `list_txns(self, *, limit: int = 50, before: str | None = None) -> list[Txn]`; `changes_for(self, txn_id: str) -> list[Change]`; `review_for(actor: Actor, auto_accept: bool) -> str` (module function). In `changes.py`: `materialize(conn, changeset: bytes) -> list[tuple[str, str, list, dict | None, dict | None]]` returning `(table_name, op, pk, old, new)` per change, column names resolved through `table_columns`, `apsw.no_change` entries dropped.
- Behaviour: `transaction()` acquires a `threading.RLock`, runs `BEGIN IMMEDIATE`, creates `apsw.Session(conn, "main")` and attaches each name in `LEDGER_TABLES`, yields; on normal exit reads `session.changeset()`, and if it is empty commits without writing a `txn` row; otherwise inserts the `txn` row (ts `to_db(utcnow())`, review from `review_for`) and one `change` row per materialized change with `seq` starting at 0, then `COMMIT`. On exception: `ROLLBACK` and re-raise. The handle's `id` is generated before yielding so callers can store it in `event.created_txn`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ledger.py
import json, threading
from trajectree.db.ledger import Ledger, review_for
from trajectree.models import Actor

def insert_project(h, pid="p1", slug="p1"):
    h.conn.execute("insert into project(id, slug, title, kind, created_at, updated_at) values (?,?,?,?,?,?)",
                   (pid, slug, "P", "research", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))

def test_transaction_records_changeset_and_changes(ledger, human):
    with ledger.transaction(human, "add project") as h:
        insert_project(h)
    txn = ledger.get_txn(h.id)
    assert txn is not None and txn.intent == "add project" and txn.review == "accepted" and len(txn.changeset) > 0
    changes = ledger.changes_for(h.id)
    assert len(changes) == 1 and changes[0].table_name == "project" and changes[0].op == "INSERT"
    assert changes[0].pk == ["p1"] and changes[0].new["slug"] == "p1" and changes[0].old is None

def test_update_change_has_old_and_new_for_changed_columns_only(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h)
    with ledger.transaction(human, "rename") as h2:
        h2.conn.execute("update project set title='Q' where id='p1'")
    c = ledger.changes_for(h2.id)[0]
    assert c.op == "UPDATE" and c.old == {"id": "p1", "title": "P"} and c.new == {"title": "Q"}

def test_agent_transaction_is_pending(ledger, agent):
    with ledger.transaction(agent, "agent add") as h:
        insert_project(h)
    assert ledger.get_txn(h.id).review == "pending"

def test_auto_accept_overrides(ledger, agent):
    with ledger.transaction(agent, "materialize", auto_accept=True) as h:
        insert_project(h)
    assert ledger.get_txn(h.id).review == "accepted"

def test_empty_transaction_writes_no_txn_row(ledger, human):
    with ledger.transaction(human, "noop") as h:
        pass
    assert ledger.get_txn(h.id) is None

def test_exception_rolls_back(ledger, human):
    try:
        with ledger.transaction(human, "boom") as h:
            insert_project(h)
            raise RuntimeError("x")
    except RuntimeError:
        pass
    assert ledger.conn.execute("select count(*) from project").fetchone()[0] == 0
    assert ledger.get_txn(h.id) is None

def test_concurrent_transactions_all_recorded(ledger, human):
    def work(n):
        for i in range(25):
            with ledger.transaction(human, f"t{n}-{i}") as h:
                insert_project(h, pid=f"p{n}-{i}", slug=f"p{n}-{i}")
    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(ledger.list_txns(limit=1000)) == 100

def test_list_txns_pages_newest_first(ledger, human):
    ids = []
    for i in range(3):
        with ledger.transaction(human, f"t{i}") as h:
            insert_project(h, pid=f"p{i}", slug=f"p{i}")
        ids.append(h.id)
    page = ledger.list_txns(limit=2)
    assert [t.id for t in page] == [ids[2], ids[1]]
    assert [t.id for t in ledger.list_txns(limit=2, before=ids[1])] == [ids[0]]
```

Note on `test_update_change_has_old_and_new_for_changed_columns_only`: the Session extension records primary key columns and changed columns in `old`; `new` holds only changed columns. The assertion above is the expected shape; if apsw reports `old` with more columns, keep the plan's rule "drop `apsw.no_change`" and update the assertion to whatever apsw actually returns, recording the observed shape in the docstring of `materialize`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ledger.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement `materialize()` in `changes.py`** (iterate `apsw.Changeset.iter(changeset)`; pk values from `change.pk_columns` indices into `new` for inserts and `old` otherwise; dict building skips `apsw.no_change`).

- [ ] **Step 4: Implement `Ledger` in `ledger.py`** with the behaviour in the Interfaces block. `list_txns` orders by `id DESC` (ULIDs sort by time) and applies `before` as `id < ?`. Add the fixtures: `ledger = Ledger(conn)`, `human = Actor(kind="human", ref="owner", tool="test")`, `agent = Actor(kind="agent", ref="run_test", model="claude-opus-5-5", backend="claude")`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_ledger.py -q`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add src/trajectree/db/ledger.py src/trajectree/db/changes.py tests/test_ledger.py tests/conftest.py
git commit -m "feat(ledger): transactions recorded as session changesets with materialized diffs"
```

---

### Task 5: Revert and review

**Files:**
- Modify: `src/trajectree/db/ledger.py`
- Create: `tests/test_revert.py`

**Interfaces:**
- Produces: `class RevertConflict(Exception)` with `conflicts: list[Conflict]`; `class AlreadyReverted(Exception)`; `Ledger.revert(self, txn_id: str, actor: Actor, intent: str | None = None) -> Txn` (the new transaction; sets `reverts_txn` on it and `reverted_by` on the old one, both by direct `UPDATE txn`, outside any session); `Ledger.set_review(self, txn_id: str, state: Literal["accepted", "rejected"], actor: Actor) -> Txn` (`accepted` sets `review`/`review_ts`; `rejected` calls `revert` then sets `review='rejected'` on the original).
- Behaviour: `revert` loads the stored changeset, computes `apsw.Changeset.invert(cs)`, and inside `self.transaction(actor, intent or f"revert {txn_id}")` calls `apsw.Changeset.apply(inverse, conn, conflict=handler)`. The handler appends a `Conflict` (reason from `apsw.mapping_session_conflict[reason]`, `change.name`, `change.op`, pk, old, new, `change.conflict`) and returns `apsw.SQLITE_CHANGESET_ABORT`. The exception apsw raises on abort is caught inside the context manager so the transaction rolls back, then `RevertConflict(conflicts)` is raised. A transaction that already has `reverted_by` raises `AlreadyReverted`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_revert.py
import pytest
from trajectree.db.ledger import RevertConflict, AlreadyReverted
from tests.test_ledger import insert_project

def title(conn, pid="p1"):
    row = conn.execute("select title from project where id=?", (pid,)).fetchone()
    return row[0] if row else None

def test_revert_restores_previous_state(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h)
    r = ledger.revert(h.id, human)
    assert title(ledger.conn) is None
    assert r.reverts_txn == h.id and ledger.get_txn(h.id).reverted_by == r.id
    assert r.intent == f"revert {h.id}"

def test_revert_keeps_later_unrelated_changes(ledger, human):
    with ledger.transaction(human, "add p1") as h1:
        insert_project(h1, "p1", "p1")
    with ledger.transaction(human, "add p2") as h2:
        insert_project(h2, "p2", "p2")
    ledger.revert(h1.id, human)
    assert title(ledger.conn, "p1") is None and title(ledger.conn, "p2") == "P"

def test_revert_conflict_aborts_and_reports(ledger, human):
    with ledger.transaction(human, "add") as h1:
        insert_project(h1)
    with ledger.transaction(human, "rename") as h2:
        h2.conn.execute("update project set title='Q' where id='p1'")
    with pytest.raises(RevertConflict) as exc:
        ledger.revert(h1.id, human)
    assert exc.value.conflicts[0].table_name == "project" and exc.value.conflicts[0].pk == ["p1"]
    assert title(ledger.conn) == "Q"
    assert ledger.get_txn(h1.id).reverted_by is None

def test_revert_of_revert_reapplies(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h)
    r = ledger.revert(h.id, human)
    ledger.revert(r.id, human)
    assert title(ledger.conn) == "P"

def test_double_revert_rejected(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h)
    ledger.revert(h.id, human)
    with pytest.raises(AlreadyReverted):
        ledger.revert(h.id, human)

def test_reject_reverts_and_marks(ledger, human, agent):
    with ledger.transaction(agent, "agent add") as h:
        insert_project(h)
    t = ledger.set_review(h.id, "rejected", human)
    assert t.review == "rejected" and t.reverted_by is not None and title(ledger.conn) is None

def test_accept_sets_review_without_new_txn(ledger, human, agent):
    with ledger.transaction(agent, "agent add") as h:
        insert_project(h)
    before = len(ledger.list_txns())
    t = ledger.set_review(h.id, "accepted", human)
    assert t.review == "accepted" and t.review_ts is not None and len(ledger.list_txns()) == before
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_revert.py -q`
Expected: ImportError for `RevertConflict`.

- [ ] **Step 3: Implement `revert`, `set_review`, the exceptions.** Foreign-key conflicts raised by apply (reason `SQLITE_CHANGESET_FOREIGN_KEY`) go through the same handler and surface as conflicts.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_revert.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/db/ledger.py tests/test_revert.py
git commit -m "feat(ledger): revert by inverted changeset with conflict reporting, review accept/reject"
```

---

### Task 6: Full-text search index

**Files:**
- Create: `src/trajectree/db/fts.py`, `tests/test_fts.py`
- Modify: `src/trajectree/db/schema.py` (call `fts.install(conn)` from `migrate`)

**Interfaces:**
- Produces: `install(conn) -> None` creating `CREATE VIRTUAL TABLE search_fts USING fts5(id UNINDEXED, kind UNINDEXED, project_id UNINDEXED, title, body)` plus `AFTER INSERT/UPDATE/DELETE` triggers on `event`, `todo`, `project` (project indexes `title` and `summary_md` with `kind='project'`, `project_id=id`; event `kind='event'`; todo `kind='todo'`); `rebuild(conn) -> None` (delete all rows, re-insert from the three tables); `search(conn, q: str, *, limit: int = 50) -> list[SearchHit]` where `SearchHit(id: str, kind: str, project_id: str | None, title: str, snippet: str)` ordered by FTS rank.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fts.py
from trajectree.db.fts import search, rebuild
from tests.test_ledger import insert_project

def add_event(h, eid, title, pid="p1"):
    h.conn.execute("insert into event(id, project_id, kind, title, t_start, created_at, updated_at) values (?,?,?,?,?,?,?)",
                   (eid, pid, "decision", title, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))

def test_search_finds_event_title(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h); add_event(h, "e1", "Encoder design frozen")
    hits = search(ledger.conn, "encoder")
    assert [(x.kind, x.id) for x in hits] == [("event", "e1")]

def test_reverted_event_disappears_from_search(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h); add_event(h, "e1", "Encoder design frozen")
    with ledger.transaction(human, "add e2") as h2:
        add_event(h2, "e2", "Dataset pipeline done")
    ledger.revert(h2.id, human)
    assert [x.id for x in search(ledger.conn, "dataset")] == []
    assert [x.id for x in search(ledger.conn, "encoder")] == ["e1"]

def test_rebuild_matches_triggers(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h); add_event(h, "e1", "Encoder design frozen")
    ledger.conn.execute("delete from search_fts")
    rebuild(ledger.conn)
    assert [x.id for x in search(ledger.conn, "encoder")] == ["e1"]

def test_changeset_excludes_fts_shadow_tables(ledger, human):
    with ledger.transaction(human, "add") as h:
        insert_project(h); add_event(h, "e1", "Encoder design frozen")
    assert {c.table_name for c in ledger.changes_for(h.id)} == {"project", "event"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fts.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement `fts.py` and hook `install` into `migrate`.** If `test_reverted_event_disappears_from_search` fails because changeset application bypasses triggers, call `rebuild(conn)` at the end of `Ledger.revert` and note it in the function docstring; the test stays.

- [ ] **Step 4: Run all tests to verify they pass**

Run: `uv run pytest -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/db/fts.py src/trajectree/db/schema.py tests/test_fts.py
git commit -m "feat(db): fts5 search index maintained by triggers"
```

---

### Task 7: Repository functions for projects and events

**Files:**
- Create: `src/trajectree/repo/__init__.py`, `src/trajectree/repo/projects.py`, `src/trajectree/repo/events.py`, `src/trajectree/repo/_rows.py`, `tests/test_repo_projects_events.py`

**Interfaces:**
- Consumes: `Ledger` (Task 4), models (Task 3), `new_id`, `to_db`, `utcnow` (Task 1).
- Produces in `_rows.py`: `row_to_model(conn, table: str, row: tuple, model: type[T]) -> T` (JSON-decodes `props`, parses timestamps) and `dumps_props(d) -> str`.
- Produces in `projects.py`: `create_project(ledger, actor, data: ProjectIn, *, intent: str | None = None) -> Project` (raises `DuplicateSlug`); `update_project(ledger, actor, project_id: str, patch: ProjectPatch, *, intent=None) -> Project` (raises `NotFound`); `get_project(conn, project_id: str) -> Project | None`; `get_project_by_slug(conn, slug) -> Project | None`; `list_projects(conn, *, status: str | None = None) -> list[Project]` ordered by `lane_order, created_at`.
- Produces in `events.py`: `create_event(ledger, actor, data: EventIn, *, intent=None) -> Event` (stores `created_txn = handle.id`); `update_event(ledger, actor, event_id, patch: EventPatch, *, intent=None) -> Event`; `get_event(conn, event_id) -> Event | None`; `list_events(conn, *, project_ids: list[str] | None = None, t_from: datetime | None = None, t_to: datetime | None = None, kinds: list[str] | None = None) -> list[Event]` ordered by `t_start` (an event overlaps the window when `t_start <= t_to` and `coalesce(t_end, t_start) >= t_from`).
- Exceptions in `repo/__init__.py`: `NotFound`, `DuplicateSlug`, `DuplicateEdge`.
- Default intents: `create_project` uses `f"create project {slug}"`, `create_event` uses `f"record event: {title}"`, updates use `f"update <table> <id>"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repo_projects_events.py
from datetime import datetime, timezone
import pytest
from trajectree.repo import DuplicateSlug, NotFound
from trajectree.repo.projects import create_project, update_project, list_projects, get_project
from trajectree.repo.events import create_event, update_event, list_events
from trajectree.models import ProjectIn, ProjectPatch, EventIn, EventPatch

UTC = timezone.utc
def ts(d): return datetime(2026, 1, d, tzinfo=UTC)

def test_create_and_list_project(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="clmi", title="CLMI", kind="research"))
    assert get_project(ledger.conn, p.id).slug == "clmi" and list_projects(ledger.conn)[0].id == p.id
    assert ledger.list_txns()[0].intent == "create project clmi"

def test_duplicate_slug(ledger, human):
    create_project(ledger, human, ProjectIn(slug="clmi", title="CLMI", kind="research"))
    with pytest.raises(DuplicateSlug):
        create_project(ledger, human, ProjectIn(slug="clmi", title="Again", kind="tooling"))

def test_update_project_records_diff(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="clmi", title="CLMI", kind="research"))
    update_project(ledger, human, p.id, ProjectPatch(status="paused"))
    c = ledger.changes_for(ledger.list_txns()[0].id)[0]
    assert c.new["status"] == "paused" and "updated_at" in c.new

def test_update_missing_project(ledger, human):
    with pytest.raises(NotFound):
        update_project(ledger, human, "nope", ProjectPatch(title="x"))

def test_event_window_query(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="clmi", title="CLMI", kind="research"))
    e1 = create_event(ledger, human, EventIn(project_id=p.id, kind="decision", title="A", t_start=ts(5)))
    e2 = create_event(ledger, human, EventIn(project_id=p.id, kind="meeting", title="B", t_start=ts(10), t_end=ts(12)))
    create_event(ledger, human, EventIn(project_id=p.id, kind="note", title="C", t_start=ts(20)))
    got = list_events(ledger.conn, project_ids=[p.id], t_from=ts(6), t_to=ts(11))
    assert [e.id for e in got] == [e2.id]
    assert [e.id for e in list_events(ledger.conn, kinds=["decision"])] == [e1.id]
    assert e1.created_txn == ledger.list_txns(limit=100)[-2].id

def test_update_event_status(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="clmi", title="CLMI", kind="research"))
    e = create_event(ledger, human, EventIn(project_id=p.id, kind="milestone", title="A", t_start=ts(5), status="planned"))
    assert update_event(ledger, human, e.id, EventPatch(status="happened")).status == "happened"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_repo_projects_events.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement `_rows.py`, `projects.py`, `events.py`.** `DuplicateSlug` is raised by catching `apsw.ConstraintError` on the unique index inside the transaction (the context manager rolls back).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_repo_projects_events.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/repo tests/test_repo_projects_events.py
git commit -m "feat(repo): project and event repositories over the ledger"
```

---

### Task 8: Repository functions for todos, edges, and pending-review lookup

**Files:**
- Create: `src/trajectree/repo/todos.py`, `src/trajectree/repo/edges.py`, `src/trajectree/repo/review.py`, `tests/test_repo_todos_edges.py`

**Interfaces:**
- Produces in `todos.py`: `create_todo(ledger, actor, data: TodoIn, *, intent=None) -> Todo`; `update_todo(ledger, actor, todo_id, patch: TodoPatch, *, intent=None) -> Todo`; `list_todos(conn, *, project_ids=None, states: list[str] | None = None, due_from=None, due_to=None) -> list[Todo]` ordered by `due` nulls last.
- Produces in `edges.py`: `create_edge(ledger, actor, data: EdgeIn, *, intent=None) -> Edge` (raises `DuplicateEdge`, and `NotFound` if either endpoint row does not exist); `delete_edge(ledger, actor, edge_id, *, intent=None) -> None`; `list_edges(conn, *, event_ids: list[str] | None = None) -> list[Edge]` (edges with at least one endpoint among the ids when given, else all).
- Produces in `review.py`: `pending_ids(conn, table_name: str) -> set[str]`: ids whose most recent `change` row (highest `txn.id`) belongs to a `txn` with `review = 'pending'`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repo_todos_edges.py
from datetime import datetime, timezone
import pytest
from trajectree.repo import DuplicateEdge, NotFound
from trajectree.repo.projects import create_project
from trajectree.repo.events import create_event
from trajectree.repo.todos import create_todo, update_todo, list_todos
from trajectree.repo.edges import create_edge, delete_edge, list_edges
from trajectree.repo.review import pending_ids
from trajectree.models import ProjectIn, EventIn, TodoIn, TodoPatch, EdgeIn

UTC = timezone.utc
def ts(d): return datetime(2026, 1, d, tzinfo=UTC)

@pytest.fixture
def two_events(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    q = create_project(ledger, human, ProjectIn(slug="b", title="B", kind="tooling"))
    e1 = create_event(ledger, human, EventIn(project_id=p.id, kind="idea", title="x", t_start=ts(1)))
    e2 = create_event(ledger, human, EventIn(project_id=q.id, kind="milestone", title="y", t_start=ts(2)))
    return p, q, e1, e2

def test_todo_lifecycle(ledger, human, two_events):
    p = two_events[0]
    t = create_todo(ledger, human, TodoIn(project_id=p.id, title="Reply to reviewer", due=ts(9)))
    assert list_todos(ledger.conn, states=["open"])[0].id == t.id
    assert update_todo(ledger, human, t.id, TodoPatch(state="done")).state == "done"
    assert list_todos(ledger.conn, states=["open"]) == []

def test_edge_create_list_delete(ledger, human, two_events):
    _, _, e1, e2 = two_events
    e = create_edge(ledger, human, EdgeIn(src_type="event", src_id=e1.id, dst_type="event", dst_id=e2.id, rel="shares_idea"))
    assert [x.id for x in list_edges(ledger.conn, event_ids=[e1.id])] == [e.id]
    with pytest.raises(DuplicateEdge):
        create_edge(ledger, human, EdgeIn(src_type="event", src_id=e1.id, dst_type="event", dst_id=e2.id, rel="shares_idea"))
    delete_edge(ledger, human, e.id)
    assert list_edges(ledger.conn) == []

def test_edge_requires_existing_endpoints(ledger, human, two_events):
    e1 = two_events[2]
    with pytest.raises(NotFound):
        create_edge(ledger, human, EdgeIn(src_type="event", src_id=e1.id, dst_type="todo", dst_id="nope", rel="blocked_by"))

def test_pending_ids_follow_latest_txn(ledger, human, agent, two_events):
    p = two_events[0]
    e = create_event(ledger, agent, EventIn(project_id=p.id, kind="milestone", title="draft", t_start=ts(3), source="agent"))
    assert pending_ids(ledger.conn, "event") == {e.id}
    ledger.set_review(ledger.list_txns()[0].id, "accepted", human)
    assert pending_ids(ledger.conn, "event") == set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_repo_todos_edges.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement `todos.py`, `edges.py`, `review.py`.** Endpoint existence is checked with one `SELECT 1 FROM <type> WHERE id = ?` per endpoint before the insert.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_repo_todos_edges.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/repo tests/test_repo_todos_edges.py
git commit -m "feat(repo): todos, edges, and pending-review lookup"
```

---

### Task 9: API application, settings, and token auth

**Files:**
- Create: `src/trajectree/config.py`, `src/trajectree/api/__init__.py`, `src/trajectree/api/app.py`, `src/trajectree/api/deps.py`, `tests/test_api_auth.py`
- Modify: `tests/conftest.py` (add `settings`, `app`, `client` fixtures)

**Interfaces:**
- Produces in `config.py`: `Settings(data_dir: Path, host: str = "127.0.0.1", port: int = 8742)` with properties `db_path` (`data_dir / "trajectree.db"`), `token_path` (`data_dir / "token"`), `exports_dir`; `load_settings() -> Settings` reading `TRAJECTREE_DATA_DIR` (default `~/.local/share/trajectree`), `TRAJECTREE_HOST`, `TRAJECTREE_PORT`; `ensure_token(settings) -> str` creating the data dir and a 43-character `secrets.token_urlsafe(32)` token file with mode `0600` when missing.
- Produces in `app.py`: `create_app(settings: Settings, *, ledger: Ledger | None = None) -> FastAPI` storing `settings`, `ledger`, `token` on `app.state`; routes `GET /api/health` (no auth, returns `{"ok": true, "version": <package version>}`), `GET /?token=...` (sets cookie `trajectree_token`, HttpOnly, SameSite=Lax, path `/`, then 303 to `/`; wrong token 401), and mounts `src/trajectree/static` at `/` when that directory exists (added in Task 13).
- Produces in `deps.py`: `require_token(request) -> None` accepting `Authorization: Bearer <token>` or the cookie, else 401; `get_ledger(request) -> Ledger`; `actor_from_request(request) -> Actor` (`kind="human"`, `ref="owner"`, `tool` = header `X-Trajectree-Client` or `"web"`).
- Fixtures: `settings(tmp_path)`, `app = create_app(settings, ledger=ledger)`, `client = TestClient(app)` with `headers={"Authorization": f"Bearer {app.state.token}"}`; also `anon = TestClient(app)` without headers.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_auth.py
def test_health_is_public(anon):
    assert anon.get("/api/health").json()["ok"] is True

def test_api_requires_token(anon):
    assert anon.get("/api/projects").status_code == 401

def test_bearer_token_accepted(client):
    assert client.get("/api/projects").status_code == 200

def test_login_url_sets_cookie_and_redirects(anon, app):
    r = anon.get(f"/?token={app.state.token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "trajectree_token" in r.cookies
    assert anon.get("/api/projects").status_code == 200

def test_bad_token_401(anon):
    r = anon.get("/?token=wrong", follow_redirects=False)
    assert r.status_code == 401 and "trajectree_token" not in r.cookies

def test_token_file_created_with_mode_600(settings):
    from trajectree.config import ensure_token
    t = ensure_token(settings)
    assert len(t) >= 43 and oct(settings.token_path.stat().st_mode)[-3:] == "600"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_auth.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement `config.py`, `deps.py`, `app.py`** with a placeholder `GET /api/projects` returning `[]` (replaced in Task 10). Compare tokens with `secrets.compare_digest`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_auth.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/config.py src/trajectree/api tests/test_api_auth.py tests/conftest.py
git commit -m "feat(api): app factory, settings, bearer/cookie token auth"
```

---

### Task 10: Entity routes

**Files:**
- Create: `src/trajectree/api/routes_entities.py`, `tests/test_api_entities.py`
- Modify: `src/trajectree/api/app.py` (include router, remove placeholder)

**Interfaces:**
- Consumes: repositories (Tasks 7, 8), `require_token`, `get_ledger`, `actor_from_request` (Task 9).
- Produces routes under `/api`, all behind `require_token`, JSON bodies are the `*In` and `*Patch` models plus an optional `intent: str` field: `GET/POST /projects`, `PATCH /projects/{id}`; `GET /events?projects=a,b&from=&to=&kinds=`, `POST /events`, `PATCH /events/{id}`; `GET /todos?projects=&states=`, `POST /todos`, `PATCH /todos/{id}`; `POST /edges`, `DELETE /edges/{id}`. Responses are the entity models. Error mapping: `NotFound` 404, `DuplicateSlug` and `DuplicateEdge` 409 with `{"detail": ...}`, pydantic validation 422.
- Request wrapper models: `ProjectCreate(ProjectIn)` with `intent: str | None = None`, likewise `EventCreate`, `TodoCreate`, `EdgeCreate`, and `*Update` wrappers around the patch models.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_entities.py
def mk_project(client, slug="clmi"):
    return client.post("/api/projects", json={"slug": slug, "title": slug.upper(), "kind": "research"}).json()

def test_project_crud_and_duplicate(client):
    p = mk_project(client)
    assert p["slug"] == "clmi"
    assert client.post("/api/projects", json={"slug": "clmi", "title": "x", "kind": "tooling"}).status_code == 409
    r = client.patch(f"/api/projects/{p['id']}", json={"status": "paused", "intent": "pause for deadline"})
    assert r.json()["status"] == "paused"

def test_event_create_validation_and_window(client):
    p = mk_project(client)
    bad = client.post("/api/events", json={"project_id": p["id"], "kind": "meeting", "title": "m",
                                            "t_start": "2026-01-02T10:00:00Z", "t_end": "2026-01-01T10:00:00Z"})
    assert bad.status_code == 422
    naive = client.post("/api/events", json={"project_id": p["id"], "kind": "note", "title": "n", "t_start": "2026-01-01T10:00:00"})
    assert naive.status_code == 422
    ok = client.post("/api/events", json={"project_id": p["id"], "kind": "decision", "title": "Freeze", "t_start": "2026-01-05T10:00:00Z"})
    assert ok.status_code == 200 and ok.json()["status"] == "happened"
    got = client.get("/api/events", params={"projects": p["id"], "from": "2026-01-01T00:00:00Z", "to": "2026-01-31T00:00:00Z"}).json()
    assert [e["title"] for e in got] == ["Freeze"]

def test_event_unknown_project_404(client):
    r = client.post("/api/events", json={"project_id": "nope", "kind": "note", "title": "n", "t_start": "2026-01-01T10:00:00Z"})
    assert r.status_code == 404

def test_todo_and_edge_routes(client):
    p = mk_project(client)
    e1 = client.post("/api/events", json={"project_id": p["id"], "kind": "idea", "title": "a", "t_start": "2026-01-01T10:00:00Z"}).json()
    e2 = client.post("/api/events", json={"project_id": p["id"], "kind": "idea", "title": "b", "t_start": "2026-01-02T10:00:00Z"}).json()
    t = client.post("/api/todos", json={"project_id": p["id"], "title": "do it", "due": "2026-01-09T00:00:00Z"}).json()
    assert client.patch(f"/api/todos/{t['id']}", json={"state": "done"}).json()["state"] == "done"
    edge = client.post("/api/edges", json={"src_type": "event", "src_id": e1["id"], "dst_type": "event", "dst_id": e2["id"], "rel": "led_to"})
    assert edge.status_code == 200
    assert client.post("/api/edges", json={"src_type": "event", "src_id": e1["id"], "dst_type": "event", "dst_id": e2["id"], "rel": "led_to"}).status_code == 409
    assert client.delete(f"/api/edges/{edge.json()['id']}").status_code == 204
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_entities.py -q`
Expected: 404s / assertion failures.

- [ ] **Step 3: Implement the router.** `create_event` must check the project exists and raise `NotFound` (add that check to the repository if Task 7 did not). Query parameters `projects`, `kinds`, `states` are comma-separated.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_entities.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/api tests/test_api_entities.py src/trajectree/repo
git commit -m "feat(api): project, event, todo and edge routes"
```

---

### Task 11: Timeline, ledger, and search routes

**Files:**
- Create: `src/trajectree/api/routes_timeline.py`, `src/trajectree/api/routes_ledger.py`, `src/trajectree/api/routes_search.py`, `tests/test_api_timeline_ledger.py`
- Modify: `src/trajectree/api/app.py` (include routers)

**Interfaces:**
- Produces: `GET /api/timeline?from=&to=&projects=` returning `TimelineResponse(projects: list[Project], events: list[Event], todos: list[Todo], edges: list[Edge], pending: dict[str, list[str]], live: list)` where `pending` has keys `event`, `todo`, `edge` from `pending_ids`, `edges` are those touching returned events, `todos` are those due in the window plus open todos with no due date, `live` is always `[]` in phase 1; `GET /api/ledger/txns?limit=&before=` returning `list[Txn]` (changeset bytes omitted from JSON); `GET /api/ledger/txns/{id}` returning `TxnDetail(txn: Txn, changes: list[Change])`; `POST /api/ledger/txns/{id}/revert` with optional `{"intent": ...}` returning the new `Txn`, 409 with `{"detail": "conflict", "conflicts": [Conflict...]}` on `RevertConflict`, 409 `{"detail": "already reverted"}` on `AlreadyReverted`; `POST /api/ledger/txns/{id}/review` with `{"state": "accepted"|"rejected"}` returning `Txn`; `GET /api/search?q=` returning `list[SearchHit]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_timeline_ledger.py
from tests.test_api_entities import mk_project

def seed(client):
    p = mk_project(client)
    e = client.post("/api/events", json={"project_id": p["id"], "kind": "decision", "title": "Freeze", "t_start": "2026-01-05T10:00:00Z"}).json()
    return p, e

def test_timeline_returns_window_and_pending(client, app, agent):
    p, e = seed(client)
    from trajectree.repo.events import create_event
    from trajectree.models import EventIn
    from datetime import datetime, timezone
    pending = create_event(app.state.ledger, agent, EventIn(project_id=p["id"], kind="milestone", title="draft",
                           t_start=datetime(2026, 1, 6, tzinfo=timezone.utc), source="agent"))
    tl = client.get("/api/timeline", params={"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z"}).json()
    assert [x["id"] for x in tl["projects"]] == [p["id"]]
    assert {x["id"] for x in tl["events"]} == {e["id"], pending.id}
    assert tl["pending"]["event"] == [pending.id] and tl["live"] == []

def test_ledger_list_detail_and_revert(client):
    p, e = seed(client)
    txns = client.get("/api/ledger/txns").json()
    assert txns[0]["intent"] == "record event: Freeze" and "changeset" not in txns[0]
    detail = client.get(f"/api/ledger/txns/{txns[0]['id']}").json()
    assert detail["changes"][0]["table_name"] == "event" and detail["changes"][0]["new"]["title"] == "Freeze"
    r = client.post(f"/api/ledger/txns/{txns[0]['id']}/revert", json={})
    assert r.status_code == 200 and r.json()["reverts_txn"] == txns[0]["id"]
    assert client.get("/api/events", params={"projects": p["id"]}).json() == []
    assert client.post(f"/api/ledger/txns/{txns[0]['id']}/revert", json={}).status_code == 409

def test_revert_conflict_returns_409(client):
    p, e = seed(client)
    create_txn = client.get("/api/ledger/txns").json()[0]["id"]
    client.patch(f"/api/events/{e['id']}", json={"title": "Freeze v2"})
    r = client.post(f"/api/ledger/txns/{create_txn}/revert", json={})
    assert r.status_code == 409 and r.json()["conflicts"][0]["table_name"] == "event"
    assert client.get("/api/events", params={"projects": p["id"]}).json()[0]["title"] == "Freeze v2"

def test_review_accept(client, app, agent):
    p, _ = seed(client)
    from trajectree.repo.events import create_event
    from trajectree.models import EventIn
    from datetime import datetime, timezone
    create_event(app.state.ledger, agent, EventIn(project_id=p["id"], kind="note", title="n",
                 t_start=datetime(2026, 1, 6, tzinfo=timezone.utc), source="agent"))
    tid = client.get("/api/ledger/txns").json()[0]["id"]
    assert client.post(f"/api/ledger/txns/{tid}/review", json={"state": "accepted"}).json()["review"] == "accepted"

def test_search_route(client):
    seed(client)
    hits = client.get("/api/search", params={"q": "freeze"}).json()
    assert hits[0]["kind"] == "event" and hits[0]["title"] == "Freeze"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_timeline_ledger.py -q`
Expected: 404s.

- [ ] **Step 3: Implement the three routers.** `Txn` JSON excludes `changeset` via a response model `TxnOut` (all fields except `changeset`).

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/api tests/test_api_timeline_ledger.py
git commit -m "feat(api): timeline, ledger (revert/review) and search routes"
```

---

### Task 12: Server entrypoint and CLI

**Files:**
- Create: `src/trajectree/main.py`, `src/trajectree/cli/__init__.py`, `src/trajectree/cli/main.py`, `tests/test_cli.py`

**Interfaces:**
- Produces in `main.py`: `run() -> None`: `settings = load_settings()`, `ensure_token`, opens the database, builds the app, prints `Trajectree ready: http://{host}:{port}/?token={token}` to stdout once, runs uvicorn with `access_log=False` (the login URL carries the token and must not be logged).
- Produces in `cli/main.py`: `main(argv: list[str], *, client: httpx.Client | None = None, env: Mapping[str, str] | None = None) -> int` and `entry()` calling `sys.exit(main(sys.argv[1:]))`. When `client` is supplied it is used as is; the CLI always builds absolute URLs from `TRAJECTREE_URL` and sets the `Authorization` and `X-Trajectree-Client` headers on each request, never on the client. Subcommands: `projects` (list as `slug  title  status`), `project add <slug> <title> --kind`, `event <project-slug> <title> [--kind note] [--at ISO|now] [--end ISO] [--status happened] [--body ...] [--intent ...]`, `todo <project-slug|-> <title> [--due ISO]`, `note <project-slug> <text>` (event of kind `note` at now), `revert <txn-id>`, `txns [--limit 20]`. Configuration from `TRAJECTREE_URL` (default `http://127.0.0.1:8742`) and `TRAJECTREE_TOKEN`, falling back to the file `~/.config/trajectree/token`. Every request sends `X-Trajectree-Client: cli`. Exit code 0 on success, 1 on HTTP error with the server's `detail` printed to stderr, 2 on usage error.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py
from trajectree.cli.main import main

def env_for(app):
    return {"TRAJECTREE_URL": "http://testserver", "TRAJECTREE_TOKEN": app.state.token}

def test_cli_creates_event_by_slug(app, client, anon, capsys):
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    assert main(["event", "clmi", "Encoder frozen", "--kind", "decision", "--at", "2026-01-05T10:00:00Z"], client=anon, env=env_for(app)) == 0
    pid = client.get("/api/projects").json()[0]["id"]
    events = client.get("/api/events", params={"projects": pid}).json()
    assert events[0]["title"] == "Encoder frozen" and events[0]["kind"] == "decision"
    assert client.get("/api/ledger/txns").json()[0]["tool"] == "cli"

def test_cli_unknown_slug_exits_1(app, anon, capsys):
    assert main(["note", "ghost", "hello"], client=anon, env=env_for(app)) == 1
    assert "ghost" in capsys.readouterr().err

def test_cli_revert_and_txns(app, client, anon, capsys):
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    main(["note", "clmi", "hello"], client=anon, env=env_for(app))
    tid = client.get("/api/ledger/txns").json()[0]["id"]
    assert main(["revert", tid], client=anon, env=env_for(app)) == 0
    assert main(["txns", "--limit", "3"], client=anon, env=env_for(app)) == 0
    assert f"revert {tid}" in capsys.readouterr().out
```

Note: the `anon` fixture is a `fastapi.testclient.TestClient`, an `httpx.Client` subclass that routes absolute `http://testserver/...` URLs to the app in-process, so the CLI exercises the real routes without a server.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement `main.py` and the CLI with `argparse`.** Slug resolution: `GET /api/projects` and match `slug`; unknown slug prints `unknown project: <slug>` to stderr and returns 1.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q`
Expected: 3 passed.

- [ ] **Step 5: Smoke the real server**

Run: `TRAJECTREE_DATA_DIR=$(mktemp -d) uv run trajectreed & sleep 2; curl -fs http://127.0.0.1:8742/api/health; kill %1`
Expected: `{"ok":true,...}` printed; the startup line shows the login URL.

- [ ] **Step 6: Commit**

```bash
git add src/trajectree/main.py src/trajectree/cli tests/test_cli.py
git commit -m "feat: trajectreed entrypoint and trajectree CLI"
```

---

### Task 13: Web app scaffold and API client

**Files:**
- Create: `web/package.json`, `web/vite.config.ts`, `web/tsconfig.json`, `web/index.html`, `web/src/main.tsx`, `web/src/App.tsx`, `web/src/api/types.ts`, `web/src/api/client.ts`, `web/src/api/client.test.ts`, `web/src/styles/tokens.css`, `web/vitest.setup.ts`
- Modify: `src/trajectree/api/app.py` (static mount serves `index.html` for `/` and unknown non-API paths), `pyproject.toml` (package data includes `static/`)

**Interfaces:**
- Produces `types.ts` mirroring the pydantic models (`Project`, `Event`, `Todo`, `Edge`, `Txn`, `Change`, `Conflict`, `TimelineResponse`, `TxnDetail`, `SearchHit`) with ISO strings for timestamps.
- Produces `client.ts`: `api.timeline(from: Date, to: Date): Promise<TimelineResponse>`, `api.projects()`, `api.createProject(body)`, `api.createEvent(body)`, `api.updateEvent(id, patch)`, `api.createTodo(body)`, `api.updateTodo(id, patch)`, `api.createEdge(body)`, `api.deleteEdge(id)`, `api.txns(limit?, before?)`, `api.txn(id)`, `api.revert(id, intent?)`, `api.review(id, state)`, `api.search(q)`. Requests go to same-origin `/api/...` with `credentials: "same-origin"` (the cookie carries the token); non-2xx responses throw `ApiError(status, detail, body)`.
- Dev: `vite.config.ts` proxies `/api` to `http://127.0.0.1:8742`; `npm run build` outputs to `../src/trajectree/static`. Dependencies: `react@19`, `react-dom@19`, `d3@7`, `@tanstack/react-query@5`; dev: `vite`, `typescript`, `vitest`, `@testing-library/react`, `@testing-library/user-event`, `jsdom`, `@types/d3`.
- `tokens.css`: the light and dark token sets copied from the mock (surface, plane, ink, ink2, muted, grid, axis, border, band, kind colors, strip ramp, future, now, sel, warn, wire), declared on `:root`, under `@media (prefers-color-scheme: dark)` guarded by `:root:not([data-theme="light"])`, and under `:root[data-theme="dark"]`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/api/client.test.ts
import { describe, it, expect, vi } from "vitest";
import { api, ApiError } from "./client";

describe("api client", () => {
  it("throws ApiError with detail and conflicts on 409", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "conflict", conflicts: [{ table_name: "event" }] }), { status: 409 })));
    await expect(api.revert("t1")).rejects.toMatchObject({ status: 409, detail: "conflict" });
    try { await api.revert("t1"); } catch (e) { expect((e as ApiError).body.conflicts[0].table_name).toBe("event"); }
  });
  it("sends timeline window as ISO query params", async () => {
    const f = vi.fn(async () => new Response(JSON.stringify({ projects: [], events: [], todos: [], edges: [], pending: {}, live: [] })));
    vi.stubGlobal("fetch", f);
    await api.timeline(new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z"));
    expect(String(f.mock.calls[0][0])).toContain("from=2026-01-01T00%3A00%3A00.000Z");
  });
});
```

- [ ] **Step 2: Scaffold the app and run the test to verify it fails**

Run: `cd web && npm install && npx vitest run`
Expected: fails because `./client` does not exist.

- [ ] **Step 3: Implement `types.ts`, `client.ts`, `tokens.css`, `App.tsx` (two tabs: Timeline and Ledger, placeholder content), `main.tsx` with a `QueryClientProvider`.** Update the FastAPI static mount so `/` and non-`/api` paths serve `index.html` when `static/` exists.

- [ ] **Step 4: Run tests and build**

Run: `cd web && npx vitest run && npm run build && ls ../src/trajectree/static/index.html`
Expected: 2 passed; `index.html` exists.

- [ ] **Step 5: Commit**

```bash
git add web src/trajectree/api/app.py pyproject.toml
git commit -m "feat(web): vite react scaffold, typed api client, design tokens"
```

---

### Task 14: Timeline layout module

**Files:**
- Create: `web/src/timeline/layout.ts`, `web/src/timeline/layout.test.ts`

**Interfaces:**
- Produces:
  - `interface LaneItem { id: string; start: number; end: number | null; title: string; kind: string }` (times in epoch ms)
  - `interface PlacedItem extends LaneItem { px: number; w: number; row: number; label: boolean; inside: boolean }`
  - `labelWidth(title: string): number` = `min(title.length * 6.4 + 14, 170)`
  - `cullVisible<T extends LaneItem>(items: T[], xz: (t: number) => number, plotW: number, margin = 400): T[]`
  - `packLane(items: LaneItem[], xz: (t: number) => number, maxRows: number, minW = 16): { blocks: PlacedItem[]; rows: number }` implementing the mock's rule: sort by `px`; a block fits a row when the row's end + 3 <= its `px`; placing with a label extends the row end by the label width when the label is outside; when no row fits and `rows.length >= maxRows`, open a new row without the outside label.
  - `binKey(t: number, dayW: number): "day" | "week" | "month"` (day when `dayW >= 3`, week when `dayW * 7 >= 3`, else month) for phase 2's strip.

- [ ] **Step 1: Write the failing tests**

```ts
// web/src/timeline/layout.test.ts
import { describe, it, expect } from "vitest";
import { packLane, cullVisible, labelWidth, type LaneItem } from "./layout";

const xz = (t: number) => t / 1000; // 1px per second
const item = (id: string, start: number, title = "t", end: number | null = null): LaneItem => ({ id, start, end, title, kind: "note" });

describe("packLane", () => {
  it("places non-overlapping items in row 0 with labels", () => {
    const { blocks, rows } = packLane([item("a", 0), item("b", 500_000)], xz, 3);
    expect(rows).toBe(1);
    expect(blocks.map(b => [b.id, b.row, b.label])).toEqual([["a", 0, true], ["b", 0, true]]);
  });
  it("moves an item whose label would collide to the next row", () => {
    const { blocks, rows } = packLane([item("a", 0, "a long title here"), item("b", 20_000)], xz, 3);
    expect(rows).toBe(2);
    expect(blocks.find(b => b.id === "b")!.row).toBe(1);
  });
  it("drops the outside label instead of exceeding maxRows", () => {
    const items = [0, 1, 2, 3].map(i => item(`i${i}`, i * 5_000, "a long title here"));
    const { blocks, rows } = packLane(items, xz, 3);
    expect(rows).toBe(4);
    expect(blocks.find(b => b.id === "i3")!.label).toBe(false);
  });
  it("never overlaps within a row", () => {
    const items = Array.from({ length: 2000 }, (_, i) => item(`x${i}`, (i * 7919) % 3_000_000, `title ${i}`));
    const { blocks } = packLane(items, xz, 6);
    const byRow = new Map<number, typeof blocks>();
    blocks.forEach(b => byRow.set(b.row, [...(byRow.get(b.row) ?? []), b]));
    for (const row of byRow.values()) {
      row.sort((a, b) => a.px - b.px);
      for (let i = 1; i < row.length; i++) expect(row[i].px).toBeGreaterThanOrEqual(row[i - 1].px + row[i - 1].w);
    }
  });
  it("uses minW for point events and label inside wide blocks", () => {
    const { blocks } = packLane([item("p", 0), item("w", 100_000, "wide", 400_000)], xz, 3);
    expect(blocks.find(b => b.id === "p")!.w).toBe(16);
    expect(blocks.find(b => b.id === "w")!.inside).toBe(true);
  });
});

describe("cullVisible", () => {
  it("drops offscreen items beyond the margin", () => {
    const items = [item("left", -1_000_000), item("in", 10_000), item("right", 5_000_000)];
    expect(cullVisible(items, xz, 1000).map(i => i.id)).toEqual(["in"]);
  });
});

describe("labelWidth", () => {
  it("caps at 170", () => { expect(labelWidth("x".repeat(100))).toBe(170); });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run src/timeline/layout.test.ts`
Expected: module not found.

- [ ] **Step 3: Implement `layout.ts`** by porting `layoutLane` and the culling predicate from the mock (`mockups/2026-10-21-timeline-lane-model-a.html`, functions `labelW`, `layoutLane`, `vis`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run src/timeline/layout.test.ts`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add web/src/timeline/layout.ts web/src/timeline/layout.test.ts
git commit -m "feat(web): pure lane packing and culling for the timeline"
```

---

### Task 15: Timeline component and details panel

**Files:**
- Create: `web/src/timeline/Timeline.tsx`, `web/src/timeline/timeline.css`, `web/src/timeline/Timeline.test.tsx`, `web/src/components/DetailsPanel.tsx`

**Interfaces:**
- Consumes: `packLane`, `cullVisible` (Task 14); types (Task 13); d3 `scaleTime`, `axisTop`, `zoom`, `select`.
- Produces `Timeline` props: `{ data: TimelineResponse; now: Date; range: [Date, Date]; selected: Selection | null; onSelect: (s: Selection | null) => void; showAllWires?: boolean }` with `type Selection = { type: "event" | "todo"; id: string }`. Renders one SVG: a 176px left gutter with one `g.lab` group per lane holding the project title and kind, a top time axis, lanes ordered as `data.projects`, lane height `8 + rows * 28 + 8` with a fixed 18px strip slot reserved below the rows (empty in phase 1), blocks from `data.events` with class `block k-<kind> s-<status>` plus `sel`, `hover`, `linked`, a `pending` dot when the id is in `data.pending.event`, todo due markers (12px diamond at `due` on the lane, class `todo-marker`, `done` todos hidden), a now line and future rectangle, wires (`path.wire` with a `text.wire-lbl` relation label) only for edges incident to the hovered or selected block unless `showAllWires`, d3 zoom on x only (`scaleExtent [0.6, 120]`, `rescaleX`, double-click zoom disabled), hover tooltip as a positioned `div.tip`. Click on a block calls `onSelect({type:"event", id})`; click on empty space calls `onSelect(null)`.
- Produces `DetailsPanel` props: `{ data: TimelineResponse; selected: Selection | null; onReview: (txnId: string, state: "accepted" | "rejected") => void; onRevert: (txnId: string) => void }` rendering title, body, badges for status and kind and a `pending` badge, a definition list (project, when, recorded, author from the creating transaction when loaded via `api.txn(event.created_txn)`, transaction id), the wires list, and action buttons `Accept` / `Reject` when pending, else `Revert transaction`.
- `timeline.css` carries the block styles from the mock (fill-opacity `.16`, dashed `4 3` for planned, dotted `2 3` for suggested, pill `rx: 11px` for idea and note, neutral task markers, amber pending dot).

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/timeline/Timeline.test.tsx
import { render, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Timeline } from "./Timeline";
import type { TimelineResponse } from "../api/types";

const data: TimelineResponse = {
  projects: [{ id: "p1", slug: "clmi", title: "CLMI", kind: "research", status: "active", lane_order: 0, summary_md: "", recurrence: null, props: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
             { id: "p2", slug: "ccx", title: "ccx", kind: "tooling", status: "active", lane_order: 1, summary_md: "", recurrence: null, props: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" }],
  events: [{ id: "e1", project_id: "p1", kind: "decision", title: "Freeze", body_md: "", t_start: "2026-01-05T10:00:00Z", t_end: null, status: "happened", source: "human", props: {}, created_txn: "t1", created_at: "2026-01-05T10:00:00Z", updated_at: "2026-01-05T10:00:00Z" },
           { id: "e2", project_id: "p2", kind: "milestone", title: "v0.1", body_md: "", t_start: "2026-01-08T10:00:00Z", t_end: null, status: "suggested", source: "agent", props: {}, created_txn: "t2", created_at: "2026-01-08T10:00:00Z", updated_at: "2026-01-08T10:00:00Z" }],
  todos: [], edges: [{ id: "x", src_type: "event", src_id: "e1", dst_type: "event", dst_id: "e2", rel: "led_to", props: {}, valid_from: null, valid_to: null, created_at: "2026-01-08T10:00:00Z" }],
  pending: { event: ["e2"], todo: [], edge: [] }, live: [],
};

describe("Timeline", () => {
  it("renders a lane per project and a block per event with status classes", () => {
    const { container } = render(<Timeline data={data} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={() => {}} />);
    expect(container.querySelectorAll("g.lab").length).toBe(2);
    expect(container.querySelectorAll("g.block").length).toBe(2);
    expect(container.querySelector("g.block.k-milestone.s-suggested circle.pending")).not.toBeNull();
  });
  it("selecting a block draws its wire and reports the selection", () => {
    const onSelect = vi.fn();
    const { container, rerender } = render(<Timeline data={data} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={onSelect} />);
    expect(container.querySelectorAll("path.wire").length).toBe(0);
    fireEvent.click(container.querySelector("g.block.k-decision")!);
    expect(onSelect).toHaveBeenCalledWith({ type: "event", id: "e1" });
    rerender(<Timeline data={data} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={{ type: "event", id: "e1" }} onSelect={onSelect} />);
    expect(container.querySelectorAll("path.wire").length).toBe(1);
  });
});
```

Note: jsdom has no layout; the component must read its width from a `ResizeObserver` or `clientWidth` with a fallback of 1200 when both report 0, so blocks render in tests.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && npx vitest run src/timeline/Timeline.test.tsx`
Expected: module not found.

- [ ] **Step 3: Implement `Timeline.tsx` and `timeline.css`** by porting the mock's `render()`, `wirePath`, zoom and tooltip code into a component that owns a `ref` to the SVG and re-renders through d3 joins on data, selection, hover and zoom changes. Implement `DetailsPanel.tsx`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add web/src/timeline web/src/components/DetailsPanel.tsx
git commit -m "feat(web): d3 timeline with lanes, blocks, wires and details panel"
```

---

### Task 16: Views: timeline page, quick capture, project form, ledger

**Files:**
- Create: `web/src/views/TimelineView.tsx`, `web/src/views/LedgerView.tsx`, `web/src/views/LedgerView.test.tsx`, `web/src/components/QuickCapture.tsx`, `web/src/components/QuickCapture.test.tsx`, `web/src/components/ProjectForm.tsx`
- Modify: `web/src/App.tsx`

**Interfaces:**
- `TimelineView`: loads `api.timeline(range)` with TanStack Query (key `["timeline", from, to]`), default range now minus 120 days to now plus 60 days, zoom preset buttons (Week, Month, Quarter+, All) that change the range, a `Show all wires` toggle, the `Timeline`, the `DetailsPanel`, and the `QuickCapture` drawer; review and revert actions invalidate `["timeline"]` and `["txns"]`.
- `QuickCapture` props `{ projects: Project[]; onCreated: () => void }`: a form with fields project (select by slug), entry type (`event` or `todo`), kind (text with datalist `decision, meeting, milestone, idea, note, blocker`), title, when (datetime-local, default now), end (optional), status (`happened, planned, suggested`), body, intent; submits through `api.createEvent` or `api.createTodo` with the local datetime converted to an ISO UTC string; shows the server's 422 detail inline.
- `ProjectForm` props `{ onCreated: () => void }`: slug, title, kind; 409 shown inline.
- `LedgerView`: paginated list from `api.txns(50, before)` (key `["txns", before]`) with columns time, actor (kind and ref, model when present), intent, review badge; expanding a row loads `api.txn(id)` and renders each change as `table_name op pk` with a two-column old/new diff of changed keys; buttons `Revert` (confirm dialog, then `api.revert`; a 409 shows the conflict rows inline) and, when pending, `Accept` / `Reject`.
- `App`: a tab bar of `role="tab"` buttons named `Timeline` and `Ledger`; a `+ Project` button opening `ProjectForm` (fields labelled `Slug`, `Title`, `Kind`; submit button `Create`); a `Quick capture` button opening the `QuickCapture` drawer (submit button `Save`). These names are contracts for the smoke test in Task 18.

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/components/QuickCapture.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { QuickCapture } from "./QuickCapture";

vi.mock("../api/client", () => ({ api: { createEvent: vi.fn(async (b) => ({ id: "e", ...b })), createTodo: vi.fn() } }));
import { api } from "../api/client";

describe("QuickCapture", () => {
  it("submits an event with an ISO UTC timestamp and intent", async () => {
    const onCreated = vi.fn();
    render(<QuickCapture projects={[{ id: "p1", slug: "clmi", title: "CLMI" } as any]} onCreated={onCreated} />);
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Freeze" } });
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "decision" } });
    fireEvent.change(screen.getByLabelText("When"), { target: { value: "2026-01-05T10:00" } });
    fireEvent.change(screen.getByLabelText("Intent"), { target: { value: "after advisor sync" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const body = (api.createEvent as any).mock.calls[0][0];
    expect(body.project_id).toBe("p1");
    expect(body.t_start).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$/);
    expect(body.intent).toBe("after advisor sync");
  });
});

// web/src/views/LedgerView.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LedgerView } from "./LedgerView";

const txn = { id: "t1", ts: "2026-01-05T10:00:00Z", actor_kind: "agent", actor_ref: "run_1", model: "claude-opus-5-5", backend: "claude", tool: "claude", tool_version: null, session_id: null, run_id: "run_1", proposed_by_run: null, intent: "record event: draft", review: "pending", review_ts: null, reverts_txn: null, reverted_by: null };
vi.mock("../api/client", () => ({ api: {
  txns: vi.fn(async () => [txn]),
  txn: vi.fn(async () => ({ txn, changes: [{ txn_id: "t1", seq: 0, table_name: "event", op: "INSERT", pk: ["e1"], old: null, new: { title: "draft" } }] })),
  review: vi.fn(async () => ({ ...txn, review: "accepted" })),
  revert: vi.fn(),
} }));
import { api } from "../api/client";

describe("LedgerView", () => {
  it("lists transactions, expands the diff, and accepts a pending one", async () => {
    render(<QueryClientProvider client={new QueryClient()}><LedgerView /></QueryClientProvider>);
    await screen.findByText("record event: draft");
    expect(screen.getByText("claude-opus-5-5")).toBeTruthy();
    fireEvent.click(screen.getByText("record event: draft"));
    await screen.findByText("draft");
    fireEvent.click(screen.getByText("Accept"));
    await waitFor(() => expect(api.review).toHaveBeenCalledWith("t1", "accepted"));
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run src/components src/views`
Expected: modules not found.

- [ ] **Step 3: Implement the views and components; wire `App.tsx`.**

- [ ] **Step 4: Run tests, build, and look at it**

Run: `cd web && npx vitest run && npm run build && cd .. && TRAJECTREE_DATA_DIR=$(mktemp -d) uv run trajectreed`
Expected: all tests pass; open the printed login URL, create a project and an event through the UI, see the block on its lane, open Ledger, revert the event, see it disappear from the timeline.

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "feat(web): timeline view, quick capture, project form, ledger view"
```

---

### Task 17: Docker image and Compose deployment

**Files:**
- Create: `Dockerfile`, `compose.yaml`, `.dockerignore`, `docker/entrypoint.sh`
- Modify: `README.md` (run instructions)

**Interfaces:**
- `Dockerfile`: stage `web` from `node:22-alpine` runs `npm ci && npm run build` in `web/`; stage `app` from `python:3.12-slim` installs uv (`COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv`), copies `pyproject.toml`, `uv.lock`, `src/`, the built `static/` from stage `web`, runs `uv sync --frozen --no-dev`, creates user `trajectree` uid 1000, sets `ENV TRAJECTREE_DATA_DIR=/data TRAJECTREE_HOST=0.0.0.0 PATH=/app/.venv/bin:$PATH`, `VOLUME /data`, `EXPOSE 8742`, `HEALTHCHECK CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8742/api/health').status==200 else 1)"`, `USER trajectree`, `ENTRYPOINT ["/app/docker/entrypoint.sh"]` which `exec trajectreed`.
- `compose.yaml`: service `trajectree`, `build: .`, `ports: ["127.0.0.1:8742:8742"]`, `volumes: ["./data:/data"]`, `read_only: true`, `tmpfs: ["/tmp"]`, `cap_drop: [ALL]`, `security_opt: ["no-new-privileges:true"]`, `user: "${UID:-1000}:${GID:-1000}"`, `restart: unless-stopped`, `mem_limit: 1g`, `pids_limit: 256`.
- `.dockerignore`: `.git`, `.venv`, `web/node_modules`, `data`, `docs`, `tests`, `.claude`.

- [ ] **Step 1: Write the files above.**

- [ ] **Step 2: Build and run**

Run: `mkdir -p data && docker compose up -d --build && sleep 3 && curl -fs http://127.0.0.1:8742/api/health`
Expected: `{"ok":true,...}`.

- [ ] **Step 3: Verify persistence across restarts**

Run: `TOKEN=$(cat data/token); curl -fs -H "Authorization: Bearer $TOKEN" -X POST http://127.0.0.1:8742/api/projects -H 'content-type: application/json' -d '{"slug":"clmi","title":"CLMI","kind":"research"}' && docker compose restart && sleep 3 && curl -fs -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8742/api/projects`
Expected: the project is listed after the restart; `data/trajectree.db` and `data/token` exist on the host with the invoking user's uid.

- [ ] **Step 4: Verify the hardening**

Run: `docker compose exec trajectree sh -c 'id -u; touch /app/x 2>&1; touch /data/ok && echo data-writable'`
Expected: `1000`, a read-only filesystem error for `/app/x`, then `data-writable`.

- [ ] **Step 5: Document in README** the three commands (build, login URL from `docker compose logs`, CLI token setup with `TRAJECTREE_TOKEN=$(cat data/token)`), then commit.

```bash
git add Dockerfile compose.yaml .dockerignore docker README.md
git commit -m "feat: docker image and hardened compose deployment"
```

---

### Task 18: End-to-end smoke test

**Files:**
- Create: `web/playwright.config.ts`, `web/e2e/smoke.spec.ts`
- Modify: `web/package.json` (script `e2e`), `.gitignore` (`web/test-results/`)

**Interfaces:**
- `playwright.config.ts`: `webServer` runs `uv run trajectreed` from the repo root with `TRAJECTREE_DATA_DIR=<tmp dir created by a global setup>` and `TRAJECTREE_PORT=8799`, `url: http://127.0.0.1:8799/api/health`; `baseURL: http://127.0.0.1:8799`; the test reads the token from `<tmp dir>/token`.

- [ ] **Step 1: Write the failing test**

```ts
// web/e2e/smoke.spec.ts
import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

test("create a project and an event, see the block, revert it from the ledger", async ({ page }) => {
  const token = readFileSync(process.env.TRAJECTREE_DATA_DIR + "/token", "utf8").trim();
  await page.goto(`/?token=${token}`);
  await expect(page).toHaveURL(/\/$/);
  await page.getByRole("button", { name: "+ Project" }).click();
  await page.getByLabel("Slug").fill("clmi");
  await page.getByLabel("Title").fill("CLMI");
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.locator("g.lab", { hasText: "CLMI" })).toBeVisible();
  await page.getByRole("button", { name: "Quick capture" }).click();
  await page.getByLabel("Title").fill("Encoder frozen");
  await page.getByLabel("Kind").fill("decision");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.locator("g.block.k-decision")).toHaveCount(1);
  await page.getByRole("tab", { name: "Ledger" }).click();
  await page.getByText("record event: Encoder frozen").click();
  page.once("dialog", d => d.accept());
  await page.getByRole("button", { name: "Revert" }).first().click();
  await page.getByRole("tab", { name: "Timeline" }).click();
  await expect(page.locator("g.block.k-decision")).toHaveCount(0);
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd web && npx playwright install chromium && npm run e2e`
Expected: fails on a missing config or selector.

- [ ] **Step 3: Write `playwright.config.ts` and the global setup; adjust labels in the views to match the test's accessible names (`+ Project`, `Create`, `Quick capture`, `Save`, `Revert`, tabs `Timeline` and `Ledger`).**

- [ ] **Step 4: Run it to verify it passes**

Run: `cd web && npm run e2e`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add web/playwright.config.ts web/e2e web/package.json .gitignore web/src
git commit -m "test: playwright smoke test for create, view and revert"
```

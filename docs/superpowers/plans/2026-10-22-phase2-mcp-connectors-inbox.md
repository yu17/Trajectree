# Phase 2: MCP Server, Connectors, Activity Strip and Inbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner's Claude Code sessions record outcomes into Trajectree through MCP write tools installed by `trajectree init-project`; the daemon ingests Claude transcripts and local git history into per-lane activity strips without any LLM call, flags stale summaries, and the owner reviews every LLM write in an inbox.

**Architecture:** The phase 1 daemon gains an MCP endpoint (official `mcp` SDK 2.x, streamable HTTP, bearer-gated, scoped tokens) whose tools call the existing repository functions under the ledger lock; a stdio shim on the host forwards Claude Code's MCP traffic to it with the project directory attached. Connectors write append-only `signal` rows outside the ledger; an in-process ingest loop runs them on an interval and computes staleness. The React app renders signals as a binned strip under each lane, badges pending writes with their transaction id, and adds an Inbox tab.

**Tech Stack:** Python 3.12, uv, apsw, FastAPI, pydantic v2, `mcp>=2.3.0` (MCPServer, in-memory `Client`, low-level `Server`, `stdio_server`), httpx2 (pulled in by `mcp`), subprocess `git`; React 19, TypeScript, d3 v7, TanStack Query, vitest, Playwright; Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-21-trajectree-architecture-design.md` (sections 5 to 11, 14 item 2, 15; Appendix B). Phase 1 plan: `docs/superpowers/plans/2026-10-21-phase1-ledger-and-timeline.md` (its Global Constraints still apply).

Deferred to later phases with the owner's spec as the reference: hooks and the running-session registry (phase 3; phase 2 derives liveness from transcript mtime, which the spec allows), `get_run` and `save_report_draft` tools (no runs or reports yet), the recurrence materializer and the dirty-tree flag (their readers, meeting-prep and the project page, are phase 4), per-session `claude_session` resource rows (the session signal carries the reference; thousands of ledger rows would have no reader yet), and connector discovery suggestions for unmapped folders.

## Global Constraints

- Everything in the phase 1 Global Constraints still holds: ULIDs, UTC `Z` second-precision timestamps, `props` JSON columns, every ledger mutation through `Ledger.transaction`, per-transaction review, loopback bind, bearer token, lane model A tokens.
- `signal`, `state` and `token` are not ledger tables: never attached to a Session, never in `LEDGER_TABLES`, written directly under `Ledger.locked()`. `signal.project_id` carries no foreign key so a project creation stays revertible after its signals arrive. Signals are idempotent on `dedupe_key`.
- Every use of the shared connection outside `Ledger.transaction` happens inside `with ledger.locked() as conn:`; routes, MCP tools, connectors and the ingest job all follow this.
- MCP endpoint: exact path `/mcp`, bearer token only (no cookie), stateless streamable HTTP with JSON responses, DNS rebinding protection disabled explicitly (the bearer token is the guard and the port is loopback-published). One `MCPServer` instance per `create_app` call.
- MCP writes carry the caller's `intent` verbatim as the transaction intent, actor kind `session` (owner token) or `agent` (scoped token), `tool="mcp"`, and start `pending`. Scoped tokens are checked per tool name and per project id; `unlink` is owner-only. No MCP tool deletes or merges anything else.
- Host paths are the identity of folder resources (what `init-project` ran against). `TRAJECTREE_PATH_MAP` (`host=container;host2=container2`) translates them inside the container for git; the transcript scanner matches on the `cwd` recorded in transcripts, which is always a host path.
- The transcript parser never raises on a bad line: unparsable JSON, missing fields and partial trailing lines are skipped; a file is only re-read from its last complete line.
- Secrets never reach a transcript, a signal payload or a log line: payloads hold session id, cwd, title, model, counts and the transcript path, never prompt text.
- The guidance file `<project>/.claude/rules/trajectree.md` is fully managed: rewritten on every `init-project`, first line `<!-- managed by trajectree init-project; edits are overwritten -->`.
- Strip colors use only `--strip-1` to `--strip-5` from `tokens.css`; the live marker uses `--now`; chip text uses `--ink2`. Text never uses a kind color.
- Web accessible names added in this phase: tab `Inbox`, buttons `Accept all`, `Copy resume command`, select `Show` (ledger filter) with options `All`, `Pending`, `Humans`, `Agents & sessions`; empty inbox text `Nothing to review`.

## Review Focus

1. A transcript whose last line is still being written (no trailing newline) or contains garbage: the scanner must count the complete lines once, skip the rest, and pick up the completed line on the next scan without double counting. Pinned in Task 10 (`test_scanner_resumes_after_partial_line`, `test_malformed_lines_are_skipped`).
2. Rejecting a pending transaction whose rows were edited afterwards: the API answers 409 with the conflicts, nothing is applied, and the review stays `pending`. Pinned in Task 2 (`test_reject_conflict_keeps_review_pending`).
3. An MCP write naming an unknown project, or with no project and a cwd that maps to none: the tool fails with the list of known slugs and writes nothing. Pinned in Task 6 (`test_write_unknown_project_lists_slugs`).
4. A scoped token used on another project, on a tool outside its allowlist, or after expiry: the call fails and no transaction is recorded. Pinned in Task 4 (`test_expired_token_resolves_none`) and Task 6 (`test_scope_denied_writes_nothing`).
5. `trajectree init-project` run twice on the same folder: one project, one resource per kind, the rules file rewritten, the exclude line present once, `claude mcp add` not repeated. Pinned in Task 13 (`test_init_project_twice_is_idempotent`).

---

### Task 1: Schema v2, signal and state repositories, summary stamping

**Files:**
- Modify: `src/trajectree/db/schema.py`, `src/trajectree/models.py`, `src/trajectree/repo/projects.py`
- Create: `src/trajectree/repo/signals.py`, `src/trajectree/repo/state.py`
- Test: `tests/test_schema_v2.py`, `tests/test_repo_signals.py`

**Interfaces:**
- Consumes: `MIGRATIONS`, `SCHEMA_VERSION`, `LEDGER_TABLES`, `table_columns` from `db/schema.py`; `to_db`, `from_db`, `utcnow`; `update_project` from `repo/projects.py`.
- Produces: `SCHEMA_VERSION = 2` with a second migration step; models `SignalKind = Literal["commits", "session", "files_changed", "job_state", "message"]`, `SignalIn(project_id: str, kind: SignalKind, t_start: AwareDatetime, dedupe_key: str, t_end: AwareDatetime | None = None, resource_id: str | None = None, payload: dict[str, Any] = {})`, `Signal(SignalIn)` adding `id: str`, `updated_at: AwareDatetime`; `StripBin(project_id: str, day: str, kind: SignalKind, count: int)`; `ResourceKind = Literal["folder", "git_local", "git_remote", "claude_session", "slurm_job", "url", "doc"]`; `ResourceIn(project_id: str, kind: ResourceKind, locator: str, title: str = "", props: dict = {})`; `Project.summary_at: AwareDatetime | None = None`.
- `repo/signals.py`: `upsert_signals(conn, rows: list[SignalIn]) -> int`; `list_signals(conn, *, project_ids: list[str] | None = None, t_from: datetime | None = None, t_to: datetime | None = None, kinds: list[str] | None = None, limit: int = 500) -> list[Signal]` (ascending `t_start`, overlap rule as `list_events`); `strip_bins(conn, *, project_ids: list[str] | None, t_from: datetime, t_to: datetime) -> list[StripBin]`; `stale_projects(conn, *, settled_before: datetime) -> list[str]`.
- `repo/state.py`: `get_state(conn, key: str) -> Any | None`; `set_state(conn, key: str, value: Any) -> None` (JSON, upsert, stamps `updated_at`).
- `update_project` sets `summary_at = now` whenever `summary_md` is among the patched fields.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_schema_v2.py
import apsw
from trajectree.db.connection import open_db
from trajectree.db.schema import LEDGER_TABLES, MIGRATIONS, table_columns

def test_v2_tables_and_column(conn):
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"signal", "state", "token"} <= names
    assert "summary_at" in table_columns(conn, "project")
    assert conn.execute("pragma user_version").fetchone()[0] == 2

def test_migrates_v1_database(tmp_path):
    path = tmp_path / "old.db"
    c = apsw.Connection(str(path))
    for stmt in MIGRATIONS[0][1]:
        c.execute(stmt)
    c.execute("pragma user_version = 1")
    c.close()
    conn = open_db(path)
    assert conn.execute("pragma user_version").fetchone()[0] == 2
    assert "summary_at" in table_columns(conn, "project")

def test_signal_dedupe_key_is_unique(conn):
    import pytest
    conn.execute("insert into signal(id, project_id, kind, t_start, payload, dedupe_key, updated_at) values ('a','p','session','2026-01-01T00:00:00Z','{}','k','2026-01-01T00:00:00Z')")
    with pytest.raises(apsw.ConstraintError):
        conn.execute("insert into signal(id, project_id, kind, t_start, payload, dedupe_key, updated_at) values ('b','p','session','2026-01-01T00:00:00Z','{}','k','2026-01-01T00:00:00Z')")

def test_new_tables_are_not_ledger_tables():
    assert not {"signal", "state", "token"} & set(LEDGER_TABLES)
```

```python
# tests/test_repo_signals.py
from datetime import datetime, timedelta, timezone
from trajectree.models import ProjectIn, ProjectPatch, SignalIn
from trajectree.repo.projects import create_project, update_project
from trajectree.repo.signals import list_signals, stale_projects, strip_bins, upsert_signals
from trajectree.repo.state import get_state, set_state
from trajectree.timeutil import utcnow

UTC = timezone.utc

def sig(pid, day, kind="session", key=None, hour=10):
    t = datetime(2026, 1, day, hour, tzinfo=UTC)
    return SignalIn(project_id=pid, kind=kind, t_start=t, t_end=t + timedelta(hours=1),
                    payload={"n": day}, dedupe_key=key or f"{kind}:{pid}:{day}")

def test_upsert_is_idempotent_and_updates_payload(conn):
    assert upsert_signals(conn, [sig("p1", 5), sig("p1", 6)]) == 2
    again = sig("p1", 5).model_copy(update={"payload": {"n": 50}})
    upsert_signals(conn, [again])
    rows = list_signals(conn, project_ids=["p1"])
    assert len(rows) == 2 and rows[0].payload == {"n": 50} and rows[0].t_start < rows[1].t_start

def test_list_signals_window_kinds_and_limit(conn):
    upsert_signals(conn, [sig("p1", 5), sig("p1", 20, kind="commits")])
    rows = list_signals(conn, project_ids=["p1"], t_from=datetime(2026, 1, 10, tzinfo=UTC), t_to=datetime(2026, 1, 31, tzinfo=UTC))
    assert [r.kind for r in rows] == ["commits"]
    assert list_signals(conn, kinds=["session"])[0].dedupe_key == "session:p1:5"
    assert len(list_signals(conn, limit=1)) == 1

def test_strip_bins_group_by_day_and_kind(conn):
    upsert_signals(conn, [sig("p1", 5), sig("p1", 5, key="x", hour=15), sig("p1", 5, kind="commits"), sig("p2", 5)])
    bins = strip_bins(conn, project_ids=["p1"], t_from=datetime(2026, 1, 1, tzinfo=UTC), t_to=datetime(2026, 2, 1, tzinfo=UTC))
    assert sorted((b.kind, b.count) for b in bins) == [("commits", 1), ("session", 2)]
    assert all(b.day == "2026-01-05" and b.project_id == "p1" for b in bins)
    assert len(strip_bins(conn, project_ids=None, t_from=datetime(2026, 1, 1, tzinfo=UTC), t_to=datetime(2026, 2, 1, tzinfo=UTC))) == 3

def test_state_round_trip(conn):
    assert get_state(conn, "k") is None
    set_state(conn, "k", {"offset": 10})
    set_state(conn, "k", {"offset": 20})
    assert get_state(conn, "k") == {"offset": 20}

def test_stale_projects(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    q = create_project(ledger, human, ProjectIn(slug="b", title="B", kind="research"))
    now = utcnow()
    upsert_signals(ledger.conn, [
        SignalIn(project_id=p.id, kind="session", t_start=now - timedelta(days=2), dedupe_key="s1"),
        SignalIn(project_id=q.id, kind="session", t_start=now - timedelta(days=2), dedupe_key="s2"),
    ])
    update_project(ledger, human, q.id, ProjectPatch(summary_md="fresh"))
    settled = now - timedelta(hours=24)
    assert stale_projects(ledger.conn, settled_before=settled) == [p.id]   # a has activity and no summary
    upsert_signals(ledger.conn, [SignalIn(project_id=q.id, kind="session", t_start=now - timedelta(hours=1), dedupe_key="s3")])
    assert stale_projects(ledger.conn, settled_before=settled) == [p.id]   # b's new activity has not settled yet

def test_summary_write_stamps_summary_at(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    assert p.summary_at is None
    p2 = update_project(ledger, human, p.id, ProjectPatch(summary_md="hello"))
    assert p2.summary_at is not None
    p3 = update_project(ledger, human, p.id, ProjectPatch(title="A2"))
    assert p3.summary_at == p2.summary_at
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_schema_v2.py tests/test_repo_signals.py -q`
Expected: ImportError for `trajectree.repo.signals` and assertion failures on `user_version`.

- [ ] **Step 3: Add migration step 2 to `MIGRATIONS` in `schema.py`** with exactly these statements, and set `SCHEMA_VERSION` from the last step as before:

```sql
ALTER TABLE project ADD COLUMN summary_at TEXT
CREATE TABLE signal (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, resource_id TEXT, kind TEXT NOT NULL, t_start TEXT NOT NULL, t_end TEXT, payload TEXT NOT NULL DEFAULT '{}', dedupe_key TEXT UNIQUE NOT NULL, updated_at TEXT NOT NULL)
CREATE INDEX signal_project_t ON signal(project_id, t_start)
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)
CREATE TABLE token (id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL, label TEXT NOT NULL, scope TEXT NOT NULL, actor TEXT NOT NULL, expires_at TEXT, created_at TEXT NOT NULL)
```

- [ ] **Step 4: Add the models** listed in Interfaces to `models.py` (`Signal`, `SignalIn`, `StripBin`, `ResourceIn`, `ResourceKind`, `SignalKind`, `Project.summary_at`).

- [ ] **Step 5: Implement `repo/signals.py` and `repo/state.py`** with the signatures above. `upsert_signals` uses `INSERT ... ON CONFLICT(dedupe_key) DO UPDATE SET project_id, resource_id, t_start, t_end, payload, updated_at = excluded.*` and returns `len(rows)`. `strip_bins` groups by `project_id, substr(t_start, 1, 10), kind`. `stale_projects` returns active projects, ordered by id, for which `MIN(ts)` over `signal.t_start` and `event.created_at` restricted to `ts > coalesce(project.summary_at, '')` is `<= to_db(settled_before)`.

- [ ] **Step 6: Stamp `summary_at` in `update_project`** when `"summary_md" in fields`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest -q`
Expected: all phase 1 tests still pass plus the new ones.

- [ ] **Step 8: Commit**

```bash
git add src/trajectree tests
git commit -m "feat(db): schema v2 with signals, state and tokens; summary_at stamping"
```

---

### Task 2: Ledger hardening and pending transaction map

**Files:**
- Modify: `src/trajectree/db/ledger.py`, `src/trajectree/repo/review.py`, `src/trajectree/api/routes_timeline.py`, `src/trajectree/api/routes_ledger.py`, `src/trajectree/api/routes_entities.py`, `src/trajectree/api/routes_search.py`
- Test: `tests/test_revert.py` (append), `tests/test_api_timeline_ledger.py` (modify `test_timeline_returns_window_and_pending`, append)

**Interfaces:**
- Consumes: `Ledger`, `RevertConflict`, `AlreadyReverted`, `pending_ids`.
- Produces: `Ledger.locked() -> Iterator[apsw.Connection]` (context manager acquiring the ledger's RLock; re-entrant inside `transaction`); `Ledger.revert(txn_id, actor, intent=None, *, mark_review: Literal["rejected"] | None = None) -> Txn` (when `mark_review` is given the `UPDATE txn SET review, review_ts` for the original runs inside the same `transaction` block as the inverse apply); `Ledger.set_review(txn_id, state, actor) -> Txn` with these rules: `accepted` on a reverted txn raises `AlreadyReverted`; `accepted` twice keeps the first `review_ts`; `rejected` when already `rejected` returns the txn unchanged; otherwise `rejected` calls `revert(..., mark_review="rejected")`; `Ledger.list_txns(*, limit=50, before=None, review: str | None = None, actor_kinds: list[str] | None = None)`; `pending_txns(conn, table_name) -> dict[str, str]` (row id to the pending txn id of its latest change; replaces `pending_ids`); `TimelineResponse.pending: dict[str, dict[str, str]]`; `GET /api/ledger/txns?review=pending&actor_kind=session,agent` (`actor_kind` is a comma list).
- All read endpoints wrap repository calls in `with ledger.locked() as conn:`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_revert.py
import pytest
from datetime import datetime, timezone
from trajectree.db.ledger import AlreadyReverted, RevertConflict
from trajectree.models import EventIn, EventPatch, ProjectIn
from trajectree.repo.events import create_event, get_event, update_event
from trajectree.repo.projects import create_project
from trajectree.repo.review import pending_txns

UTC = timezone.utc

def _pending_event(ledger, human, agent):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    ev = create_event(ledger, agent, EventIn(project_id=p.id, kind="note", title="n", t_start=datetime(2026, 1, 1, tzinfo=UTC), source="agent"))
    return ev, ledger.list_txns()[0].id

def test_reject_conflict_keeps_review_pending(ledger, human, agent):
    ev, tid = _pending_event(ledger, human, agent)
    update_event(ledger, human, ev.id, EventPatch(title="edited"))
    with pytest.raises(RevertConflict):
        ledger.set_review(tid, "rejected", human)
    t = ledger.get_txn(tid)
    assert t.review == "pending" and t.reverted_by is None

def test_reject_is_one_transaction_and_idempotent(ledger, human, agent):
    ev, tid = _pending_event(ledger, human, agent)
    before = len(ledger.list_txns())
    rejected = ledger.set_review(tid, "rejected", human)
    assert rejected.review == "rejected" and rejected.reverted_by is not None
    assert len(ledger.list_txns()) == before + 1
    assert ledger.get_txn(rejected.reverted_by).reverts_txn == tid
    assert ledger.set_review(tid, "rejected", human).reverted_by == rejected.reverted_by
    assert len(ledger.list_txns()) == before + 1
    assert get_event(ledger.conn, ev.id) is None

def test_accept_reverted_txn_refused(ledger, human, agent):
    _, tid = _pending_event(ledger, human, agent)
    ledger.revert(tid, human)
    with pytest.raises(AlreadyReverted):
        ledger.set_review(tid, "accepted", human)

def test_accept_twice_keeps_first_review_ts(ledger, human, agent, monkeypatch):
    _, tid = _pending_event(ledger, human, agent)
    first = ledger.set_review(tid, "accepted", human)
    monkeypatch.setattr("trajectree.db.ledger.utcnow", lambda: datetime(2030, 1, 1, tzinfo=UTC))
    assert ledger.set_review(tid, "accepted", human).review_ts == first.review_ts

def test_list_txns_filters(ledger, human, agent):
    _pending_event(ledger, human, agent)
    assert [t.actor_kind for t in ledger.list_txns(review="pending")] == ["agent"]
    assert len(ledger.list_txns(actor_kinds=["human"])) == 1
    assert len(ledger.list_txns(actor_kinds=["human", "agent"])) == 2

def test_pending_txns_maps_row_to_latest_pending_txn(ledger, human, agent):
    ev, tid = _pending_event(ledger, human, agent)
    assert pending_txns(ledger.conn, "event") == {ev.id: tid}
    update_event(ledger, agent, ev.id, EventPatch(title="again"))
    newer = ledger.list_txns()[0].id
    assert pending_txns(ledger.conn, "event") == {ev.id: newer}
    ledger.set_review(newer, "accepted", human)
    assert pending_txns(ledger.conn, "event") == {}

def test_locked_is_reentrant_inside_transaction(ledger, human):
    with ledger.transaction(human, "x"):
        with ledger.locked() as conn:
            assert conn is ledger.conn
```

```python
# tests/test_api_timeline_ledger.py: change the pending assertion in test_timeline_returns_window_and_pending to
    assert tl["pending"]["event"] == {pending.id: client.get("/api/ledger/txns").json()[0]["id"]} and tl["live"] == []
# and append
def test_ledger_filters(client, app, agent):
    p, _ = seed(client)
    from trajectree.repo.events import create_event
    from trajectree.models import EventIn
    from datetime import datetime, timezone
    create_event(app.state.ledger, agent, EventIn(project_id=p["id"], kind="note", title="n",
                 t_start=datetime(2026, 1, 6, tzinfo=timezone.utc), source="agent"))
    assert [t["actor_kind"] for t in client.get("/api/ledger/txns", params={"review": "pending"}).json()] == ["agent"]
    assert len(client.get("/api/ledger/txns", params={"actor_kind": "human"}).json()) == 2
    assert client.get("/api/ledger/txns", params={"review": "bogus"}).status_code == 422
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_revert.py tests/test_api_timeline_ledger.py -q`
Expected: failures on `locked`, `mark_review`, filters and the pending shape.

- [ ] **Step 3: Implement the ledger changes** per Interfaces. `review` on the route is `Literal["pending", "accepted", "rejected", "none"] | None`.

- [ ] **Step 4: Replace `pending_ids` with `pending_txns`** in `repo/review.py` and use it in `routes_timeline.py`; update `TimelineResponse.pending`.

- [ ] **Step 5: Wrap every read in the routes** (`routes_entities` list endpoints, `routes_timeline`, `routes_ledger` gets, `routes_search`) in `with ledger.locked() as conn:`.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/trajectree tests
git commit -m "fix(ledger): atomic reject, accept guard, pending txn map, reads under the lock"
```

---

### Task 3: Resources repository and routes

**Files:**
- Create: `src/trajectree/repo/resources.py`, `src/trajectree/api/routes_resources.py`
- Modify: `src/trajectree/api/app.py` (append to `PROTECTED_ROUTERS`)
- Test: `tests/test_repo_resources.py`, `tests/test_api_resources.py`

**Interfaces:**
- Consumes: `ResourceIn`, `Resource`, `Ledger`, `_rows` helpers, `NotFound`.
- Produces: `get_resource(conn, resource_id) -> Resource | None`; `find_resource(conn, project_id, kind, locator) -> Resource | None`; `list_resources(conn, *, project_ids: list[str] | None = None, kinds: list[str] | None = None) -> list[Resource]` ordered `project_id, kind, locator`; `upsert_resource(ledger, actor, data: ResourceIn, *, intent: str | None = None) -> Resource` (keyed on `(project_id, kind, locator)`; updates `title`, `props`, `updated_at` when either differs; writes nothing when identical; raises `NotFound` for an unknown project); `project_for_path(conn, path: str) -> Project | None` (the `folder` or `git_local` resource whose locator equals `path` or is an ancestor of it by path components, longest locator wins).
- Routes: `GET /api/resources?projects=&kinds=` -> `list[Resource]`; `POST /api/resources` with `ResourceCreate(ResourceIn)` plus `intent: str | None` -> `Resource` (200 for both create and update).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repo_resources.py
import pytest
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo import NotFound
from trajectree.repo.projects import create_project
from trajectree.repo.resources import list_resources, project_for_path, upsert_resource

def test_upsert_creates_then_updates_without_duplicate(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    r1 = upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator="/mnt/x", title="X"))
    r2 = upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator="/mnt/x", title="X2"))
    assert r1.id == r2.id and r2.title == "X2"
    assert len(list_resources(ledger.conn, project_ids=[p.id])) == 1

def test_upsert_unchanged_writes_no_txn(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    data = ResourceIn(project_id=p.id, kind="folder", locator="/mnt/x", title="X")
    upsert_resource(ledger, human, data)
    n = len(ledger.list_txns())
    upsert_resource(ledger, human, data)
    assert len(ledger.list_txns()) == n

def test_upsert_unknown_project_raises(ledger, human):
    with pytest.raises(NotFound):
        upsert_resource(ledger, human, ResourceIn(project_id="nope", kind="folder", locator="/x"))

def test_project_for_path_prefers_longest_component_prefix(ledger, human):
    a = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    b = create_project(ledger, human, ProjectIn(slug="b", title="B", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=a.id, kind="folder", locator="/mnt/apps"))
    upsert_resource(ledger, human, ResourceIn(project_id=b.id, kind="git_local", locator="/mnt/apps/trajectree"))
    conn = ledger.conn
    assert project_for_path(conn, "/mnt/apps/trajectree/src").id == b.id
    assert project_for_path(conn, "/mnt/apps/trajectree").id == b.id
    assert project_for_path(conn, "/mnt/apps/other").id == a.id
    assert project_for_path(conn, "/mnt/applications") is None
    assert project_for_path(conn, "/elsewhere") is None
```

```python
# tests/test_api_resources.py
from tests.test_api_entities import mk_project

def test_resources_upsert_list_and_errors(client):
    p = mk_project(client)
    body = {"project_id": p["id"], "kind": "folder", "locator": "/mnt/x", "title": "X"}
    r1 = client.post("/api/resources", json=body).json()
    r2 = client.post("/api/resources", json={**body, "title": "X2"}).json()
    assert r1["id"] == r2["id"] and r2["title"] == "X2"
    assert [r["id"] for r in client.get("/api/resources", params={"kinds": "folder"}).json()] == [r1["id"]]
    assert client.get("/api/resources", params={"kinds": "url"}).json() == []
    assert client.post("/api/resources", json={**body, "kind": "weird"}).status_code == 422
    assert client.post("/api/resources", json={**body, "project_id": "nope"}).status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_repo_resources.py tests/test_api_resources.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement `repo/resources.py`** per Interfaces. `project_for_path` loads all `folder` and `git_local` resources, keeps those where `PurePosixPath(path)` equals or `is_relative_to` the locator, picks the longest locator, and returns `get_project`.

- [ ] **Step 4: Implement `routes_resources.py`** following `routes_entities.py` (`_strip`, `_csv`, `actor_from_request`) and append its router to `PROTECTED_ROUTERS`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/trajectree tests
git commit -m "feat(repo): resources with upsert, path mapping and routes"
```

---

### Task 4: Scoped tokens and principals

**Files:**
- Create: `src/trajectree/auth/__init__.py`, `src/trajectree/auth/scopes.py`, `src/trajectree/auth/tokens.py`
- Test: `tests/test_tokens.py`

**Interfaces:**
- Consumes: `token` table (Task 1), `Actor`, `new_id`, `to_db`, `from_db`, `utcnow`.
- Produces in `scopes.py`: `Scope(BaseModel)` with `projects: list[str] | None = None`, `tools: list[str] | None = None`, `write: bool = True` and `allows(self, tool: str, project_id: str | None, *, write: bool) -> bool` (false when `write and not self.write`; false when `tools is not None and tool not in tools`; false when `project_id is not None and projects is not None and project_id not in projects`); `Principal(BaseModel)` with `actor: Actor`, `scope: Scope`, `token_id: str | None = None`; `owner_principal(session_ref: str | None = None) -> Principal` returning `Actor(kind="session", ref=session_ref or "owner", tool="mcp")` with the full scope and `token_id=None`.
- Produces in `tokens.py`: `mint_token(conn, *, label: str, scope: Scope, actor: Actor, ttl: timedelta | None) -> tuple[str, str]` returning `(token_id, secret)` where the secret is `"trj_" + secrets.token_urlsafe(32)` and only `sha256(secret).hexdigest()` is stored; `resolve_token(conn, secret: str, *, now: datetime | None = None) -> Principal | None` (None for unknown, expired or revoked); `revoke_token(conn, token_id: str) -> None` (deletes the row).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tokens.py
from datetime import datetime, timedelta, timezone
from trajectree.auth.scopes import Scope, owner_principal
from trajectree.auth.tokens import mint_token, resolve_token, revoke_token
from trajectree.models import Actor

UTC = timezone.utc
RUN = Actor(kind="agent", ref="run_1", model="claude-opus-5-5", backend="claude", tool="mcp")

def test_mint_and_resolve_round_trip(conn):
    scope = Scope(projects=["p1"], tools=["record_event"])
    tid, secret = mint_token(conn, label="run_1", scope=scope, actor=RUN, ttl=timedelta(hours=1))
    assert secret.startswith("trj_")
    principal = resolve_token(conn, secret)
    assert principal.actor == RUN and principal.scope == scope and principal.token_id == tid
    assert conn.execute("select count(*) from token where hash = ?", (secret,)).fetchone()[0] == 0

def test_unknown_and_revoked_tokens_resolve_none(conn):
    tid, secret = mint_token(conn, label="x", scope=Scope(), actor=RUN, ttl=None)
    assert resolve_token(conn, "trj_nope") is None
    revoke_token(conn, tid)
    assert resolve_token(conn, secret) is None

def test_expired_token_resolves_none(conn):
    _, secret = mint_token(conn, label="x", scope=Scope(), actor=RUN, ttl=timedelta(minutes=5))
    assert resolve_token(conn, secret, now=datetime.now(UTC) + timedelta(minutes=6)) is None
    assert resolve_token(conn, secret, now=datetime.now(UTC) + timedelta(minutes=4)) is not None

def test_scope_allows_matrix():
    full = Scope()
    assert full.allows("unlink", "p1", write=True) and full.allows("timeline", None, write=False)
    s = Scope(projects=["p1"], tools=["record_event", "timeline"])
    assert s.allows("record_event", "p1", write=True)
    assert not s.allows("record_event", "p2", write=True)
    assert not s.allows("add_todo", "p1", write=True)
    assert s.allows("timeline", None, write=False)
    assert not Scope(write=False).allows("record_event", "p1", write=True)

def test_owner_principal():
    p = owner_principal("sess-1")
    assert p.actor.kind == "session" and p.actor.ref == "sess-1" and p.token_id is None
    assert owner_principal().actor.ref == "owner"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tokens.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement `scopes.py` and `tokens.py`** per Interfaces. `scope` and `actor` are stored as JSON (`model_dump_json`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_tokens.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/auth tests/test_tokens.py
git commit -m "feat(auth): scoped tokens and principals"
```

---

### Task 5: MCP server factory and read tools

**Files:**
- Modify: `pyproject.toml` (add `mcp>=2.3.0` to dependencies; run `uv lock`), `tests/conftest.py` (add `anyio_backend` fixture returning `"asyncio"`)
- Create: `src/trajectree/mcpserver/__init__.py`, `src/trajectree/mcpserver/server.py`, `src/trajectree/mcpserver/read_tools.py`
- Test: `tests/test_mcp_read.py`

**Interfaces:**
- Consumes: `Ledger.locked`, repositories (`projects`, `events`, `todos`, `edges`, `resources`, `signals`), `fts.search`, `Principal`, `owner_principal`, `resolve_token`, `project_for_path`.
- Produces in `server.py`: `ToolDeps` dataclass `(ledger: Ledger, settings: Settings, resolve: Callable[[Context], Principal], cwd_of: Callable[[Context], str | None])`; `build_mcp(ledger: Ledger, settings: Settings, *, resolve: Callable[[Context], Principal], cwd_of: Callable[[Context], str | None] | None = None) -> MCPServer` (name `"trajectree"`, `instructions` one paragraph naming the write tools and the `intent` rule; default `cwd_of` reads header `x-trajectree-cwd` from `ctx.headers`); `principal_from_headers(headers: Mapping[str, str] | None, *, owner_token: str, conn: apsw.Connection) -> Principal` (bearer equal to the owner token, compared with `secrets.compare_digest`, gives `owner_principal(headers.get("x-trajectree-session"))`; otherwise `resolve_token`; otherwise raises `ToolError("unauthorized")`); `resolve_project(conn, slug: str | None, cwd: str | None) -> Project` (slug lookup, else `project_for_path(cwd)`, else `ToolError` whose message is `unknown project '<slug>'; known projects: a, b, c` or `no project given and the working directory maps to none; known projects: ...`); `parse_when(value: str | None, default: datetime) -> datetime` (ISO 8601 with `Z` accepted; naive input rejected; bad input raises `ToolError("'<value>' is not an ISO 8601 datetime")`).
- Produces in `read_tools.py`: `register_read_tools(mcp: MCPServer, deps: ToolDeps) -> None` adding tools, exact names and signatures: `list_projects(status: str | None = None) -> list[Project]`; `get_project(project: str) -> ProjectDetail` where `ProjectDetail(project: Project, open_todos: list[Todo], resources: list[Resource], recent_events: list[Event])` (recent 20 by `t_start` descending); `timeline(projects: list[str] | None = None, start: str | None = None, end: str | None = None, kinds: list[str] | None = None) -> TimelineOut(events: list[Event], todos: list[Todo], edges: list[Edge])` (defaults: `start` = now minus 90 days, `end` = now plus 30 days; `projects` are slugs); `search(q: str, limit: int = 20) -> list[SearchHit]`; `list_todos(project: str | None = None, states: list[str] | None = None) -> list[Todo]` (default states `["open", "doing"]`); `get_signals(project: str, start: str | None = None, end: str | None = None, kinds: list[str] | None = None, limit: int = 200) -> list[Signal]`.
- Every tool takes `ctx: Context`, obtains `principal = deps.resolve(ctx)`, and runs its reads inside `with deps.ledger.locked() as conn:`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_read.py
from datetime import datetime, timezone
import pytest
from mcp import Client
from trajectree.auth.scopes import owner_principal
from trajectree.mcpserver.server import build_mcp, parse_when
from trajectree.models import EventIn, ProjectIn, TodoIn
from trajectree.repo.events import create_event
from trajectree.repo.projects import create_project
from trajectree.repo.todos import create_todo
from mcp.server.mcpserver.exceptions import ToolError

UTC = timezone.utc
pytestmark = pytest.mark.anyio

@pytest.fixture
def mcp(ledger, settings):
    return build_mcp(ledger, settings, resolve=lambda ctx: owner_principal(None))

def seed(ledger, human):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research", summary_md="sum"))
    e = create_event(ledger, human, EventIn(project_id=p.id, kind="decision", title="Freeze", t_start=datetime(2026, 1, 5, tzinfo=UTC)))
    t = create_todo(ledger, human, TodoIn(project_id=p.id, title="write up"))
    return p, e, t

async def test_tool_names(mcp):
    async with Client(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"list_projects", "get_project", "timeline", "search", "list_todos", "get_signals"} <= names

async def test_get_project_by_slug(mcp, ledger, human):
    p, e, t = seed(ledger, human)
    async with Client(mcp) as c:
        r = await c.call_tool("get_project", {"project": "a"})
        assert not r.is_error
        d = r.structured_content
        assert d["project"]["slug"] == "a" and [x["id"] for x in d["recent_events"]] == [e.id]
        assert [x["id"] for x in d["open_todos"]] == [t.id]

async def test_unknown_project_lists_known_slugs(mcp, ledger, human):
    seed(ledger, human)
    async with Client(mcp) as c:
        r = await c.call_tool("get_project", {"project": "zzz"})
        assert r.is_error and "known projects: a" in r.content[0].text

async def test_timeline_window_and_bad_date(mcp, ledger, human):
    p, e, _ = seed(ledger, human)
    async with Client(mcp) as c:
        r = await c.call_tool("timeline", {"projects": ["a"], "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"})
        assert [x["id"] for x in r.structured_content["events"]] == [e.id]
        r2 = await c.call_tool("timeline", {"start": "yesterday"})
        assert r2.is_error and "ISO 8601" in r2.content[0].text

async def test_search_and_todos(mcp, ledger, human):
    p, e, t = seed(ledger, human)
    async with Client(mcp) as c:
        hits = (await c.call_tool("search", {"q": "freeze"})).structured_content["result"]
        assert hits[0]["id"] == e.id
        todos = (await c.call_tool("list_todos", {"project": "a"})).structured_content["result"]
        assert [x["id"] for x in todos] == [t.id]

def test_parse_when():
    assert parse_when("2026-01-05T10:00:00Z", datetime(2000, 1, 1, tzinfo=UTC)) == datetime(2026, 1, 5, 10, tzinfo=UTC)
    assert parse_when(None, datetime(2000, 1, 1, tzinfo=UTC)) == datetime(2000, 1, 1, tzinfo=UTC)
    with pytest.raises(ToolError):
        parse_when("2026-01-05T10:00:00", datetime(2000, 1, 1, tzinfo=UTC))
```

- [ ] **Step 2: Add the dependency and run the tests to verify they fail**

Run: `uv add 'mcp>=2.3.0' && uv run pytest tests/test_mcp_read.py -q`
Expected: ImportError for `trajectree.mcpserver`.

- [ ] **Step 3: Implement `server.py` and `read_tools.py`** per Interfaces. Imports: `from mcp.server import MCPServer`, `from mcp.server.mcpserver import Context`, `from mcp.server.mcpserver.exceptions import ToolError`. Tool functions are `async def` returning pydantic models or lists of them (the SDK wraps a list as `{"result": [...]}`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_mcp_read.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/trajectree/mcpserver tests
git commit -m "feat(mcp): server factory and read tools"
```

---

### Task 6: MCP write tools with scope enforcement

**Files:**
- Create: `src/trajectree/mcpserver/write_tools.py`
- Modify: `src/trajectree/mcpserver/server.py` (call `register_write_tools`)
- Test: `tests/test_mcp_write.py`

**Interfaces:**
- Consumes: Task 5 `ToolDeps`, `resolve_project`, `parse_when`; repositories; `Scope.allows`.
- Produces: `register_write_tools(mcp, deps) -> None` with these tools (all `async`, all take `ctx: Context` and a required `intent: str`): `record_event(title: str, intent: str, kind: str = "note", when: str | None = None, end: str | None = None, body_md: str = "", status: EventStatus = "happened", project: str | None = None) -> Event`; `add_note(text: str, intent: str, project: str | None = None) -> Event` (kind `note`, `t_start` now); `update_event(event_id: str, intent: str, title: str | None = None, body_md: str | None = None, kind: str | None = None, status: EventStatus | None = None, when: str | None = None, end: str | None = None) -> Event`; `add_todo(title: str, intent: str, project: str | None = None, due: str | None = None, body_md: str = "", priority: int = 0) -> Todo`; `update_todo(todo_id: str, intent: str, title: str | None = None, state: TodoState | None = None, due: str | None = None, body_md: str | None = None, priority: int | None = None) -> Todo`; `link(src_type: EntityType, src_id: str, dst_type: EntityType, dst_id: str, rel: str, intent: str) -> Edge`; `unlink(edge_id: str, intent: str) -> str` (returns `"unlinked <edge_id>"`; owner only, `ToolError("unlink is owner-only")` when `principal.token_id` is set); `set_project_status(project: str, status: ProjectStatus, intent: str) -> Project`; `set_project_summary(project: str, summary_md: str, intent: str) -> Project`; `upsert_resource(project: str, kind: ResourceKind, locator: str, intent: str, title: str = "") -> Resource`.
- Rules shared by all writes: `principal = deps.resolve(ctx)`; the target project id is resolved first (from `project`, from the row for `update_*`, from both endpoints for `link` when they are events, todos, resources or projects); `principal.scope.allows(<tool name>, project_id, write=True)` must hold, else `ToolError("token not permitted: <tool> on <slug>")` and nothing is written; `source_for(actor) -> Source` maps `human -> "human"`, `connector -> "connector"`, else `"agent"`; repository errors (`NotFound`, `InvalidRange`, `DuplicateEdge`, `ValueError`) become `ToolError(str(exc))`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_write.py
from datetime import datetime, timezone
import pytest
from mcp import Client
from trajectree.auth.scopes import Principal, Scope, owner_principal
from trajectree.mcpserver.server import build_mcp
from trajectree.models import Actor, EventIn, ProjectIn, ResourceIn
from trajectree.repo.edges import list_edges
from trajectree.repo.events import create_event
from trajectree.repo.projects import create_project, get_project
from trajectree.repo.resources import upsert_resource

UTC = timezone.utc
pytestmark = pytest.mark.anyio

def two_projects(ledger, human):
    a = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    b = create_project(ledger, human, ProjectIn(slug="b", title="B", kind="tooling"))
    return a, b

def owner_mcp(ledger, settings, cwd=None):
    return build_mcp(ledger, settings, resolve=lambda ctx: owner_principal("sess-1"), cwd_of=lambda ctx: cwd)

async def test_record_event_lands_pending_with_session_actor(ledger, settings, human):
    a, _ = two_projects(ledger, human)
    async with Client(owner_mcp(ledger, settings)) as c:
        r = await c.call_tool("record_event", {"project": "a", "title": "Decided", "kind": "decision", "intent": "log decision"})
        assert not r.is_error and r.structured_content["source"] == "agent"
    t = ledger.list_txns()[0]
    assert (t.actor_kind, t.actor_ref, t.tool, t.review, t.intent) == ("session", "sess-1", "mcp", "pending", "log decision")

async def test_cwd_defaults_project(ledger, settings, human):
    a, _ = two_projects(ledger, human)
    upsert_resource(ledger, human, ResourceIn(project_id=a.id, kind="folder", locator="/mnt/x"))
    async with Client(owner_mcp(ledger, settings, cwd="/mnt/x/sub")) as c:
        r = await c.call_tool("add_note", {"text": "hello", "intent": "note"})
        assert r.structured_content["project_id"] == a.id and r.structured_content["kind"] == "note"

async def test_write_unknown_project_lists_slugs(ledger, settings, human):
    two_projects(ledger, human)
    n = len(ledger.list_txns())
    async with Client(owner_mcp(ledger, settings)) as c:
        r = await c.call_tool("record_event", {"project": "zzz", "title": "x", "intent": "i"})
        assert r.is_error and "known projects: a, b" in r.content[0].text
        r2 = await c.call_tool("add_note", {"text": "x", "intent": "i"})
        assert r2.is_error and "working directory" in r2.content[0].text
    assert len(ledger.list_txns()) == n

async def test_scope_denied_writes_nothing(ledger, settings, human):
    a, b = two_projects(ledger, human)
    run = Principal(actor=Actor(kind="agent", ref="run_1", model="m", backend="claude", tool="mcp"),
                    scope=Scope(projects=[b.id], tools=["record_event"]), token_id="tok")
    mcp = build_mcp(ledger, settings, resolve=lambda ctx: run)
    n = len(ledger.list_txns())
    async with Client(mcp) as c:
        denied = await c.call_tool("record_event", {"project": "a", "title": "x", "intent": "i"})
        assert denied.is_error and "not permitted" in denied.content[0].text
        denied2 = await c.call_tool("add_todo", {"project": "b", "title": "x", "intent": "i"})
        assert denied2.is_error and "not permitted" in denied2.content[0].text
        ok = await c.call_tool("record_event", {"project": "b", "title": "x", "intent": "i"})
        assert not ok.is_error
    txns = ledger.list_txns()
    assert len(txns) == n + 1 and txns[0].actor_kind == "agent" and txns[0].actor_ref == "run_1"

async def test_set_project_summary_and_status(ledger, settings, human):
    a, _ = two_projects(ledger, human)
    async with Client(owner_mcp(ledger, settings)) as c:
        r = await c.call_tool("set_project_summary", {"project": "a", "summary_md": "# A", "intent": "summarise"})
        assert r.structured_content["summary_at"] is not None
        r2 = await c.call_tool("set_project_status", {"project": "a", "status": "paused", "intent": "pause"})
        assert r2.structured_content["status"] == "paused"
    assert get_project(ledger.conn, a.id).summary_md == "# A"
    assert all(t.review == "pending" for t in ledger.list_txns()[:2])

async def test_link_and_unlink_owner_only(ledger, settings, human):
    a, b = two_projects(ledger, human)
    e1 = create_event(ledger, human, EventIn(project_id=a.id, kind="note", title="1", t_start=datetime(2026, 1, 1, tzinfo=UTC)))
    e2 = create_event(ledger, human, EventIn(project_id=b.id, kind="note", title="2", t_start=datetime(2026, 1, 2, tzinfo=UTC)))
    async with Client(owner_mcp(ledger, settings)) as c:
        edge = (await c.call_tool("link", {"src_type": "event", "src_id": e1.id, "dst_type": "event", "dst_id": e2.id, "rel": "caused", "intent": "l"})).structured_content
        assert edge["rel"] == "caused"
    scoped = Principal(actor=Actor(kind="agent", ref="r", tool="mcp"), scope=Scope(), token_id="tok")
    async with Client(build_mcp(ledger, settings, resolve=lambda ctx: scoped)) as c:
        r = await c.call_tool("unlink", {"edge_id": edge["id"], "intent": "u"})
        assert r.is_error and "owner-only" in r.content[0].text
    async with Client(owner_mcp(ledger, settings)) as c:
        r = await c.call_tool("unlink", {"edge_id": edge["id"], "intent": "u"})
        assert not r.is_error
    assert list_edges(ledger.conn) == []

async def test_todo_round_trip_and_bad_range(ledger, settings, human):
    a, _ = two_projects(ledger, human)
    async with Client(owner_mcp(ledger, settings)) as c:
        todo = (await c.call_tool("add_todo", {"project": "a", "title": "t", "due": "2026-03-01T00:00:00Z", "intent": "i"})).structured_content
        done = (await c.call_tool("update_todo", {"todo_id": todo["id"], "state": "done", "intent": "i"})).structured_content
        assert done["state"] == "done" and done["due"].startswith("2026-03-01")
        ev = (await c.call_tool("record_event", {"project": "a", "title": "x", "when": "2026-01-05T10:00:00Z", "intent": "i"})).structured_content
        bad = await c.call_tool("update_event", {"event_id": ev["id"], "end": "2026-01-01T00:00:00Z", "intent": "i"})
        assert bad.is_error
        missing = await c.call_tool("update_event", {"event_id": "nope", "title": "x", "intent": "i"})
        assert missing.is_error
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_mcp_write.py -q`
Expected: tools unknown (`is_error` with `Unknown tool`), assertions fail.

- [ ] **Step 3: Implement `write_tools.py`** per Interfaces and register it in `build_mcp`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_mcp_write.py tests/test_mcp_read.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/mcpserver tests/test_mcp_write.py
git commit -m "feat(mcp): write tools with intent, actor attribution and scope checks"
```

---

### Task 7: Context pack compiler

**Files:**
- Create: `src/trajectree/context/__init__.py`, `src/trajectree/context/pack.py`, `src/trajectree/api/routes_context.py`
- Modify: `src/trajectree/mcpserver/read_tools.py` (tool `get_context_pack`), `src/trajectree/api/app.py` (router), `src/trajectree/cli/main.py` (`context` subcommand)
- Test: `tests/test_context_pack.py`, `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: repositories, `fts.search`, `list_signals`.
- Produces: `ContextPack(BaseModel)` with `markdown: str`, `tokens_est: int`, `dropped: list[str]`, `projects: list[str]` (slugs); `build_context_pack(conn, *, project_ids: list[str], t_from: datetime, t_to: datetime, token_budget: int = 4000, question: str | None = None) -> ContextPack`.
- Format (exact headings): line 1 `# Trajectree context (<from date> to <to date>)`; per project `## <title> (<slug>) — <status>` then `### Summary`, `### Events` (one line each: `- <YYYY-MM-DD> [<kind>/<status>] <title> (<id>)`), `### Open todos` (`- [ ] <title> (due <YYYY-MM-DD>) (<id>)`, `(no due)` when absent), `### Resources` (`- <kind> <locator>`), `### Activity` (last 10 signals: `- <YYYY-MM-DD> <kind> <payload.title or payload.count>`); when `question` is given, a final `## Search hits for "<question>"` section with `- [<kind>] <title> (<project slug>) (<id>)`.
- Budget: `tokens_est = len(markdown) // 4`; the per-project budget is `token_budget // len(project_ids)`; section shares of that budget: summary 40 %, events 30 %, todos 10 %, resources 10 %, activity 10 %; a section that would exceed its share is cut at whole lines (summary at whole characters) and ends with `(dropped N more)`; each cut adds `"<slug>: <section> dropped N"` to `dropped`.
- Tool: `get_context_pack(projects: list[str] | None = None, start: str | None = None, end: str | None = None, token_budget: int = 4000, question: str | None = None) -> ContextPack` (default: all active projects, now minus 90 days to now plus 30 days).
- Route: `GET /api/context?projects=<slugs or ids csv>&from=&to=&budget=&q=` -> `ContextPack` (defaults as the tool).
- CLI: `trajectree context <slug>... [--days 90] [--budget 4000] [--q TEXT]` prints `markdown` to stdout.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_context_pack.py
from datetime import datetime, timedelta, timezone
import pytest
from mcp import Client
from trajectree.auth.scopes import owner_principal
from trajectree.context.pack import build_context_pack
from trajectree.mcpserver.server import build_mcp
from trajectree.models import EventIn, ProjectIn, TodoIn
from trajectree.repo.events import create_event
from trajectree.repo.projects import create_project
from trajectree.repo.todos import create_todo

UTC = timezone.utc
FROM, TO = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)

def seed(ledger, human, n_events=2):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research", summary_md="The summary."))
    for i in range(n_events):
        create_event(ledger, human, EventIn(project_id=p.id, kind="note", title=f"Event {i}", t_start=datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=i)))
    create_todo(ledger, human, TodoIn(project_id=p.id, title="Write up"))
    return p

def test_pack_sections_and_ids(ledger, human):
    p = seed(ledger, human)
    pack = build_context_pack(ledger.conn, project_ids=[p.id], t_from=FROM, t_to=TO)
    md = pack.markdown
    assert md.startswith("# Trajectree context (2026-01-01 to 2026-02-01)")
    assert "## A (a) — active" in md and "### Summary\nThe summary." in md
    assert "- 2026-01-02 [note/happened] Event 0 (" in md and "- [ ] Write up (no due) (" in md
    assert pack.dropped == [] and pack.projects == ["a"] and pack.tokens_est == len(md) // 4

def test_pack_respects_budget_and_reports_drops(ledger, human):
    p = seed(ledger, human, n_events=200)
    pack = build_context_pack(ledger.conn, project_ids=[p.id], t_from=FROM, t_to=TO, token_budget=600)
    assert pack.tokens_est <= 700
    assert any(d.startswith("a: events dropped") for d in pack.dropped)
    assert "(dropped" in pack.markdown

def test_pack_question_adds_search_hits(ledger, human):
    p = seed(ledger, human)
    pack = build_context_pack(ledger.conn, project_ids=[p.id], t_from=FROM, t_to=TO, question="write")
    assert '## Search hits for "write"' in pack.markdown and "[todo] Write up (a)" in pack.markdown

@pytest.mark.anyio
async def test_get_context_pack_tool(ledger, settings, human):
    seed(ledger, human)
    mcp = build_mcp(ledger, settings, resolve=lambda ctx: owner_principal(None))
    async with Client(mcp) as c:
        r = await c.call_tool("get_context_pack", {"projects": ["a"], "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"})
        assert r.structured_content["markdown"].startswith("# Trajectree context") and r.structured_content["projects"] == ["a"]
```

```python
# append to tests/test_cli.py (uses the file's existing env_for helper and the anon client)
def test_cli_context_prints_pack(app, client, anon, capsys):
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    main(["note", "clmi", "hello"], client=anon, env=env_for(app))
    assert main(["context", "clmi", "--days", "30"], client=anon, env=env_for(app)) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Trajectree context") and "hello" in out
```

```python
# tests/test_api_timeline_ledger.py: append
def test_context_route(client):
    p, e = seed(client)
    r = client.get("/api/context", params={"projects": p["slug"], "from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z"})
    assert r.status_code == 200 and "Freeze" in r.json()["markdown"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_context_pack.py tests/test_cli.py tests/test_api_timeline_ledger.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement `context/pack.py`**, the tool, the route and the CLI subcommand per Interfaces.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree tests
git commit -m "feat(context): token-budgeted context pack via MCP, API and CLI"
```

---

### Task 8: Mount the MCP endpoint with a bearer gate

**Files:**
- Create: `src/trajectree/api/mcp_mount.py`
- Modify: `src/trajectree/api/app.py`, `tests/conftest.py`
- Test: `tests/test_api_mcp.py`

**Interfaces:**
- Consumes: `build_mcp`, `principal_from_headers`, `resolve_token`, `token_matches`.
- Produces in `mcp_mount.py`: `class BearerGate` (ASGI callable constructed with `(handler, *, owner_token: str, ledger: Ledger)`; for `scope["type"] == "http"` it reads `Authorization`, accepts the owner token or a token that `resolve_token` resolves (under `ledger.locked()`), and otherwise sends `401` with body `{"detail": "Unauthorized"}`; on success it awaits `handler(scope, receive, send)`); `mount_mcp(app: FastAPI, mcp: MCPServer, *, ledger: Ledger, owner_token: str) -> None` which calls `mcp.streamable_http_app(streamable_http_path="/", json_response=True, stateless_http=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))` only to initialise `mcp.session_manager`, then appends `Route("/mcp", endpoint=BearerGate(mcp.session_manager.handle_request, ...), methods=["GET", "POST", "DELETE"])` to `app.router.routes`. (Verified on starlette 1.9 and mcp 2.3.0: a `Mount("/mcp")` answers 405 on `/mcp` itself, this `Route` serves the exact path.)
- `create_app` builds `mcp = build_mcp(ledger, settings, resolve=<production resolver>)`, stores `app.state.mcp`, runs `mcp.session_manager.run()` in the app lifespan, calls `mount_mcp` after the API routers and before the root and SPA fallback routes, and the SPA fallback also rejects `mcp` paths with 404. The production resolver calls `principal_from_headers(ctx.headers, owner_token=token, conn=conn)` inside `ledger.locked()`.
- `tests/conftest.py`: `client` and `anon` fixtures become context-managed (`with TestClient(...) as c: yield c`) so the lifespan runs; the `settings` fixture is unchanged for now.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_mcp.py
from datetime import timedelta
from trajectree.auth.scopes import Scope
from trajectree.auth.tokens import mint_token
from trajectree.models import Actor
from tests.test_api_entities import mk_project

HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

def call(name, args, id=3):
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": args}}

def test_mcp_requires_bearer(anon, app):
    assert anon.post("/mcp", json=INIT, headers=HDR).status_code == 401
    anon.cookies.set("trajectree_token", app.state.token)
    assert anon.post("/mcp", json=INIT, headers=HDR).status_code == 401

def test_mcp_initialize_and_tools_list(client):
    r = client.post("/mcp", json=INIT, headers=HDR)
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "trajectree"
    names = {t["name"] for t in client.post("/mcp", json=LIST, headers=HDR).json()["result"]["tools"]}
    assert {"record_event", "get_context_pack", "unlink"} <= names

def test_mcp_scoped_token_accepted_and_attributed(client, app):
    p = mk_project(client)
    _, secret = mint_token(app.state.ledger.conn, label="run", scope=Scope(projects=[p["id"]], tools=["record_event"]),
                           actor=Actor(kind="agent", ref="run_9", model="m", backend="claude", tool="mcp"), ttl=timedelta(hours=1))
    h = {**HDR, "Authorization": f"Bearer {secret}"}
    r = client.post("/mcp", json=call("record_event", {"project": p["slug"], "title": "T", "intent": "i"}), headers=h)
    assert r.status_code == 200 and not r.json()["result"].get("isError")
    pending = client.get("/api/ledger/txns", params={"review": "pending"}).json()
    assert pending[0]["actor_kind"] == "agent" and pending[0]["actor_ref"] == "run_9"
    assert client.post("/mcp", json=INIT, headers={**HDR, "Authorization": "Bearer trj_bogus"}).status_code == 401

def test_mcp_owner_write_over_http_uses_cwd_header(client, app):
    p = mk_project(client)
    client.post("/api/resources", json={"project_id": p["id"], "kind": "folder", "locator": "/home/u/proj"})
    r = client.post("/mcp", json=call("add_note", {"text": "hi", "intent": "i"}), headers={**HDR, "X-Trajectree-Cwd": "/home/u/proj/src"})
    assert r.json()["result"]["structuredContent"]["project_id"] == p["id"]
    assert client.get("/api/ledger/txns", params={"review": "pending"}).json()[0]["actor_kind"] == "session"

def test_spa_fallback_does_not_shadow_mcp(client):
    assert client.get("/mcp/anything").status_code in (404, 405)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api_mcp.py -q`
Expected: 404 or 405 responses instead of 200/401.

- [ ] **Step 3: Implement `mcp_mount.py`, the lifespan and the resolver in `create_app`, and the fixture change**.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass (phase 1 API tests run inside the lifespan now).

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/api tests
git commit -m "feat(api): bearer-gated MCP endpoint at /mcp"
```

---

### Task 9: Stdio shim `trajectree mcp`

**Files:**
- Create: `src/trajectree/cli/shim.py`
- Modify: `src/trajectree/cli/main.py` (`mcp` subcommand)
- Test: `tests/test_shim.py`, `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `mcp.server.Server`, `mcp.server.stdio.stdio_server`, `mcp.client.Client`, `mcp.client.streamable_http.streamable_http_client`, `httpx2.AsyncClient`, `_token` and `DEFAULT_URL` from `cli/main.py`.
- Produces: `build_proxy(connect: Callable[[], Client]) -> Server` (low-level server named `trajectree-shim`; `on_list_tools` opens `connect()` and returns the remote `ListToolsResult`, or `ListToolsResult(tools=[])` after writing the error to stderr; `on_call_tool` opens `connect()`, forwards `call_tool(params.name, params.arguments or {})` and returns the remote `CallToolResult` unchanged, or `CallToolResult(is_error=True, content=[TextContent(type="text", text=f"trajectree daemon unreachable: {exc}")])`); `remote_connector(url: str, token: str, cwd: str | None) -> Callable[[], Client]` building `Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}", "X-Trajectree-Client": "shim", **({"X-Trajectree-Cwd": cwd} if cwd else {})}, timeout=httpx2.Timeout(30, read=120))))`; `run_shim(env: Mapping[str, str]) -> int` (token from `_token(env)`, exit 2 with the same stderr message as the CLI when missing; url `env.get("TRAJECTREE_URL", DEFAULT_URL).rstrip("/") + "/mcp"`; cwd `env.get("CLAUDE_PROJECT_DIR") or os.getcwd()`; `anyio.run` of `stdio_server()` plus `server.run(read, write, server.create_initialization_options())`; returns 0). Nothing is ever printed to stdout by the shim except protocol traffic.
- `main(["mcp"])` dispatches to `run_shim(env)` before any HTTP client is built.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_shim.py
import pytest
from mcp import Client
from trajectree.auth.scopes import owner_principal
from trajectree.cli.shim import build_proxy
from trajectree.mcpserver.server import build_mcp
from trajectree.models import ProjectIn
from trajectree.repo.projects import create_project

pytestmark = pytest.mark.anyio

async def test_proxy_forwards_list_and_call(ledger, settings, human):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    local = build_mcp(ledger, settings, resolve=lambda ctx: owner_principal(None))
    proxy = build_proxy(lambda: Client(local))
    async with Client(proxy) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert "record_event" in names and "get_context_pack" in names
        r = await c.call_tool("list_projects", {})
        assert not r.is_error and r.structured_content["result"][0]["slug"] == "a"
        bad = await c.call_tool("get_project", {"project": "zzz"})
        assert bad.is_error and "known projects" in bad.content[0].text

async def test_proxy_reports_unreachable_daemon():
    proxy = build_proxy(lambda: Client("http://127.0.0.1:9/mcp"))
    async with Client(proxy) as c:
        assert (await c.list_tools()).tools == []
        r = await c.call_tool("list_projects", {})
        assert r.is_error and "unreachable" in r.content[0].text
```

```python
# append to tests/test_cli.py
def test_mcp_subcommand_requires_token(tmp_path, capsys):
    assert main(["mcp"], env={"HOME": str(tmp_path)}) == 2
    assert "no token" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_shim.py tests/test_cli.py -q`
Expected: ImportError / argparse error for `mcp`.

- [ ] **Step 3: Implement `shim.py` and the subcommand** per Interfaces.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_shim.py tests/test_cli.py -q`
Expected: all pass.

- [ ] **Step 5: Manual check** (not automated): with the daemon running (`uv run trajectreed`) and `TRAJECTREE_TOKEN` exported, `printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' | uv run trajectree mcp` prints one JSON-RPC result line with `serverInfo.name` equal to `trajectree-shim`. Record the outcome in the task report.

- [ ] **Step 6: Commit**

```bash
git add src/trajectree/cli tests
git commit -m "feat(cli): stdio MCP shim forwarding to the daemon with the project directory"
```

---

### Task 10: Path map settings and the Claude transcript scanner

**Files:**
- Create: `src/trajectree/connectors/__init__.py`, `src/trajectree/connectors/paths.py`, `src/trajectree/connectors/claude_sessions.py`
- Modify: `src/trajectree/config.py`, `tests/conftest.py` (`settings` fixture sets `claude_home=None`, `backends_root=None`, `ingest_interval_s=0`)
- Test: `tests/test_paths.py`, `tests/test_claude_sessions.py`

**Interfaces:**
- Consumes: `project_for_path`, `upsert_signals`, `get_state`/`set_state`, `SignalIn`.
- Produces in `config.py`: `Settings` gains `claude_home: Path | None = None`, `backends_root: Path | None = None`, `path_map: list[tuple[str, str]] = []`, `ingest_interval_s: int = 900`, `live_window_s: int = 600`, `stale_settle_h: int = 24`, `scan_since_days: int = 365`; `load_settings` reads `TRAJECTREE_CLAUDE_HOME` (empty string means None; default `~/.claude` when that directory exists), `TRAJECTREE_BACKENDS_ROOT` (same rule, default `~/.config/claude-backends`), `TRAJECTREE_PATH_MAP` (`host=container;host2=container2`), `TRAJECTREE_INGEST_EVERY`, `TRAJECTREE_LIVE_WINDOW`, `TRAJECTREE_STALE_SETTLE_H`, `TRAJECTREE_SCAN_SINCE_DAYS`.
- Produces in `paths.py`: `to_local(settings, host_path: str) -> Path` (longest component-wise prefix from `path_map`, identity when none matches); `encode_project_dir(cwd: str) -> str` (every character outside `[A-Za-z0-9]` becomes `-`, matching Claude Code's folder naming; used by tests and by nothing else).
- Produces in `claude_sessions.py`: `SessionSummary(BaseModel)` with `session_id: str | None`, `cwd: str | None`, `title: str | None`, `git_branch: str | None`, `model: str | None`, `version: str | None`, `t_start: AwareDatetime | None`, `t_end: AwareDatetime | None`, `prompts: int = 0`, `assistant_msgs: int = 0`, `tokens_in: int = 0`, `tokens_out: int = 0`, `transcript_path: str`, `backend: str`; `parse_lines(lines: Iterable[bytes], acc: SessionSummary) -> SessionSummary` (per line: `json.loads`, skip on any exception or non-dict; `type == "user"` with `message.role == "user"` counts a prompt when `message.content` is a string or a list containing a `{"type": "text"}` block, and skips records with `isMeta` true; `type == "assistant"` increments `assistant_msgs`, sets `model` from `message.model`, adds `usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens` to `tokens_in` and `usage.output_tokens` to `tokens_out`; any record's `cwd`, `sessionId`, `gitBranch`, `version` fill empty fields; `timestamp` updates `t_start` (min) and `t_end` (max); `type == "ai-title"` sets `title` from `aiTitle`; `type == "summary"` sets `title` from `summary` only when still empty); `LiveSession(BaseModel)` with `project_id, session_id, cwd, title, started: AwareDatetime | None, last_seen: AwareDatetime`; `ScanReport(BaseModel)` with `files_seen: int, files_read: int, signals_written: int, live: list[LiveSession]`; `transcript_roots(settings) -> list[tuple[str, Path]]` (`("claude", claude_home / "projects")` when set, plus `(name, backends_root / name / "projects")` for each directory under `backends_root`); `scan_transcripts(conn, settings, *, now: datetime) -> ScanReport`.
- Scan algorithm: for every `*.jsonl` directly inside each `<root>/<dir>/` whose mtime is newer than `now - scan_since_days`: state key `scan:<path>` holds `{"offset": int, "size": int, "mtime": float, "summary": dict}`; when size and mtime are unchanged the stored summary is reused (`files_read` not incremented); when the size shrank the offset resets to 0 and the summary to empty; otherwise the file is opened in binary mode, seeked to `offset`, and only bytes up to and including the last `\n` are parsed, with `offset` advanced to that point; the summary is stored back. A summary with a `cwd` that `project_for_path` maps gets `SignalIn(project_id, kind="session", t_start=summary.t_start or mtime, t_end=summary.t_end or mtime, dedupe_key=f"claude_session:{session_id}", payload={session_id, cwd, title, git_branch, model, prompts, tokens_in, tokens_out, transcript_path, backend, running})` where `running = (now - mtime) < live_window_s`; running mapped sessions form `live`. Summaries without `session_id` are skipped. All signals are written with one `upsert_signals` call at the end.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_paths.py
from pathlib import Path
from trajectree.config import Settings, load_settings
from trajectree.connectors.paths import encode_project_dir, to_local

def test_to_local_maps_longest_component_prefix(tmp_path):
    s = Settings(data_dir=tmp_path, path_map=[("/mnt/apps", "/projects"), ("/mnt/apps/big", "/big")])
    assert to_local(s, "/mnt/apps/x/y") == Path("/projects/x/y")
    assert to_local(s, "/mnt/apps/big/z") == Path("/big/z")
    assert to_local(s, "/mnt/appsx") == Path("/mnt/appsx")

def test_encode_project_dir():
    assert encode_project_dir("/mnt/Apps/Projects/Trajectree") == "-mnt-Apps-Projects-Trajectree"
    assert encode_project_dir("/home/u/my proj.v2") == "-home-u-my-proj-v2"

def test_load_settings_parses_phase2_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAJECTREE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRAJECTREE_PATH_MAP", "/mnt/apps=/projects;/data=/d")
    monkeypatch.setenv("TRAJECTREE_CLAUDE_HOME", "")
    monkeypatch.setenv("TRAJECTREE_INGEST_EVERY", "0")
    s = load_settings()
    assert s.path_map == [("/mnt/apps", "/projects"), ("/data", "/d")] and s.claude_home is None and s.ingest_interval_s == 0
```

```python
# tests/test_claude_sessions.py
import json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from trajectree.connectors.claude_sessions import SessionSummary, parse_lines, scan_transcripts
from trajectree.connectors.paths import encode_project_dir
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.projects import create_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.signals import list_signals

UTC = timezone.utc
CWD = "/home/u/proj"
SID = "11111111-1111-1111-1111-111111111111"

def rec_user(ts, text="hi", cwd=CWD, sid=SID, tool_result=False, meta=False):
    content = [{"type": "tool_result", "content": "x"}] if tool_result else text
    d = {"type": "user", "message": {"role": "user", "content": content}, "timestamp": ts, "cwd": cwd, "sessionId": sid, "gitBranch": "main", "version": "2.1.308"}
    if meta: d["isMeta"] = True
    return d

def rec_assistant(ts, model="claude-opus-5-5", cwd=CWD, sid=SID):
    return {"type": "assistant", "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7}},
            "timestamp": ts, "cwd": cwd, "sessionId": sid}

def write_transcript(root: Path, cwd: str, sid: str, records, newline_at_end=True) -> Path:
    d = root / "projects" / encode_project_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    body = "\n".join(json.dumps(r) for r in records)
    p.write_text(body + ("\n" if newline_at_end else ""))
    return p

def setup(ledger, human, settings, tmp_path):
    home = tmp_path / "claude"
    s = settings.model_copy(update={"claude_home": home, "backends_root": tmp_path / "backends"})
    p = create_project(ledger, human, ProjectIn(slug="proj", title="Proj", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=CWD))
    return home, s, p

def test_parse_lines_counts_prompts_tokens_and_title():
    acc = SessionSummary(transcript_path="x", backend="claude")
    lines = [json.dumps(r).encode() for r in [
        rec_user("2026-01-05T10:00:00.000Z"), rec_user("2026-01-05T10:01:00.000Z", tool_result=True),
        rec_user("2026-01-05T10:01:30.000Z", meta=True), rec_assistant("2026-01-05T10:02:00.000Z"),
        {"type": "ai-title", "aiTitle": "Fix the build", "sessionId": SID}, b"not json".decode()]]
    out = parse_lines(lines, acc)
    assert (out.prompts, out.assistant_msgs, out.tokens_in, out.tokens_out) == (1, 1, 115, 7)
    assert out.title == "Fix the build" and out.model == "claude-opus-5-5" and out.cwd == CWD and out.git_branch == "main"
    assert out.t_start == datetime(2026, 1, 5, 10, tzinfo=UTC) and out.t_end == datetime(2026, 1, 5, 10, 2, tzinfo=UTC)

def test_scan_maps_sessions_and_skips_unmapped(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z"), rec_assistant("2026-01-05T10:02:00.000Z"), rec_user("2026-01-05T10:03:00.000Z")])
    write_transcript(home, "/elsewhere", "2" * 36, [rec_user("2026-01-05T10:00:00.000Z", cwd="/elsewhere", sid="2" * 36)])
    now = datetime.now(UTC)
    report = scan_transcripts(ledger.conn, s, now=now)
    assert report.files_seen == 2 and report.files_read == 2 and report.signals_written == 1
    sig = list_signals(ledger.conn, project_ids=[p.id])[0]
    assert sig.kind == "session" and sig.dedupe_key == f"claude_session:{SID}"
    assert sig.payload["prompts"] == 2 and sig.payload["backend"] == "claude" and sig.payload["running"] is True
    assert report.live[0].session_id == SID and report.live[0].project_id == p.id

def test_scan_is_incremental(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    path = write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z")])
    now = datetime.now(UTC)
    scan_transcripts(ledger.conn, s, now=now)
    assert scan_transcripts(ledger.conn, s, now=now).files_read == 0
    with path.open("a") as f:
        f.write(json.dumps(rec_user("2026-01-05T10:05:00.000Z")) + "\n")
    os.utime(path, None)
    r = scan_transcripts(ledger.conn, s, now=now)
    assert r.files_read == 1 and list_signals(ledger.conn, project_ids=[p.id])[0].payload["prompts"] == 2

def test_scanner_resumes_after_partial_line(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    path = write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z")])
    partial = json.dumps(rec_user("2026-01-05T10:05:00.000Z"))
    with path.open("a") as f:
        f.write(partial[:40])
    now = datetime.now(UTC)
    scan_transcripts(ledger.conn, s, now=now)
    assert list_signals(ledger.conn, project_ids=[p.id])[0].payload["prompts"] == 1
    with path.open("a") as f:
        f.write(partial[40:] + "\n")
    os.utime(path, None)
    scan_transcripts(ledger.conn, s, now=now)
    assert list_signals(ledger.conn, project_ids=[p.id])[0].payload["prompts"] == 2

def test_malformed_lines_are_skipped(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    path = write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z")])
    with path.open("a") as f:
        f.write("{broken\n[1,2]\n" + json.dumps(rec_user("2026-01-05T10:06:00.000Z")) + "\n")
    scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    assert list_signals(ledger.conn, project_ids=[p.id])[0].payload["prompts"] == 2

def test_live_depends_on_mtime_and_shrunk_file_rescans(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    path = write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z"), rec_user("2026-01-05T10:01:00.000Z")])
    old = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(path, (old, old))
    r = scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    assert r.live == [] and list_signals(ledger.conn, project_ids=[p.id])[0].payload["running"] is False
    write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z")])   # rewritten shorter
    scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    assert list_signals(ledger.conn, project_ids=[p.id])[0].payload["prompts"] == 1

def test_backend_profiles_are_scanned(ledger, human, settings, tmp_path):
    home, s, p = setup(ledger, human, settings, tmp_path)
    write_transcript(tmp_path / "backends" / "deepseek", CWD, "3" * 36, [rec_user("2026-01-05T10:00:00.000Z", sid="3" * 36)])
    scan_transcripts(ledger.conn, s, now=datetime.now(UTC))
    assert list_signals(ledger.conn, project_ids=[p.id])[0].payload["backend"] == "deepseek"

def test_scan_without_roots_is_noop(ledger, settings):
    r = scan_transcripts(ledger.conn, settings, now=datetime.now(UTC))
    assert r.files_seen == 0 and r.live == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_paths.py tests/test_claude_sessions.py -q`
Expected: ImportError / unknown Settings fields.

- [ ] **Step 3: Extend `Settings` and `load_settings`**, update the `settings` fixture, implement `paths.py` and `claude_sessions.py` per Interfaces.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree tests
git commit -m "feat(connectors): incremental Claude transcript scanner and host path map"
```

---

### Task 11: Local git connector

**Files:**
- Create: `src/trajectree/connectors/local_git.py`
- Test: `tests/test_local_git.py`

**Interfaces:**
- Consumes: `list_resources(kinds=["git_local"])`, `to_local`, `upsert_signals`, `get_state`/`set_state`.
- Produces: `GitReport(BaseModel)` with `repos: int, signals_written: int, errors: list[str]`; `poll_git(conn, settings, *, now: datetime, since_days: int = 90) -> GitReport`. Per `git_local` resource: `local = to_local(settings, resource.locator)`; when `local / ".git"` does not exist, append `f"{resource.locator}: not a git repository"` to `errors` and continue; `since` is the start of the UTC day of `last_poll - 1 day` when state `git:<resource_id>` holds `{"last_poll": iso}`, else `now - since_days`; run `["git", "-c", "safe.directory=*", "-C", str(local), "log", "--all", f"--since={since.isoformat()}", "--date=iso-strict", "--format=%H%x1f%aI%x1f%an%x1f%s"]` with `subprocess.run(capture_output=True, text=True, timeout=60, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})`; a non-zero exit appends `f"{resource.locator}: {stderr.strip()}"` to `errors`; commits are grouped by the UTC date of the author time into `SignalIn(project_id=resource.project_id, resource_id=resource.id, kind="commits", t_start=<first commit of the day>, t_end=<last commit of the day>, dedupe_key=f"git:{resource.id}:{day}", payload={"count": n, "subjects": first 20 subjects, "authors": sorted unique, "shas": first 50})`; after a successful log the state stores `{"last_poll": to_db(now)}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_local_git.py
import os, subprocess
from datetime import datetime, timezone
from trajectree.connectors.local_git import poll_git
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.projects import create_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.signals import list_signals

UTC = timezone.utc

def commit(repo, name, when):
    (repo / name).write_text(name)
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x"}
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"add {name}"], check=True, capture_output=True, env=env)

def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    commit(repo, "a", "2026-01-05T10:00:00+00:00")
    commit(repo, "b", "2026-01-06T09:00:00+00:00")
    return repo

def test_poll_groups_commits_per_day(ledger, human, settings, tmp_path):
    repo = make_repo(tmp_path)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator=str(repo)))
    r = poll_git(ledger.conn, settings, now=datetime(2026, 1, 7, tzinfo=UTC), since_days=30)
    assert r.repos == 1 and r.signals_written == 2 and r.errors == []
    sigs = list_signals(ledger.conn, project_ids=[p.id], kinds=["commits"])
    assert [s.payload["count"] for s in sigs] == [1, 1] and sigs[0].payload["subjects"] == ["add a"]
    assert sigs[0].dedupe_key.startswith("git:") and sigs[0].dedupe_key.endswith(":2026-01-05")

def test_second_poll_recomputes_whole_day(ledger, human, settings, tmp_path):
    repo = make_repo(tmp_path)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator=str(repo)))
    poll_git(ledger.conn, settings, now=datetime(2026, 1, 6, 12, tzinfo=UTC), since_days=30)
    commit(repo, "c", "2026-01-06T11:00:00+00:00")
    poll_git(ledger.conn, settings, now=datetime(2026, 1, 6, 13, tzinfo=UTC), since_days=30)
    sigs = list_signals(ledger.conn, project_ids=[p.id], kinds=["commits"])
    assert [s.payload["count"] for s in sigs] == [1, 2]

def test_path_map_and_non_repo_error(ledger, human, settings, tmp_path):
    repo = make_repo(tmp_path)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator="/host/repo"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="git_local", locator=str(tmp_path / "plain")))
    (tmp_path / "plain").mkdir()
    s = settings.model_copy(update={"path_map": [("/host/repo", str(repo))]})
    r = poll_git(ledger.conn, s, now=datetime(2026, 1, 7, tzinfo=UTC), since_days=30)
    assert r.repos == 2 and r.signals_written == 2 and r.errors == [f"{tmp_path / 'plain'}: not a git repository"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_local_git.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement `local_git.py`** per Interfaces.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_local_git.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/connectors/local_git.py tests/test_local_git.py
git commit -m "feat(connectors): local git commit signals grouped per day"
```

---

### Task 12: Ingest job, staleness, exports, scheduler, timeline strip and signals routes

**Files:**
- Create: `src/trajectree/ingest/__init__.py`, `src/trajectree/ingest/job.py`, `src/trajectree/ingest/exports.py`, `src/trajectree/ingest/scheduler.py`, `src/trajectree/api/routes_signals.py`
- Modify: `src/trajectree/api/routes_timeline.py`, `src/trajectree/api/app.py` (lifespan starts the loop when `settings.ingest_interval_s > 0`), `src/trajectree/cli/main.py` (`ingest` subcommand)
- Test: `tests/test_ingest.py`, `tests/test_api_signals_timeline.py`, `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `scan_transcripts`, `poll_git`, `stale_projects`, `strip_bins`, `list_signals`, `get_state`/`set_state`, `LiveSession`, `StripBin`.
- Produces in `job.py`: `IngestReport(BaseModel)` with `started: AwareDatetime, duration_ms: int, scan: ScanReport | None, git: GitReport, stale: list[str], exports_written: int`; `run_ingest(ledger, settings, *, now: datetime | None = None) -> IngestReport` (holds `ledger.locked()` for each database phase, never across the git subprocess calls; scanner runs only when `transcript_roots(settings)` is non-empty; stores `state["live_sessions"]` as a list of `LiveSession` dicts, `state["stale_projects"]` as the id list and `state["ingest:last"]` as the report dict).
- Produces in `exports.py`: `render_summary_md(conn, project: Project) -> str` with exactly `# <title>`, a line `status: <status> · slug: <slug> · summary updated: <YYYY-MM-DD or never>`, `## Summary` (summary_md or `_No summary yet._`), `## Open todos` (same line format as the context pack), `## Recent events` (last 10, context-pack line format), `## Resources` (`- <kind> <locator>`); `write_exports(conn, settings) -> int` writing `settings.exports_dir / "projects" / <slug> / "SUMMARY.md"` for every project and returning how many files changed (unchanged content is not rewritten).
- Produces in `scheduler.py`: `async def ingest_loop(ledger, settings, *, interval_s: float, stop: asyncio.Event, run=run_ingest) -> None` (runs `await asyncio.to_thread(run, ledger, settings)` immediately, then after every `interval_s` until `stop` is set; exceptions are logged with `logging.getLogger("trajectree.ingest").exception` and the loop continues).
- Timeline route: `TimelineResponse` gains `strip: list[StripBin]`, `live: list[LiveSession]` (from `state["live_sessions"]`, filtered to the requested projects), `stale: list[str]` (from `state["stale_projects"]`, filtered likewise).
- Routes in `routes_signals.py`: `GET /api/signals?projects=&from=&to=&kinds=&limit=` -> `list[Signal]` (`limit` `Query(200, ge=1, le=2000)`); `POST /api/ingest/run` -> `IngestReport`; `GET /api/ingest/status` -> `IngestReport | None`.
- CLI: `trajectree ingest` posts to `/api/ingest/run` and prints `ingest: <files_read> transcripts read, <signals> signals, <n> stale, <ms> ms`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest.py
import asyncio, json
from datetime import datetime, timezone
import pytest
from tests.test_claude_sessions import CWD, SID, rec_assistant, rec_user, write_transcript
from trajectree.ingest.exports import render_summary_md, write_exports
from trajectree.ingest.job import run_ingest
from trajectree.ingest.scheduler import ingest_loop
from trajectree.models import ProjectIn, ProjectPatch, ResourceIn
from trajectree.repo.projects import create_project, get_project, update_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.state import get_state

UTC = timezone.utc

def test_run_ingest_scans_exports_and_flags_stale(ledger, human, settings, tmp_path):
    home = tmp_path / "claude"
    s = settings.model_copy(update={"claude_home": home})
    p = create_project(ledger, human, ProjectIn(slug="proj", title="Proj", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=CWD))
    write_transcript(home, CWD, SID, [rec_user("2026-01-05T10:00:00.000Z"), rec_assistant("2026-01-05T10:02:00.000Z")])
    report = run_ingest(ledger, s, now=datetime.now(UTC))
    assert report.scan.signals_written == 1 and report.git.repos == 0
    assert report.stale == [p.id] and report.exports_written == 1
    summary = (s.exports_dir / "projects" / "proj" / "SUMMARY.md").read_text()
    assert summary.startswith("# Proj\n") and "_No summary yet._" in summary
    assert get_state(ledger.conn, "stale_projects") == [p.id] and get_state(ledger.conn, "live_sessions")[0]["session_id"] == SID
    assert get_state(ledger.conn, "ingest:last")["exports_written"] == 1

def test_exports_rewrite_only_on_change(ledger, human, settings):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    assert write_exports(ledger.conn, settings) == 1
    assert write_exports(ledger.conn, settings) == 0
    update_project(ledger, human, p.id, ProjectPatch(summary_md="Now summarised"))
    assert write_exports(ledger.conn, settings) == 1
    md = render_summary_md(ledger.conn, get_project(ledger.conn, p.id))
    assert "## Summary\nNow summarised" in md and "status: active · slug: a · summary updated: " in md

def test_run_ingest_without_roots_still_exports(ledger, human, settings):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    r = run_ingest(ledger, settings)
    assert r.scan is None and r.exports_written == 1 and r.duration_ms >= 0

@pytest.mark.anyio
async def test_ingest_loop_runs_until_stopped(ledger, settings):
    calls = []
    def fake(ledger_, settings_):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")   # must not kill the loop
    stop = asyncio.Event()
    task = asyncio.create_task(ingest_loop(ledger, settings, interval_s=0.02, stop=stop, run=fake))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert len(calls) >= 3
```

```python
# tests/test_api_signals_timeline.py
from datetime import datetime, timezone
from tests.test_api_entities import mk_project
from trajectree.models import SignalIn
from trajectree.repo.signals import upsert_signals
from trajectree.repo.state import set_state

UTC = timezone.utc

def test_timeline_includes_strip_live_and_stale(client, app):
    p = mk_project(client)
    conn = app.state.ledger.conn
    upsert_signals(conn, [SignalIn(project_id=p["id"], kind="session", t_start=datetime(2026, 1, 5, 10, tzinfo=UTC), dedupe_key="s1"),
                          SignalIn(project_id=p["id"], kind="commits", t_start=datetime(2026, 1, 5, 11, tzinfo=UTC), dedupe_key="g1", payload={"count": 3})])
    set_state(conn, "stale_projects", [p["id"], "other"])
    set_state(conn, "live_sessions", [{"project_id": p["id"], "session_id": "x", "cwd": "/c", "title": None, "started": None, "last_seen": "2026-01-05T12:00:00Z"},
                                      {"project_id": "other", "session_id": "y", "cwd": "/d", "title": None, "started": None, "last_seen": "2026-01-05T12:00:00Z"}])
    tl = client.get("/api/timeline", params={"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z"}).json()
    assert sorted((b["kind"], b["count"]) for b in tl["strip"]) == [("commits", 1), ("session", 1)]
    assert tl["stale"] == [p["id"]] and [l["session_id"] for l in tl["live"]] == ["x"]

def test_signals_route_filters_and_limits(client, app):
    p = mk_project(client)
    upsert_signals(app.state.ledger.conn, [SignalIn(project_id=p["id"], kind="session", t_start=datetime(2026, 1, 5, tzinfo=UTC), dedupe_key="s1"),
                                           SignalIn(project_id=p["id"], kind="commits", t_start=datetime(2026, 1, 6, tzinfo=UTC), dedupe_key="g1")])
    rows = client.get("/api/signals", params={"projects": p["id"], "kinds": "commits"}).json()
    assert [r["dedupe_key"] for r in rows] == ["g1"]
    assert client.get("/api/signals", params={"limit": 0}).status_code == 422

def test_ingest_run_and_status(client):
    assert client.get("/api/ingest/status").json() is None
    r = client.post("/api/ingest/run")
    assert r.status_code == 200 and "duration_ms" in r.json()
    assert client.get("/api/ingest/status").json()["duration_ms"] == r.json()["duration_ms"]
```

```python
# append to tests/test_cli.py
def test_cli_ingest(app, anon, capsys):
    assert main(["ingest"], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.startswith("ingest: ")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ingest.py tests/test_api_signals_timeline.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement `ingest/`, the routes, the timeline fields, the lifespan hook and the CLI subcommand** per Interfaces.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree tests
git commit -m "feat(ingest): LLM-free ingest job with staleness, exports, scheduler and strip data"
```

---

### Task 13: `trajectree init-project` and the guidance template

**Files:**
- Create: `src/trajectree/guidance/__init__.py`, `src/trajectree/guidance/rules.md`, `src/trajectree/cli/init_project.py`
- Modify: `src/trajectree/cli/main.py` (`init-project` subcommand; `main(..., runner=None)`)
- Test: `tests/test_init_project.py`

**Interfaces:**
- Consumes: `_Api` from `cli/main.py` (`get`, `post`, `project_id`), `SLUG_PATTERN`.
- Produces in `guidance/__init__.py`: `render_rules(slug: str, title: str) -> str` (reads `rules.md` via `importlib.resources`, substitutes `{slug}` and `{title}`; the rendered text starts with `<!-- managed by trajectree init-project; edits are overwritten -->`).
- `rules.md` content, in this order and no longer than 60 lines: the marker line; `# Trajectree` heading; one sentence saying this folder is Trajectree project `{slug}` (`{title}`) and that the `trajectree` MCP server records project history; **When to record** (a decision taken, a milestone reached, a blocker found, a follow-up owed, a relation to another project noticed); **How** (`record_event` with `kind` decision, milestone, blocker, meeting, idea or note and a one-line `intent`; `add_todo` for follow-ups; `link` with `rel` such as `depends_on`, `derived_from`, `caused`, `relates_to`; omit `project`, the server defaults it from this folder); **Style** (title at most 80 characters, body at most 5 lines, no secrets, no raw logs or tool output, one entry per outcome, `search` before recording something that may already exist); **Review** (every write lands pending; the owner accepts or rejects in the Trajectree inbox); **Context** (call `get_context_pack` when the owner asks about other projects or how this work relates to them; `get_project` for this project's summary and open todos).
- Produces in `init_project.py`: `slug_from_path(path: Path) -> str` (lowercased basename, runs of characters outside `[a-z0-9]` become `-`, trimmed of `-`, truncated to 64); `init_project(api, path: Path, *, slug: str | None, title: str | None, kind: str, runner: Callable[[list[str]], Any], out: TextIO, no_exclude: bool = False) -> int` performing: resolve `path`; `slug = slug or slug_from_path(path)`, return 2 with `invalid slug: ...` on stderr when it fails `SLUG_PATTERN`; find the project by slug via `GET /api/projects` or `POST /api/projects` with `title or path.name` and `kind`; `POST /api/resources` for `folder` (title `path.name`) and, when `path / ".git"` exists, for `git_local`; write `path / ".claude" / "rules" / "trajectree.md"`; when `path / ".git"` exists and not `no_exclude`, append `.claude/rules/trajectree.md` to `path / ".git" / "info" / "exclude"` unless already present (creating `info/` if needed); run `runner(["claude", "mcp", "get", "trajectree"])`, and when its `returncode != 0` run `runner(["claude", "mcp", "add", "--scope", "user", "trajectree", "--", "trajectree", "mcp"])`; when `runner` raises `FileNotFoundError`, print `mcp: claude not found; run: claude mcp add --scope user trajectree -- trajectree mcp`. Prints exactly four lines: `project: <slug> (<id>)`, `resources: folder` or `resources: folder, git_local`, `rules: <rules path>`, `mcp: configured` / `mcp: already configured` / the not-found line. Returns 0.
- `_parser`: `init-project PATH [--slug SLUG] [--title TITLE] [--kind {research,tooling,routine,admin}] [--no-exclude]` with `--kind` default `research`. `main(argv, *, client=None, env=None, runner=None)`; `runner` defaults to `subprocess.run` with `capture_output=True`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_init_project.py
from types import SimpleNamespace
from trajectree.cli.init_project import slug_from_path
from trajectree.cli.main import main

class FakeClaude:
    def __init__(self, registered=False):
        self.registered = registered
        self.calls = []
    def __call__(self, args):
        self.calls.append(args)
        if args[:3] == ["claude", "mcp", "get"]:
            return SimpleNamespace(returncode=0 if self.registered else 1, stdout="", stderr="")
        self.registered = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

def env_for(app):
    return {"TRAJECTREE_TOKEN": app.state.token, "TRAJECTREE_URL": "http://testserver"}

def make_repo(tmp_path):
    repo = tmp_path / "My Proj"
    (repo / ".git" / "info").mkdir(parents=True)
    return repo

def test_slug_from_path(tmp_path):
    assert slug_from_path(tmp_path / "My Proj_v2") == "my-proj-v2"
    assert slug_from_path(tmp_path / "---x---") == "x"

def test_init_project_creates_project_resources_rules_and_mcp(tmp_path, client, app, capsys):
    repo = make_repo(tmp_path)
    fake = FakeClaude()
    assert main(["init-project", str(repo)], client=client, env=env_for(app), runner=fake) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("project: my-proj (") and out[1] == "resources: folder, git_local" and out[3] == "mcp: configured"
    assert client.get("/api/projects").json()[0]["slug"] == "my-proj"
    kinds = {r["kind"]: r["locator"] for r in client.get("/api/resources").json()}
    assert kinds == {"folder": str(repo.resolve()), "git_local": str(repo.resolve())}
    rules = (repo / ".claude" / "rules" / "trajectree.md").read_text()
    assert rules.startswith("<!-- managed by trajectree init-project; edits are overwritten -->") and "my-proj" in rules and "record_event" in rules
    assert (repo / ".git" / "info" / "exclude").read_text().count(".claude/rules/trajectree.md") == 1
    assert fake.calls == [["claude", "mcp", "get", "trajectree"],
                          ["claude", "mcp", "add", "--scope", "user", "trajectree", "--", "trajectree", "mcp"]]

def test_init_project_twice_is_idempotent(tmp_path, client, app, capsys):
    repo = make_repo(tmp_path)
    fake = FakeClaude()
    main(["init-project", str(repo)], client=client, env=env_for(app), runner=fake)
    (repo / ".claude" / "rules" / "trajectree.md").write_text("edited")
    assert main(["init-project", str(repo)], client=client, env=env_for(app), runner=fake) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "mcp: already configured"
    assert len(client.get("/api/projects").json()) == 1 and len(client.get("/api/resources").json()) == 2
    assert (repo / ".git" / "info" / "exclude").read_text().count(".claude/rules/trajectree.md") == 1
    assert (repo / ".claude" / "rules" / "trajectree.md").read_text() != "edited"
    assert fake.calls[-1] == ["claude", "mcp", "get", "trajectree"]

def test_init_project_without_git_or_claude(tmp_path, client, app, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    def missing(args):
        raise FileNotFoundError("claude")
    assert main(["init-project", str(plain), "--slug", "plain-one", "--title", "Plain", "--kind", "tooling"], client=client, env=env_for(app), runner=missing) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "resources: folder" and out[3].startswith("mcp: claude not found; run: claude mcp add --scope user trajectree")
    p = client.get("/api/projects").json()[0]
    assert (p["slug"], p["title"], p["kind"]) == ("plain-one", "Plain", "tooling")
    assert not (plain / ".git").exists()

def test_init_project_rejects_bad_slug(tmp_path, client, app, capsys):
    assert main(["init-project", str(tmp_path), "--slug", "Bad Slug"], client=client, env=env_for(app), runner=FakeClaude()) == 2
    assert "invalid slug" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_init_project.py -q`
Expected: ImportError / argparse error.

- [ ] **Step 3: Write `rules.md`, `guidance/__init__.py`, `init_project.py` and the subcommand** per Interfaces.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree tests
git commit -m "feat(cli): init-project registers folders, installs guidance and configures MCP"
```

---

### Task 14: Web: pending map, ledger filters and chain, Inbox tab

**Files:**
- Modify: `web/src/api/types.ts`, `web/src/api/client.ts`, `web/src/App.tsx`, `web/src/views/LedgerView.tsx`, `web/src/components/DetailsPanel.tsx`, `web/src/views/views.css`, `web/src/components/DetailsPanel.test.tsx`, `web/src/views/LedgerView.test.tsx`, `web/src/App.test.tsx`, `web/src/timeline/Timeline.test.tsx` (fixture shape)
- Create: `web/src/views/InboxView.tsx`, `web/src/views/InboxView.test.tsx`

**Interfaces:**
- Consumes: Task 2 response shapes, Task 12 routes.
- Produces in `types.ts`: `TimelineResponse.pending: Record<string, Record<string, string>>`, `strip: StripBin[]`, `live: LiveSession[]`, `stale: string[]`; `StripBin { project_id: string; day: string; kind: SignalKind; count: number }`; `SignalKind = "commits" | "session" | "files_changed" | "job_state" | "message"`; `LiveSession { project_id; session_id; cwd; title: string | null; started: string | null; last_seen: string }`; `Signal { id; project_id; resource_id: string | null; kind: SignalKind; t_start; t_end: string | null; payload: Record<string, unknown>; dedupe_key; updated_at }`; `Resource`; `IngestReport { started: string; duration_ms: number; stale: string[]; exports_written: number }`.
- Produces in `client.ts`: `txns(limit = 50, before?: string, filter?: { review?: Review; actor_kind?: string })`; `todos(states?: string[])` -> `GET /api/todos?states=`; `signals(projects: string[], from: Date, to: Date)`; `runIngest()` -> `POST /api/ingest/run`.
- `DetailsPanel`: `const txnId = data.pending.event?.[event.id] ?? event.created_txn`; the pending badge and the Accept/Reject buttons use that id; `isPending = event.id in (data.pending.event ?? {})`.
- `LedgerView`: a `<select aria-label="Show">` with options `All`, `Pending`, `Humans`, `Agents & sessions` mapping to `{}`, `{review: "pending"}`, `{actor_kind: "human"}`, `{actor_kind: "agent,session"}`; the query key includes the filter; each row shows `<span className="chain">reverts <last 6 chars of reverts_txn></span>` and/or `<span className="chain">reverted by <last 6 chars></span>` when set.
- `InboxView`: query `["txns", "pending"]` via `api.txns(200, undefined, { review: "pending" })`; heading `Pending writes (N)`; button `Accept all` (disabled when N is 0) that calls `api.review(id, "accepted")` for each pending id sequentially then invalidates `timeline`, `txns`, `txn`; one row per txn: local time, `actor_kind/actor_ref`, model when present, intent, a change summary from `api.txn(id)` loaded on expand (reuse the `Diff` component by exporting it from `LedgerView.tsx`), `Accept` and `Reject` buttons wired to `useTxnActions`; empty state paragraph `Nothing to review`; second section `Open todos` listing `api.todos(["open", "doing"])` sorted by due ascending with undated last, each `- <title> · <project title or inbox> · <due date or no due>`.
- `App`: third tab `Inbox`; its label shows the pending count as ` (N)` when N > 0, from the same pending query.

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/views/InboxView.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { InboxView } from "./InboxView";
import type { Txn } from "../api/types";

const txn = (id: string): Txn => ({ id, ts: "2026-01-05T10:00:00Z", actor_kind: "session", actor_ref: "owner", model: null, backend: null, tool: "mcp", tool_version: null, session_id: null, run_id: null, proposed_by_run: null, intent: `intent ${id}`, review: "pending", review_ts: null, reverts_txn: null, reverted_by: null });

const api = vi.hoisted(() => ({
  txns: vi.fn(), txn: vi.fn(), todos: vi.fn(), review: vi.fn(), revert: vi.fn(),
}));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));

const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);

describe("InboxView", () => {
  beforeEach(() => { vi.clearAllMocks(); api.todos.mockResolvedValue([]); api.review.mockResolvedValue(undefined); });
  it("shows the empty state", async () => {
    api.txns.mockResolvedValue([]);
    wrap(<InboxView />);
    expect(await screen.findByText("Nothing to review")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Accept all" })).toBeDisabled();
  });
  it("lists pending writes and accepts them all", async () => {
    api.txns.mockResolvedValue([txn("t1"), txn("t2")]);
    wrap(<InboxView />);
    expect(await screen.findByText("intent t1")).toBeTruthy();
    expect(api.txns).toHaveBeenCalledWith(200, undefined, { review: "pending" });
    fireEvent.click(screen.getByRole("button", { name: "Accept all" }));
    await waitFor(() => expect(api.review).toHaveBeenCalledTimes(2));
    expect(api.review).toHaveBeenCalledWith("t1", "accepted");
  });
  it("lists open todos by due date with undated last", async () => {
    api.txns.mockResolvedValue([]);
    api.todos.mockResolvedValue([
      { id: "a", project_id: null, title: "Later", body_md: "", due: "2026-02-01T00:00:00Z", state: "open", priority: 0, props: {}, created_at: null, updated_at: null },
      { id: "b", project_id: null, title: "Undated", body_md: "", due: null, state: "open", priority: 0, props: {}, created_at: null, updated_at: null },
      { id: "c", project_id: null, title: "Soon", body_md: "", due: "2026-01-10T00:00:00Z", state: "doing", priority: 0, props: {}, created_at: null, updated_at: null },
    ]);
    wrap(<InboxView />);
    const items = await screen.findAllByRole("listitem");
    expect(items.map(li => li.textContent?.split(" · ")[0])).toEqual(["Soon", "Later", "Undated"]);
  });
});
```

```tsx
// web/src/components/DetailsPanel.test.tsx: add
it("reviews the pending transaction, not the creating one", async () => {
  // data.pending.event = { e1: "txn-pending" }, event e1 has created_txn "txn-create"; mock fetch for /api/ledger/txns/txn-pending
  // click Accept -> onReview called with ("txn-pending", "accepted")
});
```

```tsx
// web/src/views/LedgerView.test.tsx: add
it("filters by review state and shows the revert chain", async () => {
  // select "Pending" -> fetch URL contains review=pending; a txn with reverts_txn "abcdef123456" renders text "reverts 123456"
});
```

```tsx
// web/src/App.test.tsx: add
it("has an Inbox tab", () => { /* role tab name /Inbox/ exists */ });
```

Update `Timeline.test.tsx` fixtures to `pending: { event: { e2: "t2" }, todo: {}, edge: {} }, strip: [], live: [], stale: []`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd web && npm test`
Expected: type errors on `pending` and missing `InboxView`.

- [ ] **Step 3: Implement the type, client, DetailsPanel, LedgerView, InboxView and App changes** per Interfaces; add `.chain{color:var(--muted);font-size:12px}` and inbox styles to `views.css`.

- [ ] **Step 4: Run the tests and the build**

Run: `cd web && npm test && npm run build`
Expected: all vitest files pass; `tsc` clean.

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "feat(web): inbox tab, pending txn review, ledger filters and revert chain"
```

---

### Task 15: Web: activity strip, lane chips, live marker, bin details

**Files:**
- Modify: `web/src/timeline/layout.ts`, `web/src/timeline/layout.test.ts`, `web/src/timeline/render.ts`, `web/src/timeline/Timeline.tsx`, `web/src/timeline/Timeline.test.tsx`, `web/src/timeline/timeline.css`, `web/src/components/DetailsPanel.tsx`, `web/src/components/DetailsPanel.test.tsx`, `web/src/views/TimelineView.tsx`

**Interfaces:**
- Consumes: `StripBin`, `LiveSession`, `Signal`, `api.signals`, existing `binKey`.
- Produces in `layout.ts`: `StripCell { key: string; start: number; end: number; sessions: number; commits: number; total: number; level: 1 | 2 | 3 | 4 | 5 }`; `binStrip(bins: StripBin[], mode: "day" | "week" | "month"): StripCell[]` (UTC day, ISO week starting Monday, or calendar month; `start`/`end` are the bucket bounds in epoch ms; `sessions` counts `session` bins, `commits` counts `commits` bins, `total` is the sum of all counts; `level = Math.min(5, Math.floor(Math.log2(total)) + 1)` so 1 → 1, 2–3 → 2, 4–7 → 3, 8–15 → 4, 16+ → 5; sorted by `start`).
- `Timeline.tsx`: `export type Selection = { type: "event" | "todo"; id: string } | { type: "bin"; projectId: string; start: number; end: number }`.
- `render.ts`: `STRIP_SLOT` now holds a strip row at `laneY + PAD + rows * ROW_H + 3`, height `STRIP_SLOT - 8`; the mode comes from `binKey(0, dayWidthPx)` where `dayWidthPx = xn(86_400_000) - xn(0)`; per lane `rect.strip-cell.l-<level>` elements keyed by `projectId + key`, `x = xn(start)`, `width = max(2, xn(end) - xn(start) - 1)`, culled like blocks; hovering a cell shows the tooltip `<n> sessions · <m> commits` with the bucket's start date as title; clicking calls `onSelect({ type: "bin", projectId, start, end })` and stops propagation; the selected cell gets class `sel`; lanes with a `live` session draw `circle.live` (r 4) at `nowX` on the strip row's centre; the gutter label gets `g.chip.running` (text `● running`) when the lane has a live session and `g.chip.stale` (text `stale`) when the lane id is in `data.stale`, each a `rect` plus `text`, placed on a third label line at `y = 28`; `fill` of chip text is `var(--ink2)`.
- `timeline.css`: `.strip-cell.l-1{fill:var(--strip-1)}` … `.l-5{fill:var(--strip-5)}`, `.strip-cell.sel{stroke:var(--sel);stroke-width:1.5}`, `.live{fill:var(--now)}`, `.chip rect{rx:7px;fill:var(--band)} .chip.stale rect{fill:color-mix(in srgb,var(--warn) 25%,transparent)} .chip text{fill:var(--ink2);font-size:10px}`.
- `DetailsPanel`: for a `bin` selection, `useQuery(["signals", projectId, start, end], () => api.signals([projectId], new Date(start), new Date(end)))`; heading `Activity`, the bucket's date range, then `Sessions` list (title or the first 8 characters of the session id, `· <prompts> prompts · <model>`, a `Copy resume command` button calling `navigator.clipboard.writeText(\`cd ${cwd} && claude --resume ${session_id}\`)` and showing `Copied` for 1.5 s) and `Commits` list (`<count> commits` then the subjects); empty state `No activity in this bin.`

- [ ] **Step 1: Write the failing tests**

```ts
// web/src/timeline/layout.test.ts: add
import { binStrip } from "./layout";
describe("binStrip", () => {
  const bins = [
    { project_id: "p", day: "2026-01-05", kind: "session" as const, count: 1 },
    { project_id: "p", day: "2026-01-06", kind: "session" as const, count: 2 },
    { project_id: "p", day: "2026-01-06", kind: "commits" as const, count: 5 },
    { project_id: "p", day: "2026-01-12", kind: "commits" as const, count: 1 },
  ];
  it("keeps days apart in day mode and levels by log2", () => {
    const cells = binStrip(bins, "day");
    expect(cells.map(c => [c.key, c.sessions, c.commits, c.level])).toEqual([["2026-01-05", 1, 0, 1], ["2026-01-06", 2, 5, 3], ["2026-01-12", 0, 1, 1]]);
    expect(cells[0].end - cells[0].start).toBe(86_400_000);
  });
  it("groups ISO weeks starting Monday", () => {
    const cells = binStrip(bins, "week");
    expect(cells.map(c => [c.key, c.total])).toEqual([["2026-W02", 8], ["2026-W03", 1]]);
    expect(new Date(cells[0].start).toISOString()).toBe("2026-01-05T00:00:00.000Z");
  });
  it("groups months and caps the level at 5", () => {
    const cells = binStrip([...bins, { project_id: "p", day: "2026-01-20", kind: "session" as const, count: 40 }], "month");
    expect(cells).toHaveLength(1);
    expect(cells[0].key).toBe("2026-01");
    expect(cells[0].level).toBe(5);
  });
});
```

```tsx
// web/src/timeline/Timeline.test.tsx: add (extend the fixture with strip/live/stale)
it("renders strip cells, a live marker and lane chips; clicking a cell selects the bin", () => {
  const onSelect = vi.fn();
  const d = { ...data, strip: [{ project_id: "p1", day: "2026-01-05", kind: "session" as const, count: 3 }], stale: ["p2"],
    live: [{ project_id: "p1", session_id: "s", cwd: "/c", title: null, started: null, last_seen: "2026-01-10T00:00:00Z" }] };
  const { container } = render(<Timeline data={d} now={new Date("2026-01-10T00:00:00Z")} range={[new Date("2026-01-01T00:00:00Z"), new Date("2026-02-01T00:00:00Z")]} selected={null} onSelect={onSelect} />);
  expect(container.querySelectorAll("rect.strip-cell").length).toBe(1);
  expect(container.querySelector("rect.strip-cell")!.classList.contains("l-2")).toBe(true);
  expect(container.querySelectorAll("circle.live").length).toBe(1);
  expect(container.querySelector("g.chip.running text")!.textContent).toBe("● running");
  expect(container.querySelector("g.chip.stale text")!.textContent).toBe("stale");
  fireEvent.click(container.querySelector("rect.strip-cell")!);
  expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ type: "bin", projectId: "p1" }));
});
```

```tsx
// web/src/components/DetailsPanel.test.tsx: add
it("lists a bin's sessions and copies the resume command", async () => {
  // mock fetch GET /api/signals -> one session signal payload {session_id:"abcd1234-...", cwd:"/home/u/proj", title:"Fix build", prompts: 3, model:"claude-opus-5-5"}
  // selected = {type:"bin", projectId:"p1", start, end}; navigator.clipboard.writeText mocked
  // expect text "Fix build" and "3 prompts"; click "Copy resume command" -> writeText called with "cd /home/u/proj && claude --resume abcd1234-..."
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd web && npm test`
Expected: `binStrip` missing, DOM queries empty.

- [ ] **Step 3: Implement `binStrip`, the render additions, the Selection union, the panel's bin branch and the CSS** per Interfaces.

- [ ] **Step 4: Run the tests and the build**

Run: `cd web && npm test && npm run build`
Expected: all pass; build clean.

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "feat(web): activity strip, live marker, lane chips and bin details"
```

---

### Task 16: Deployment, documentation and end-to-end smoke

**Files:**
- Modify: `Dockerfile`, `compose.yaml`, `README.md`, `web/playwright.config.ts`, `web/e2e/smoke.spec.ts`

**Interfaces:**
- `Dockerfile`: install `git` in the runtime stage (`apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*`) before `USER trajectree`.
- `compose.yaml` adds volumes `${HOME}/.claude:/host/claude:ro`, `${HOME}/.config/claude-backends:/host/claude-backends:ro`, `${TRAJECTREE_PROJECTS_ROOT:?set TRAJECTREE_PROJECTS_ROOT to the folder that holds your project folders}:/projects:ro` and environment `TRAJECTREE_CLAUDE_HOME=/host/claude`, `TRAJECTREE_BACKENDS_ROOT=/host/claude-backends`, `TRAJECTREE_PATH_MAP=${TRAJECTREE_PROJECTS_ROOT}=/projects`.
- `README.md` gains sections: **Connect a project folder** (`trajectree init-project ~/path/to/project`, what it writes, the exclude line, the token file `~/.config/trajectree/token` written from `data/token`), **MCP** (shim is registered by init-project; direct HTTP alternative `claude mcp add --transport http --scope user trajectree http://127.0.0.1:8742/mcp --header "Authorization: Bearer <token>"`; sessions' writes land pending in the Inbox), **Ingest** (what the loop reads, `TRAJECTREE_INGEST_EVERY`, `TRAJECTREE_PROJECTS_ROOT` and the path map, `trajectree ingest` to run now, `data/exports/projects/<slug>/SUMMARY.md`), **Environment variables** table listing every `TRAJECTREE_*` variable with its default.
- `web/playwright.config.ts` env adds `TRAJECTREE_INGEST_EVERY: "0"` and `TRAJECTREE_CLAUDE_HOME: ""` so the smoke run never scans the developer's real transcripts.
- `web/e2e/smoke.spec.ts` appends, after the revert assertions: open the `Inbox` tab and expect `Nothing to review`; call `POST /api/ingest/run` through `page.request` with the bearer header and expect status 200; return to `Timeline` and expect `g.lab` for `CLMI` still visible.

- [ ] **Step 1: Make the Dockerfile, compose and playwright changes; extend the smoke test**

- [ ] **Step 2: Validate the compose file**

Run: `TRAJECTREE_PROJECTS_ROOT=/tmp UID=1000 GID=1000 docker compose config >/dev/null && echo ok`
Expected: `ok`. If `docker` is unavailable in the environment, record that in the report and have the reviewer check the YAML by eye.

- [ ] **Step 3: Run the smoke test**

Run: `cd web && npm run e2e`
Expected: 1 passed.

- [ ] **Step 4: Write the README sections** per Interfaces.

- [ ] **Step 5: Run everything once more**

Run: `uv run pytest -q && cd web && npm test && npm run build`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile compose.yaml README.md web/playwright.config.ts web/e2e
git commit -m "feat(deploy): read-only transcript and project mounts, git in image, phase 2 docs and smoke"
```

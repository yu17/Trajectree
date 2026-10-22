# Phase 3: Runner, Summaries, Ask, Quick-add Proposals, Usage and Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The daemon runs the owner's headless CLIs (`claude -p`, the same binary under a ccx backend profile, optionally `codex exec`) as capped, attributed, revertible runs: regenerate a project's summary from the stale chip or the CLI, answer a question over a context pack, and parse free text into proposals the owner accepts; every run's tokens and cost land on a usage page with caps and a pause switch; Claude Code http hooks make running sessions live on the timeline and let MCP writes carry their session id.

**Architecture:** A `runner` package builds one `RunSpec` per run (prompt from an editable file under `<data>/prompts`, working directory, explicit tool allowlist, budget, timeout, a per-run MCP token minted for exactly that run's scope), spawns the CLI as a subprocess outside the ledger lock, parses its JSON result into a `run` row, and revokes the token. A `RunService` worker in the daemon's event loop executes queued runs one at a time (the interactive slot), enforces the daily cap and the pause switch, and applies accepted proposals as human transactions that record `proposed_by_run`. A `session` registry fed by `POST /api/hooks/claude` replaces mtime guessing for liveness and lets the MCP server attach a session id to owner writes. The React app gains a Runs tab (usage, caps, run history with proposals), an Ask panel, a free-text quick-add mode, and a clickable stale chip.

**Tech Stack:** Python 3.12, uv, apsw, FastAPI, pydantic v2, `mcp>=2.3.0`, subprocess (Claude Code 2.1.309 native binary; `codex exec` 0.163 optional); React 19, TypeScript, d3 v7, TanStack Query, vitest, Playwright; Docker Compose with a container-private `cfg` volume.

**Spec:** `docs/superpowers/specs/2026-10-21-trajectree-architecture-design.md` (sections 5 `run`, 6, 9, 10, 11, 14 item 3, 15 and Appendix B). Earlier plans whose Global Constraints still apply: `docs/superpowers/plans/2026-10-21-phase1-ledger-and-timeline.md`, `docs/superpowers/plans/2026-10-22-phase2-mcp-connectors-inbox.md`.

Verified against the installed tools on 2026-10-22 (Claude Code 2.1.309, codex-cli 0.163.0): `claude -p` takes `--output-format json`, `--json-schema <json>`, `--max-budget-usd <amount>`, `--mcp-config <json-or-file>`, `--strict-mcp-config`, `--permission-mode dontAsk`, `--tools <csv|"">`, `--allowedTools <items...>`, `--setting-sources user`, `--append-system-prompt <text>`, `--model <m>`; **there is no `--max-turns` flag in 2.1.309**, so turns are recorded from the result's `num_turns` and never capped by flag. The JSON result carries `type`, `subtype` (`success`, `error_max_budget_usd`, `error_during_execution`, `error_max_structured_output_retries`), `is_error`, `result`, `structured_output`, `session_id`, `total_cost_usd`, `usage` (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`), `num_turns`, `duration_ms`. Stdio MCP servers inherit `CLAUDE_CODE_SESSION_ID`. Http hooks are `{"type": "http", "url": ..., "headers": {...}, "timeout": n}`; payloads carry `session_id`, `transcript_path`, `cwd`, `hook_event_name`, plus `source` (SessionStart) or `reason` (SessionEnd). ccx is one line, `CLAUDE_CONFIG_DIR=<profile> exec claude "$@"`, so the runner reproduces it instead of shipping the script (Appendix B's "environment variable the builder adds to ccx" is unnecessary). `codex exec` takes `--json`, `--output-schema <file>`, `-o <file>`, `--ephemeral`, `--skip-git-repo-check`, `--sandbox read-only`, `-C <dir>`, `-m <model>`, and `-c mcp_servers.<name>.url=... -c mcp_servers.<name>.bearer_token_env_var=...`.

Deferred to phase 4 with the spec as the reference: scheduled routines (the `routine` table, cron, retries, the second execution slot: phase 3 has only on-request interactive runs, which the spec puts in one serial slot), cross-project-review, meeting-prep, report-draft and `save_report_draft`, the per-session summarize button and daemon-side transcript pre-digestion (regenerate-summary reads the context pack, whose activity section already lists sessions and commits since the summary), `get_run` as an MCP tool (runs are read over HTTP), the `session_end` trigger, codex in the image (the argv builder and parser ship and are tested; the owner installs the binary when wanted), and SSE.

## Global Constraints

- Everything in the phase 1 and phase 2 Global Constraints still holds.
- `run` and `session` are not ledger tables: never attached to a Session, never in `LEDGER_TABLES`, written directly under `Ledger.locked()`. `run.project_id` and `session.project_id` carry no foreign key.
- No subprocess ever runs while the ledger lock is held. The run worker reads what it needs under the lock, releases it, spawns the CLI, and takes the lock again to store the result.
- The CLI's environment is built from scratch, never inherited wholesale: `PATH`, `LANG`, `TERM=dumb`, `HOME=<cfg_root>/home`, `CLAUDE_CONFIG_DIR` (`<cfg_root>/claude` for the claude backend, `<cfg_root>/backends/<name>` for `ccx:<name>`), `DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_OAUTH_TOKEN` (claude backend only), `CODEX_HOME=<cfg_root>/codex` and `TRAJECTREE_RUN_TOKEN` (codex only), plus the variables named in `Settings.run_env_passthrough` (default empty; for proxies and tests). The daemon's own `TRAJECTREE_*` variables never reach a run.
- Every run gets its own MCP token minted just before launch and revoked in a `finally` after it, TTL `timeout_s + 60` seconds. Scope per kind: `summarize` is `Scope(projects=[project_id], tools=["set_project_summary"], write=True)`; `ask` and `quick_add` are `Scope(write=False)`. The token's actor is `Actor(kind="agent", ref=run_id, model=<model or None>, backend=<backend>, tool=<runner>, tool_version=<CLI version or None>, run_id=run_id)`, so every transaction a run writes carries its run id.
- Headless invocation shape, fixed: `<bin> -p <prompt> --output-format json --permission-mode dontAsk --setting-sources user --strict-mcp-config --mcp-config <inline json> --tools <csv or ""> --allowedTools <items...> --max-budget-usd <usd> [--model <m>] [--json-schema <inline json>] [--append-system-prompt <text>]`. The mcp-config JSON is `{"mcpServers": {"trajectree": {"type": "http", "url": "http://127.0.0.1:<port>/mcp", "headers": {"Authorization": "Bearer <run token>"}}}}`. Wall clock is capped by the daemon (`timeout_s`: SIGTERM, 5 s grace, SIGKILL), never by the CLI.
- Tests never call an LLM: runners are exercised through an injected `popen` or the executable `tests/bin/fake-claude`, which prints canned JSON.
- Secrets: the OAuth token lives at `<cfg_root>/claude/oauth_token` (0600, directory 0700), is never logged, never stored in a run row, never returned by an API. Run logs under `<data_dir>/runs/<run_id>.log` hold the masked argv (bearer tokens replaced by `***`) and the CLI's stdout and stderr only.
- Prompts live at `<data_dir>/prompts/<kind>.md`, seeded from `src/trajectree/runner/prompts/<kind>.md` when missing and never overwritten after that. Placeholders are `$name` (`string.Template.safe_substitute`).
- Accepting a proposal is a human transaction (review `accepted`) whose actor carries `proposed_by_run=<run id>`, `model` and `backend` copied from the run, `tool` from the client header; its intent is the proposal's `reason`. Each proposal can be applied once.
- `POST /api/hooks/claude` accepts the owner token or a scoped token whose scope allows tool `hooks`; it stores only `session_id`, `cwd`, `transcript_path`, `hook_event_name`, `source`, `reason`; it answers `200 {}` for every well-authenticated request and never returns fields that steer the session. A hook token is minted by `trajectree hooks install` with `Scope(projects=[], tools=["hooks"], write=False)` and no expiry.
- Host-side writes are explicit and disclosed: `trajectree hooks install` shows the exact JSON it will merge into the Claude settings file, writes `<file>.trajectree.bak` first, and proceeds only with `--yes` or a typed `y`; `trajectree login` writes nothing on the host. The README's "What gets written where" table lists every write.
- Web accessible names added in this phase: tab `Runs`; headings `Usage`, `Caps`, `Run history`, `Proposals`, `Ask`; buttons `Ask`, `Parse`, `Accept`, `Pause runs`, `Resume runs`, `Save caps`; inputs `Question`, `Describe it`, `USD per run`, `USD per day`, `Timeout (s)`, `From`, `To`; empty states `No runs yet`, `No proposals`; the stale chip's SVG `<title>` is `Regenerate summary`. Spend bars use `--strip-3`, the cap line `--now`, text ink tokens only.

## Review Focus

1. The CLI prints no JSON, truncated JSON or exits non-zero without a result (crash, OOM kill): the run ends `failed` with the first 500 characters of stderr (or stdout) as `error`, zero usage, the token revoked, the log kept. Pinned in Task 4 (`test_parse_garbage_is_failed`) and Task 6 (`test_run_failure_revokes_token`).
2. A run that outlives `timeout_s`: the process is terminated then killed, the run ends `failed` with error `timeout after <n>s`, and the worker moves on to the next queued run. Pinned in Task 4 (`test_launch_kills_on_timeout`) and Task 6 (`test_worker_continues_after_timeout`).
3. The daily cap is reached or runs are paused: a new request is recorded `capped` without launching anything, or rejected with 409 while paused; the cap check counts today's spend in UTC. Pinned in Task 6 (`test_daily_cap_marks_capped_without_launch`, `test_paused_enqueue_raises`).
4. A hook payload for a cwd no folder resource maps to, with missing fields, or with an unknown token: the first is stored with `project_id` None, the second is ignored with 200, the third gets 401; the registry never raises on a payload. Pinned in Task 8 (`test_hook_unknown_cwd_missing_fields_bad_token`).
5. Accepting a proposal twice, or a proposal naming an unknown slug: the second accept is 409 and exactly one transaction exists; the unknown slug is 422 listing the known slugs and writes nothing. Pinned in Task 6 (`test_accept_proposal_twice_is_409`, `test_accept_unknown_slug_writes_nothing`).

---

### Task 1: Schema v3, run and session repositories, caps

**Files:**
- Modify: `src/trajectree/db/schema.py`, `src/trajectree/models.py`, `src/trajectree/db/ledger.py:171-200` (`list_txns`), `src/trajectree/timeutil.py`, `src/trajectree/repo/__init__.py`
- Create: `src/trajectree/repo/runs.py`, `src/trajectree/repo/sessions.py`, `src/trajectree/repo/caps.py`
- Test: `tests/test_schema_v3.py`, `tests/test_repo_runs.py`, `tests/test_repo_sessions.py`

**Interfaces:**
- Consumes: `MIGRATIONS`, `SCHEMA_VERSION`, `table_columns`; `to_db`, `from_db`, `utcnow`; `new_id`; the `setting(key, value)` table (v1, no `updated_at`).
- Produces `SCHEMA_VERSION = 3` with `_V3_STATEMENTS`:
  - `CREATE TABLE run (id TEXT PRIMARY KEY, kind TEXT NOT NULL, project_id TEXT, status TEXT NOT NULL, runner TEXT NOT NULL, backend TEXT NOT NULL, model TEXT, session_id TEXT, started TEXT, finished TEXT, tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, tokens_cache INTEGER NOT NULL DEFAULT 0, est_usd REAL NOT NULL DEFAULT 0, turns INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0, result_md TEXT NOT NULL DEFAULT '', proposals TEXT NOT NULL DEFAULT '[]', error TEXT, log_path TEXT, token_id TEXT, props TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)`, `CREATE INDEX run_created ON run(created_at)`, `CREATE INDEX run_status ON run(status)`.
  - `CREATE TABLE session (id TEXT PRIMARY KEY, cwd TEXT NOT NULL, project_id TEXT, transcript_path TEXT, backend TEXT NOT NULL DEFAULT 'claude', started_at TEXT NOT NULL, last_seen TEXT NOT NULL, ended_at TEXT, end_reason TEXT, source TEXT)`, `CREATE INDEX session_cwd ON session(cwd)`.
- `timeutil.parse_iso(value: str) -> datetime`: ISO 8601 with `Z` or an offset; `ValueError` on anything else, including naive values.
- `repo/__init__.py`: `class UnknownProject(Exception)` whose message is `unknown project '<name>'; known projects: <sorted slugs>` (built by the helper `unknown_project(conn, name) -> UnknownProject` in `repo/projects.py`).
- Models: `RunKind = Literal["summarize", "ask", "quick_add"]`; `RunStatus = Literal["queued", "running", "ok", "failed", "capped", "cancelled"]`; `RUNNER_PATTERN = r"^(claude|codex|ccx:[A-Za-z0-9][A-Za-z0-9._-]*)$"`; `ProposalKind = Literal["event", "todo"]`; `Proposal(kind: ProposalKind, project: str, title: str, reason: str, event_kind: str = "note", when: str | None = None, due: str | None = None, body_md: str = "", status: Literal["happened", "planned", "suggested"] = "happened", applied_txn: str | None = None, applied_id: str | None = None)`; `Run(id: str, kind: RunKind, project_id: str | None, status: RunStatus, runner: str = Field(pattern=RUNNER_PATTERN), backend: str, model: str | None, session_id: str | None, started: AwareDatetime | None, finished: AwareDatetime | None, tokens_in: int = 0, tokens_out: int = 0, tokens_cache: int = 0, est_usd: float = 0.0, turns: int = 0, duration_ms: int = 0, result_md: str = "", proposals: list[Proposal] = Field(default_factory=list), error: str | None = None, log_path: str | None = None, token_id: str | None = None, props: dict[str, Any] = Field(default_factory=dict), created_at: AwareDatetime)`; `SessionRow(id: str, cwd: str, project_id: str | None, transcript_path: str | None, backend: str = "claude", started_at: AwareDatetime, last_seen: AwareDatetime, ended_at: AwareDatetime | None = None, end_reason: str | None = None, source: str | None = None)`; `Caps(usd_per_run: float = Field(1.0, gt=0, le=100), usd_per_day: float = Field(5.0, gt=0, le=1000), timeout_s: int = Field(600, ge=1, le=7200), paused: bool = False)`; `UsageDay(day: str, est_usd: float, runs: int, tokens_in: int, tokens_out: int)`; `UsageSlice(key: str, est_usd: float, runs: int)`.
- `repo/runs.py`: `create_run(conn, *, kind: RunKind, runner: str, backend: str, model: str | None, project_id: str | None, props: dict | None = None) -> Run` (status `queued`, `created_at` now); `get_run(conn, run_id) -> Run | None`; `update_run(conn, run_id, **fields) -> Run` (datetimes via `to_db`, `proposals` as a list of `Proposal` dumped to JSON, `props` dumped; raises `NotFound`); `list_runs(conn, *, limit: int = 50, kind: str | None = None, status: str | None = None, with_open_proposals: bool = False) -> list[Run]` (newest `created_at` first; `with_open_proposals` keeps runs where `EXISTS (SELECT 1 FROM json_each(run.proposals) WHERE json_extract(value, '$.applied_txn') IS NULL)`); `oldest_queued(conn) -> Run | None`; `spent_usd(conn, *, day: str) -> float` (sum of `est_usd` where `substr(coalesce(started, created_at), 1, 10) = day`); `usage_days(conn, *, days: int, now: datetime) -> list[UsageDay]` (one row per UTC day from `now - (days - 1)` to today, zeros filled, ascending); `usage_by(conn, *, field: Literal["kind", "runner"], since: datetime) -> list[UsageSlice]`; `txn_ids_for_run(conn, run_id) -> list[str]` (`SELECT id FROM txn WHERE run_id = ? ORDER BY id`).
- `repo/sessions.py`: `start_session(conn, *, session_id, cwd, transcript_path, project_id, source, backend="claude", now) -> SessionRow` (insert, or on conflict update `cwd`, `transcript_path`, `project_id`, `source`, `last_seen` and clear `ended_at`/`end_reason`; `started_at` kept); `touch_session(conn, *, session_id, cwd, project_id, now) -> SessionRow` (update `last_seen`, `cwd`, `project_id` and clear `ended_at`; insert with `started_at = now`, `source = "stop"` when unknown); `end_session(conn, *, session_id, reason, now) -> SessionRow | None` (None when unknown); `running_sessions(conn, *, since: datetime) -> list[SessionRow]` (`ended_at IS NULL AND last_seen >= since`, newest `last_seen` first); `session_for_cwd(conn, *, cwd: str, since: datetime) -> SessionRow | None` (newest running with that exact cwd); `ended_session_ids(conn, ids: list[str]) -> set[str]`.
- `repo/caps.py`: `get_caps(conn) -> Caps` (row `setting.key = 'caps'`; defaults when absent or invalid); `set_caps(conn, caps: Caps) -> None` (upsert JSON).
- `Ledger.list_txns(..., run_id: str | None = None)` adds `run_id = ?`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_schema_v3.py
import apsw
from trajectree.db.connection import open_db
from trajectree.db.schema import LEDGER_TABLES, MIGRATIONS, table_columns

def test_v3_tables(conn):
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"run", "session"} <= names
    assert "proposals" in table_columns(conn, "run") and "ended_at" in table_columns(conn, "session")
    assert conn.execute("pragma user_version").fetchone()[0] == 3
    assert "run" not in LEDGER_TABLES and "session" not in LEDGER_TABLES

def test_migrates_v2_database(tmp_path):
    path = tmp_path / "old.db"
    c = apsw.Connection(str(path))
    for _, stmts in MIGRATIONS[:2]:
        for s in stmts:
            c.execute(s)
    c.execute("pragma user_version = 2")
    c.close()
    conn = open_db(path)
    assert conn.execute("pragma user_version").fetchone()[0] == 3
```

```python
# tests/test_repo_runs.py
from datetime import datetime, timezone
import pytest
from trajectree.models import Caps, Proposal
from trajectree.repo.caps import get_caps, set_caps
from trajectree.repo.runs import create_run, get_run, list_runs, oldest_queued, spent_usd, update_run, usage_by, usage_days
from trajectree.timeutil import parse_iso
UTC = timezone.utc

def test_create_update_and_list_runs(conn):
    r = create_run(conn, kind="ask", runner="claude", backend="claude", model=None, project_id=None)
    assert r.status == "queued" and r.proposals == [] and get_run(conn, r.id).id == r.id and oldest_queued(conn).id == r.id
    p = Proposal(kind="todo", project="a", title="Email Bob", reason="follow-up owed")
    r2 = update_run(conn, r.id, status="ok", est_usd=0.25, started=datetime(2026, 1, 5, 10, tzinfo=UTC), proposals=[p])
    assert r2.est_usd == 0.25 and r2.proposals[0].title == "Email Bob" and r2.proposals[0].applied_txn is None
    assert oldest_queued(conn) is None
    assert [x.id for x in list_runs(conn, with_open_proposals=True)] == [r.id]
    update_run(conn, r.id, proposals=[p.model_copy(update={"applied_txn": "t1"})])
    assert list_runs(conn, with_open_proposals=True) == []
    assert list_runs(conn, kind="summarize") == [] and list_runs(conn, status="ok")[0].id == r.id

def test_spent_and_usage(conn):
    for i, (day, usd) in enumerate([("2026-01-05", 0.5), ("2026-01-05", 0.25), ("2026-01-04", 1.0)]):
        r = create_run(conn, kind="ask" if i else "summarize", runner="claude", backend="claude", model=None, project_id=None)
        update_run(conn, r.id, status="ok", est_usd=usd, tokens_in=10, tokens_out=5, started=parse_iso(day + "T12:00:00Z"))
    assert spent_usd(conn, day="2026-01-05") == 0.75 and spent_usd(conn, day="2026-01-06") == 0.0
    days = usage_days(conn, days=3, now=datetime(2026, 1, 6, 8, tzinfo=UTC))
    assert [(d.day, d.est_usd, d.runs) for d in days] == [("2026-01-04", 1.0, 1), ("2026-01-05", 0.75, 2), ("2026-01-06", 0.0, 0)]
    by_kind = {s.key: s.est_usd for s in usage_by(conn, field="kind", since=datetime(2026, 1, 1, tzinfo=UTC))}
    assert by_kind == {"summarize": 0.5, "ask": 1.25}

def test_caps_defaults_and_roundtrip(conn):
    assert get_caps(conn) == Caps()
    set_caps(conn, Caps(usd_per_day=2.0, paused=True))
    assert get_caps(conn).usd_per_day == 2.0 and get_caps(conn).paused is True
    conn.execute("update setting set value = 'nonsense' where key = 'caps'")
    assert get_caps(conn) == Caps()

def test_parse_iso():
    assert parse_iso("2026-01-05T10:00:00Z") == datetime(2026, 1, 5, 10, tzinfo=UTC)
    for bad in ("2026-01-05T10:00:00", "yesterday", ""):
        with pytest.raises(ValueError):
            parse_iso(bad)
```

```python
# tests/test_repo_sessions.py
from datetime import datetime, timedelta, timezone
from trajectree.repo.sessions import end_session, ended_session_ids, running_sessions, session_for_cwd, start_session, touch_session
UTC = timezone.utc
T0 = datetime(2026, 1, 5, 10, tzinfo=UTC)

def test_session_lifecycle(conn):
    s = start_session(conn, session_id="s1", cwd="/p", transcript_path="/t.jsonl", project_id="p1", source="startup", now=T0)
    assert s.started_at == T0 and s.ended_at is None
    touch_session(conn, session_id="s1", cwd="/p", project_id="p1", now=T0 + timedelta(minutes=5))
    assert running_sessions(conn, since=T0)[0].last_seen == T0 + timedelta(minutes=5)
    assert session_for_cwd(conn, cwd="/p", since=T0).id == "s1" and session_for_cwd(conn, cwd="/q", since=T0) is None
    assert end_session(conn, session_id="s1", reason="other", now=T0 + timedelta(minutes=6)).end_reason == "other"
    assert running_sessions(conn, since=T0) == [] and ended_session_ids(conn, ["s1", "zz"]) == {"s1"}
    start_session(conn, session_id="s1", cwd="/p", transcript_path="/t.jsonl", project_id="p1", source="resume", now=T0 + timedelta(hours=1))
    assert running_sessions(conn, since=T0)[0].started_at == T0  # reopened, original start kept
    assert end_session(conn, session_id="ghost", reason="other", now=T0) is None

def test_touch_unknown_session_creates_it(conn):
    s = touch_session(conn, session_id="s9", cwd="/x", project_id=None, now=T0)
    assert s.started_at == T0 and s.source == "stop" and s.project_id is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_schema_v3.py tests/test_repo_runs.py tests/test_repo_sessions.py -q`
Expected: FAIL (ImportError / no `run` table).

- [ ] **Step 3: Implement the schema step, the models, `parse_iso`, `UnknownProject`, the three repositories and the `run_id` filter** per Interfaces. `usage_days` fills missing days in Python after one grouped query.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all green (188 existing plus the new ones).

- [ ] **Step 5: Commit**

```bash
git add src/trajectree tests/test_schema_v3.py tests/test_repo_runs.py tests/test_repo_sessions.py
git commit -m "feat(db): schema v3 with run and session tables, run/session/caps repositories"
```

---

### Task 2: Hardening wave from the phase 2 reviews

**Files:**
- Modify: `src/trajectree/ingest/job.py`, `src/trajectree/ingest/exports.py`, `src/trajectree/api/routes_signals.py`, `src/trajectree/api/routes_timeline.py`, `src/trajectree/api/app.py`, `src/trajectree/repo/signals.py` (`strip_bins`)
- Test: `tests/test_ingest.py`, `tests/test_api_signals_timeline.py`, `tests/test_repo_signals.py`

**Interfaces:**
- `ingest/job.py`: module-level `_RUNNING = threading.Lock()`; `class IngestBusy(Exception)`; `run_ingest` acquires it with `acquire(blocking=False)` and raises `IngestBusy` when held; `IngestReport.exports_errors: list[str] = []`; the final `set_state(conn, "ingest:last", ...)` runs in a `finally` with whatever phases completed (defaults for the missing ones: `scan=None`, `git=GitReport(repos=0, signals_written=0, errors=[<exception text>])`, `stale=[]`, `exports_written=0`); the exception is re-raised after storing.
- `ingest/exports.py`: `write_files(files) -> tuple[int, list[str]]` writes `path.with_name(path.name + ".tmp")` then `os.replace`; an `OSError` on one file appends `f"{path}: {exc}"` to the error list and the loop continues; `write_exports(conn, settings, *, now)` returns the same tuple; `render_summary_md(conn, project, *, now: datetime)` keeps only events with `t_start <= now` in Recent events; `render_exports(conn, settings, *, now)`.
- `app.py`: `IngestBusy` → `409 {"detail": "ingest already running"}`.
- `routes_timeline.py`: `_live_from_state(raw: Any, shown: set[str]) -> list[LiveSession]` validates each entry with `LiveSession.model_validate` and skips invalid ones (Task 8 moves this into `repo/sessions.live_sessions`).
- `repo/signals.py` `strip_bins`: the commits sum uses `CASE WHEN typeof(json_extract(payload, '$.count')) IN ('integer', 'real') THEN json_extract(payload, '$.count') ELSE 1 END`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest.py (append; reuse the module's imports)
import pytest
from trajectree.ingest.exports import write_files
from trajectree.ingest.job import IngestBusy
import trajectree.ingest.job as job

def test_ingest_busy_raises(ledger, settings):
    assert job._RUNNING.acquire(blocking=False)
    try:
        with pytest.raises(IngestBusy):
            run_ingest(ledger, settings)
    finally:
        job._RUNNING.release()

def test_ingest_stores_last_even_when_a_phase_fails(ledger, human, settings, monkeypatch):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    def boom(*a, **k):
        raise RuntimeError("git exploded")
    monkeypatch.setattr(job, "collect_git", boom)
    with pytest.raises(RuntimeError):
        run_ingest(ledger, settings)
    assert "git exploded" in " ".join(get_state(ledger.conn, "ingest:last")["git"]["errors"])

def test_write_files_is_atomic_and_isolates_errors(tmp_path):
    good = tmp_path / "ok" / "SUMMARY.md"
    bad = tmp_path / "blocked" / "SUMMARY.md"
    bad.parent.mkdir()
    bad.parent.chmod(0o500)
    try:
        written, errors = write_files([(good, "hello\n"), (bad, "x\n")])
    finally:
        bad.parent.chmod(0o700)
    assert written == 1 and good.read_text() == "hello\n" and len(errors) == 1 and str(bad) in errors[0]
    assert not list(good.parent.glob("*.tmp"))

def test_summary_md_excludes_future_events(ledger, human, settings):
    from trajectree.models import EventIn
    from trajectree.repo.events import create_event
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    create_event(ledger, human, EventIn(project_id=p.id, kind="note", title="Past", t_start=datetime(2026, 1, 1, tzinfo=UTC)))
    create_event(ledger, human, EventIn(project_id=p.id, kind="milestone", title="Future", t_start=datetime(2027, 1, 1, tzinfo=UTC), status="planned"))
    md = render_summary_md(ledger.conn, get_project(ledger.conn, p.id), now=datetime(2026, 6, 1, tzinfo=UTC))
    assert "Past" in md and "Future" not in md
```

```python
# tests/test_api_signals_timeline.py (append)
def test_ingest_run_409_when_busy(client):
    import trajectree.ingest.job as job
    assert job._RUNNING.acquire(blocking=False)
    try:
        assert client.post("/api/ingest/run").status_code == 409
    finally:
        job._RUNNING.release()

def test_timeline_skips_malformed_live_entries(client, app):
    p = mk_project(client)
    set_state(app.state.ledger.conn, "live_sessions", [{"project_id": p["id"]}, {"project_id": p["id"], "session_id": "ok", "cwd": "/c", "title": None, "started": None, "last_seen": "2026-01-05T12:00:00Z"}])
    tl = client.get("/api/timeline", params={"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z"}).json()
    assert [l["session_id"] for l in tl["live"]] == ["ok"]
```

```python
# tests/test_repo_signals.py (append)
def test_strip_bins_tolerate_non_numeric_counts(conn):
    upsert_signals(conn, [SignalIn(project_id="p", kind="commits", t_start=datetime(2026, 1, 5, tzinfo=UTC), dedupe_key="a", payload={"count": "many"}),
                          SignalIn(project_id="p", kind="commits", t_start=datetime(2026, 1, 5, 1, tzinfo=UTC), dedupe_key="b", payload={"count": 2})])
    bins = strip_bins(conn, project_ids=["p"], t_from=datetime(2026, 1, 1, tzinfo=UTC), t_to=datetime(2026, 2, 1, tzinfo=UTC))
    assert bins[0].count == 3
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_ingest.py tests/test_api_signals_timeline.py tests/test_repo_signals.py -q`
Expected: FAIL (`IngestBusy` missing, tuple unpack, `now` keyword, count 2 or error).

- [ ] **Step 3: Implement** per Interfaces; update the existing callers of `write_files`, `write_exports` and `render_summary_md` (job and tests) for the new signatures.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree tests
git commit -m "fix(ingest): run guard, atomic isolated exports, always store ingest:last, lenient live entries, strip count hardening"
```

---

### Task 3: Runner settings, credentials store, backend profile sync, runner status

**Files:**
- Modify: `src/trajectree/config.py`, `README.md` (environment table rows only)
- Create: `src/trajectree/runner/__init__.py`, `src/trajectree/runner/credentials.py`
- Test: `tests/test_config.py` (new), `tests/test_runner_credentials.py`

**Interfaces:**
- `Settings` gains `cfg_dir: Path | None = None` (property `cfg_root -> Path` = `cfg_dir or data_dir / "cfg"`), `claude_bin: str = "claude"`, `codex_bin: str = "codex"`, `default_runner: str = "claude"`, `run_env_passthrough: list[str] = []`; env `TRAJECTREE_CFG_DIR`, `TRAJECTREE_CLAUDE_BIN`, `TRAJECTREE_CODEX_BIN`, `TRAJECTREE_DEFAULT_RUNNER`, `TRAJECTREE_RUN_ENV_PASSTHROUGH` (comma-separated); properties `runs_dir = data_dir / "runs"`, `prompts_dir = data_dir / "prompts"`, `claude_config_dir = cfg_root / "claude"`, `backends_cfg_dir = cfg_root / "backends"`, `codex_home = cfg_root / "codex"`, `run_home = cfg_root / "home"`.
- `runner/credentials.py`:
  - `OAUTH_FILE = "oauth_token"`; `ensure_cfg_dirs(settings) -> None` creates `cfg_root`, `claude_config_dir`, `run_home`, `backends_cfg_dir` with mode 0700 (`exist_ok`).
  - `store_oauth_token(settings, token: str) -> Path`: strips, raises `ValueError("empty token")` on blank, `ensure_cfg_dirs`, writes the token with `os.open(path, O_WRONLY|O_CREAT|O_TRUNC, 0o600)`, writes `claude_config_dir/settings.json` as `{}` and `claude_config_dir/.claude.json` as `{"hasCompletedOnboarding": true}` only when missing; returns the token path.
  - `load_oauth_token(settings) -> str | None`.
  - `profile_has_credentials(settings_json: Path) -> bool`: ccx's predicate (`env.ANTHROPIC_AUTH_TOKEN` or `env.ANTHROPIC_API_KEY` or top-level `apiKeyHelper` non-empty); False on a missing file or invalid JSON.
  - `sync_backends(settings) -> list[str]`: for each directory under `settings.backends_root` holding `settings.json`, copy `settings.json` and, when present, `.claude.json` into `backends_cfg_dir/<name>/` (dirs 0700, files 0600, contents via `read_bytes`, nothing else copied); returns the sorted names; `[]` when `backends_root` is None.
  - `class RunnerInfo(BaseModel)`: `available: bool`, `version: str | None`, `authenticated: bool`; `class BackendInfo(BaseModel)`: `name: str`, `credentials: bool`; `class RunnerStatus(BaseModel)`: `claude: RunnerInfo`, `codex: RunnerInfo`, `backends: list[BackendInfo]`.
  - `cli_version(binary: str, *, run=subprocess.run) -> str | None`: `[binary, "--version"]` with `capture_output=True, text=True, timeout=10`; first whitespace-separated token of stdout; None on `FileNotFoundError`, `TimeoutExpired`, `OSError` or a non-zero exit; cached per binary in `_versions: dict[str, str | None]`; `reset_version_cache()`.
  - `runner_status(settings, *, which=shutil.which, run=subprocess.run) -> RunnerStatus`: `claude.available = which(settings.claude_bin) is not None`, `version = cli_version(settings.claude_bin, run=run)` when available, `authenticated = load_oauth_token(settings) is not None`; codex likewise with `authenticated = (settings.codex_home / "auth.json").exists()`; backends from `backends_cfg_dir` subdirectories, `credentials = profile_has_credentials(dir / "settings.json")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
from trajectree.config import load_settings

def test_runner_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAJECTREE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("TRAJECTREE_CFG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TRAJECTREE_CLAUDE_BIN", "/opt/claude")
    monkeypatch.setenv("TRAJECTREE_DEFAULT_RUNNER", "ccx:kimi")
    monkeypatch.setenv("TRAJECTREE_RUN_ENV_PASSTHROUGH", "HTTPS_PROXY, NO_PROXY")
    s = load_settings()
    assert s.cfg_root == tmp_path / "cfg" and s.claude_bin == "/opt/claude" and s.default_runner == "ccx:kimi"
    assert s.claude_config_dir == tmp_path / "cfg" / "claude" and s.runs_dir == tmp_path / "d" / "runs"
    assert s.run_env_passthrough == ["HTTPS_PROXY", "NO_PROXY"]

def test_cfg_defaults_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAJECTREE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TRAJECTREE_CFG_DIR", raising=False)
    s = load_settings()
    assert s.cfg_root == tmp_path / "cfg" and s.run_env_passthrough == [] and s.default_runner == "claude"
```

```python
# tests/test_runner_credentials.py
import json, stat
from types import SimpleNamespace
import pytest
from trajectree.runner.credentials import (cli_version, load_oauth_token, profile_has_credentials, reset_version_cache,
                                           runner_status, store_oauth_token, sync_backends)

def test_store_and_load_oauth_token(settings):
    path = store_oauth_token(settings, " sk-ant-oat01-abc \n")
    assert load_oauth_token(settings) == "sk-ant-oat01-abc"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads((settings.claude_config_dir / ".claude.json").read_text()) == {"hasCompletedOnboarding": True}
    assert (settings.claude_config_dir / "settings.json").read_text() == "{}"
    assert load_oauth_token(settings.model_copy(update={"cfg_dir": settings.cfg_root / "nope"})) is None
    with pytest.raises(ValueError):
        store_oauth_token(settings, "   ")

def test_sync_backends_copies_only_config(settings, tmp_path):
    root = tmp_path / "backends"
    for name, key in (("kimi", "k"), ("empty", "")):
        d = root / name
        (d / "projects" / "x").mkdir(parents=True)
        (d / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": key, "ANTHROPIC_BASE_URL": "https://x"}}))
        (d / "history.jsonl").write_text("secret\n")
    (root / "kimi" / ".claude.json").write_text('{"hasCompletedOnboarding": true}')
    (root / "notaprofile").mkdir()
    s = settings.model_copy(update={"backends_root": root})
    assert sync_backends(s) == ["empty", "kimi"]
    copied = s.backends_cfg_dir / "kimi"
    assert sorted(p.name for p in copied.iterdir()) == [".claude.json", "settings.json"]
    assert stat.S_IMODE((copied / "settings.json").stat().st_mode) == 0o600
    assert profile_has_credentials(copied / "settings.json") and not profile_has_credentials(s.backends_cfg_dir / "empty" / "settings.json")
    assert sync_backends(settings) == []

def test_runner_status_and_version_cache(settings):
    reset_version_cache()
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="2.1.309 (Claude Code)\n", stderr="")
    which = lambda b: "/usr/bin/x" if b == settings.claude_bin else None
    st = runner_status(settings, which=which, run=run)
    assert st.claude.model_dump() == {"available": True, "version": "2.1.309", "authenticated": False} and st.codex.available is False
    runner_status(settings, which=which, run=run)
    assert len(calls) == 1
    assert cli_version("definitely-missing-binary-xyz") is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_config.py tests/test_runner_credentials.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement** per Interfaces; add the five rows to the README environment table (`TRAJECTREE_CFG_DIR`, `TRAJECTREE_CLAUDE_BIN`, `TRAJECTREE_CODEX_BIN`, `TRAJECTREE_DEFAULT_RUNNER`, `TRAJECTREE_RUN_ENV_PASSTHROUGH`).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_config.py tests/test_runner_credentials.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/config.py src/trajectree/runner README.md tests/test_config.py tests/test_runner_credentials.py
git commit -m "feat(runner): cfg dir settings, oauth token store, backend profile sync, runner status"
```

---

### Task 4: Runner core: spec, argv, environment, launch, result parsing, fake CLI

**Files:**
- Create: `src/trajectree/runner/spec.py`, `src/trajectree/runner/argv.py`, `src/trajectree/runner/parse.py`, `src/trajectree/runner/launch.py`, `tests/bin/fake-claude` (executable, `#!/usr/bin/env python3`)
- Test: `tests/test_runner_argv.py`, `tests/test_runner_parse.py`, `tests/test_runner_launch.py`

**Interfaces:**
- `runner/spec.py`: `class ToolPolicy(BaseModel)`: `tools: list[str]` (built-in tools to enable; empty means `--tools ""`), `allowed: list[str]`; `class RunSpec(BaseModel)`: `run_id: str`, `kind: RunKind`, `runner: str`, `backend: str`, `model: str | None`, `prompt: str`, `system_prompt: str | None`, `cwd: Path`, `tools: ToolPolicy`, `usd_cap: float`, `timeout_s: int`, `mcp_url: str`, `mcp_token: str`, `json_schema: dict | None`; exceptions `MissingCredentials(Exception)`, `RunsPaused(Exception)`, `AlreadyApplied(Exception)`.
- `runner/argv.py`:
  - `MCP_SERVER_NAME = "trajectree"`; `mcp_config_json(spec) -> str` (compact JSON per Global Constraints).
  - `claude_argv(spec, *, binary: str) -> list[str]` in the fixed order of the Global Constraints; `--tools` gets `",".join(spec.tools.tools)` or `""`; each `allowed` entry is its own argument after `--allowedTools`; `--model`, `--json-schema` and `--append-system-prompt` only when set.
  - `codex_argv(spec, *, binary: str, schema_path: Path | None, last_message_path: Path) -> list[str]`: `[binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only", "-C", str(spec.cwd), "-c", f'mcp_servers.{MCP_SERVER_NAME}.url="{spec.mcp_url}"', "-c", f'mcp_servers.{MCP_SERVER_NAME}.bearer_token_env_var="TRAJECTREE_RUN_TOKEN"', "-o", str(last_message_path)]`, then `["--output-schema", str(schema_path)]` when given, `["-m", model]` when set, then the prompt (system prompt prepended with a blank line, since codex has no flag for it).
  - `run_env(spec, settings, *, base: Mapping[str, str], oauth_token: str | None) -> dict[str, str]` per Global Constraints; `MissingCredentials` with a message ending in `run: trajectree login` when the claude backend has no `oauth_token`, or `run: trajectree backends sync` when `settings.backends_cfg_dir / <name> / settings.json` is missing or `profile_has_credentials` is False.
  - `mask(argv: list[str], secrets: list[str]) -> list[str]` replaces every occurrence of each non-empty secret inside each argument with `***`.
- `runner/parse.py`: `class Usage(BaseModel)`: `tokens_in: int = 0`, `tokens_out: int = 0`, `tokens_cache: int = 0`; `class ParsedResult(BaseModel)`: `ok: bool`, `subtype: str`, `result_text: str = ""`, `structured: dict | None = None`, `session_id: str | None = None`, `usage: Usage = Usage()`, `est_usd: float = 0.0`, `turns: int = 0`, `duration_ms: int = 0`, `error: str | None = None`; `parse_claude_json(stdout: str) -> ParsedResult`: the last line (or the whole text) parsing as a JSON object with `"type": "result"`; `ok = subtype == "success" and not is_error`; `tokens_in = input_tokens`, `tokens_cache = cache_creation_input_tokens + cache_read_input_tokens`, `tokens_out = output_tokens`; `est_usd = total_cost_usd`; `error` = `result` text (or the subtype) for non-success; raises `ValueError("no result object")` when nothing parses; `parse_codex(stdout: str, last_message: str | None) -> ParsedResult`: `result_text = last_message or ""`, `ok = last_message is not None`, usage summed from every JSONL object with a `usage` dict holding `input_tokens`/`output_tokens` (zeros when absent), `structured = json.loads(last_message)` when it parses to a dict, `subtype` `"success"` or `"error"`.
- `runner/launch.py`: `class RunOutcome(BaseModel)`: `exit_code: int | None`, `timed_out: bool`, `stdout: str`, `stderr_tail: str` (last 2000 chars), `log_path: str`, `duration_ms: int`; `launch(argv, *, env, cwd: Path, timeout_s: int, log_path: Path, secrets: list[str], popen=subprocess.Popen) -> RunOutcome`: creates `log_path.parent` and `cwd`, writes `# argv: <masked>\n# cwd: <cwd>\n`, runs `popen(argv, cwd=cwd, env=env, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, text=True)` with `communicate(timeout=timeout_s)`; on `TimeoutExpired`: `terminate()`, `wait(5)` else `kill()`, then `communicate()`, `timed_out=True`; appends `--- stdout ---` and `--- stderr ---` sections (masked) to the log; `FileNotFoundError` becomes `exit_code=None`, `stderr_tail="<argv[0]>: not found"`; never raises on a non-zero exit.
- `tests/bin/fake-claude`: controlled by env; mode = contents of the file named by `FAKE_CLAUDE_MODE_FILE` when set, else `FAKE_CLAUDE_MODE`, default `ok`. Modes: `ok` prints one JSON result line (`type result`, `subtype success`, `is_error false`, `result` from `FAKE_CLAUDE_RESULT` or `"done"`, `structured_output` parsed from `FAKE_CLAUDE_STRUCTURED` when set, `session_id "fake-sess"`, `total_cost_usd 0.0123`, `usage {input_tokens: 100, output_tokens: 20, cache_creation_input_tokens: 5, cache_read_input_tokens: 7}`, `num_turns 2`, `duration_ms 345`); `sleep` sleeps 60 s; `garbage` prints `not json` to stdout, `boom` to stderr, exits 1; `budget` prints a result with `subtype error_max_budget_usd`, `is_error true`, `result "Reached budget"`, exits 1; `mcp` reads the `--mcp-config` argument, POSTs `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"set_project_summary","arguments":{"project": $FAKE_CLAUDE_PROJECT, "summary_md": "# Fresh summary", "intent": "regenerate summary"}}}` to its url with its Authorization header and `Accept: application/json, text/event-stream` via `urllib.request`, then behaves like `ok`. With `--version` as the only argument it prints `2.1.309 (Claude Code)`. When `FAKE_CLAUDE_ARGV_FILE` is set it writes `json.dumps(sys.argv[1:])` there; when `FAKE_CLAUDE_ENV_FILE` is set it writes `json.dumps(dict(os.environ))` there.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runner_argv.py
import json
from pathlib import Path
import pytest
from trajectree.runner.argv import claude_argv, codex_argv, mask, run_env
from trajectree.runner.spec import MissingCredentials, RunSpec, ToolPolicy

def spec(**over):
    base = dict(run_id="r1", kind="ask", runner="claude", backend="claude", model=None, prompt="Q?", system_prompt=None,
                cwd=Path("/tmp/w"), tools=ToolPolicy(tools=[], allowed=["mcp__trajectree__*"]), usd_cap=0.5, timeout_s=120,
                mcp_url="http://127.0.0.1:8742/mcp", mcp_token="trj_secret", json_schema=None)
    return RunSpec(**{**base, **over})

def test_claude_argv_fixed_shape():
    s = spec(model="sonnet", json_schema={"type": "object"}, system_prompt="Be brief.",
             tools=ToolPolicy(tools=["Read", "Grep", "Glob", "Bash"], allowed=["Read", "Grep", "Glob", "Bash(git log *)", "mcp__trajectree__*"]))
    argv = claude_argv(s, binary="claude")
    assert argv[:5] == ["claude", "-p", "Q?", "--output-format", "json"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk" and argv[argv.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in argv
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert cfg["mcpServers"]["trajectree"] == {"type": "http", "url": "http://127.0.0.1:8742/mcp", "headers": {"Authorization": "Bearer trj_secret"}}
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,Bash"
    i = argv.index("--allowedTools")
    assert argv[i + 1:i + 6] == ["Read", "Grep", "Glob", "Bash(git log *)", "mcp__trajectree__*"]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.5" and argv[argv.index("--model") + 1] == "sonnet"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}
    assert argv[argv.index("--append-system-prompt") + 1] == "Be brief." and "--max-turns" not in argv

def test_claude_argv_no_tools_and_optional_flags_absent():
    argv = claude_argv(spec(), binary="/opt/claude")
    assert argv[argv.index("--tools") + 1] == "" and "--model" not in argv and "--json-schema" not in argv and "--append-system-prompt" not in argv

def test_codex_argv():
    argv = codex_argv(spec(runner="codex", backend="codex", model="gpt-5", system_prompt="Sys."), binary="codex",
                      schema_path=Path("/tmp/s.json"), last_message_path=Path("/tmp/last.md"))
    assert argv[:3] == ["codex", "exec", "--json"] and "--ephemeral" in argv and argv[argv.index("--sandbox") + 1] == "read-only"
    assert 'mcp_servers.trajectree.url="http://127.0.0.1:8742/mcp"' in argv
    assert 'mcp_servers.trajectree.bearer_token_env_var="TRAJECTREE_RUN_TOKEN"' in argv
    assert argv[argv.index("-o") + 1] == "/tmp/last.md" and argv[argv.index("--output-schema") + 1] == "/tmp/s.json"
    assert argv[-1] == "Sys.\n\nQ?" and argv[argv.index("-m") + 1] == "gpt-5"

def test_run_env_is_built_from_scratch(settings):
    base = {"PATH": "/usr/bin", "TRAJECTREE_DATA_DIR": "/data", "ANTHROPIC_API_KEY": "leak", "LANG": "C.UTF-8", "HTTPS_PROXY": "http://p"}
    s = settings.model_copy(update={"run_env_passthrough": ["HTTPS_PROXY"]})
    env = run_env(spec(), s, base=base, oauth_token="oat")
    assert env["PATH"] == "/usr/bin" and env["LANG"] == "C.UTF-8" and env["TERM"] == "dumb" and env["HTTPS_PROXY"] == "http://p"
    assert env["CLAUDE_CONFIG_DIR"] == str(s.claude_config_dir) and env["HOME"] == str(s.run_home)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oat" and env["DISABLE_AUTOUPDATER"] == "1"
    assert "TRAJECTREE_DATA_DIR" not in env and "ANTHROPIC_API_KEY" not in env and "TRAJECTREE_RUN_TOKEN" not in env

def test_run_env_ccx_and_codex(settings):
    prof = settings.backends_cfg_dir / "kimi"
    prof.mkdir(parents=True)
    (prof / "settings.json").write_text('{"env": {"ANTHROPIC_AUTH_TOKEN": "k"}}')
    env = run_env(spec(runner="ccx:kimi", backend="kimi"), settings, base={"PATH": "/bin"}, oauth_token=None)
    assert env["CLAUDE_CONFIG_DIR"] == str(prof) and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    env = run_env(spec(runner="codex", backend="codex"), settings, base={"PATH": "/bin"}, oauth_token=None)
    assert env["TRAJECTREE_RUN_TOKEN"] == "trj_secret" and env["CODEX_HOME"] == str(settings.codex_home)

def test_run_env_missing_credentials(settings):
    with pytest.raises(MissingCredentials, match="trajectree login"):
        run_env(spec(), settings, base={"PATH": "/bin"}, oauth_token=None)
    with pytest.raises(MissingCredentials, match="backends sync"):
        run_env(spec(runner="ccx:nope", backend="nope"), settings, base={"PATH": "/bin"}, oauth_token=None)

def test_mask():
    assert mask(["--mcp-config", '{"Authorization": "Bearer trj_s"}', "x"], ["trj_s", ""]) == ["--mcp-config", '{"Authorization": "Bearer ***"}', "x"]
```

```python
# tests/test_runner_parse.py
import json
import pytest
from trajectree.runner.parse import parse_claude_json, parse_codex

RESULT = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 345, "num_turns": 2, "result": "Answer.",
          "session_id": "abc", "total_cost_usd": 0.0123, "structured_output": {"answer": "A", "proposals": []},
          "usage": {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 7}}

def test_parse_success_picks_last_result_line():
    r = parse_claude_json('{"type":"system","subtype":"init"}\n' + json.dumps(RESULT) + "\n")
    assert r.ok and r.result_text == "Answer." and r.structured == {"answer": "A", "proposals": []}
    assert (r.usage.tokens_in, r.usage.tokens_out, r.usage.tokens_cache) == (100, 20, 12)
    assert r.est_usd == 0.0123 and r.turns == 2 and r.duration_ms == 345 and r.session_id == "abc" and r.error is None

def test_parse_budget_error():
    r = parse_claude_json(json.dumps({**RESULT, "subtype": "error_max_budget_usd", "is_error": True, "result": "Reached budget"}))
    assert not r.ok and r.subtype == "error_max_budget_usd" and r.error == "Reached budget" and r.est_usd == 0.0123

def test_parse_garbage_is_failed():
    with pytest.raises(ValueError, match="no result object"):
        parse_claude_json('not json\n{"type": "assistant"}\n')
    with pytest.raises(ValueError):
        parse_claude_json("")

def test_parse_codex():
    events = '{"type":"turn.completed","usage":{"input_tokens":50,"output_tokens":9}}\n{"type":"other"}\nnot json\n'
    r = parse_codex(events, '{"answer": "A", "proposals": []}')
    assert r.ok and r.structured == {"answer": "A", "proposals": []} and r.usage.tokens_in == 50 and r.usage.tokens_out == 9
    assert not parse_codex("", None).ok
```

```python
# tests/test_runner_launch.py
import json, os, time
from pathlib import Path
from trajectree.runner.launch import launch
FAKE = str(Path(__file__).parent / "bin" / "fake-claude")

def test_launch_captures_and_masks(tmp_path):
    log = tmp_path / "r.log"
    out = launch([FAKE, "-p", "hi", "--mcp-config", '{"h": "Bearer trj_x"}'], env={**os.environ, "FAKE_CLAUDE_MODE": "ok"},
                 cwd=tmp_path / "w", timeout_s=20, log_path=log, secrets=["trj_x"])
    assert out.exit_code == 0 and not out.timed_out and json.loads(out.stdout.strip().splitlines()[-1])["subtype"] == "success"
    text = log.read_text()
    assert "trj_x" not in text and "Bearer ***" in text and "--- stdout ---" in text and out.duration_ms >= 0

def test_launch_kills_on_timeout(tmp_path):
    t0 = time.monotonic()
    out = launch([FAKE], env={**os.environ, "FAKE_CLAUDE_MODE": "sleep"}, cwd=tmp_path, timeout_s=1, log_path=tmp_path / "r.log", secrets=[])
    assert out.timed_out and time.monotonic() - t0 < 10

def test_launch_missing_binary(tmp_path):
    out = launch(["/definitely/missing/bin"], env={"PATH": "/bin"}, cwd=tmp_path, timeout_s=5, log_path=tmp_path / "r.log", secrets=[])
    assert out.exit_code is None and "not found" in out.stderr_tail

def test_fake_claude_garbage_and_version(tmp_path):
    out = launch([FAKE], env={**os.environ, "FAKE_CLAUDE_MODE": "garbage"}, cwd=tmp_path, timeout_s=5, log_path=tmp_path / "r.log", secrets=[])
    assert out.exit_code == 1 and "boom" in out.stderr_tail
    out = launch([FAKE, "--version"], env={**os.environ}, cwd=tmp_path, timeout_s=5, log_path=tmp_path / "v.log", secrets=[])
    assert out.stdout.startswith("2.1.309")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_runner_argv.py tests/test_runner_parse.py tests/test_runner_launch.py -q`
Expected: FAIL (ImportError, missing script).

- [ ] **Step 3: Implement the four modules and the fake CLI** per Interfaces; `chmod +x tests/bin/fake-claude`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_runner_argv.py tests/test_runner_parse.py tests/test_runner_launch.py -q`
Expected: PASS (the timeout test takes about one second).

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/runner tests/test_runner_argv.py tests/test_runner_parse.py tests/test_runner_launch.py
git add --chmod=+x tests/bin/fake-claude
git commit -m "feat(runner): run spec, headless argv and env, subprocess launch with timeout, result parsing, fake CLI"
```

---

### Task 5: Run kinds, prompts, proposal schema, run planning

**Files:**
- Create: `src/trajectree/runner/prompts/__init__.py` (empty), `src/trajectree/runner/prompts/summarize.md`, `src/trajectree/runner/prompts/ask.md`, `src/trajectree/runner/prompts/quick_add.md`, `src/trajectree/runner/kinds.py`
- Modify: `src/trajectree/repo/projects.py` (`resolve_project_ref`, `unknown_project`)
- Test: `tests/test_runner_kinds.py`

**Interfaces:**
- Consumes: `build_context_pack`, `to_local`, `list_resources`, `Scope`, `ToolPolicy`, `Run`.
- `repo/projects.py`: `resolve_project_ref(conn, ref: str) -> Project` (slug first, then id; raises `UnknownProject`); `unknown_project(conn, name) -> UnknownProject`.
- `runner/kinds.py`:
  - Request models: `SummarizeRequest(project: str, runner: str | None = None, model: str | None = None)`; `AskRequest(question: str = Field(min_length=1, max_length=4000), projects: list[str] | None = None, start: AwareDatetime | None = None, end: AwareDatetime | None = None, token_budget: int = Field(4000, ge=200, le=100_000), runner: str | None = None, model: str | None = None)`; `QuickAddRequest(text: str = Field(min_length=1, max_length=4000), project: str | None = None, runner: str | None = None, model: str | None = None)`; `RunRequest = SummarizeRequest | AskRequest | QuickAddRequest`; `KIND_OF = {SummarizeRequest: "summarize", AskRequest: "ask", QuickAddRequest: "quick_add"}`; `request_from_props(kind: RunKind, data: dict) -> RunRequest`.
  - `PROPOSAL_SCHEMA: dict`: `{"type": "object", "additionalProperties": false, "required": ["answer", "proposals"], "properties": {"answer": {"type": "string"}, "proposals": {"type": "array", "items": {"type": "object", "additionalProperties": false, "required": ["kind", "project", "title", "reason"], "properties": {"kind": {"enum": ["event", "todo"]}, "project": {"type": "string"}, "title": {"type": "string"}, "reason": {"type": "string"}, "event_kind": {"type": "string"}, "when": {"type": "string"}, "due": {"type": "string"}, "body_md": {"type": "string"}, "status": {"enum": ["happened", "planned", "suggested"]}}}}}}`.
  - `SUMMARIZE_SYSTEM = "You are updating the owner's project tracker. Write through the trajectree MCP tools only: exactly one set_project_summary call for project `$project_slug`, nothing else; anything else you notice goes into `proposals`."`; `READ_ONLY_SYSTEM = "You are answering from the owner's project tracker. You have read-only access; put anything worth recording into `proposals` instead of writing."`.
  - `ensure_prompts(settings) -> None`: creates `prompts_dir` (parents) and copies each packaged `<kind>.md` that is missing (`importlib.resources.files("trajectree.runner.prompts")`); `load_prompt(settings, kind) -> str`; `render_prompt(template: str, vars: Mapping[str, str]) -> str` = `string.Template(template).safe_substitute(vars)`.
  - `runner_parts(runner: str) -> tuple[str, str]`: `"claude"` → `("claude", "claude")`, `"ccx:kimi"` → `("ccx:kimi", "kimi")`, `"codex"` → `("codex", "codex")`; `ValueError(f"invalid runner '{runner}'")` otherwise.
  - `class RunPlan(BaseModel)`: `prompt: str`, `system_prompt: str`, `cwd: Path`, `tools: ToolPolicy`, `json_schema: dict`, `scope: Scope`.
  - `plan_run(conn, settings, *, run: Run, request: RunRequest, now: datetime) -> RunPlan` (the caller holds the lock; prompts already seeded):
    - summarize: project = `resolve_project_ref(conn, run.project_id or request.project)`; `since = project.summary_at or now - 90 days`; `pack = build_context_pack(conn, project_ids=[project.id], t_from=since, t_to=now + 30 days, token_budget=6000)`; vars `project_title`, `project_slug`, `previous_summary` (`project.summary_md` or `_none yet_`), `context_pack` (`pack.markdown`), `since` (`YYYY-MM-DD`), `today`; `system_prompt = render_prompt(SUMMARIZE_SYSTEM, {"project_slug": slug})`; `tools = ToolPolicy(tools=["Read", "Grep", "Glob", "Bash"], allowed=["Read", "Grep", "Glob", "Bash(git log *)", "mcp__trajectree__*"])`; `cwd` = `to_local(settings, locator)` of the project's first `folder` resource when that directory exists, else `settings.runs_dir / run.id`; `scope = Scope(projects=[project.id], tools=["set_project_summary"], write=True)`.
    - ask: project ids = resolved `request.projects` (each via `resolve_project_ref`) or all active projects; `t_from = request.start or now - 90 days`, `t_to = request.end or now + 30 days`; `pack = build_context_pack(..., token_budget=request.token_budget, question=request.question)`; vars `question`, `context_pack`, `projects` (comma-joined slugs), `today`; `system_prompt = READ_ONLY_SYSTEM`; `tools = ToolPolicy(tools=[], allowed=["mcp__trajectree__*"])`; `cwd = settings.runs_dir / run.id`; `scope = Scope(write=False)`.
    - quick_add: vars `text`, `today`, `projects` (one line per active project: `- <slug>: <title>`), `default_project` (slug of `resolve_project_ref(request.project)` when given, else the first active project's slug, else `inbox`); same system prompt, tools, cwd and scope as ask.
    - every plan: `json_schema = PROPOSAL_SCHEMA`.
- Prompt files (required content, wording is the implementer's):
  - `summarize.md`: states the task (rewrite the summary of `$project_title` (`$project_slug`) as of `$today`, covering activity since `$since`), shows `$previous_summary` and `$context_pack` under headings, requires 5 to 15 lines of Markdown with status, recent progress, open questions and next steps, requires calling `set_project_summary` for `$project_slug` with intent `regenerate summary`, requires unrecorded events or follow-ups to go into `proposals` (not into writes), and `answer` to be one line saying what changed.
  - `ask.md`: answer `$question` from `$context_pack` only, cite event ids in parentheses, say plainly when the pack lacks the information, put follow-ups worth recording into `proposals` with a `reason` each.
  - `quick_add.md`: parse `$text` (written on `$today`) into zero or more proposals; events for things that happened or are planned, todos for things owed; dates as ISO 8601 with a `Z` suffix relative to `$today`; choose `project` from the list `$projects`, default `$default_project`; `answer` is one sentence saying what was understood.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runner_kinds.py
from datetime import datetime, timezone
import pytest
from trajectree.auth.scopes import Scope
from trajectree.models import ProjectIn, ProjectPatch, ResourceIn
from trajectree.repo import UnknownProject
from trajectree.repo.projects import create_project, update_project
from trajectree.repo.resources import upsert_resource
from trajectree.repo.runs import create_run
from trajectree.runner.kinds import (AskRequest, PROPOSAL_SCHEMA, QuickAddRequest, SummarizeRequest, ensure_prompts, load_prompt,
                                     plan_run, render_prompt, request_from_props, runner_parts)
UTC = timezone.utc
NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)

def test_ensure_prompts_seeds_once(settings):
    ensure_prompts(settings)
    p = settings.prompts_dir / "summarize.md"
    assert p.exists() and "$context_pack" in p.read_text() and "set_project_summary" in p.read_text()
    p.write_text("edited $project_slug")
    ensure_prompts(settings)
    assert load_prompt(settings, "summarize") == "edited $project_slug"
    assert "$question" in load_prompt(settings, "ask") and "$text" in load_prompt(settings, "quick_add")

def test_render_prompt_leaves_unknown_and_dollars():
    assert render_prompt("a $x b $unknown $$5", {"x": "1"}) == "a 1 b $unknown $5"

def test_runner_parts():
    assert runner_parts("claude") == ("claude", "claude") and runner_parts("ccx:kimi") == ("ccx:kimi", "kimi") and runner_parts("codex") == ("codex", "codex")
    with pytest.raises(ValueError, match="invalid runner"):
        runner_parts("ccx:../x")

def test_plan_summarize(ledger, human, settings, tmp_path):
    ensure_prompts(settings)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    update_project(ledger, human, p.id, ProjectPatch(summary_md="Old summary"))
    folder = tmp_path / "proj"
    folder.mkdir()
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=str(folder)))
    run = create_run(ledger.conn, kind="summarize", runner="claude", backend="claude", model=None, project_id=p.id)
    plan = plan_run(ledger.conn, settings, run=run, request=SummarizeRequest(project="a"), now=NOW)
    assert "Old summary" in plan.prompt and "(a)" in plan.prompt and "# Trajectree context" in plan.prompt and "$" not in plan.system_prompt
    assert plan.scope == Scope(projects=[p.id], tools=["set_project_summary"], write=True)
    assert plan.tools.tools == ["Read", "Grep", "Glob", "Bash"] and "Bash(git log *)" in plan.tools.allowed
    assert plan.cwd == folder and plan.json_schema == PROPOSAL_SCHEMA

def test_plan_ask_and_unknown_project(ledger, human, settings):
    ensure_prompts(settings)
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    run = create_run(ledger.conn, kind="ask", runner="claude", backend="claude", model=None, project_id=None)
    plan = plan_run(ledger.conn, settings, run=run, request=AskRequest(question="What is blocked?"), now=NOW)
    assert "What is blocked?" in plan.prompt and plan.tools.tools == [] and plan.scope == Scope(write=False) and plan.cwd == settings.runs_dir / run.id
    with pytest.raises(UnknownProject, match="known projects: a"):
        plan_run(ledger.conn, settings, run=run, request=AskRequest(question="q", projects=["zzz"]), now=NOW)

def test_plan_quick_add_lists_projects(ledger, human, settings):
    ensure_prompts(settings)
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    create_project(ledger, human, ProjectIn(slug="b", title="B", kind="tooling"))
    run = create_run(ledger.conn, kind="quick_add", runner="claude", backend="claude", model=None, project_id=None)
    plan = plan_run(ledger.conn, settings, run=run, request=QuickAddRequest(text="Met Bob, send slides by Friday", project="b"), now=NOW)
    assert "- a: A" in plan.prompt and "- b: B" in plan.prompt and "send slides" in plan.prompt and "2026-01-10" in plan.prompt
    assert plan.prompt.index("b") >= 0 and plan.scope.write is False

def test_request_from_props_roundtrip():
    req = AskRequest(question="q", projects=["a"])
    assert request_from_props("ask", req.model_dump(mode="json")) == req
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_runner_kinds.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement** per Interfaces; `pyproject.toml` needs no change (hatch includes package data under `src/trajectree`; verify with `uv run python -c "import importlib.resources as r; print(r.files('trajectree.runner.prompts').joinpath('ask.md').read_text()[:20])"`).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_runner_kinds.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/runner src/trajectree/repo/projects.py tests/test_runner_kinds.py
git commit -m "feat(runner): run kinds, editable prompts, proposal schema, run planning"
```

---

### Task 6: RunService: queue worker, caps, token lifecycle, execution, proposals

**Files:**
- Create: `src/trajectree/runner/service.py`
- Modify: `tests/conftest.py` (settings fixture: `cfg_dir=tmp_path / "cfg"`, `claude_bin=<tests/bin/fake-claude>`, `run_env_passthrough=["FAKE_CLAUDE_MODE", "FAKE_CLAUDE_MODE_FILE", "FAKE_CLAUDE_STRUCTURED", "FAKE_CLAUDE_RESULT", "FAKE_CLAUDE_PROJECT", "FAKE_CLAUDE_ARGV_FILE", "FAKE_CLAUDE_ENV_FILE"]`; new fixture `logged_in(settings)` that calls `store_oauth_token(settings, "fake-oauth")`)
- Test: `tests/test_run_service.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4, 5; `mint_token`, `revoke_token`, `resolve_token`; `events.create_event`, `todos.create_todo`; `parse_iso`.
- `class RunService`:
  - `__init__(self, ledger, settings, *, launch=launch, popen=subprocess.Popen, now=utcnow, base_env: Mapping[str, str] | None = None, version=cli_version)`; `base_env` None means `os.environ` read at each execute.
  - `enqueue(self, request: RunRequest) -> Run`: under one lock: `caps = get_caps(conn)`; `RunsPaused` if `caps.paused`; `runner = request.runner or settings.default_runner`, `runner_parts` (ValueError propagates); project validation: summarize resolves `request.project` (`UnknownProject`), ask resolves each of `request.projects`, quick_add resolves `request.project` when given; `create_run(conn, kind=KIND_OF[type(request)], runner=runner, backend=backend, model=request.model, project_id=<summarize project id or None>, props={"request": request.model_dump(mode="json")})`.
  - `execute(self, run_id: str) -> Run`: one run's lifecycle, synchronous:
    1. Under the lock: load run (`NotFound`), `caps`; if `spent_usd(conn, day=today) + caps.usd_per_run > caps.usd_per_day` → `update_run(status="capped", error=f"daily cap {caps.usd_per_day} USD reached", finished=now)` and return; `update_run(status="running", started=now)`; `ensure_prompts`; `request = request_from_props(run.kind, run.props["request"])`; `plan = plan_run(...)` (`UnknownProject`/`NotFound` → failed with the message, return); `token_id, secret = mint_token(conn, label=f"run:{run_id}", scope=plan.scope, actor=<Global Constraints actor; tool_version=self.version(binary)>, ttl=timedelta(seconds=caps.timeout_s + 60))`; `update_run(token_id=token_id)`; build `RunSpec(usd_cap=caps.usd_per_run, timeout_s=caps.timeout_s, mcp_url=f"http://127.0.0.1:{settings.port}/mcp", mcp_token=secret, json_schema=plan.json_schema, ...)`; `env = run_env(spec, settings, base=self.base_env or os.environ, oauth_token=load_oauth_token(settings))` (`MissingCredentials` → failed with its message; token revoked; return).
    2. Outside the lock: `ensure_cfg_dirs`; `run_dir = settings.runs_dir / run_id` (mkdir); for codex write `run_dir / "schema.json"` and use `run_dir / "last.md"`; `argv` per runner (`claude_argv` for claude and ccx, binary `settings.claude_bin`; `codex_argv` with `settings.codex_bin`); `outcome = self.launch(argv, env=env, cwd=plan.cwd, timeout_s=caps.timeout_s, log_path=settings.runs_dir / f"{run_id}.log", secrets=[secret, oauth_token or ""], popen=self.popen)`.
    3. Under the lock, in a `try` whose `finally` calls `revoke_token(conn, token_id)`: `timed_out` → failed `timeout after {caps.timeout_s}s`; `exit_code is None` → failed with `stderr_tail`; else parse (`parse_claude_json(outcome.stdout)` or `parse_codex(outcome.stdout, last.md text or None)`), `ValueError` → failed with `(outcome.stderr_tail or outcome.stdout)[:500]`; otherwise `status = "ok"` when `parsed.ok`, `"capped"` when `parsed.subtype == "error_max_budget_usd"`, else `"failed"` with `parsed.error`; store `tokens_*`, `est_usd`, `turns`, `duration_ms = outcome.duration_ms`, `session_id`, `result_md = structured["answer"] if that is a string else parsed.result_text`, `proposals = [Proposal.model_validate(x) for x in structured["proposals"] if valid]` (invalid items dropped, non-list → `[]`), `log_path`, `finished`.
  - `async def worker(self, stop: asyncio.Event, *, poll_s: float = 0.5) -> None`: until `stop`: `run = await asyncio.to_thread(self._next_queued)`; if any, `await asyncio.to_thread(self.execute, run.id)` inside `try/except Exception: log.exception(...)`, and on an exception mark the run `failed` with `str(exc)[:500]`; else wait `poll_s` or `stop`. One run at a time.
  - `apply_proposal(self, run_id: str, index: int, *, actor: Actor) -> Run`: under one lock: run (`NotFound`), `0 <= index < len(proposals)` else `NotFound(f"proposal {index} not found")`, `AlreadyApplied` when `applied_txn`; project: `resolve_project_ref(conn, proposal.project)` (`UnknownProject`), except that a `todo` whose project is `inbox` and no such project exists gets `project_id=None`; `who = actor.model_copy(update={"proposed_by_run": run.id, "model": run.model, "backend": run.backend})`; event → `events.create_event(ledger, who, EventIn(project_id, kind=event_kind, title, body_md, t_start=parse_iso(when) if when else now, status, source="agent"), intent=reason)` (bad `when` → `ValueError`), txn id from `created.created_txn`; todo → `todos.create_todo(ledger, who, TodoIn(project_id, title, body_md, due=parse_iso(due) if due else None), intent=reason)`, txn id = `ledger.list_txns(limit=1)[0].id` (same lock, so it is this write); `update_run(proposals=[... with applied_txn, applied_id])`.
  - `today(self) -> str` from `self.now()` in UTC.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_run_service.py
import asyncio, json, os
from datetime import timezone
import pytest
from trajectree.auth.scopes import Scope
from trajectree.auth.tokens import resolve_token
from trajectree.models import Actor, Caps, ProjectIn, Proposal
from trajectree.repo import NotFound, UnknownProject
from trajectree.repo.caps import set_caps
from trajectree.repo.events import list_events
from trajectree.repo.projects import create_project
from trajectree.repo.runs import get_run, update_run
from trajectree.runner.kinds import AskRequest, QuickAddRequest, SummarizeRequest
from trajectree.runner.service import RunService
from trajectree.runner.spec import AlreadyApplied, RunsPaused
import trajectree.runner.service as service_mod

STRUCTURED = json.dumps({"answer": "A", "proposals": [
    {"kind": "event", "project": "a", "title": "Decided X", "reason": "decision taken", "event_kind": "decision", "when": "2026-01-05T10:00:00Z"},
    {"kind": "todo", "project": "a", "title": "Email Bob", "reason": "follow-up owed", "due": "2026-01-20T00:00:00Z"},
    {"kind": "bogus"}]})

def svc(ledger, settings, mode="ok", **extra):
    env = {"PATH": os.environ["PATH"], "FAKE_CLAUDE_MODE": mode, "FAKE_CLAUDE_STRUCTURED": STRUCTURED, **extra}
    return RunService(ledger, settings, base_env=env)

def test_ask_run_ok_records_usage_and_proposals(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = svc(ledger, settings)
    run = s.enqueue(AskRequest(question="q"))
    assert run.status == "queued" and run.kind == "ask" and run.runner == "claude"
    done = s.execute(run.id)
    assert done.status == "ok" and done.est_usd == 0.0123 and (done.tokens_in, done.tokens_out, done.tokens_cache) == (100, 20, 12)
    assert done.turns == 2 and done.session_id == "fake-sess" and done.result_md == "A"
    assert [p.title for p in done.proposals] == ["Decided X", "Email Bob"]   # the bogus item was dropped
    assert done.token_id and resolve_token(ledger.conn, "trj_x") is None
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0
    log = (settings.runs_dir / f"{run.id}.log").read_text()
    assert "Bearer ***" in log and "fake-oauth" not in log and "trj_" not in log

def test_summarize_mints_scoped_token_with_run_actor(ledger, human, settings, logged_in, monkeypatch):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    seen = {}
    real = service_mod.mint_token
    def spy(conn, **kw):
        seen.update(kw)
        return real(conn, **kw)
    monkeypatch.setattr(service_mod, "mint_token", spy)
    s = svc(ledger, settings)
    run = s.execute(s.enqueue(SummarizeRequest(project="a")).id)
    assert run.status == "ok" and run.project_id == p.id
    assert seen["scope"] == Scope(projects=[p.id], tools=["set_project_summary"], write=True)
    a = seen["actor"]
    assert (a.kind, a.ref, a.run_id, a.tool, a.backend, a.tool_version) == ("agent", run.id, run.id, "claude", "claude", "2.1.309")

def test_run_failure_revokes_token(ledger, human, settings, logged_in):
    s = svc(ledger, settings, mode="garbage")
    run = s.execute(s.enqueue(AskRequest(question="q")).id)
    assert run.status == "failed" and "boom" in run.error and run.est_usd == 0 and run.tokens_in == 0
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0 and (settings.runs_dir / f"{run.id}.log").exists()

def test_budget_subtype_is_capped(ledger, human, settings, logged_in):
    run = svc(ledger, settings, mode="budget").execute(svc(ledger, settings).enqueue(AskRequest(question="q")).id)
    assert run.status == "capped" and run.est_usd == 0.0123

def test_daily_cap_marks_capped_without_launch(ledger, human, settings, logged_in):
    set_caps(ledger.conn, Caps(usd_per_run=0.5, usd_per_day=1.0))
    s = svc(ledger, settings)
    first = update_run(ledger.conn, s.enqueue(AskRequest(question="q")).id, status="ok", est_usd=0.6, started=s.now())
    run = s.execute(s.enqueue(AskRequest(question="q")).id)
    assert run.status == "capped" and "daily cap" in run.error and run.token_id is None
    assert not (settings.runs_dir / f"{run.id}.log").exists()

def test_paused_enqueue_raises(ledger, settings):
    set_caps(ledger.conn, Caps(paused=True))
    with pytest.raises(RunsPaused):
        svc(ledger, settings).enqueue(AskRequest(question="q"))

def test_missing_credentials_fails_fast(ledger, settings):
    run = svc(ledger, settings).execute(svc(ledger, settings).enqueue(AskRequest(question="q")).id)
    assert run.status == "failed" and "trajectree login" in run.error and not (settings.runs_dir / f"{run.id}.log").exists()
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0

def test_invalid_runner_and_unknown_project_rejected_at_enqueue(ledger, human, settings):
    with pytest.raises(ValueError, match="invalid runner"):
        svc(ledger, settings).enqueue(AskRequest(question="q", runner="evil; rm -rf"))
    with pytest.raises(UnknownProject):
        svc(ledger, settings).enqueue(SummarizeRequest(project="zzz"))

@pytest.mark.anyio
async def test_worker_continues_after_timeout(ledger, human, settings, logged_in, tmp_path):
    set_caps(ledger.conn, Caps(timeout_s=1))
    mode = tmp_path / "mode"
    mode.write_text("sleep")
    s = svc(ledger, settings, FAKE_CLAUDE_MODE_FILE=str(mode))
    r1 = s.enqueue(AskRequest(question="slow"))
    r2 = s.enqueue(AskRequest(question="fast"))
    stop = asyncio.Event()
    task = asyncio.create_task(s.worker(stop, poll_s=0.05))
    for _ in range(200):
        await asyncio.sleep(0.05)
        if get_run(ledger.conn, r1.id).status not in ("queued", "running"):
            mode.write_text("ok")
        if get_run(ledger.conn, r2.id).status == "ok":
            break
    stop.set()
    await asyncio.wait_for(task, 5)
    assert get_run(ledger.conn, r1.id).status == "failed" and get_run(ledger.conn, r1.id).error == "timeout after 1s"
    assert get_run(ledger.conn, r2.id).status == "ok"

def test_accept_proposal_creates_human_txn_with_provenance(ledger, human, settings, logged_in):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = svc(ledger, settings)
    run = s.execute(s.enqueue(QuickAddRequest(text="x")).id)
    web = Actor(kind="human", ref="owner", tool="web")
    after = s.apply_proposal(run.id, 0, actor=web)
    ev = list_events(ledger.conn, project_ids=[p.id])[0]
    assert ev.title == "Decided X" and ev.kind == "decision" and ev.source == "agent" and after.proposals[0].applied_id == ev.id
    t = ledger.get_txn(after.proposals[0].applied_txn)
    assert (t.actor_kind, t.proposed_by_run, t.model, t.backend, t.review, t.intent, t.tool) == ("human", run.id, None, "claude", "accepted", "decision taken", "web")
    after = s.apply_proposal(run.id, 1, actor=web)
    assert after.proposals[1].applied_txn is not None and ledger.get_txn(after.proposals[1].applied_txn).intent == "follow-up owed"

def test_accept_proposal_twice_is_409(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = svc(ledger, settings)
    run = s.execute(s.enqueue(QuickAddRequest(text="x")).id)
    s.apply_proposal(run.id, 0, actor=human)
    n = len(ledger.list_txns())
    with pytest.raises(AlreadyApplied):
        s.apply_proposal(run.id, 0, actor=human)
    assert len(ledger.list_txns()) == n
    with pytest.raises(NotFound):
        s.apply_proposal(run.id, 7, actor=human)

def test_accept_unknown_slug_writes_nothing(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="b", title="B", kind="research"))   # proposals name "a"
    s = svc(ledger, settings)
    run = s.execute(s.enqueue(QuickAddRequest(text="x")).id)
    n = len(ledger.list_txns())
    with pytest.raises(UnknownProject, match="known projects: b"):
        s.apply_proposal(run.id, 0, actor=human)
    assert len(ledger.list_txns()) == n
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_run_service.py -q`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement `RunService` and the conftest changes** per Interfaces.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/runner/service.py tests/conftest.py tests/test_run_service.py
git commit -m "feat(runner): run service with serial worker, caps, per-run tokens, proposal apply"
```

---

### Task 7: API for runs, ask, quick-add, summaries, usage, caps, runner status, credentials; lifespan worker

**Files:**
- Create: `src/trajectree/api/routes_runs.py`
- Modify: `src/trajectree/api/app.py`
- Test: `tests/test_api_runs.py`

**Interfaces:**
- Response models in `routes_runs.py`: `RunDetail(run: Run, txn_ids: list[str])`; `UsageReport(days: list[UsageDay], by_kind: list[UsageSlice], by_runner: list[UsageSlice], spent_today: float, caps: Caps)`; `CredentialsBody(backend: Literal["claude"] = "claude", oauth_token: str)`; `SummarizeBody(runner: str | None = None, model: str | None = None)`.
- Routes (all under the owner-only `/api` router; `runs: RunService = request.app.state.runs`):
  - `POST /projects/{project_id}/summarize` body `SummarizeBody | None` → 202 `Run` (`runs.enqueue(SummarizeRequest(project=project_id, ...))`).
  - `POST /ask` body `AskRequest` → 202 `Run`; `POST /quick-add` body `QuickAddRequest` → 202 `Run`.
  - `GET /runs?limit=50&kind=&status=&open_proposals=false` → `list[Run]` (`limit` 1..500); `GET /runs/{run_id}` → `RunDetail` (404); `POST /runs/{run_id}/proposals/{index}/accept` → `Run` (actor `actor_from_request`).
  - `GET /usage?days=30` (1..365) → `UsageReport` (`since` = start of the first day); `GET /caps` → `Caps`; `PUT /caps` body `Caps` → `Caps`.
  - `GET /runner/status` → `RunnerStatus` (`runner_status(settings)`); `POST /runner/credentials` body `CredentialsBody` → `{"stored": true}` (`ValueError` → 422); `POST /backends/sync` → `{"synced": list[str]}`.
- `app.py`: `app.state.runs = RunService(ledger, settings)`; the lifespan starts `runs.worker(stop)` as a task (always) and stops it like the ingest task (same `stop` event, `wait_for(task, 10)`); exception handlers: `RunsPaused` → 409 `{"detail": "runs are paused"}`, `AlreadyApplied` → 409 `{"detail": "proposal already applied"}`, `UnknownProject` → 422, `ValueError` from `runs.enqueue` (invalid runner) → 422 (catch in the route and raise `HTTPException(422, str(exc))`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_runs.py
import json, socket, threading, time
import httpx
import pytest
import uvicorn
from tests.test_api_entities import mk_project
from trajectree.api.app import create_app
from trajectree.runner.credentials import store_oauth_token

STRUCTURED = json.dumps({"answer": "Parsed", "proposals": [{"kind": "event", "project": "clmi", "title": "Decided X", "reason": "decision taken", "event_kind": "decision"}]})

def wait_run(client, run_id, deadline=15):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        d = client.get(f"/api/runs/{run_id}").json()
        if d["run"]["status"] not in ("queued", "running"):
            return d
        time.sleep(0.1)
    raise AssertionError("run did not finish")

def test_quick_add_run_and_accept_proposal(client, settings, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", STRUCTURED)
    store_oauth_token(settings, "fake")
    p = mk_project(client)   # slug clmi
    r = client.post("/api/quick-add", json={"text": "we decided X"})
    assert r.status_code == 202 and r.json()["status"] == "queued"
    d = wait_run(client, r.json()["id"])
    assert d["run"]["status"] == "ok" and d["run"]["result_md"] == "Parsed" and d["txn_ids"] == []
    a = client.post(f"/api/runs/{d['run']['id']}/proposals/0/accept")
    assert a.status_code == 200 and a.json()["proposals"][0]["applied_txn"]
    assert client.get("/api/events", params={"projects": p["id"]}).json()[0]["title"] == "Decided X"
    assert client.post(f"/api/runs/{d['run']['id']}/proposals/0/accept").status_code == 409
    assert client.post(f"/api/runs/{d['run']['id']}/proposals/9/accept").status_code == 404
    assert [x["id"] for x in client.get("/api/runs", params={"open_proposals": "true"}).json()] == []
    assert client.get("/api/runs").json()[0]["id"] == d["run"]["id"]

def test_caps_pause_and_usage(client):
    assert client.get("/api/caps").json()["paused"] is False
    assert client.put("/api/caps", json={"usd_per_run": 0.5, "usd_per_day": 2, "timeout_s": 120, "paused": True}).status_code == 200
    assert client.post("/api/ask", json={"question": "q"}).status_code == 409
    assert client.put("/api/caps", json={"usd_per_run": 0.5, "usd_per_day": 2, "timeout_s": 120, "paused": False}).status_code == 200
    u = client.get("/api/usage", params={"days": 7}).json()
    assert len(u["days"]) == 7 and u["caps"]["usd_per_day"] == 2 and u["spent_today"] == 0
    assert client.put("/api/caps", json={"usd_per_run": -1}).status_code == 422
    assert client.post("/api/ask", json={"question": "q", "runner": "evil"}).status_code == 422
    assert client.post("/api/ask", json={"question": "q", "projects": ["zzz"]}).status_code == 422

def test_runner_status_credentials_and_backends_sync(client, settings, tmp_path):
    assert client.get("/api/runner/status").json()["claude"]["authenticated"] is False
    assert client.post("/api/runner/credentials", json={"oauth_token": "  "}).status_code == 422
    assert client.post("/api/runner/credentials", json={"oauth_token": "tok"}).json() == {"stored": True}
    assert client.get("/api/runner/status").json()["claude"]["authenticated"] is True
    assert client.post("/api/backends/sync").json() == {"synced": []}

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def test_summarize_run_end_to_end_over_http(ledger, human, settings, monkeypatch):
    """The fake CLI calls set_project_summary over the real MCP endpoint with its run token."""
    port = free_port()
    s = settings.model_copy(update={"port": port})
    store_oauth_token(s, "fake")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "mcp")
    monkeypatch.setenv("FAKE_CLAUDE_PROJECT", "clmi")
    app = create_app(s, ledger=ledger)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    h = {"Authorization": f"Bearer {app.state.token}"}
    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        p = httpx.post(base + "/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"}, headers=h).json()
        run = httpx.post(base + f"/api/projects/{p['id']}/summarize", json={}, headers=h).json()
        for _ in range(150):
            d = httpx.get(base + f"/api/runs/{run['id']}", headers=h).json()
            if d["run"]["status"] not in ("queued", "running"):
                break
            time.sleep(0.1)
        assert d["run"]["status"] == "ok", d["run"]["error"]
        assert len(d["txn_ids"]) == 1
        t = httpx.get(base + f"/api/ledger/txns/{d['txn_ids'][0]}", headers=h).json()["txn"]
        assert (t["actor_kind"], t["actor_ref"], t["run_id"], t["review"], t["tool"]) == ("agent", run["id"], run["id"], "pending", "claude")
        assert httpx.get(base + "/api/projects", headers=h).json()[0]["summary_md"] == "# Fresh summary"
    finally:
        server.should_exit = True
        thread.join(10)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_api_runs.py -q`
Expected: FAIL (404s / ImportError).

- [ ] **Step 3: Implement the routes, the handlers and the lifespan worker** per Interfaces.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/api tests/test_api_runs.py
git commit -m "feat(api): runs, ask, quick-add, summarize, usage, caps, runner status and credentials; run worker in lifespan"
```

---

### Task 8: Hooks endpoint, session registry, live merge, token API, session ids on MCP writes

**Files:**
- Create: `src/trajectree/api/routes_hooks.py`, `src/trajectree/api/routes_tokens.py`, `src/trajectree/connectors/hooks.py`
- Modify: `src/trajectree/api/app.py`, `src/trajectree/api/routes_timeline.py`, `src/trajectree/api/mcp_mount.py`, `src/trajectree/repo/sessions.py`, `src/trajectree/auth/tokens.py`, `src/trajectree/auth/scopes.py`, `src/trajectree/mcpserver/server.py`, `src/trajectree/cli/shim.py`
- Test: `tests/test_api_hooks.py`, `tests/test_api_tokens.py`, `tests/test_api_mcp.py`, `tests/test_shim.py`

**Interfaces:**
- `connectors/hooks.py`: `HOOK_TOOL = "hooks"`; `handle_hook(conn, payload: Any, *, now: datetime) -> SessionRow | None`: requires `payload` to be a dict with non-empty strings `hook_event_name` and `session_id`, else None; `cwd` must be a non-empty string for `SessionStart` and `Stop`, else None; `project = project_for_path(conn, cwd)`; `SessionStart` → `start_session(..., transcript_path=<str or None>, project_id=<id or None>, source=<str or None>)`; `Stop` → `touch_session`; `SessionEnd` → `end_session(reason=<str or None>)`; any other event → None. Never raises on payload shape.
- `routes_hooks.py`: `require_hook_token(request) -> Principal`: bearer header; owner token → `owner_principal()`; else `resolve_token` under the lock and `scope.allows(HOOK_TOOL, None, write=False)`; otherwise `HTTPException(401)`. `router = APIRouter(prefix="/api/hooks", dependencies=[Depends(require_hook_token)])` registered directly on the app (not through the owner-only router); `POST /claude` is `async def`, reads `await request.body()`, `json.loads` in a `try` (bad JSON → payload None), runs `handle_hook` via `anyio.to_thread.run_sync` under `ledger.locked()`, returns `{}`.
- `routes_tokens.py` (owner-only, appended to `PROTECTED_ROUTERS`): `TokenCreate(label: str = Field(min_length=1, max_length=64), scope: Scope, ttl_s: int | None = Field(None, ge=60), actor_kind: Literal["agent", "session"] = "session")`; `TokenOut(id: str, label: str, scope: Scope, expires_at: AwareDatetime | None, created_at: AwareDatetime)`; `TokenSecret(TokenOut)` adds `secret: str`; `POST /tokens` → `TokenSecret` (actor `Actor(kind=actor_kind, ref=label, tool="token")`); `GET /tokens` → `list[TokenOut]`; `DELETE /tokens/{token_id}` → 204 (404 when unknown); `GET /sessions` → `list[SessionRow]` running within `settings.live_window_s`.
- `auth/tokens.py`: `list_tokens(conn) -> list[dict]` (`id`, `label`, `scope` (parsed), `expires_at`, `created_at`, newest first); `revoke_token` returns `bool` (row existed).
- `repo/sessions.py`: `live_sessions(conn, *, scan_live: Any, shown: set[str] | None, now: datetime, window_s: int) -> list[LiveSession]`: registry rows from `running_sessions(since=now - window_s)` with a `project_id` become `LiveSession(project_id, session_id=id, cwd, title=None, started=started_at, last_seen)`; `scan_live` entries are validated leniently (invalid skipped) and dropped when their `session_id` is already present or in `ended_session_ids`; `shown` filters by project; sorted by `last_seen` descending. `routes_timeline.py` uses it (replacing Task 2's `_live_from_state`).
- `auth/scopes.py`: `owner_principal(session_ref)` → `Actor(kind="session", ref=session_ref or "owner", session_id=session_ref, tool="mcp")`.
- `mcpserver/server.py`: `principal_from_headers(headers, *, owner_token, conn, now: datetime | None = None, live_window_s: int = 900)`: for the owner token without `X-Trajectree-Session` but with `X-Trajectree-Cwd`, `session_for_cwd(conn, cwd=<cwd>, since=now - live_window_s)` supplies the session id. `mcp_mount.owner_or_token_resolver(ledger, token, *, live_window_s)` passes `settings.live_window_s` (update `app.py`).
- `cli/shim.py`: `remote_connector(url, token, cwd, session_id: str | None = None, transport=None)` adds `X-Trajectree-Session` when given; `run_shim` passes `env.get("CLAUDE_CODE_SESSION_ID")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_hooks.py
from tests.test_api_entities import mk_project
from trajectree.repo.state import set_state

def hook(event, sid="s1", cwd="/home/u/proj", **extra):
    return {"session_id": sid, "transcript_path": "/t.jsonl", "cwd": cwd, "hook_event_name": event, **extra}

def test_hook_lifecycle_updates_registry_and_timeline_live(client):
    p = mk_project(client)
    client.post("/api/resources", json={"project_id": p["id"], "kind": "folder", "locator": "/home/u/proj"})
    assert client.post("/api/hooks/claude", json=hook("SessionStart", source="startup")).json() == {}
    rows = client.get("/api/sessions").json()
    assert rows[0]["id"] == "s1" and rows[0]["project_id"] == p["id"] and rows[0]["source"] == "startup"
    tl = client.get("/api/timeline", params={"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"}).json()
    assert [l["session_id"] for l in tl["live"]] == ["s1"]
    client.post("/api/hooks/claude", json=hook("Stop", stop_hook_active=False))
    assert client.get("/api/sessions").json()[0]["ended_at"] is None
    client.post("/api/hooks/claude", json=hook("SessionEnd", reason="other"))
    assert client.get("/api/sessions").json() == []
    tl = client.get("/api/timeline", params={"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"}).json()
    assert tl["live"] == []

def test_hook_token_scope(client, anon):
    good = client.post("/api/tokens", json={"label": "hooks", "scope": {"projects": [], "tools": ["hooks"], "write": False}}).json()["secret"]
    bad = client.post("/api/tokens", json={"label": "run", "scope": {"tools": ["record_event"]}}).json()["secret"]
    assert anon.post("/api/hooks/claude", json=hook("SessionStart"), headers={"Authorization": f"Bearer {good}"}).status_code == 200
    assert anon.get("/api/projects", headers={"Authorization": f"Bearer {good}"}).status_code == 401
    assert anon.post("/api/hooks/claude", json=hook("SessionStart"), headers={"Authorization": f"Bearer {bad}"}).status_code == 401

def test_hook_unknown_cwd_missing_fields_bad_token(client, anon):
    assert client.post("/api/hooks/claude", json=hook("SessionStart", cwd="/nowhere")).status_code == 200
    rows = client.get("/api/sessions").json()
    assert rows[0]["project_id"] is None
    tl = client.get("/api/timeline", params={"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"}).json()
    assert tl["live"] == []
    assert client.post("/api/hooks/claude", json={}).status_code == 200 and client.post("/api/hooks/claude", content=b"not json").status_code == 200
    assert client.post("/api/hooks/claude", json={"hook_event_name": "Stop"}).status_code == 200
    assert len(client.get("/api/sessions").json()) == 1
    assert anon.post("/api/hooks/claude", json=hook("Stop")).status_code == 401
    assert anon.post("/api/hooks/claude", json=hook("Stop"), headers={"Authorization": "Bearer trj_nope"}).status_code == 401

def test_registry_wins_over_scan_and_ended_hides_scan(client, app):
    p = mk_project(client)
    client.post("/api/resources", json={"project_id": p["id"], "kind": "folder", "locator": "/home/u/proj"})
    live = [{"project_id": p["id"], "session_id": sid, "cwd": "/home/u/proj", "title": "T", "started": None, "last_seen": "2026-01-05T12:00:00Z"} for sid in ("s1", "s2")]
    set_state(app.state.ledger.conn, "live_sessions", live)
    client.post("/api/hooks/claude", json=hook("SessionStart", sid="s1"))
    client.post("/api/hooks/claude", json=hook("SessionEnd", sid="s1", reason="other"))
    client.post("/api/hooks/claude", json=hook("SessionStart", sid="s3"))
    tl = client.get("/api/timeline", params={"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"}).json()
    assert sorted(l["session_id"] for l in tl["live"]) == ["s2", "s3"]
```

```python
# tests/test_api_tokens.py
from datetime import timedelta
from trajectree.auth.tokens import mint_token, resolve_token
from trajectree.models import Actor
from trajectree.auth.scopes import Scope

def test_token_create_list_delete(client, app):
    r = client.post("/api/tokens", json={"label": "hooks", "scope": {"projects": [], "tools": ["hooks"], "write": False}})
    assert r.status_code == 200 and r.json()["secret"].startswith("trj_") and r.json()["expires_at"] is None
    listed = client.get("/api/tokens").json()
    assert listed[0]["label"] == "hooks" and "secret" not in listed[0] and listed[0]["scope"]["tools"] == ["hooks"]
    assert client.delete(f"/api/tokens/{r.json()['id']}").status_code == 204
    assert client.get("/api/tokens").json() == [] and client.delete(f"/api/tokens/{r.json()['id']}").status_code == 404
    assert client.post("/api/tokens", json={"label": "", "scope": {}}).status_code == 422

def test_expired_token_is_not_listed_as_live(client, app):
    _, secret = mint_token(app.state.ledger.conn, label="old", scope=Scope(), actor=Actor(kind="agent", ref="x"), ttl=timedelta(seconds=-1))
    assert resolve_token(app.state.ledger.conn, secret) is None
    assert client.get("/api/tokens").json()[0]["label"] == "old"
```

```python
# tests/test_api_mcp.py (append)
def test_owner_mcp_write_carries_session_id_from_header_or_cwd(client):
    p = mk_project(client)
    client.post("/api/resources", json={"project_id": p["id"], "kind": "folder", "locator": "/home/u/proj"})
    client.post("/mcp", json=call("add_note", {"text": "a", "intent": "i"}), headers={**HDR, "X-Trajectree-Session": "sess-9"})
    t = client.get("/api/ledger/txns", params={"review": "pending"}).json()[0]
    assert t["session_id"] == "sess-9" and t["actor_ref"] == "sess-9" and t["actor_kind"] == "session"
    client.post("/api/hooks/claude", json={"session_id": "live-1", "cwd": "/home/u/proj", "hook_event_name": "SessionStart", "transcript_path": "/t"})
    client.post("/mcp", json=call("add_note", {"text": "b", "intent": "i"}), headers={**HDR, "X-Trajectree-Cwd": "/home/u/proj"})
    t = client.get("/api/ledger/txns", params={"review": "pending"}).json()[0]
    assert t["session_id"] == "live-1" and t["actor_ref"] == "live-1"
```

In `tests/test_shim.py`, extend `test_remote_connector_sends_headers` to pass `session_id="sess-1"` and assert `seen[0]["x-trajectree-session"] == "sess-1"` (the existing test records lower-cased headers; keep its pattern).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_api_hooks.py tests/test_api_tokens.py tests/test_api_mcp.py tests/test_shim.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement** per Interfaces.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree tests
git commit -m "feat(hooks): session registry fed by Claude Code http hooks, live merge, token API, session ids on owner MCP writes"
```

---

### Task 9: CLI run commands

**Files:**
- Modify: `src/trajectree/cli/main.py`
- Test: `tests/test_cli_runs.py`

**Interfaces:**
- Module-level `_sleep = time.sleep` and `POLL_S = 2.0` (tests monkeypatch `_sleep`); `main(argv, *, client=None, env=None, runner=None, stdin: TextIO | None = None)` (`stdin` defaults to `sys.stdin`).
- `_wait_run(api, run_id) -> dict`: polls `GET /api/runs/{id}` until `status` not in (`queued`, `running`), sleeping `POLL_S`; returns the `RunDetail` dict.
- `_print_run(detail, out)`: `run <id> <status> · $<est_usd:.4f> · <turns> turns · <duration_ms> ms`; when `txn_ids`: `pending: <n> transaction(s) in the inbox`; when `result_md`: a blank line then the text; proposals numbered from 1 as `  [n] <kind> · <project> · <title> (<reason>)` followed by `accept with: trajectree accept <run id> <n>`; when `error`: `error: <text>` to stderr. Exit code 1 for `failed`/`capped`.
- Subcommands (the three run commands all print `run <id> queued` first, then, unless `--no-wait`, wait and `_print_run`):
  - `summarize <project> [--runner R] [--model M] [--no-wait]` → `POST /api/projects/{id}/summarize` (slug resolved with `api.project_id`).
  - `ask <question> [--projects a,b] [--days 90] [--budget 4000] [--runner] [--model] [--no-wait]` → `POST /api/ask` with `start = now - days`, `end = now + 30 days`.
  - `add <text> [--project slug] [--runner] [--model] [--no-wait]` → `POST /api/quick-add`.
  - `accept <run_id> <n>` → `POST /api/runs/{id}/proposals/{n-1}/accept`; prints `applied: <kind> <applied_id> (txn <applied_txn>)`; `n < 1` → exit 2 `invalid proposal number`.
  - `runs [--limit 20]` → one line per run: `<id>  <created_at>  <kind>  <status>  $<est_usd:.4f>  <project slug or ->` (slugs via one `GET /api/projects`).
  - `usage [--days 30]` → `today: $<spent_today:.4f> of $<usd_per_day> · paused: yes|no`, then `<day>  $<est_usd:.4f>  <runs> runs` for days with runs, then `by kind:` lines `<key>  $<est_usd:.4f>  <runs>`.
  - `caps [--usd-per-run X] [--usd-per-day Y] [--timeout S] [--pause | --resume]` → `GET /api/caps`, merge, `PUT`; prints `caps: run $<x> · day $<y> · timeout <s>s · paused <yes|no>`.
  - `login` → prints `Run 'claude setup-token' on this machine, then paste the token (input is not echoed):` to stderr, reads one line from `stdin` (uses `getpass.getpass` when `stdin.isatty()`), `POST /api/runner/credentials`; prints `stored`; empty → exit 2 `no token given`.
  - `backends sync` → `POST /api/backends/sync`; prints `synced: a, b` or `synced: none`.
  - `runner status` → lines `claude: <available|missing> <version or ''> <logged in|not logged in>`, `codex: ...`, `backends: <name (ok|no key)>, ...`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_runs.py
import io, json
import pytest
from tests.test_cli import env_for
from trajectree.cli import main as cli
from trajectree.runner.credentials import store_oauth_token

STRUCTURED = json.dumps({"answer": "Parsed", "proposals": [{"kind": "todo", "project": "clmi", "title": "Email Bob", "reason": "owed"}]})

@pytest.fixture(autouse=True)
def fast(monkeypatch, settings):
    monkeypatch.setattr(cli, "_sleep", lambda s: None)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", STRUCTURED)
    store_oauth_token(settings, "fake")

def test_cli_add_waits_prints_proposals_and_accept(app, client, anon, capsys):
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    assert cli.main(["add", "email Bob about slides"], client=anon, env=env_for(app)) == 0
    out = capsys.readouterr().out
    assert "run " in out and " ok " in out and "[1] todo · clmi · Email Bob (owed)" in out and "accept with: trajectree accept" in out
    run_id = out.split("run ")[1].split()[0]
    assert cli.main(["accept", run_id, "1"], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.startswith("applied: todo ")
    assert client.get("/api/todos").json()[0]["title"] == "Email Bob"
    assert cli.main(["accept", run_id, "1"], client=anon, env=env_for(app)) == 1
    assert cli.main(["accept", run_id, "0"], client=anon, env=env_for(app)) == 2

def test_cli_summarize_no_wait_and_runs_listing(app, client, anon, capsys):
    client.post("/api/projects", json={"slug": "clmi", "title": "CLMI", "kind": "research"})
    assert cli.main(["summarize", "clmi", "--no-wait"], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.endswith(" queued\n")
    assert cli.main(["runs", "--limit", "5"], client=anon, env=env_for(app)) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert "summarize" in line and line.endswith("clmi")

def test_cli_caps_usage_login_and_status(app, anon, capsys):
    assert cli.main(["caps", "--usd-per-day", "2", "--pause"], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.strip() == "caps: run $1.0 · day $2.0 · timeout 600s · paused yes"
    assert cli.main(["ask", "q"], client=anon, env=env_for(app)) == 1   # paused → 409
    assert cli.main(["caps", "--resume"], client=anon, env=env_for(app)) == 0
    assert cli.main(["usage"], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.startswith("today: $0.0000 of $2.0 · paused: no")
    assert cli.main(["login"], client=anon, env=env_for(app), stdin=io.StringIO("sk-ant-oat01-x\n")) == 0
    assert capsys.readouterr().out.strip() == "stored"
    assert cli.main(["login"], client=anon, env=env_for(app), stdin=io.StringIO("\n")) == 2
    assert cli.main(["runner", "status"], client=anon, env=env_for(app)) == 0
    assert "claude:" in capsys.readouterr().out
    assert cli.main(["backends", "sync"], client=anon, env=env_for(app)) == 0
    assert capsys.readouterr().out.strip() == "synced: none"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli_runs.py -q`
Expected: FAIL (argparse exits 2).

- [ ] **Step 3: Implement** per Interfaces.

- [ ] **Step 4: Run the CLI tests**

Run: `uv run pytest tests/test_cli_runs.py tests/test_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/cli/main.py tests/test_cli_runs.py
git commit -m "feat(cli): summarize, ask, add, accept, runs, usage, caps, login, backends sync, runner status"
```

---

### Task 10: CLI `hooks install|uninstall|show`

**Files:**
- Create: `src/trajectree/cli/hooks.py`
- Modify: `src/trajectree/cli/main.py`
- Test: `tests/test_cli_hooks.py`

**Interfaces:**
- `HOOK_EVENTS = ("SessionStart", "Stop", "SessionEnd")`; `HOOK_LABEL = "hooks"`; `hooks_url(base: str) -> str` = `base.rstrip("/") + "/api/hooks/claude"`; `settings_path(env: Mapping[str, str]) -> Path`: `$CLAUDE_CONFIG_DIR/settings.json` when set, else `<HOME>/.claude/settings.json` (HOME resolved like `_token` does).
- `handler(url: str, secret: str) -> dict` = `{"type": "http", "url": url, "headers": {"Authorization": f"Bearer {secret}"}, "timeout": 3}`.
- `merge_hooks(settings: dict, url: str, secret: str) -> dict` (pure, deep copy): for each event in `HOOK_EVENTS`, remove every handler whose `url == url` from every group, drop groups left empty, then append `{"hooks": [handler(url, secret)]}`; other events and groups untouched. `remove_hooks(settings: dict, url: str) -> dict`: same removal without the append; drops events left empty and the `hooks` key when empty.
- `redact(settings: dict) -> dict`: a deep copy with every `Authorization` header value replaced by `Bearer ***`.
- `install(api, env, *, out: TextIO, confirm: Callable[[str], bool], yes: bool, settings_file: Path | None) -> int`: path = `settings_file or settings_path(env)`; load JSON (`{}` when missing; invalid JSON → prints `cannot parse <path>` to stderr, returns 1); `GET /api/tokens` and `DELETE` every token labelled `hooks`; `POST /api/tokens` `{"label": "hooks", "scope": {"projects": [], "tools": ["hooks"], "write": false}}`; merged = `merge_hooks(current, hooks_url(base), secret)`; prints `settings file: <path>`, the JSON of `redact(merged)["hooks"]` restricted to the three events, and `This writes <path> (backup at <path>.trajectree.bak).`; unless `yes or confirm("Proceed? [y/N] ")`: `DELETE` the new token, prints `aborted`, returns 1; writes the backup (copy of the existing bytes, when the file exists), writes `json.dumps(merged, indent=2) + "\n"`, prints `hooks: installed (3 events)`, returns 0.
- `uninstall(api, env, *, out, settings_file)`: backup, write `remove_hooks`, `DELETE` every `hooks` token, prints `hooks: removed`; returns 0 (also when nothing was present).
- `show(api, env, *, out, settings_file)`: one line per event `<event>: present|absent`, then `token: present|absent`.
- `main`: subparser `hooks` with `install [--yes] [--settings-file P]`, `uninstall [--settings-file P]`, `show [--settings-file P]`; default `confirm` reads `input(prompt).strip().lower() == "y"` when `sys.stdin.isatty()`, else returns False.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_hooks.py
import json
from tests.test_cli import env_for
from trajectree.cli import main as cli
from trajectree.cli.hooks import merge_hooks, remove_hooks

EXISTING = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify"}]}],
                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "lint"}]}]},
            "statusLine": {"type": "command", "command": "x"}}

def test_merge_preserves_other_hooks_and_is_idempotent():
    url = "http://127.0.0.1:8742/api/hooks/claude"
    once = merge_hooks(EXISTING, url, "trj_1")
    twice = merge_hooks(once, url, "trj_2")
    for ev in ("SessionStart", "Stop", "SessionEnd"):
        ours = [h for g in twice["hooks"][ev] for h in g["hooks"] if h.get("url") == url]
        assert len(ours) == 1 and ours[0]["headers"]["Authorization"] == "Bearer trj_2" and ours[0]["type"] == "http" and ours[0]["timeout"] == 3
    assert twice["hooks"]["Stop"][0]["hooks"][0]["command"] == "notify" and twice["hooks"]["PreToolUse"] == EXISTING["hooks"]["PreToolUse"]
    assert twice["statusLine"] == EXISTING["statusLine"] and EXISTING["hooks"].get("SessionStart") is None   # input untouched
    assert remove_hooks(twice, url) == EXISTING

def test_install_writes_backup_rotates_token_and_authenticates(app, client, anon, tmp_path, capsys):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps(EXISTING))
    env = {**env_for(app), "HOME": str(tmp_path)}
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(f)], client=anon, env=env) == 0
    assert cli.main(["hooks", "install", "--yes", "--settings-file", str(f)], client=anon, env=env) == 0
    out = capsys.readouterr().out
    assert "Bearer ***" in out and "trj_" not in out and out.count("hooks: installed (3 events)") == 2
    assert (tmp_path / "settings.json.trajectree.bak").exists()
    written = json.loads(f.read_text())
    secret = written["hooks"]["SessionStart"][-1]["hooks"][0]["headers"]["Authorization"].split()[1]
    assert written["hooks"]["SessionStart"][-1]["hooks"][0]["url"] == "http://testserver/api/hooks/claude"
    assert [t["label"] for t in client.get("/api/tokens").json()] == ["hooks"]   # rotated, not accumulated
    payload = {"session_id": "s1", "cwd": "/x", "hook_event_name": "SessionStart", "transcript_path": "/t"}
    assert anon.post("/api/hooks/claude", json=payload, headers={"Authorization": f"Bearer {secret}"}).status_code == 200
    assert cli.main(["hooks", "show", "--settings-file", str(f)], client=anon, env=env) == 0
    assert capsys.readouterr().out == "SessionStart: present\nStop: present\nSessionEnd: present\ntoken: present\n"
    assert cli.main(["hooks", "uninstall", "--settings-file", str(f)], client=anon, env=env) == 0
    assert json.loads(f.read_text()) == EXISTING and client.get("/api/tokens").json() == []

def test_install_aborts_without_confirmation(app, client, anon, tmp_path, capsys):
    f = tmp_path / "settings.json"
    env = {**env_for(app), "HOME": str(tmp_path)}
    assert cli.main(["hooks", "install", "--settings-file", str(f)], client=anon, env=env) == 1   # stdin is not a tty
    assert "aborted" in capsys.readouterr().out and not f.exists() and client.get("/api/tokens").json() == []

def test_install_default_path_and_bad_json(app, anon, tmp_path, capsys):
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    (cfg / "settings.json").write_text("{not json")
    assert cli.main(["hooks", "install", "--yes"], client=anon, env={**env_for(app), "HOME": str(tmp_path)}) == 1
    assert "cannot parse" in capsys.readouterr().err
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli_hooks.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement** per Interfaces.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/cli tests/test_cli_hooks.py
git commit -m "feat(cli): hooks install/uninstall/show with confirmed settings merge and a dedicated hook token"
```

---

### Task 11: Web: Runs tab with usage, caps, run history and proposals

**Files:**
- Modify: `web/src/api/types.ts`, `web/src/api/client.ts`, `web/src/App.tsx`, `web/src/App.test.tsx`, `web/src/views/views.css`
- Create: `web/src/views/RunsView.tsx`, `web/src/components/Proposals.tsx`, `web/src/components/useRun.ts`
- Test: `web/src/views/RunsView.test.tsx`, `web/src/components/Proposals.test.tsx`

**Interfaces:**
- `types.ts`: `RunKind`, `RunStatus`, `Proposal`, `Run`, `RunDetail`, `Caps`, `UsageDay`, `UsageSlice`, `UsageReport`, `RunnerInfo`, `BackendInfo`, `RunnerStatus`, `SessionRow`, `ContextPack {markdown, tokens_est, dropped, projects}`, `AskRequest`, `QuickAddRequest`; `IngestReport.exports_errors: string[]`; all mirroring the Python models field for field.
- `client.ts`: `runs(opts?: { limit?: number; kind?: RunKind; status?: RunStatus; openProposals?: boolean })` → `GET /api/runs` (`open_proposals=true` when set); `run(id)` → `RunDetail`; `summarize(projectId, body: { runner?: string; model?: string } = {})`; `ask(body: AskRequest)`; `quickAdd(body: QuickAddRequest)`; `acceptProposal(runId, index)` → `Run`; `usage(days = 30)`; `caps()`; `setCaps(caps: Caps)`; `runnerStatus()`; `context(projects: string[], from: Date, to: Date, budget = 4000)` → `ContextPack`.
- `useRun(runId: string | null)`: `useQuery({ queryKey: ["run", runId], queryFn: () => api.run(runId!), enabled: runId !== null, refetchInterval: q => (q.state.data && ["queued", "running"].includes(q.state.data.run.status) ? 1500 : false) })`.
- `Proposals({ run, onApplied }: { run: Run; onApplied?: () => void })`: `<h2>Proposals</h2>`; `No proposals` when empty; `ul.proposals > li` per item: `span.badge` kind, project slug, `b` title, `span.muted` reason, `when`/`due` when present; button `Accept` (disabled while the mutation runs); applied items show `span.badge applied` and no button; `errorText` inline on failure. The mutation calls `api.acceptProposal(run.id, index)` then invalidates `["run", run.id]`, `["runs"]`, `["timeline"]`, `["todos"]`, `["txns"]` and calls `onApplied`.
- `RunsView`: top line from `api.runnerStatus()`: `claude <version> · logged in` or `claude: not logged in (run trajectree login)` or `claude: not installed`. Section `Usage` (`api.usage(30)`): line `spent today $<x.xx> of $<cap>`; `svg.usage-chart` 30 bars `rect.bar` (height ∝ `est_usd`, scale max = `max(cap, max est_usd)`, fill `var(--strip-3)`, each with `<title>YYYY-MM-DD: $x.xx (n runs)</title>`), `line.cap` at `usd_per_day` (stroke `var(--now)`); two tables `by kind` and `by runner`. Section `Caps`: form with labelled inputs `USD per run`, `USD per day`, `Timeout (s)`, button `Save caps` (PUT with the three values and the current `paused`), and a button `Pause runs` / `Resume runs` (PUT with `paused` flipped). Section `Run history` (`api.runs({ limit: 50 })`): `ul.runs > li.run-row` with `kind · status · project slug (via projects query) · $est_usd · turns turns · duration ms`, the status as `span.badge.run-<status>`, a `button.linklike` on the intent-like label (`kind` + created time) that expands `RunDetailView` (result_md in `pre.answer`, error in `p.form-error`, `Proposals`, txn ids as `code`, log path in `span.muted`); empty `No runs yet`.
- `App.tsx`: tab `Runs` after `Ledger`; `App.test.tsx` adds `runs`, `usage`, `caps`, `runnerStatus` mocks and asserts the `Runs` tab renders `Run history`.

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/views/RunsView.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { RunsView } from "./RunsView";
import type { Run } from "../api/types";

const run = (id: string, over: Partial<Run> = {}): Run => ({ id, kind: "ask", project_id: null, status: "ok", runner: "claude", backend: "claude", model: null, session_id: "s", started: "2026-01-05T10:00:00Z", finished: "2026-01-05T10:01:00Z", tokens_in: 100, tokens_out: 20, tokens_cache: 12, est_usd: 0.0123, turns: 2, duration_ms: 345, result_md: "Answer.", proposals: [], error: null, log_path: "/data/runs/r1.log", token_id: null, props: {}, created_at: "2026-01-05T10:00:00Z", ...over });
const api = vi.hoisted(() => ({ runs: vi.fn(), run: vi.fn(), usage: vi.fn(), caps: vi.fn(), setCaps: vi.fn(), runnerStatus: vi.fn(), projects: vi.fn(), acceptProposal: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));
const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);
const caps = { usd_per_run: 1, usd_per_day: 5, timeout_s: 600, paused: false };

describe("RunsView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.projects.mockResolvedValue([]);
    api.runnerStatus.mockResolvedValue({ claude: { available: true, version: "2.1.309", authenticated: false }, codex: { available: false, version: null, authenticated: false }, backends: [] });
    api.usage.mockResolvedValue({ days: [{ day: "2026-01-04", est_usd: 1, runs: 1, tokens_in: 1, tokens_out: 1 }, { day: "2026-01-05", est_usd: 0.5, runs: 2, tokens_in: 1, tokens_out: 1 }], by_kind: [{ key: "ask", est_usd: 1.5, runs: 3 }], by_runner: [], spent_today: 0.5, caps });
    api.caps.mockResolvedValue(caps);
    api.setCaps.mockImplementation(async (c) => c);
  });
  it("renders usage bars, the cap line and the runner status", async () => {
    api.runs.mockResolvedValue([]);
    const { container } = wrap(<RunsView />);
    expect(await screen.findByText(/not logged in/)).toBeTruthy();
    await waitFor(() => expect(container.querySelectorAll("svg.usage-chart rect.bar").length).toBe(2));
    expect(container.querySelector("svg.usage-chart line.cap")).not.toBeNull();
    expect(screen.getByText(/spent today \$0\.50 of \$5/)).toBeTruthy();
    expect(await screen.findByText("No runs yet")).toBeTruthy();
  });
  it("saves caps and toggles pause", async () => {
    api.runs.mockResolvedValue([]);
    wrap(<RunsView />);
    const perDay = await screen.findByLabelText("USD per day");
    fireEvent.change(perDay, { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "Save caps" }));
    await waitFor(() => expect(api.setCaps).toHaveBeenCalledWith({ ...caps, usd_per_day: 7 }));
    fireEvent.click(screen.getByRole("button", { name: "Pause runs" }));
    await waitFor(() => expect(api.setCaps).toHaveBeenLastCalledWith(expect.objectContaining({ paused: true })));
  });
  it("lists runs and expands one with its proposals", async () => {
    const r1 = run("r1", { proposals: [{ kind: "todo", project: "clmi", title: "Email Bob", reason: "owed", event_kind: "note", when: null, due: null, body_md: "", status: "happened", applied_txn: null, applied_id: null }] });
    api.runs.mockResolvedValue([r1]);
    api.run.mockResolvedValue({ run: r1, txn_ids: ["t1"] });
    const { container } = wrap(<RunsView />);
    const row = await screen.findByRole("button", { name: /ask/ });   // the row's expand button; the by-kind table has no button
    expect(container.querySelector("span.badge.run-ok")).not.toBeNull();
    fireEvent.click(row);
    expect(await screen.findByText("Email Bob")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByText("t1")).toBeTruthy();
  });
});
```

```tsx
// web/src/components/Proposals.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi } from "vitest";
import { Proposals } from "./Proposals";
import type { Run } from "../api/types";

const api = vi.hoisted(() => ({ acceptProposal: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));
const base: Run = { id: "r1", kind: "quick_add", project_id: null, status: "ok", runner: "claude", backend: "claude", model: null, session_id: null, started: null, finished: null, tokens_in: 0, tokens_out: 0, tokens_cache: 0, est_usd: 0, turns: 0, duration_ms: 0, result_md: "", proposals: [
  { kind: "event", project: "clmi", title: "Decided X", reason: "decision", event_kind: "decision", when: "2026-01-05T10:00:00Z", due: null, body_md: "", status: "happened", applied_txn: null, applied_id: null },
  { kind: "todo", project: "clmi", title: "Done one", reason: "r", event_kind: "note", when: null, due: null, body_md: "", status: "happened", applied_txn: "t9", applied_id: "x" },
], error: null, log_path: null, token_id: null, props: {}, created_at: "2026-01-05T10:00:00Z" };

describe("Proposals", () => {
  it("accepts a proposal and marks applied ones", async () => {
    api.acceptProposal.mockResolvedValue({ ...base, proposals: [{ ...base.proposals[0], applied_txn: "t1", applied_id: "e1" }, base.proposals[1]] });
    const onApplied = vi.fn();
    render(<QueryClientProvider client={new QueryClient()}><Proposals run={base} onApplied={onApplied} /></QueryClientProvider>);
    expect(screen.getAllByText("applied").length).toBe(1);
    expect(screen.getAllByRole("button", { name: "Accept" }).length).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(api.acceptProposal).toHaveBeenCalledWith("r1", 0));
    await waitFor(() => expect(onApplied).toHaveBeenCalled());
  });
  it("shows the empty state", () => {
    render(<QueryClientProvider client={new QueryClient()}><Proposals run={{ ...base, proposals: [] }} /></QueryClientProvider>);
    expect(screen.getByText("No proposals")).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd web && npm test -- RunsView Proposals`
Expected: FAIL (modules missing).

- [ ] **Step 3: Implement** per Interfaces. Load the `dataviz` skill before writing the chart; keep it to the tokens named in Global Constraints.

- [ ] **Step 4: Run the web suite and the build**

Run: `cd web && npm test && npm run build`
Expected: all green, build clean.

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "feat(web): Runs tab with usage chart, caps, run history and proposals"
```

---

### Task 12: Web: Ask panel, free-text quick-add, stale chip action, pending todo badges, inbox proposals

**Files:**
- Create: `web/src/components/AskPanel.tsx`
- Modify: `web/src/components/QuickCapture.tsx`, `web/src/App.tsx`, `web/src/timeline/render.ts`, `web/src/timeline/Timeline.tsx`, `web/src/timeline/timeline.css`, `web/src/views/TimelineView.tsx`, `web/src/components/DetailsPanel.tsx`, `web/src/views/InboxView.tsx`
- Test: `web/src/components/AskPanel.test.tsx`, `web/src/components/QuickCapture.test.tsx`, `web/src/timeline/Timeline.test.tsx`, `web/src/components/DetailsPanel.test.tsx`, `web/src/views/InboxView.test.tsx`

**Interfaces:**
- `AskPanel({ projects }: { projects: Project[] })`: `<h2>Ask</h2>`; one checkbox per active project labelled by title, all checked initially; date inputs `From` (default today minus 90 days) and `To` (default today plus 30 days); textarea `Question`; muted line `pack ≈ <tokens_est> tokens` from `api.context(selected slugs, from, to)` (refetched when the selection or dates change); button `Ask` (disabled when the question is empty or a run is in flight) → `api.ask({ question, projects: slugs, start, end })` → `useRun(id)`; while `queued`/`running` shows `running…`; on finish shows `run.result_md` in `div.answer` (`pre`), `run.error` in `p.form-error`, and `<Proposals run=... />`.
- `App.tsx`: header button `Ask` toggles panel `ask` in the drawer, rendering `<AskPanel projects={projects.data ?? []} />`.
- `QuickCapture`: a new block above the form: textarea `Describe it`, button `Parse` (disabled when empty or a run is in flight) → `api.quickAdd({ text })` → `useRun`; shows `running…`, then `run.result_md` (muted) and `<Proposals run onApplied={onCreated} />`; the existing structured form stays below under `<h3>Or fill the form</h3>`; the existing tests keep passing.
- `render.ts`: `Chip` gains `projectId: string`; `RenderInput.onChip?: (projectId: string, kind: "running" | "stale") => void`; the chip `g` gets `.on("click", (ev, c) => { ev.stopPropagation(); onChip?.(c.projectId, c.kind); })` and, for the stale chip, an appended `<title>Regenerate summary</title>`; CSS `g.chip.stale { cursor: pointer }`. `Timeline` prop `onChip?` passed through.
- `TimelineView`: `onChip` runs `useMutation(api.summarize(projectId))` for `stale` and shows `Summary regeneration queued (run <id>)` through a local `notice` state rendered by `ActionNotice` (`error` prop reused for the text, styled the same); `running` is a no-op.
- Pending todo badges: `render.ts` todo marks get class `pending` when `data.pending.todo?.[todo.id]` exists (`stroke: var(--warn)`, `stroke-width: 2`); `DetailsPanel` todo branch: `span.badge.warn` `pending` and buttons `Accept`/`Reject` calling `onReview(data.pending.todo[todo.id], ...)` when pending; otherwise unchanged.
- `InboxView`: a `Proposals` section (`<h2>Proposals</h2>`) listing `api.runs({ openProposals: true, limit: 50 })` (query key `["runs", "open-proposals"]`), each as `kind · <created time> · result_md` followed by `<Proposals run />`; empty `No proposals`.

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/components/AskPanel.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi } from "vitest";
import { AskPanel } from "./AskPanel";

const api = vi.hoisted(() => ({ context: vi.fn(), ask: vi.fn(), run: vi.fn(), acceptProposal: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));
const projects = [{ id: "p1", slug: "clmi", title: "CLMI", kind: "research", status: "active" }, { id: "p2", slug: "ccx", title: "ccx", kind: "tooling", status: "active" }] as any;
const run = (status: string) => ({ run: { id: "r1", kind: "ask", status, result_md: status === "ok" ? "Blocked on data." : "", proposals: [], error: null, project_id: null, runner: "claude", backend: "claude", model: null, session_id: null, started: null, finished: null, tokens_in: 0, tokens_out: 0, tokens_cache: 0, est_usd: 0, turns: 0, duration_ms: 0, log_path: null, token_id: null, props: {}, created_at: "2026-01-05T10:00:00Z" }, txn_ids: [] });

describe("AskPanel", () => {
  it("shows the pack size, asks with the selected slugs and renders the answer", async () => {
    api.context.mockResolvedValue({ markdown: "", tokens_est: 1234, dropped: [], projects: ["clmi", "ccx"] });
    api.ask.mockResolvedValue(run("queued").run);
    api.run.mockResolvedValueOnce(run("running")).mockResolvedValue(run("ok"));
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AskPanel projects={projects} /></QueryClientProvider>);
    expect(await screen.findByText(/pack ≈ 1234 tokens/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText("ccx"));
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "What is blocked?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(api.ask).toHaveBeenCalledWith(expect.objectContaining({ question: "What is blocked?", projects: ["clmi"] })));
    expect(await screen.findByText("Blocked on data.", {}, { timeout: 4000 })).toBeTruthy();
  });
});
```

Add to `QuickCapture.test.tsx` (extend the mock with `quickAdd`, `run`, `acceptProposal`): `it("parses free text into proposals")` that types into `Describe it`, clicks `Parse`, mocks `api.run` returning a finished run with one proposal, and expects the proposal title and an `Accept` button. Add to `Timeline.test.tsx`: `it("clicking the stale chip reports it")` with `stale: ["p1"]`, `onChip` spy, `fireEvent.click(container.querySelector("g.chip.stale")!)` → called with `("p1", "stale")`, and `container.querySelector("g.chip.stale title")!.textContent === "Regenerate summary"`; and `it("marks pending todos")` with a todo in `pending.todo` → a todo mark element with class `pending`. Add to `DetailsPanel.test.tsx`: a pending todo selection shows the `pending` badge and `Accept` calls `onReview("t5", "accepted")`. Add to `InboxView.test.tsx`: `api.runs` mocked with one run holding an open proposal → heading `Proposals` and the proposal title render; with none → `No proposals`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd web && npm test`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement** per Interfaces.

- [ ] **Step 4: Run the web suite and the build**

Run: `cd web && npm test && npm run build`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "feat(web): ask panel, free-text quick-add proposals, stale chip regenerates, pending todo badges, inbox proposals"
```

---

### Task 13: Deployment, documentation and end-to-end smoke

**Files:**
- Modify: `Dockerfile`, `compose.yaml`, `README.md`, `web/playwright.config.ts`, `web/e2e/smoke.spec.ts`

**Interfaces:**
- `Dockerfile` runtime stage: `apt-get install -y --no-install-recommends git curl ca-certificates`; `RUN mkdir -p /opt/claude && HOME=/opt/claude bash -c "curl -fsSL https://claude.ai/install.sh | bash" && chmod -R a+rX /opt/claude && /opt/claude/.local/bin/claude --version`; `ENV PATH=/opt/claude/.local/bin:/app/.venv/bin:$PATH DISABLE_AUTOUPDATER=1 TRAJECTREE_CFG_DIR=/cfg HOME=/cfg/home`; `RUN mkdir /cfg && chown 1000:1000 /cfg`; `VOLUME /cfg`.
- `compose.yaml`: top-level `volumes: { cfg: {} }`; service volumes add `cfg:/cfg` and replace `${HOME}/.claude:/host/claude:ro` with `${HOME}/.claude/projects:/host/claude/projects:ro`; environment adds `TRAJECTREE_CFG_DIR: /cfg`, `HOME: /cfg/home`, `DISABLE_AUTOUPDATER: "1"`.
- `README.md` new sections: **Runs (summaries, ask, quick-add)** (what each run does; summaries land as pending transactions, ask and quick-add return proposals the owner accepts; caps and the pause switch; `trajectree summarize|ask|add|accept|runs|usage|caps`; prompts editable under `data/prompts/`; logs under `data/runs/`); **Log in the runner** (`claude setup-token` on the host, `trajectree login`, where the token lives (`cfg` volume), `trajectree backends sync` for ccx profiles, `trajectree runner status`; codex optional: install the binary into the image yourself and set `TRAJECTREE_CODEX_BIN`); **Live sessions (hooks)** (`trajectree hooks install`, the three events, the dedicated token, `hooks show`, `hooks uninstall`); **What gets written where**: a table with rows for the container (`./data`: `trajectree.db`, `token`, `exports/`, `runs/*.log`, `prompts/*.md`; the `cfg` volume: Claude Code config, OAuth token, backend profile copies, run homes) and for the host CLI (`init-project`: `<project>/.claude/rules/trajectree.md` and `.git/info/exclude`, Claude Code's user MCP config via `claude mcp add`; `hooks install`: `~/.claude/settings.json` after confirmation, with `.trajectree.bak`; everything else: nothing). Note that the Claude home mount is now only `~/.claude/projects`.
- `web/playwright.config.ts` env adds `TRAJECTREE_CLAUDE_BIN: path.resolve(__dirname, "../tests/bin/fake-claude")`, `TRAJECTREE_CFG_DIR: path.join(dataDir, "cfg")`, `TRAJECTREE_RUN_ENV_PASSTHROUGH: "FAKE_CLAUDE_MODE,FAKE_CLAUDE_STRUCTURED"`, `FAKE_CLAUDE_MODE: "ok"`, `FAKE_CLAUDE_STRUCTURED: JSON.stringify({ answer: "Parsed one decision", proposals: [{ kind: "event", project: "clmi", title: "Encoder frozen (parsed)", reason: "quick add", event_kind: "decision" }] })`.
- `web/e2e/smoke.spec.ts` appends after the ingest assertions: `page.request.post("/api/runner/credentials", { headers, data: { oauth_token: "fake" } })` expecting 200; click `Quick capture`, fill `Describe it` with `we froze the encoder`, click `Parse`, expect text `Encoder frozen (parsed)` visible, click `Accept`, expect `applied` visible; click tab `Timeline`, expect `g.block.k-decision` count 1; click tab `Runs`, expect heading `Run history` and a `span.badge.run-ok`.

- [ ] **Step 1: Make the Dockerfile, compose, playwright and smoke changes**

- [ ] **Step 2: Validate the compose file**

Run: `TRAJECTREE_PROJECTS_ROOT=/tmp UID=1000 GID=1000 docker compose config >/dev/null && echo ok`
Expected: `ok`.

- [ ] **Step 3: Build the image**

Run: `docker compose build 2>&1 | tail -5`
Expected: a successful build whose log shows the `claude --version` line. If the native install step fails (network or installer change), record the exact error in the report and leave the build failing for the reviewer rather than removing the step.

- [ ] **Step 4: Run the smoke test**

Run: `cd web && npm run e2e`
Expected: 1 passed.

- [ ] **Step 5: Write the README sections** per Interfaces.

- [ ] **Step 6: Run everything once more**

Run: `uv run pytest -q && cd web && npm test && npm run build`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile compose.yaml README.md web/playwright.config.ts web/e2e
git commit -m "feat(deploy): Claude Code in the image, cfg volume, narrower transcript mount, phase 3 docs and smoke"
```

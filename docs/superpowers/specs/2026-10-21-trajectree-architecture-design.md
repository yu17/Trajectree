# Trajectree: architecture design

Date: 2026-10-21. Status: draft for approval. Author: brainstormed with the project owner; calls marked "(my call)" were made by the assistant and can be overridden.

## 1. Intent

Trajectree is a self-hosted, single-user tracker for everything a PhD student has in flight: research projects, tooling side projects, recurring routines such as organizing the group meeting, and one-off tasks. It keeps the history of each project, its current status, and suggested next steps, and shows all of it on one horizontal timeline with one lane per project and wires between related blocks across projects.

LLM sessions maintain the record from two sides. The owner's own Claude Code sessions in project folders log decisions, milestones and todos through Trajectree's MCP tools, guided by a short CLAUDE.md section. The daemon runs headless LLM jobs on request: per-project summary regeneration, cross-project reviews, meeting prep, decks, ask, and quick capture. Ingestion from folders, repos, sessions and the cluster is scheduled and needs no LLM. Every LLM write is recorded as a transaction that can be inspected and reverted, so a wrong write is traceable and cheap to undo.

Why this exists: the owner has many concurrent projects and no centralized track record. Existing tools cover pieces (markdown vaults, team PM Gantts, agent-memory layers, Claude Code transcript viewers) but nothing combines self-hosted, LLM-maintained, timeline-first, cross-project links. Research summary in Appendix A.

## 2. Goals, non-goals, anti-goals

Goals
- One place to see, per project and across projects: what happened, what was decided, what is planned, what the agent suggests next.
- Flexible topology: new relation kinds and event kinds without schema migrations; natural-language text everywhere.
- Transactional record: every write, human or agent, is one transaction with author metadata (time, actor, model, backend, tool version, session and run ids) and a stored diff. Any transaction can be reverted later without losing unrelated later changes.
- Context building: compile a context pack for any selection of projects and time range, usable in the app and from any external LLM session.
- Integration with the owner's existing surfaces: project folders, local and remote git repos, Claude Code sessions past and running, Slurm jobs, and later the chatmaid communications project.
- Usage visibility: every LLM run's tokens and estimated cost are recorded; caps and a pause switch exist.
- Safe to run on an always-on home server, in a container, with read-only access to the owner's files.

Non-goals (v1)
- Managing or hosting long-running interactive LLM sessions. Those run in the owner's terminals; Trajectree observes them and offers a resume command.
- Multi-user, auth beyond a local bearer token, mobile app, calendar sync, email handling (chatmaid's job).
- Replacing git, Claude Code's own session storage, or the cluster scheduler.
- Being a general chat assistant.

Anti-goals (would make it a failure even if it works)
- The agent silently rewriting history. Every agent write must be visibly attributed and revertible.
- Lanes flooded with raw activity so decisions are invisible. Raw activity lives in the activity strip, curated events are the blocks (lane model A, chosen from the mock).
- A second LLM harness to maintain. The agent layer reuses the owner's headless CLIs (claude, ccx, codex) rather than embedding an SDK.
- Spend surprises. Runs are capped and recorded.

## 3. Constraints and environment

- Runs on the owner's always-on Linux workstation, which doubles as a home server. Docker deployment is preferred for isolation from external attack and from agent mistakes.
- Owner is a heavy Claude Code user on a subscription. Headless CLI runs (not `--bare`) use that login. Other models come through ccx (Claude Code on alternate Anthropic-compatible backends: DeepSeek, Kimi, LM Studio) and optionally codex.
- Claude Code stores transcripts under the Claude home as one JSONL per session per project folder. Anthropic documents the entry format as internal and version-unstable. Hooks of type `http` can POST session lifecycle events to a URL.
- Data must live on an external persistent volume and be a single file plus a folder of exports, so backup is a copy.
- Backend: Python. Frontend: React + TypeScript. Decided by the owner.

## 4. System overview

One Python daemon owns one SQLite file and serves the web UI, the HTTP API, and an MCP endpoint. Everything that changes data goes through the ledger. The agent is a tenant of the daemon, invoked as a subprocess, and reaches the data only through MCP tools.

```
 host (owner's workstation)                      container: trajectreed
 ┌──────────────────────────┐                   ┌───────────────────────────────────────────┐
 │ terminals: claude / ccx  │ http hooks        │  FastAPI app                              │
 │ / codex sessions ────────┼──────────────────▶│   ├─ HTTP API + SSE (web UI, CLI, shim)   │
 │   (stdio MCP shim) ──────┼── MCP over HTTP ─▶│   ├─ MCP server (read + write tools)      │
 │ ~/.claude/projects (ro) ─┼─ bind mount ─────▶│   ├─ connectors (claude-sessions, git,     │
 │ project folders (ro) ────┼─ bind mount ─────▶│   │   folders, github, slurm, chatmaid)   │
 │ chatmaid (later) ◀───────┼── HTTP/MCP ──────▶│   ├─ scheduler (routines, runs, caps)     │
 │ browser ◀────────────────┼── 127.0.0.1:port ─┤   ├─ runner: spawns `claude -p` /         │
 └──────────────────────────┘                   │   │   `ccx <b> -p` / `codex exec`        │
                                                │   ├─ context compiler, report builder     │
                                                │   └─ ledger (apsw + Session ext, FTS5,    │
                                                │       optional sqlite-vec)                │
                                                │  /data: trajectree.db, exports/, config   │
                                                │  /cfg: container-private CLI configs     │
                                                └───────────────────────────────────────────┘
```

Components
- Ledger: SQLite in WAL mode via apsw. Every mutation is one transaction with a Session-extension changeset stored alongside author metadata. Full-text (FTS5) and vector (sqlite-vec, optional) indexes are derived and rebuilt from base rows.
- HTTP API and SSE: JSON API used by the web UI, the CLI, and the host-side MCP shim; SSE stream for live updates (new events, run progress, session start/stop).
- MCP server: the agent's only data access and the integration surface for external LLM sessions and chatmaid. Read tools and write tools are separate; write tools carry an `intent` string that becomes the transaction's intent.
- Runner: spawns a headless CLI per run with a prompt, a working directory, a tool policy, caps, and a per-run MCP config pointing back at the daemon with a scoped token. Parses the JSON result for text, session id, usage, cost.
- Scheduler: routines and runs tables in the same database, an asyncio loop with cron expressions for ingestion and staleness checks; LLM routines run on request by default and may be scheduled; serial execution of scheduled LLM runs, retries for infrastructure errors only, caps.
- Project-session guidance: a managed CLAUDE.md section plus MCP configuration installed by `trajectree init-project`, so the owner's interactive Claude Code sessions in a project folder record outcomes through the MCP write tools. These sessions are the primary per-project writers.
- Connectors: pull or receive from one source each and write provenance-tagged signals and resources. Signals feed the activity strip; agent routines condense them into events.
- Context compiler: builds a token-budgeted markdown context pack for a selection; used by ask, routines, reports, and exported through MCP and CLI.
- Report builder: drafts Marp-flavored markdown decks and memos from a context pack; editable in the UI; exported to HTML, and to PDF or PPTX when the Chromium layer is present.
- Web UI: React + TypeScript, d3 timeline (lane model A), project pages, inbox, ledger, routines and usage, reports editor, quick capture, ask panel.
- CLI: quick capture, run a routine now, revert a transaction, export a context pack, the stdio MCP shim.

## 5. Data model (the ledger)

Identifiers are ULIDs (my call: sortable, no coordination). Timestamps are UTC ISO strings; the UI renders local time. All tables carry `props` JSON for fields that do not deserve a column yet.

Core tables
- `project`: id, slug, title, kind (research, tooling, routine, admin, inbox), status (active, paused, done, archived), lane_order, summary_md, recurrence nullable (an RRULE string, routine projects only), props, created_at, updated_at. Lanes are projects. Routines such as the group meeting are projects of kind `routine` so their instances get a lane; a daemon-side recurrence materializer (section 7) creates their planned instances. One-offs without a home go to the single `inbox` project. `summary_md` is the agent-authored narrative summary (my call: prose is written by the agent through a write tool and reviewed like any transaction; the daemon never invents prose).
- `event`: id, project_id, kind (open string; seeded vocabulary: decision, meeting, milestone, idea, note, task_due, blocker), title, body_md, t_start, t_end nullable, status (happened, planned, suggested, dropped), source (human, agent, connector), props, created_txn. Events are the timeline blocks. Review state is not stored on events; it is derived from the transactions that touched the row (see Transactions and revert).
- `todo`: id, project_id nullable, title, body_md, due nullable, state (open, doing, done, dropped), priority, props. Todos render as due markers on the lane and in the inbox.
- `resource`: id, project_id, kind (folder, git_local, git_remote, claude_session, slurm_job, url, doc), locator, title, props. Examples of locator: an absolute path, a remote URL, a Claude session id plus transcript path, a Slurm job id plus host.
- `edge`: id, src_type, src_id, dst_type, dst_id, rel (open string; seeded vocabulary: forked_from, derived_from, depends_on, blocked_by, shares_idea, shares_module, supersedes, caused, presented_at, mentions, relates_to), props, valid_from nullable, valid_to nullable. All relations live here. A wire on the timeline is an edge between two events of different projects; same-lane order is implicit by time and needs no edge.
- `signal`: id, project_id, resource_id, kind (commits, session, files_changed, job_state, message), t_start, t_end, payload, dedupe_key unique. Raw connector output. Append-only, idempotent on dedupe_key, written outside the ledger (my call: machine-generated, high volume, revert is meaningless). Signals are never shown as blocks.
- `report`: id, title, kind (deck, memo), scope JSON (projects, range), source_md, props, created_at, updated_at. Each save is a ledger transaction, so reports are versioned.
- `routine`: id, name, trigger (a cron expression, or an event kind such as `session_end` with a debounce window), tz, enabled, runner (claude, ccx:<backend>, codex), prompt_ref (a prompt file under /data/prompts or a skill name), scope (projects, lookback days), cwd_mode (project_folder, data_dir), tool_policy (allowed tools list), caps (usd_per_run, turns, usd_per_day), write_mode (direct, propose), delivery (none, inbox, chatmaid), next_fire.
- `run`: id, routine_id nullable, kind (routine, ask, quick_add, report, summarize), started, finished, status (queued, running, ok, failed, capped, cancelled), runner, backend, model, session_id, tokens_in, tokens_out, tokens_cache, est_usd, turns, duration_ms, txn_ids JSON, result_md, error, log_path.
- `txn`: id, ts, actor_kind (human, agent, session, connector), actor_ref (user, run id, session id, connector name), model, backend, tool, tool_version, session_id, run_id nullable, proposed_by_run nullable (set when a human applied a proposal an interactive run produced; model and backend are copied from that run), intent, changeset BLOB, review (none, pending, accepted, rejected), review_ts nullable, reverts_txn nullable, reverted_by nullable.
- `change`: txn_id, table_name, op (insert, update, delete), pk, old JSON, new JSON. Materialized from the changeset at commit time so the UI diff view and search over history do not need to decode blobs.
- `setting`: key, value JSON (tokens, caps, feature flags).

Derived indexes
- FTS5 virtual tables over event, todo, project summary, report, and change (old and new text). Maintained by triggers.
- `vec` table (sqlite-vec) keyed by content hash of the embedded text; embeddings computed by a local model by default (my call: fastembed with a small ONNX model inside the container, zero cost; builder may swap). Vector search is optional in v1; FTS alone is acceptable initially.

Transactions and revert
- One ledger call wraps one SQLite transaction with a Session attached to the base tables. On commit the changeset and the materialized change rows are stored with the metadata above. Write MCP tools, API mutations, and CLI commands all go through this call.
- Revert of transaction T: invert T's changeset, apply with a conflict handler that aborts and reports the conflicting rows, and record the result as a new transaction with `reverts_txn = T`. Later unrelated changes survive. Conflicts surface in the UI instead of being overwritten.
- Review is per transaction, not per row (my call: one agent action touches several rows, and reject is a revert, which is transaction-level). Transactions by agent and session actors, and by connectors other than the recurrence materializer, start `pending`; human transactions are `accepted`. The nightly ingestion job writes no ledger transactions. The inbox lists pending transactions with their change rows. Accept sets the review fields on the transaction; reject reverts it. The timeline shows the amber dot on a block whose latest touching transaction is pending; the same rule badges todos, edges, resources and summaries.
- Agents never get delete or merge tools in v1. Destructive changes are human-only, so "propose" mode for routines reduces to writing a todo in the inbox describing the proposed change.
- FTS and vector tables are not tracked by the Session extension; after a revert the triggers and the embedding job resync them from base rows.

Exports
- `/data/exports/projects/<slug>/SUMMARY.md` is rendered by the daemon from `project.summary_md` plus a deterministic status block (status, open todos, last events, resources) whenever either changes. It is read-only for humans (owner's decision) and exists so a project's state can be opened in an editor or fed to a Claude Code session.
- Reports export to `/data/exports/reports/<id>.{md,html,pdf,pptx}`.

## 6. Agent layer

Runner contract
- Input: prompt (system additions plus user text), cwd (project folder mount or data dir), tool policy (explicit allowed tools; read-only file tools plus a narrow Bash pattern for git log by default), caps (max turns, max budget), runner choice (claude, ccx:<backend>, codex), model override, and a per-run MCP config that points at the daemon's MCP endpoint with a run-scoped bearer token.
- Invocation shapes (builder verifies flags against the installed versions): `claude -p` with JSON output, a non-interactive permission mode, allowed tools, strict MCP config, max turns, max budget, appended system prompt, and optional resume; `ccx <backend> -p` with the same flags; `codex exec` with JSON output and its own MCP config.
- Output: parsed JSON result text, session id, usage, estimated cost, turns, duration, written to the run row. The run's transactions are those recorded with its run id. The run's full stdout and stderr go to a log file under /data/runs.
- Attribution: every write tool call during a run becomes a transaction with actor_kind agent, actor_ref run id, model, backend, tool and version, session id, and the tool call's intent.
- Billing: non-bare headless runs use the owner's subscription login inside the container. Cost fields are the CLI's API-equivalent estimate, which is a proxy for subscription usage, not the rate-limit window.

Usage monitor and caps
- Every run records tokens in, out, cached, estimated dollars, turns, and duration, per routine and per runner.
- Caps: per run (budget and turns, enforced by the CLI flags and by the daemon killing the subprocess on timeout), per routine per day, and a global daily cap in estimated dollars. Exceeding a cap marks the run `capped` and pauses the routine until the next day. A global pause switch stops all scheduled runs.
- Usage page: spend per day, per routine, per runner; last runs with status and cost; cap status.

MCP tools (v1)
- Read: list_projects, get_project, timeline (projects, from, to, kinds), search (FTS, optional vector), list_todos, get_signals (project, from, to), get_context_pack (projects, from, to, token_budget), get_run.
- Write (each takes `intent`): record_event, update_event, add_todo, update_todo, link (src, dst, rel), unlink, set_project_status, set_project_summary, upsert_resource, save_report_draft, add_note. There is no create_project tool; projects are created by the owner, with connector discovery feeding suggestions to the inbox.
- Scope: a run-scoped token limits write tools to the routine's projects and write mode. Interactive runs ask and quick-add get read-only tokens; regenerate-summary gets a token limited to `set_project_summary` for its project; report-draft gets `save_report_draft` only. The owner's token (host shim, chatmaid) has full scope.
- External use: host Claude Code sessions reach the same endpoint through a stdio shim command (`trajectree mcp`) that forwards to HTTP with the owner's token, or through a direct streamable-HTTP MCP config. This is how a session in a project folder logs a decision or pulls context for related projects.

Routines (v1 set; prompts live under /data/prompts and are editable). Owner's decision after the builder check: the daemon's scheduled work is ingestion without an LLM; LLM runs are on request, per project or cross-project.
- nightly-ingest (no LLM): polls every connector, computes per-project staleness (signals or events newer than the summary beyond a threshold the builder sets), renders each SUMMARY.md status block, and materializes recurrence instances. Cheap enough to run more often than nightly; the cron is configurable.
- regenerate-summary (interactive, per project, on request from the stale chip, the project page, or the CLI): reads the context pack plus the daemon's pre-digested view of commits, sessions, file changes and job states since the last summary, and rewrites the summary through `set_project_summary`, which lands pending (owner's decision: the owner clicked the button, so this run holds a write token scoped to that one tool and project). Events and todos it spots come back as proposals, like other interactive runs.
- cross-project-review (daemon-side, on request, optionally scheduled): stale projects, overdue todos, suggested next steps as `suggested` events in the future zone, lint for contradictions, orphans, duplicates. Writes through the MCP tools, pending.
- meeting-prep (daemon-side, on request, optionally scheduled with a lead time before the next planned instance): drafts an agenda from recent events across projects; delivery to inbox, later chatmaid.
- report-draft (daemon-side, on request): section 8.
- Session summaries: a per-session summarize button in the UI runs one on demand; otherwise sessions are covered by regenerate-summary and by project sessions logging their own outcomes (next subsection). The daemon pre-digests each transcript into user prompts, assistant text, tool names and the final message, capped in size, so raw JSONL never enters a prompt. The `session_end` trigger with a debounce window exists in the routine model; it is off by default.
- ask and quick-add: read-only (my call: interactive runs hold no write tokens beyond the regenerate-summary exception, and the owner is present to confirm). They return answer text plus structured proposals via the CLI's JSON-schema output; the UI shows proposals with accept buttons, and accepting applies them through the API as a human transaction that records `proposed_by_run`, model and backend, so attribution survives.

Project sessions as writers (owner's decision after the builder check)
- The owner's interactive Claude Code sessions in a project folder are the primary per-project writers. `trajectree init-project <path>` registers the folder as a resource of a project (creating the project if needed), installs a managed, idempotent guidance section delimited by markers (in CLAUDE.md, or as a `.claude/rules/trajectree.md` file; builder's choice, updatable in place), and makes sure the MCP server is configured once at user scope through the stdio shim.
- The guidance tells a session when to write (a decision taken, a milestone reached, a blocker found, a follow-up owed, a relation to another project noticed), to keep entries short with a stated intent, and to fetch the context pack when the owner asks about related projects. The shim passes its working directory, so write tools default the project from cwd.
- These writes carry actor_kind `session`, the session id the daemon correlates from its running-session registry (hooks), and the model where known. They start pending like other LLM writes (my call: consistent with direct-write-with-review; the inbox offers bulk accept).

Scheduler
- Tables above; a loop wakes every 30 seconds, computes next fires from cron expressions and event triggers, enqueues runs. Two execution slots (my call): scheduled or requested cross-project routines run serially in one slot, interactive runs (ask, quick-add, regenerate-summary, report-draft, per-session summarize) run in a second slot so they never wait behind a review. Caps apply to both. Infrastructure failures retry with backoff up to three times; content failures do not retry. Missed fires run once if within a grace window, else skip. Manual trigger from UI, API, and CLI.

## 7. Connectors

Each connector is a module with `discover()` for resources, `poll()` for signals, and optional `on_hook(payload)`. Signals are idempotent on a dedupe key. Integration order decided by the owner: Claude Code sessions, local git, folders, GitHub and GitLab, Slurm, chatmaid.

- claude-sessions: host Claude Code is configured with `http` hooks for SessionStart, Stop, and SessionEnd that POST to the daemon on the loopback port; the daemon maps `cwd` to a project via folder resources and marks the session running or ended. A running session is a live marker on the lane's activity strip at the now line plus a running chip on the lane label; sessions are never blocks (my call, consistent with the signals rule and the chosen lane model). Independently, a periodic scan of the read-only Claude home finds transcripts by mtime, parses them defensively (user and assistant records; cwd, session id, git branch, timestamps, model, usage, AI title), and writes session signals and `claude_session` resources. The running-agents listing command only sees the container's own config, so host liveness comes from hooks and transcript mtime. The owner's ccx backend profiles keep their own session stores under each profile directory; the scanner reads those from the read-only mount of the backends root (Appendix B).
- recurrence materializer (daemon code, no LLM): for routine projects with a recurrence rule, creates planned instance events for the next eight weeks as connector transactions that are auto-accepted because they are deterministic, and rewrites only future instances when the rule changes. meeting-prep and the timeline read these like any planned event.
- local-git: per `git_local` resource, `git log --since` grouped per day and branch into commit signals; `git status --porcelain` for a dirty-tree flag. Runs via subprocess against the read-only mount.
- folders: periodic mtime scan (nightly by default, honoring .gitignore and an exclude list; data folders flagged by size) into files_changed signals per day.
- github and gitlab: poll with the host CLIs' auth if mounted, else a token in settings; PRs, issues, releases become signals; releases may become milestone events pending review.
- slurm: SSH to the login node with a dedicated restricted key; `squeue --me --json` and `sacct --json` into job resources and job_state signals; completion feeds the digest.
- chatmaid (contract only in v1): inbound via the MCP write tools or an HTTP endpoint with source `chatmaid` and a `message` signal kind; outbound through a `deliver(channel, message)` adapter interface, with v1 implementing only inbox and log. Trajectree never stores message bodies, only references and summaries chatmaid chooses to send.

## 8. Context compiler and reports

Context pack
- Input: project set, time range, token budget, optional question. Output: markdown with one section per project: summary, events in range as compact lines with ids, open todos, resources with the latest git log lines and session titles, plus retrieval hits from FTS (and vectors when enabled) for the question. Per-section caps keep the whole pack under budget; the compiler reports what it dropped.
- Exposed through the ask panel, MCP `get_context_pack`, CLI export, and the report builder.

Reports and decks
- A report run takes a context pack and a template (deck or memo) and drafts Marp-flavored markdown with speaker notes. The owner edits it in the app (markdown editor with preview). Each save is a transaction. Export: HTML always; PDF and PPTX through marp-cli with headless Chromium, shipped as an optional image variant. Decided with the owner; Marp is my call because it keeps decks as diffable text the agent can draft and the owner can edit.

## 9. Web UI

Timeline (lane model A, chosen from the clickable mock)
- Lanes are projects, ordered by kind then recency, collapsible and pinnable. Lane identity is position and label, not color. The lane label carries chips for a running session and for a stale summary; the stale chip opens regenerate-summary.
- Blocks are curated events. Kind color uses three validated hues (decision blue, meeting orange, milestone aqua); idea and note use a neutral pill shape; task due markers are neutral. The timeline endpoint returns events plus a small list of live sessions for the strip markers; signals never become blocks. Status: happened solid, planned dashed, suggested dotted and italic. Agent writes pending review carry a small amber dot. Text always uses ink tokens, never the kind color. The palette passed the colorblind and contrast validator in light and dark mode; dark mode is a separate validated set, not an inversion.
- Each lane has a thin activity strip under its blocks, built from signals, binned per day, week, or month depending on zoom, with hover showing the day's digest. Clicking a bin opens its sessions, commits and jobs in the details panel, including the copy-resume-command action for sessions; a running session shows as a live marker at the now line.
- Semantic zoom on time only: wheel zooms from days to years, drag pans, lane heights are fixed per row count. Row packing per lane with per-block label fallback and viewport culling, as prototyped.
- Wires render only for the hovered or selected block, with a toggle to show all. Cross-lane wires are cubic curves with a relation label; same-row wires arc above.
- A now line and a shaded future zone hold planned and suggested blocks.
- Details panel on click: title, body, status, kind, project, when, recorded, author (you, agent run with model and routine, or connector), transaction id, wires, and actions (accept, revert transaction, edit, add wire, copy resume command for sessions).

Other views
- Project page: summary with a stale chip and a Regenerate summary button when activity postdates it, events list, todos, resources with live status (dirty tree, running session, running jobs), routines touching it.
- Inbox: unreviewed LLM writes (agent runs and project sessions) with bulk accept, open todos by due date, proposals.
- Ledger: transactions newest first with actor, intent, cost, diff view per row, revert button with conflict preview.
- Routines and usage: schedules, run history, spend charts, caps, pause switch.
- Reports: list, editor with preview, export buttons.
- Quick capture: one text box; structured form fallback; the agent's parse is shown as proposals and applied on confirmation.
- Ask: select projects and range, see the pack size, ask, read the answer, optionally accept proposed writes.
- Settings: tokens, mounts discovered, connector status, runner availability, embedding model.

## 10. CLI

`trajectree` on the host (thin, talks HTTP): `init-project <path>` (register the folder, install the guidance section, configure MCP), `note "..."`, `todo "..."`, `event ...`, `summarize <project>` (regenerate-summary), `review` (cross-project review), `run <routine>`, `revert <txn>`, `context <projects...>`, `mcp` (stdio shim), `login` helper that runs the CLI login inside the container, `backends sync` to copy ccx profiles into the container-private config volume (settings and onboarding files copied, symlinked rules and skills resolved to copies, session stores not copied).

## 11. Deployment and security

- Docker Compose, one service. Image: multi-stage, Node build of the UI, Python runtime with uv, Claude Code native install, ccx script, codex optional, marp-cli with Chromium in a separate `-reports` tag.
- Volumes and mounts: `/data` persistent (database, exports, prompts, runs, config); `/cfg` persistent and container-private for CLI configs (`CLAUDE_CONFIG_DIR` and ccx backend profiles copied by `backends sync`, never mounted from the host); host Claude home and the host ccx backends root mounted read-only for transcript scanning; project root mounted read-only; a dedicated SSH key mounted read-only for Slurm. The scanner excludes the container's own `/cfg` transcripts.
- One-time `trajectree login` performs the CLI device login inside the container. Credentials never leave `/cfg`.
- Process: non-root, host uid, read-only root filesystem, all capabilities dropped, no-new-privileges, memory and pid limits. Port bound to 127.0.0.1 only. A bearer token generated at first run guards the API and MCP; run-scoped tokens are short-lived and limited to the routine's projects.
- Agent containment: project folders are read-only inside the container; the agent has no delete or merge tools; tool policies are explicit allowlists; budgets and turn caps bound every run. Network egress is needed for the LLM backends; an egress allowlist via a sidecar proxy is optional and left to the builder.
- Host-side pieces: the hook entries in the owner's Claude settings (http type, loopback URL), the stdio MCP shim, and the CLI. These are plain scripts with no container dependency.
- Backup: copy `/data`. Restore: start with the same volume.

## 12. Decided stack

- Backend: Python 3.12+, uv, FastAPI, uvicorn, apsw (SQLite with Session extension, FTS5), sqlite-vec optional, pydantic, croniter, httpx, the official MCP Python SDK for the server, fastembed optional. Tests with pytest.
- Frontend: React 19, TypeScript, Vite, d3 v7 for the timeline, TanStack Query for data, CodeMirror 6 for markdown editing. Tests with vitest and a Playwright smoke test.
- Packaging: Docker Compose; systemd user unit as an alternative for a native run.
- Rejected alternatives: Claude Agent SDK (owner prefers headless CLIs, subscription login, and model choice via ccx); Dolt (true branching but a second server and MySQL dialect); React Flow (uniform zoom fights the time axis, DOM ceiling near a thousand nodes); markdown vault as source of truth (no transactions, no relations; kept as derived export); OpenClaw and Hermes (own agent loops, lose Claude Code's tools).

## 13. Left to the builder

- Exact DDL, migrations strategy, API routes and payloads, SSE event shapes.
- Verification of CLI flags and JSON output fields against the installed claude, ccx, and codex versions, and the behaviour of each CLI with a read-only working directory.
- The transcript parser's tolerance strategy and the project-mapping heuristic for cwd values that match no folder resource.
- Prompt texts for routines, the quick-add parsing schema, and the Marp templates.
- d3 implementation details beyond the prototyped behaviour; lane virtualization if lanes exceed a few dozen.
- Embedding model choice and chunking; whether to enable vectors in v1.
- Test strategy and fixtures, including a fake runner that replays recorded CLI output.
- Egress restriction, if any.

## 14. Phasing (for the implementation plan)

1. Scaffold, ledger with transactions and revert, HTTP API, minimal timeline with manual events and structured quick capture, Docker image and volumes.
2. MCP server with scoped tokens, host shim, init-project guidance, claude-sessions and local-git connectors, nightly-ingest with staleness, activity strip, details panel, ledger view, inbox with review actions.
3. Runner, regenerate-summary, ask, quick-add proposals, usage page and caps, hooks for live sessions.
4. Folders, GitHub and GitLab, Slurm, cross-project review and meeting-prep, reports and decks, chatmaid contract and inbox delivery.

## 15. Builder check outcome

A fresh reviewer read the draft and raised eight questions. All are settled in the text above; the calls, in the reviewer's order of importance:

1. Review is per transaction; all agent transactions and non-deterministic connector transactions start pending; badges on rows derive from the latest touching transaction.
2. Project summaries are agent-authored prose in `project.summary_md`, written through `set_project_summary`, reviewable; SUMMARY.md is rendered by the daemon.
3. Session summaries: a per-session on-demand button, coverage by regenerate-summary and by project sessions logging their own outcomes, daemon-side transcript pre-digestion, and an off-by-default `session_end` trigger. (This superseded an earlier call that folded summaries into an LLM nightly digest; see the owner's changes below.)
4. Interactive runs are read-only and return structured proposals the UI applies on confirmation, recording the originating run, model and backend on the transaction.
5. Routine projects carry an RRULE; a daemon-side materializer creates their planned instances.
6. Running sessions are strip markers and lane chips, never blocks.
7. ccx is the owner's own launcher; its contract is in Appendix B.
8. Two execution slots: scheduled routines serial, interactive runs separate.

Changes requested by the owner after reading the reviewed draft (2026-10-21):
- The owner's project sessions are the primary per-project writers through the MCP write tools, with guidance installed by `trajectree init-project`.
- The nightly job is ingestion plus a staleness check with no LLM call.
- Summary regeneration is an on-request interactive run per project, triggered from the stale chip or the CLI, allowed to write `set_project_summary` directly as pending; other findings come back as proposals.
- Cross-project reviews, meeting prep and decks stay daemon-side on request.

## Appendix B: ccx contract (from /mnt/Apps/Projects/ccx, read 2026-10-21)

ccx is a bash script that runs Claude Code against an alternate backend by selecting a separate Claude Code configuration directory under `~/.config/claude-backends/<name>/` and then executing `claude` with every remaining argument, so `ccx <backend> -p ...` accepts the same flags as `claude -p`. Each profile holds a `settings.json` whose `env` block sets the Anthropic-compatible base URL, the auth token, the main and small model names and the four model-alias mappings, with permissions defaulting to manual mode; a `.claude.json` with onboarding complete, user-scope MCP servers and folder trust flags; and symlinks to the owner's shared rules, skills, agents and commands. Logins, session history and auto-memory are per profile and live inside the profile directory, so each backend has its own `projects/` session store. ccx refuses to start a profile without credentials. Known profiles: deepseek, kimi, lmstudio (a local server on the loopback interface, which from inside the container needs host networking or a host gateway address; builder decides).

Consequences for Trajectree: the runner treats `ccx <backend>` as `claude` with a different config root; `backends sync` copies profiles into `/cfg/backends/<name>/` and points ccx at that root through an environment variable the builder adds to ccx (the owner maintains ccx, so a small change there is acceptable); the transcript scanner reads each host profile's `projects/` store from the read-only backends mount; model attribution on runs comes from the profile's configured model names.

## Appendix A: research summary (2026-10-21)

Prior art: no tool combines the requirements. Borrowed ideas: PROJECTMEM (append-only event log with regenerated summaries, separate read and write MCP tools), OpenClaw and Hermes (job model: schedule, target, delivery, run history), Karpathy's LLM-wiki pattern (ingest, query, lint routines), Perfetto (flows drawn only on selection), Linear timeline (projects-only lanes), cctimereporter and claude-code-log (Claude transcripts are parseable; format is unstable).

Component verdicts: custom d3 timeline over React Flow (semantic x-only zoom; React Flow zooms uniformly and tops out near a thousand DOM nodes); SQLite with the Session extension over Dolt (per-transaction invert and apply in-process via apsw; Node's built-in SQLite cannot invert changesets, one reason for Python); own scheduler tables over APScheduler 4 (still alpha) or Huey; Claude Code http hooks for session liveness.

Facts that shaped decisions: headless CLI runs in non-bare mode use the subscription login; Claude Code's own scheduled tasks either run in Anthropic's cloud without local file access or need the desktop app open; sqlite-vec 0.1.9 is stable enough for brute-force search at this scale; Kuzu was archived in October 2025 and forked as LadybugDB; Graphiti is the only bitemporal agent-memory graph but needs Neo4j or FalkorDB.

Key sources: code.claude.com/docs/en/headless, /hooks, /claude-directory, /agent-sdk; sqlite.org/sessionintro.html; rogerbinns.github.io/apsw/session.html; github.com/asg017/sqlite-vec; github.com/d3/d3; reactflow.dev/learn/advanced-use/performance; arxiv.org/html/2606.12329v1 (PROJECTMEM); github.com/openclaw/openclaw; perfetto.dev/docs/visualization/perfetto-ui; linear.app/docs/timeline.

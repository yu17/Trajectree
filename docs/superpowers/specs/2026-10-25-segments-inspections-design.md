# Work segments, inspections and tiered events

Date: 2026-10-25. Status: approved by the owner on 2026-10-25. Builds on the
architecture design (`2026-10-21-trajectree-architecture-design.md`) and the
instance/import/models design (`2026-10-23-instance-import-models-design.md`);
where this document disagrees with them, this document wins.

## 1. Intent

Trajectree should turn a project's history, imported or ongoing, into blocks
and pills on its lane, with an agent the owner chooses doing the reading and
the owner approving the result in the Inbox. The owner's own Claude Code
sessions should stay out of it unless the owner opts in, because guidance and
MCP tools in every session cost context the owner would rather spend on the
work.

Today none of that happens. Ingest records each transcript as one `session`
signal spanning its first to last timestamp; blocks come only from the owner's
sessions writing through MCP, from `summarize` runs the owner starts, and from
the import's starting-point block. Measured on the owner's data (Appendix A):

- a pixiv_dl transcript holds three bursts of work, on Aug 30, Sep 22 and
  Sep 29, and shows on the activity strip as one tick on Aug 30;
- the later pixiv_dl transcript (`ddd53b9c`) continues the earlier one
  (`facdbc2d`): all 155 of the earlier one's records are copied into it, so
  the same work is counted twice;
- a session that started before the last summary and was resumed weeks later
  never marks the project stale again, because staleness compares the
  session's start with `summary_at`;
- the transcript digest keeps the first and last ten assistant messages and
  cuts at 12,000 characters, so for Keccak-Attack (25 active days, 49,000
  characters of digest) the middle three weeks never reach a model.

The owner's usage pattern makes these the common case, not the exception:
few transcripts per project, each resumed across weeks; sessions ended by
inactivity or a reboot, so `SessionEnd` hooks rarely fire; Codex used alongside
Claude Code.

## 2. Goals

1. Ingest, still without a model, splits every source's activity into **work
   segments** separated by idle gaps, counts copied resume history once, and
   groups a project's segments into **stretches**. The strip, staleness and
   the "what is new" window all follow the stretches.
2. Each project with uninspected stretches appears in the Inbox as **due for
   inspection**. The owner picks a model there and starts the inspection; one
   model can start several projects at once. Nothing calls a model before
   that.
3. An **inspection run** reads the project's uninspected stretches, decides
   for each whether it is new progress or a continuation of the latest block,
   and writes blocks, pills, todos and a summary update through the MCP write
   tools. A continuation revises the existing block in place. Every write lands
   pending in the Inbox.
4. **Import** uses the same path. A large backlog first gets an agent
   **boundary pass** that groups the stretches into episodes, then one
   inspection per episode, in order.
5. Events have two **tiers**: a **block** is a checkpoint, a **pill** is a
   moment on the way to one. The agent chooses the tier from the prompt's
   tests. A block is drawn at its end date; pills are hidden when zoomed out
   and lie between the blocks when zoomed in.
6. **Codex** sessions get the same liveness as Claude Code's through Codex's
   own hooks, and Codex transcripts go through the same segment rules.
7. Session guidance (the rules file and the user-scope MCP registration)
   becomes **opt-in** at `init-project`, with a command to add or remove it
   later.

## 3. Non-goals

- Any automatic model call. The nightly pass stays LLM-free; inspections start
  only when the owner starts them.
- Drawing blocks as spans. The span stays in the data and the details panel.
- Changing the rule that signals never become blocks.
- Removing `summarize`, `summarize --session`, `ask`, `quick-add`, routines or
  reports. They stay; inspection is a new run kind beside them. The one
  change they need: `summarize --session <id>` today looks a session up by the
  whole-session signal key, so it must resolve the id across that session's
  segment signals and digest the whole transcript, as it does now.
- Reading transcripts in any way other than Trajectree's existing read-only
  mounts and defensive parsers.

## 4. Anti-goals

Even if everything works, these would make it a failure:

- Two blocks for one piece of work because it was done in two sittings.
- A lane that repeats the activity strip: a pill per commit, or a pill per
  prompt.
- Lanes unreadable when zoomed out because pills are drawn everywhere.
- Guidance or MCP tools appearing in the owner's sessions without being asked
  for.
- A model called on a project the owner did not start an inspection for.
- Raw transcript text in a prompt. Digests only, as today.

## 5. Constraints

- All of the existing hardening holds: ingest is LLM-free; every agent write is
  a pending transaction; runs go through the confined runner with scoped
  tokens; prompts fence data in `<data>` blocks; the proposals schema stays
  valid under Codex strict mode (every property required, optional ones
  nullable).
- Transcript formats are internal to Claude Code and Codex and change between
  versions. Parsing stays defensive: an unrecognised line is skipped, never
  fatal.
- The hook receiver's body limit (64 KiB) and field caps stay.
- Codex 0.163 hooks run `command` or `mcp_tool` handlers, not `http`.
- Claude Code 2.1.310, Codex 0.163, the instance at
  `~/.local/share/trajectree`, published on 127.0.0.1:40210.
- Backwards compatibility of stored data: events already in the ledger keep
  their meaning; signals may be rebuilt (they live outside the ledger and are
  idempotent on their dedupe keys).

## 6. Work segments (ingest)

### 6.1 Definitions

- A **segment** is a contiguous stretch of activity in one source, a
  transcript, a repository or a folder, with no idle gap longer than
  `TRAJECTREE_SEGMENT_GAP_S` (default 7200, two hours; chosen from the owner's
  bursts in Appendix A). A segment has `t_start`, `t_end`, the source, and a
  payload describing what happened in it.
- A **stretch** is the union of a project's segments, across sources, whose
  gaps are shorter than the same threshold. Stretches are what the owner sees
  counted in the Inbox and what an inspection reads.

### 6.2 Transcript segments

- Each transcript is split on record timestamps. A gap longer than the
  threshold starts a new segment. Each segment is one `session` signal with
  dedupe key `<source>_session:<session id>:<segment start, ISO minute>`;
  the start of a segment never changes once it exists, so the key is stable
  as the file grows and the last segment extends. The payload keeps what the
  whole-session payload keeps today (session id, cwd, title, branch, model,
  tokens, transcript path, backend, running) plus `segment` (ordinal from 1),
  `records`, `prompts` (count) and `first_prompt` (first 200 characters of the
  segment's first prompt).
- The incremental reader stays: files are read from their stored cursor, and
  per-segment accumulators live in the stored summary.
- **Copied resume history counts once.** When Claude Code continues a
  session, it writes a new transcript with a new session id. The original
  transcript ends with a `continued-in` record whose `continuedInSessionId`
  names the continuation. The continuation opens with its own metadata records
  (`custom-title`, `mode`, `permission-mode`, `file-history-snapshot` and the
  like), then the original's records copied verbatim, same `uuid` and
  `timestamp`, with `sessionId` rewritten, then its own new records (on
  pixiv_dl: ten metadata records, the 155 copied records at positions 10–164,
  then the Sep 29 work). A record belongs to the transcript that first recorded
  it. A continuation contributes only the records after the copied run; its
  segments start there, and its signals carry
  `resumed_from: <original session id>`. Mechanism: follow the `continued-in`
  marker when the original has one; otherwise compare record `uuid`s against
  sibling transcripts in the same project directory. No global index of
  record ids is needed. The builder verifies whether Codex `resume` copies
  history into a new rollout or appends to the same file, and applies the same
  rule if it copies.
- Subagent transcripts and child rollouts stay skipped, as today.

### 6.3 Git and folder segments

`commits` and `files_changed` signals are already day-grained. They stay as
they are for the strip. For stretches they are treated as segments whose span
is the day's first to last commit (or the day) and merged with the transcript
segments by the gap rule.

### 6.4 Stretch computation

Stretches are computed from the signals at read time, not stored: sort a
project's segments by `t_start`, merge while the gap to the previous segment's
`t_end` is under the threshold. The Inbox count, the inspection input and the
"due" test all use this function.

### 6.5 Strip, liveness and staleness

- The strip bins by segment start day, so a resumed session shows on every day
  it was active.
- Liveness is unchanged: a segment whose transcript was modified inside
  `TRAJECTREE_LIVE_WINDOW` is running.
- Staleness changes meaning: a project is **due for inspection** when it has an
  uninspected stretch (section 8.4). The stale chip on the lane and the
  project page says "N stretches to inspect" and opens the inspection start
  (model picker). `summary_at` keeps its role for `summarize` only.

### 6.6 Migration of stored signals

Whole-session signals (`claude_session:<id>` and `codex_session:<id>`, no
segment suffix) would double count. The schema migration for this change
deletes all `session` signals and resets the transcript cursors, so the next
ingest pass rebuilds them as segments. Signals are machine-generated and
outside the ledger; nothing else refers to them by id. The owner's instance
holds one imported project at the time of writing.

## 7. Tiers: blocks and pills

### 7.1 Model

- `event.tier`, a new column, `block` or `pill`, default `block`. `kind`
  keeps its meaning (decision, meeting, milestone, idea, note, task,
  starting_point); `tier` says how big the event is. A decision may be a pill
  (a choice made along the way) or a block (a documented design that is a
  checkpoint); the agent, or the owner, decides.
- Migration: existing events get `block`, except kinds `idea` and `note`,
  which get `pill` so they keep the rounded look they have today.
- A pill may belong to a block through an edge with the new rel `part_of`
  (pill → block). The details panel of a block lists its pills; the details
  panel of a pill names its block. Edges are used rather than a column because
  a pill may exist before its block does, and because the owner can relink by
  hand with the existing `link` tool and edge API.
- `tier` is exposed everywhere events are: the events API (`POST` and
  `PATCH /api/events`), the timeline response, the MCP `record_event` and
  `update_event` tools, and the proposals schema (`tier`, nullable, default
  `block` when null). The CLI `event` command gets `--tier`.

### 7.2 What the agent is told (verbatim tests, used in every prompt that creates events)

- A **block** is a checkpoint: the project is left in a state you could stop
  at, demo, hand over or return to; something works, ships or is written down.
  A complex feature may be several blocks only if each one is a checkpoint by
  this test.
- A **pill** is a step on the way to a block: a design choice made, a notable
  commit (a revert, a switch of approach, the commit that first made something
  work), a blocker hit or cleared, a meeting held.
- Not a pill: ordinary commits, individual prompts, file edits. Those are
  already on the activity strip.
- A release is a checkpoint by the block test: something ships. The
  `milestone` events the remote connector records for releases stay blocks.
- Titles are 80 characters or fewer. A block body is 10 lines or fewer and
  says what was reached, how, and what was left open; a pill body is 3 lines
  or fewer. Bodies cite the dates of the segments they rest on.
- Every pill is linked `part_of` the block it leads to, when that block
  exists or is created in the same run.

### 7.3 Rendering

- A block is drawn at `t_end` (or `t_start` when `t_end` is null) as a fixed
  label chip, not as a bar from start to end. Rows are laid out to avoid label
  overlap as today. The details panel shows "start → end".
- Pills are drawn smaller (glyph plus short label) at their timestamp, in the
  same rows between blocks. They are hidden while the visible range is wider
  than `PILL_ZOOM_DAYS` (a web constant, initial value 60; the builder tunes it
  against the mock data so a month view shows pills and a quarter view does
  not). A hidden pill still appears in its block's details panel.
- Pending edits keep the amber dot. A block whose pending edit moved its end
  date is drawn at the new date.
- Timeline tests cover: pills hidden at a wide range and shown at a narrow one;
  a block with a span drawn at its end; a `part_of` edge not drawn as a wire.

## 8. Inspections

### 8.1 What an inspection is

A run of the new kind `inspect` (interactive slot, caps as for other runs)
over one project. Input: the project's context pack (summary, the most recent
blocks with their pills and bodies, open todos), and the project's uninspected
stretches, each rendered as a time span, its sources, a digest per segment,
and the **blocks whose spans overlap or abut the stretch's dates**, with their
pills (so a stretch that came back as due is read next to the block that
already covers part of it). A stretch inspected before is read whole, for
continuity, and marked "inspected up to <date>" so the agent knows what the
existing block already covers.
Tools: the same read access as `summarize` and the starting-point run (Read,
Grep, Glob and `git log` in the project folder, secrets denied), so the agent
can check that a checkpoint really works or is written down; the prompt says
the folder may be inspected read-only for detail.
Output: writes through the MCP write tools, scoped to the project and the tools
`record_event`, `update_event`, `add_todo`, `link`, `set_project_summary`,
all landing pending; a one-line `answer`; and `proposals` for anything outside
the project (a relation to another project, say), which stay proposals.

Writes go through tools rather than proposals because a continuation must edit
an existing event, and the proposals schema can only create. The spec's rule
"direct write with review" covers this: the owner clicked Start, so the run
holds a write token scoped to its project.

### 8.2 The continuation decision (verbatim, in the inspect prompt)

For each stretch, in time order:

- It is a **continuation** when it works on the same deliverable as an
  existing block: first the block whose span overlaps or abuts the stretch's
  dates, otherwise the most recent block, provided that block has not been
  superseded by a later checkpoint. Then: do not create a new block. Call
  `update_event` on that block: move `t_end` to the end of the stretch, revise
  the title if the checkpoint changed, revise the body to include the new
  work, keep its tier. Add pills for notable moments in the stretch and link
  them `part_of` the block. Work the block's body already describes is not
  recorded again.
- Otherwise it is **new progress**: create a block for each checkpoint the
  stretch reaches, pills for the notable moments, and todos for follow-ups
  left open. Cite the stretch's dates.
- A stretch that reaches no checkpoint and holds no notable moment (an
  exploration that went nowhere, a quick question) gets nothing. Say so in the
  answer.
- Call `set_project_summary` once, only if the stretches change the project's
  status, open questions or next steps.
- Each write carries a one-line intent naming the stretch dates.

### 8.3 Per-segment digests

`digest_transcript` and `digest_codex` are generalised to a time window: a
digest of the records inside `[t_start, t_end]` of one segment, with the same
shape (prompts, tools used, assistant head and tail, final message). The
per-segment cap is `settings.digest_max_chars` (12,000 today). Git segments are
rendered as their commit lines, folder segments as their path lists, as the
activity digest renders them now.

### 8.4 Due, watermark and concurrency

- `project.inspected_at`, a new column: the end of the newest stretch the last
  successful inspection covered. Set when the run finishes `ok`; a failed or
  capped run leaves it unchanged. Existing projects start with null, so
  everything since their scan start is due once; blocks they already have are
  in the context pack, and the continuation rule keeps them from being
  recorded twice.
- A stretch is **uninspected** when it ends after `inspected_at`, or when any
  of its segments was written or changed after the last inspection started
  (late commits, a source picked later, a folder added later). The builder
  picks how "changed" is tracked; the scanner must not mark an unchanged signal
  as changed on every pass.
- A stretch is **due** only once it has **settled**: its last activity is
  older than the segment gap and the project has no live session. Work in
  progress never sits in the Inbox; yesterday's work is due by the morning.
  The 24-hour `stale_settle_h` delay does not apply to inspections (it keeps
  its role for `summarize` staleness only).
- **Rejecting an inspection's writes does not move the watermark back.** To
  run a project's history again (another model, say), the owner uses
  **Re-inspect since <date>** on the project page, or `trajectree inspect
  <slug> --since <date>`: it sets `inspected_at` to that date and starts an
  inspection. The agent sees the existing blocks, accepted or rejected, in
  the context pack as they stand after the rejection.
- One inspection per project at a time: starting one while another is queued
  or running returns 409.
- Resolving the model follows the existing rules: a model name, or a runner
  alone for its default; no default anywhere.

### 8.5 Budget and the boundary pass

- A single inspection run covers at most `TRAJECTREE_INSPECT_BUDGET_CHARS` of
  rendered stretch digests (builder's initial value; must fit alongside the
  context pack within the runner's prompt limits for every supported runner).
- When a project's uninspected material exceeds the budget (an import, or a
  long absence), the inspection becomes a **series**: first a run of kind
  `inspect_boundaries`, read-only, over a compact timeline, one line per
  segment (dates, source, prompt count, first prompt, commit subjects, file
  counts); it returns a JSON list of **episodes**, each a span covering one or
  more whole stretches and a one-line label, in order, each fitting the budget.
  Then one `inspect` run per episode, serially and in order, each seeing the
  blocks the previous one created, so continuations across episodes work.
- The series is one owner action with one model. The runs share
  `props.series_id`; the Inbox row shows "episode k of n" while it runs; a
  failure stops the series and leaves the watermark at the last episode that
  finished. The owner restarts from the Inbox; the next series covers only
  what is still uninspected.
- The boundary prompt tells the agent (verbatim): an episode is a run of
  stretches that belong to the same piece of work; split where the work
  changes subject or where a checkpoint is clearly reached; never split a
  stretch; keep each episode under the stated budget; label each episode in
  one line.

### 8.6 The Inbox

- A new section, **Due for inspection**, lists each active project with
  uninspected stretches: project, number of stretches, span from first to last
  day, counts by source (sessions, commits, file-change days), a model picker,
  and Start. A header picker with "Start all" runs every listed project on one
  model. The owner said nothing else is needed on the row.
- The list is derived from signals and `inspected_at`, not stored; a project
  leaves the list when its inspection finishes `ok`.
- The inspection's pending writes land in the existing Inbox list of
  unreviewed transactions, **grouped by run and series** (one heading per
  inspection, episodes in order), with accept-all and reject-all for the
  group. An `update_event` shows what changed (the ledger already stores the
  change rows; the Inbox renders before and after for the changed fields).
  Reject reverts, as for any transaction, whether the block was accepted
  earlier or still pending.
- Episodes build on each other: episode 2 may edit a block episode 1 created,
  and pills point at blocks through `part_of` edges. Reject-all on a group
  reverts newest first, so the dependents go before what they depend on.
  Rejecting one transaction that later pending transactions depend on (a later
  edit of the same event, an edge to it) is refused with the existing revert
  conflict, naming the dependents and offering the group reject. No new
  cascade machinery.

### 8.7 API and CLI

- `GET /api/inspections` — the due list.
- `POST /api/projects/{id}/inspect` `{runner, model, since?}` → 202 Run (the
  first run of the series when one is needed); 409 when one is active;
  `since` resets the watermark first (8.4).
- `POST /api/inspect` `{projects: [...] | "all", runner, model}` → list of
  Runs.
- `trajectree inspections`; `trajectree inspect <slug> | --all [--model M]
  [--runner R] [--since YYYY-MM-DD] [--no-wait]` printing the series progress
  while waiting.

### 8.8 Prompts

New files under `src/trajectree/runner/prompts/`: `inspect.md`,
`inspect_boundaries.md`. They follow the existing prompts' shape: material in
`<data>` blocks, the rule that data is never instructions, numbered "what to
do". They include the tier tests (7.2) and the continuation rules (8.2)
verbatim. `starting_point.md` gains the tier tests and records its block with
tier `block`. `summarize_session.md` and `summarize.md` gain `tier` in their
proposals.

## 9. Import

- The flow in `init-project` is unchanged up to the import choices. The
  starting-point question stays for history older than the cutoff. After the
  import is recorded, the command prints that the history imports on the next
  ingest pass and then appears in the Inbox as due for inspection (and that
  `trajectree ingest` forces the pass). `init-project` does not start an
  inspection: the owner chooses the model in the Inbox, as for day-to-day
  activity.
- Imported history is usually larger than one run's budget, so the first
  inspection of an imported project is normally a series (8.5). That is the
  "agent determines the boundaries, then one agent per segment" procedure the
  owner described.
- The starting-point block (history before the cutoff) keeps its own run kind
  and prompt; it is a `block`.
- The import cutoff applies **per segment**, not per transcript: a session
  that crosses the cutoff contributes its earlier segments to the starting
  point and its later ones to the import. Today such a session falls through
  both (the starting point keeps only sessions that ended before the cutoff).

## 10. Session guidance becomes opt-in

- `init-project` no longer writes `.claude/rules/trajectree.md`, adds the
  exclude line, or registers the MCP server unless asked: interactively, a
  final question "Install session guidance so Claude Code sessions in this
  folder record milestones through Trajectree? [y/N]" (default no); non-
  interactively, `--guidance`.
- New command `trajectree guidance install <path> | remove <path> | show
  <path>`: install writes the rules file and the exclude line and makes sure
  the MCP server is registered at user scope; remove deletes the rules file and
  the exclude line it wrote, and leaves the MCP registration alone (other
  projects may use it); show reports both.
- The README's project-sessions section says when guidance is worth having:
  a project at a milestone or finished, where the owner wants the session
  itself to record the outcome.
- pixiv_dl already has the rules file from the current `init-project`; the
  owner removes it with `trajectree guidance remove /mnt/Apps/Projects/pixiv_dl`
  once this ships, or by deleting the file now.

## 11. Codex

### 11.1 Liveness through Codex hooks

- `trajectree hooks install` also installs Codex hooks when a Codex home exists
  (`$CODEX_HOME` or `~/.codex`): `SessionStart`, `Stop` and `SessionEnd`
  handlers of type `command` running `trajectree hooks post --token-file
  <file>`, which reads the hook JSON on stdin and POSTs it to
  `/api/hooks/codex` with the token. The token is the same liveness-only
  kind as Claude's, one per Codex config file, stored in a 0600 file under
  the Trajectree config directory because a command hook has no headers to
  carry it. `hooks show` and `hooks uninstall` cover both.
- On the owner's host, Codex 0.163 reads hooks from `~/.codex/hooks.json`
  (same shape as Claude's: event → matcher groups → handlers), gated by
  `[features] hooks = true` in `config.toml`, which is already set. Each
  handler is trusted separately in `config.toml` as
  `[hooks.state."<file>:<event>:<group index>:<handler index>"]
  trusted_hash = "sha256:…"`; the key is positional. The installer appends
  Trajectree's handlers as the last matcher group of each event, so the owner's
  existing groups keep their indexes, and uninstall removes only that group.
  If a group of the owner's was added after Trajectree's, uninstall warns that
  the trust entries after it need re-approval rather than silently shifting
  them. The builder confirms the payload fields (`session_id`, `cwd`,
  `transcript_path`, `hook_event_name`, `source`) against the installed
  version.
- **Trust (owner's decision, 2026-10-25):** the installer does **not** write
  `trusted_hash` entries for its own handlers; it prints that Codex will ask
  to trust the new hooks on its next start, and `hooks show` reports a Codex
  handler as "installed, not yet trusted" until the entry appears. Writing the
  hash ourselves would skip an approval step in the owner's tool.
- The receiver gains `/api/hooks/codex`; `handle_hook` takes the backend from
  the route and stores it on the session row (`session.backend` exists).

### 11.2 Ending sessions without SessionEnd

A session in the registry with no `Stop` for `TRAJECTREE_SESSION_IDLE_S`
(default: the segment gap) is marked ended with `end_reason = idle` and
`ended_at = last_seen`. The check runs on the scheduler tick. This makes the
existing `session_end` routine trigger usable with the owner's habits (sessions
left to die in the background or killed at reboot) and keeps the live list
honest. A later `Stop` from the same session id (a terminal that went quiet and
came back; a continued Claude Code session gets a new id and a new row) reopens
it: `ended_at` is cleared, and the `session_end` trigger's handled set is keyed
by `(session id, ended_at)` so the session fires again when it idles out again.
A turn that runs longer than the idle window without a `Stop` would count as
ended until its next `Stop` reopens it; that is accepted.

### 11.3 Codex transcripts

Codex rollouts go through the segment rules of section 6, with the digest
generalised per segment as in 8.3.

## 12. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TRAJECTREE_SEGMENT_GAP_S` | 7200 | Idle gap that separates segments and stretches |
| `TRAJECTREE_SESSION_IDLE_S` | the gap | Silence after which a hooked session counts as ended |
| `TRAJECTREE_INSPECT_BUDGET_CHARS` | builder's value | Rendered digest budget per inspect run; above it, a series |
| `PILL_ZOOM_DAYS` (web constant) | 60 | Visible range above which pills are hidden |

All pass through the deploy compose template and `env.example` like the
existing variables.

## 13. Data model changes (summary)

- `event.tier TEXT NOT NULL DEFAULT 'block'`; backfill `pill` for kinds
  `idea`, `note`.
- `project.inspected_at TEXT`.
- Edge rel `part_of`.
- Run kinds `inspect`, `inspect_boundaries` (both interactive slot);
  `run.props.series_id`, `episode`, `episodes`.
- `session` signals keyed per segment; migration deletes old `session` signals
  and resets transcript cursors.
- `session.backend` populated from the hook route; `end_reason` may be `idle`.

## 14. Left to the builder

- The accumulator and cursor layout for per-segment incremental reading, and
  the prefix-comparison mechanism for copied resume history.
- The stretch merge function's placement and its tests.
- How "a segment changed since the last inspection" is tracked without
  touching unchanged signals.
- `TRAJECTREE_INSPECT_BUDGET_CHARS` and the rendered line format of the
  boundary pass timeline.
- Prompt wording beyond the verbatim tests and rules in 7.2, 8.2 and 8.5.
- The exact pill glyph and label treatment; the row layout of the Inbox
  section; how an `update_event`'s before and after are shown.
- Codex hook configuration format, feature flag and payload, verified against
  the installed version; whether Codex resume copies history.
- Test strategy. Expected at least: segment splitting and stable keys on a
  growing file; copied-prefix dedupe on the two pixiv_dl shapes (same prefix,
  diverging tail); stretch merging across sources; due/watermark including the
  resumed-old-segment case; 409 on a second start; series ordering and
  stop-on-failure; tier migration; timeline zoom behaviour; hooks install
  merging for Codex; idle end and reopen.
- Suggested order: (a) segments, dedupe, strip and due computation; (b) tier,
  rendering, MCP and proposal fields; (c) inspect runs, prompts, Inbox, API,
  CLI; (d) import flow text and guidance opt-in; (e) Codex hooks and idle end.
  Each step leaves the instance working.

## Appendix A. Measurements (2026-10-25, owner's host)

| What | Value |
|---|---|
| Transcripts under `~/.claude/projects` | 837 |
| pixiv_dl `facdbc2d` (original) | 426 KiB, 156 records, 8 prompts, active Aug 30 and Sep 22; ends with a `continued-in` record whose `continuedInSessionId` is `ddd53b9c` |
| pixiv_dl `ddd53b9c` (continuation) | 624 KiB, 191 records, 12 prompts, active Aug 30 (19:20–19:59, 5 prompts), Sep 22 (3 min, 3 prompts), Sep 29 (4 min, 4 prompts); 3 segments at a 2 h gap; opens with 10 metadata records, then the original's 155 records at positions 10–164 (same `uuid` and `timestamp`, `sessionId` rewritten), then its own; full digest 6,251 chars |
| Largest transcripts, full digest vs 12,000 cap | Misaka-Project 124 MiB, 10 days, 40,778 chars; Keccak-Attack 47 MiB, 25 days, 49,052 chars; Trajectree 50 MiB, 5 days, 18,450 chars |
| Codex | 0.163.0 in the image; hooks in `~/.codex/hooks.json` (SessionStart, Stop, UserPromptSubmit, PermissionRequest groups present, one handler each); `[features] hooks = true` and 4 `trusted_hash` entries in `config.toml`; handlers `command` and `mcp_tool` |
| Claude Code | 2.1.310; owner's `~/.claude/settings.json` carries its own hooks in separate matcher groups |

## 15. Calls made at the builder check

A fresh reviewer planned from the first draft and asked nine questions. These
were settled here without the owner (each is open to override):

1. A stretch that comes back as due is read whole, marked "inspected up to
   <date>", next to the blocks overlapping its dates; the continuation target
   is the overlapping block before the most recent one (8.1, 8.2).
2. Due means settled: last activity older than the segment gap, no live
   session; the 24-hour `stale_settle_h` is not applied to inspections (8.4).
3. Rejecting writes does not move the watermark; "Re-inspect since <date>"
   does (8.4, 8.7).
4. The Inbox groups an inspection's transactions; reject-all reverts newest
   first; a single reject with dependents is refused with the existing
   conflict (8.6).
5. Inspect runs get the same read access to the project folder as `summarize`
   (8.1).
6. Idle-ended sessions reopen on a later `Stop`; the `session_end` handled set
   is keyed by `(id, ended_at)`; a turn longer than the idle window is an
   accepted edge (11.2).
7. Existing projects start with a null watermark; the import cutoff applies
   per segment (8.4, 9).
8. Releases are blocks; "a release" is no longer a pill example (7.2).
9. Codex hook trust: the owner approved the default in 11.1 (no trust hashes
   written by the installer).

## Appendix B. Owner decisions recorded in this design

- Results appear in the Inbox after the overnight pass; the owner picks the
  model and starts the inspection. No automatic model calls.
- The MCP route from the owner's own sessions is rarely wanted: it pollutes
  context and suits only a finished project or a major milestone.
- Blocks are major updates (a feature added, a document prepared); a complex
  feature may be several blocks if each is a checkpoint. Pills are smaller:
  intermediate commits that matter, design decisions chosen. The agent decides
  which, from a carefully written prompt.
- Pills have timestamps, are hidden at wide zoom, and lie between blocks when
  zoomed in. Blocks are drawn at their end date, not as spans.
- A continuation may change an accepted block in place as a pending edit.

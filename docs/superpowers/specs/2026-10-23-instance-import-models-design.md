# Trajectree: instances, project import, model choice, Codex sessions

Date: 2026-10-23. Status: approved by the owner on 2026-10-24. Author: brainstormed with the project owner; calls marked "(my call)" were made by the assistant and can be overridden. Builds on the architecture design `docs/superpowers/specs/2026-10-21-trajectree-architecture-design.md` (v1, fully implemented on main at a9b4275); where this document is silent, that one still holds.

## 1. Intent

The owner is about to deploy a real instance over `/mnt/Apps/Projects` (44 folders, 15 git repos, 3,163 Claude Code transcripts since April 2026, 16 Codex sessions) and have Trajectree establish the current state of every project retroactively. Four gaps stand in the way:

- **A. Instance install.** Today the instance is the repo checkout: compose builds from the working tree, and `./data`, `./cfg` and `.env` land inside it. Because the checkout sits inside the projects root, which runs can read, a run can read the owner token and the OAuth token through `/projects/Trajectree/{data,cfg}`. The owner wants "running compose in a directory installs an instance there", independent of the dev checkout.
- **B. Import choices per project.** The history look-back is one global setting (`TRAJECTREE_SCAN_SINCE_DAYS`) applied silently on each resource's first poll; nothing asks the owner what to import, and history older than the cutoff is dropped without a trace. The owner wants to be asked, per project, which sources to import and how far back, with everything older condensed into one starting-point block.
- **C. Explicit model choice.** Runs fall back to `TRAJECTREE_DEFAULT_RUNNER` and the CLI's own default model; the web UI never sends a model. The owner wants no defaults at all: every action names its model, chosen from a known list of backends and their models.
- **D. Codex sessions.** Only Claude Code transcripts are ingested; the owner's Codex sessions are invisible.

Order of work: A, B, C, D, as the owner asked. B's starting-point block needs a model before C's catalog exists: B ships with the model name (and optional runner) taken as typed, and C adds catalog validation and the numbered model list to that prompt along with every other run command. (my call) D adds the `codex` source to B's probe.

## 2. Goals, non-goals, anti-goals

Goals
- An instance is a directory: copy one compose file and an `.env` into it, run `docker compose up -d --build`, and everything the instance owns lives there.
- Registering a project asks what to import and from when, and summarizes the rest into one block on the timeline.
- No LLM run starts without a model the owner chose for it; the choice is made from a refreshable catalog of backends and their models.
- Codex sessions appear in the activity strip like Claude Code sessions.

Non-goals
- Making `ccx:lmstudio` (profile base URL `127.0.0.1:40090`) reachable from the container. That needs backend URL rewriting and host networking; later.
- Live hooks for Codex sessions.
- Per-task-type default models (explicitly rejected by the owner in favour of no defaults).
- Hand-maintaining a model list in the UI (adding or removing catalog entries by hand).
- A web UI for project registration; `init-project` stays a host CLI command.

Anti-goals
- A deployment that silently runs code from the dev checkout's working tree.
- Any path by which a run can read the instance's tokens.
- A run that "just works" on a model the owner never picked.
- History silently dropped: anything not imported must either be in the starting-point block or be a choice the owner made.

## 3. Constraints carried from v1

These hold for every change below, as they did for phases 1 to 4:
- Secrets (OAuth token, run tokens `trj_…`, connector tokens, API keys found in backend profiles, ssh key) never appear in logs, run rows, API responses, error details, signal payloads, catalog rows or argv.
- No subprocess, HTTP or ssh call while the ledger lock is held.
- The container stays read-only, non-root, `cap_drop: ALL`, `no-new-privileges`; host folders are mounted read-only.
- The host is written only by `init-project` and `hooks install`, as disclosed in the README's "What gets written where" table; any new host write is added there.
- Every agent write lands pending in the Inbox, attributed to its run.
- New with this design: every runner process runs under Landlock (section 4), fail-closed.
- Never force-push; main is pushed only on the owner's request.

## 4. A. Instance install

### Decided
- **Template.** The repo gains `deploy/compose.yaml` and `deploy/env.example`. Installing an instance is: create a directory (the owner's choice: `~/.local/share/trajectree`), copy both files in (the second as `.env`), create `data/` and `cfg/` (with `chmod 700 cfg`) so Docker does not create them root-owned, fill `.env`, run `docker compose up -d --build` there.
- **Everything instance-owned is relative to that directory:** `./data` → `/data`, `./cfg` → `/cfg`, `.env`. Host inputs are absolute paths taken from variables: `${HOME}/.claude/projects`, `${HOME}/.config/claude-backends`, `${HOME}/.codex/sessions` (for D), `${TRAJECTREE_PROJECTS_ROOT}`.
- **The image is built from git, not from a checkout.** The template's build context is `${TRAJECTREE_GIT_URL}#${TRAJECTREE_REF}`, defaulting to `https://github.com/yu17/Trajectree.git` and `main` (the repo reads anonymously; Compose accepts git URL contexts). The owner can pin a tag or commit. Unpushed or uncommitted dev work can never reach an instance.
- **Template settings:** compose project `name: trajectree`; `target: reports` (full image); build arg `INSTALL_CODEX=1`; `mem_limit: 4g`; pass-through of `TRAJECTREE_INGEST_EVERY`, `TRAJECTREE_SCAN_SINCE_DAYS`, `TRAJECTREE_CHATMAID_URL`, `TRAJECTREE_GITLAB_HOST`, `TZ` → `TRAJECTREE_TZ`, `UID`/`GID`; all the v1 hardening.
- **The dev compose stays** in the repo root for development and image testing, renamed `name: trajectree-dev` and published on `127.0.0.1:8743`, so running it can never replace or collide with the instance.
- **Host CLI** is installed from git too: `uv tool install git+https://github.com/yu17/Trajectree@<ref>`; it keeps reading `~/.config/trajectree/token` and `TRAJECTREE_URL`.
- **README** gets an "Install an instance" section with the whole sequence: directory and files, build, token copy, host CLI, runner logins (`claude setup-token` + `trajectree login`; `docker compose exec -e CODEX_HOME=/cfg/codex trajectree codex login --device-auth` for Codex, which gives the container its own login instead of sharing the host's `auth.json`; `trajectree backends sync`), `trajectree models --refresh` (C), then registering projects (B). The existing "Run with Docker" section becomes the dev section and says plainly not to keep instance state inside the projects root.
- **Run read-deny rules** (v1: `/cfg/**` and `/data/**`) stay, and gain the backend profiles mount (`/host/claude-backends/**`). Instance state is no longer under the projects root. (my call: no attempt to detect a checkout under `/projects`.)

### Run confinement with Landlock (decided with the owner after the Codex sandbox check, section 9)
Every runner process (`claude`, `codex`, `ccx:<backend>`) is started behind a Landlock ruleset applied by the daemon, so the kernel, not the agent CLI, decides what a run can touch. This applies to the whole process tree, whichever tool the model uses.
- **Mechanism:** the daemon starts each run as `python -m trajectree.runner.confine <rules> -- <runner argv>`. That small module restricts itself with Landlock, then executes the runner. (my call: a wrapper process rather than `preexec_fn`, which is unsafe in the daemon's multithreaded process.) It uses only the Python standard library (raw `landlock_*` syscalls), as the throwaway test wrapper did.
- **Allowed, read and execute:** the system directories (`/usr`, `/lib`, `/lib64`, `/bin`, `/sbin`, `/etc`, `/opt`, `/app`), `/proc` and `/sys`, and the folders of **all registered projects** (every `folder` resource, through the path map), so an agent can read another project the work refers to (decided with the owner). Folders under `/projects` that are not registered stay unreadable. The set is computed when the run starts; a project registered later is readable from the next run on.
- **Allowed, read and write:** `/dev`; a private temp directory for the run (its `TMPDIR`), never `/tmp` as a whole; the run's own directory `data/runs/<run_id>/`, which holds its MCP config and output files; and only that runner's own config directory: `/cfg/claude` for `claude`, `/cfg/backends/<name>` for `ccx:<name>`, `/cfg/codex` for `codex`. The run's `HOME` lies inside the allowed directories.
- **Everything else is denied:** the rest of `/data` (database, owner token, other runs), the rest of `/cfg` (other runners' credentials, connector tokens, the shared home), `/host/*` (transcripts, backend profiles), and unregistered folders.
- **Where the kernel supports it** (Landlock ABI 6 and later), abstract Unix sockets and signals are scoped, so a run cannot signal or connect to processes outside its domain.
- **Fail closed:** if Landlock is unavailable or a rule cannot be added, the run fails with an infrastructure error saying so; it never runs unconfined. The Landlock ABI version used is recorded on the run.
- **Codex** runs with `--sandbox danger-full-access`: Codex's own bubblewrap sandbox cannot work in the hardened container, and Codex's documentation recommends this mode when the container provides isolation. The Landlock layer is that isolation.
- **Claude** keeps its v1 permission rules (`Bash(git log *)`, read-deny rules) as a second layer.
- **What remains possible, accepted:**
  - A run can read its own runner's login. An agent program must read its credential to work.
  - Network access stays open, because the model API needs it, so a run could still send readable project files somewhere (accepted in v1).
- **The image gains `ripgrep`** (Codex's search command); no `bwrap`.
- **Not confined:** the daemon's own subprocesses, which no model controls (`git`, `ssh`, `marp`).
- **Left to the builder:** the exact rule set per run kind; how the confinement is tested (a real-kernel test that skips when Landlock is unavailable, plus a container smoke test driving `codex exec` with a local mock of the Responses API, as in section 9); the per-run `HOME` layout.

### Left to the builder
- Whether `pids_limit` needs raising for Chromium exports under the 4 GB limit (verify with a PDF export).
- How to smoke-test the template in CI-less development (a `docker compose -f deploy/compose.yaml config` check with a local context override is enough).

## 5. B. Import choices and the starting-point block

### The interaction (decided with the owner)
`trajectree init-project <folder>` (interactive, on a TTY):
1. Registers the project as today (slug, title, kind, rules file, exclude, MCP shim).
2. Probes the folder and prints a numbered list of sources found, each with a count and the date of its oldest item, for example:
   ```
   1. git            412 commits, oldest 2023-03-14
   2. claude         1 session, oldest 2026-07-02
   3. codex          0 sessions
   4. folder         1,204 files, oldest change 2022-11-01
   5. github remote  github.com/ferhaterata/psca (suggested; polled after you accept it)
   ```
   Sources with nothing found are listed but not selectable.
3. Asks the owner to pick some or all (`1,2,4`, `all`).
4. For each picked source with anything older than the default cutoff (`TRAJECTREE_SCAN_SINCE_DAYS` days ago), asks how far back to import: the default cutoff, a date, or everything. Sources with nothing older are not asked.
5. If any picked source has history older than its chosen start, asks which model writes the starting-point block (same model choice as C: a model name, a runner only if the name is ambiguous), or lets the owner skip the block.
6. Prints a summary of the choices, asks for confirmation, then saves them. The next ingest pass imports; the starting-point run is queued right away.

Every choice has a flag for scripted use: `--sources git,claude,folder` (or `all`), `--since <source>=<YYYY-MM-DD|all|default>` (repeatable), `--start-model`, `--start-runner`, `--no-start-block`, `--yes`. Without a TTY, missing choices are an error naming the flags; nothing is defaulted silently. (my call, consistent with C's "no defaults")

### What gets imported
- From each picked source, history from its chosen start onward is imported by the existing connectors exactly as today (commits, session signals, file activity, remote suggestions).
- Sources not picked are not imported for that project, now or by later ingest passes, until the owner re-runs `init-project` and picks them.
- The chosen start replaces `TRAJECTREE_SCAN_SINCE_DAYS` for that project's first poll of that source; afterwards the connectors stay incremental, as they already are (per-resource `last_poll` cursors for git, folders, remotes and Slurm). `TRAJECTREE_SCAN_SINCE_DAYS` becomes only the default offered by the prompt and the bound for projects registered without the prompt (the seeded `inbox` project; API-created projects).
- Claude and Codex sessions have no per-project resource today: they attach to a project by `cwd`. The per-project choices therefore also gate attachment: a session is attached to a project only if that project picked the session source and the session ends on or after the chosen start. The daemon's transcript-file mtime cutoff must not hide files the owner asked for ("everything" means no cutoff for that project). (my call on the mechanism; the builder decides where the choice is stored)
- **Remotes.** The probe lists the folder's fetch remotes (discovered daemon-side as the ingest does today). Picking one in `init-project` is the owner accepting it: it is created as an accepted `git_remote` resource in a human transaction, with no Inbox step. A remote not picked is recorded as declined for that project, so ingest never suggests it again (the existing `remote:suggested` mechanism). No "how far back" question is asked for remotes (their oldest item is unknown without a network call); a remote's first poll uses the project's git start if one was chosen, otherwise the default cutoff. A remote that appears later (a new `git remote add`) is suggested in the Inbox as today. (my call, from the builder check)
- The probe runs **in the daemon** (`POST /api/import/probe` or similar), so it reports what the daemon can actually read through its mounts and path map, not what the host sees. (my call)

### The starting-point block
- One event on the project's lane, `kind: starting_point` (event kinds are free text), `status: happened`, dated at the **latest** chosen start among the picked sources that skipped older history; sources set to "everything" do not count. Its input is everything not imported, from every picked source (each source's history older than that source's own start), so nothing the owner chose to skip is lost; items imported individually stay on the timeline as usual. (my call, from the builder check) Its title says the project's state at that date; its body is a Markdown summary of what came before: what the project is, what was built or found, open threads, as of that date.
- It is written by a new run kind (`starting_point`), daemon-side, read-only on the folder like `summarize`, with a scope allowing only `record_event` on that project. It lands pending in the Inbox like every agent write.
- Its input is the history older than each picked source's start, condensed to a token budget: git commits (dates, subjects, sizes), session digests (Claude and, after D, Codex), and a folder snapshot (top-level tree with sizes and dates). Remote data is not included (remotes are only suggestions at registration time). (my call)
- The prompt is seeded at `data/prompts/starting_point.md` and editable, with the v1 `<data>` fencing and rules sentence.
- If no picked source has anything older than its start, there is no block and no model question.
- Re-running `init-project` on an existing project shows what is already imported per source and offers only sources not yet picked; a source already imported is not re-imported. It may add a new starting-point block only for newly picked sources. (my call)

### Left to the builder
- Where per-project import choices live (resource props, project props, or state keys) and how connectors read them.
- The token budget split across sources in the starting-point input, and how a huge git history is condensed.
- How the folder probe counts files cheaply on large folders (CLMI is 53 GB; reuse the folder connector's skips and big-directory rule).
- Exact wording of prompts and the summary screen.

## 6. C. Explicit model choice and the model catalog

### Decided with the owner
- **No defaults.** Every action that starts a run must name a model. A request without one fails with the error `no model was chosen`. `TRAJECTREE_DEFAULT_RUNNER` is removed, as is any fallback to a CLI's default model.
- **CLI:** every run command (`summarize`, `summarize --session`, `ask`, `add`, `report draft`, `run`, `review`, `prep`, and B's `init-project`) keeps `--model` and `--runner`. `--model` is required; `--runner` is needed only when the model name alone is ambiguous, either because two backends list the same name or because one model is reachable through more than one runner (for example a future direct LM Studio connector next to `ccx:lmstudio`). An ambiguous name without `--runner` fails, naming the runners that offer it.
- **`trajectree models`** prints the known catalog: each backend, its models, when it was last refreshed, and the last refresh error. `trajectree models --refresh` refreshes the catalog first, then prints it.
- **Web UI:** every control that starts a run (Summarize on the timeline stale chip and in the Project tab, Summarize session, Ask, Quick capture, Draft report, run-now on a routine) gets a two-level picker: backends at the top level, their models beneath. Nothing is preselected and nothing is remembered between uses; the action stays disabled until a model is picked. (my call: disabled control rather than an error after clicking)
- **Settings:** a Models section listing each backend with its models, last refresh time and last error, and a Refresh button.
- **Routines** run unattended, so each stores its runner and model. A routine without a model cannot be enabled (the enable fails with `no model was chosen`), and the scheduler never fires one. The two seeded routines (`cross-project-review`, `meeting-prep`) are seeded without a model. The CLI gains a way to set a routine's model (`trajectree routine set <name> --model … [--runner …]`); the Routines section in the web UI gets the picker.

### The catalog
- Stored in the daemon (a non-ledger table, like `routine`), one row per runner and model: runner (`claude`, `codex`, `ccx:<backend>`), model id exactly as passed to that runner, a display name when the backend gives one, where it came from, and per runner the last refresh time and error.
- **Refresh is explicit:** the CLI command and the Settings button, plus automatically after `trajectree backends sync` (profiles may have changed) and once at daemon start when the catalog is empty. (my call on the two automatic triggers) No refresh happens on the request path.
- **What each runner lists** (verified sources marked):
  - `codex`: the models in Codex's own cache, `/cfg/codex/models_cache.json` (verified on the host: a `models` array with `slug`, `display_name`, `visibility`), excluding `visibility: hide`. If the file is missing, the refresh error says to log Codex in. The builder checks whether a Codex command can refresh that cache non-interactively; if not, the cache Codex maintains is what is listed.
  - `ccx:<backend>`: every model named in the synced profile's `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_*_MODEL` and `CLAUDE_CODE_SUBAGENT_MODEL` keys, deduplicated, kept verbatim (for example `deepseek-flash[1m]`) (verified on the host), plus, best effort, the models the backend's own models endpoint returns using the profile's credentials. An unreachable backend keeps its profile models and records the error.
  - `claude`: the subscription's model list from Anthropic's models API using the stored OAuth token, if that works (the builder verifies); otherwise a list shipped with Trajectree of the current Claude models (`claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`), recorded as the source so the owner can see it is not live.
  - A runner whose binary or credentials are missing lists no models and shows why.
- **Validation at request time:** the model must be in the catalog for the resolved runner. With `--runner` given and the model not in the catalog, the run is accepted with a warning in the run's result, because a shipped or cached list can lag a real new model. (my call) Without `--runner`, an unknown model fails with `unknown model <name>; run: trajectree models --refresh`.
- `GET /api/models` serves the catalog to the web UI; `POST /api/models/refresh` refreshes it. Both are owner-only, and neither returns credentials or backend URLs containing them.

### Left to the builder
- How the run request models and API change shape (required `model`, optional `runner`), and the migration of stored routines (existing rows keep their runner/model; rows without a model are disabled on migration with `last_error` saying why).
- The picker component's exact look; it must work at phone width and be keyboard-accessible with an accessible name.
- Timeouts for backend model-list calls (short; refresh is never on the request path).

## 7. D. Codex sessions

### Decided
- The instance mounts `${HOME}/.codex/sessions` read-only at `/host/codex/sessions` (only the transcripts; never `auth.json`, `config.toml` or the SQLite state).
- A Codex session becomes a `session` signal attached by `cwd` exactly like a Claude Code session (same signal kind, `backend: codex` in the payload), so the activity strip, project attachment, session listing and per-session summaries treat both alike. It appears as the `codex` source in B's probe and prompt.
- Format (verified on the host, Codex 0.163.0): files at `sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl`; the first record is `session_meta` with `id` and `cwd`; `turn_context` records carry `model` and `cwd`; `response_item` records carry messages; `event_msg` records carry task and token-count events. Like Claude's format it is internal and may change: the parser is defensive and skips what it does not recognize.
- `summarize --session <id>` works for a Codex session through a Codex-specific digest (prompts, tool calls, assistant messages, final message), with the same size cap.
- No live hooks for Codex; a Codex session counts as live by file modification time, like a Claude session between hook events.

### Left to the builder
- Incremental reading (byte offsets, as the Claude scanner does) and the configuration variable for the Codex sessions root (mirroring `TRAJECTREE_CLAUDE_HOME`; empty disables).
- Which records hold token counts.

### Decided after the builder check
- A Codex session's title is its first user prompt, shortened. Codex 0.163 keeps thread names only in `~/.codex/session_index.jsonl`, outside the mounted transcripts; bind-mounting that single file would go stale because Codex rewrites it by replacing the file. (my call)

## 8. What success looks like

- In `~/.local/share/trajectree`, `docker compose up -d --build` builds from GitHub `main` and starts an instance whose state is entirely in that directory; the dev checkout can be deleted without affecting it.
- `trajectree init-project /mnt/Apps/Projects/psca` lists git, Claude sessions and the folder with their oldest dates, asks how far back for git (2023 history predates the default cutoff), asks for a model, and after one ingest pass and one run the project shows its commits from the chosen start onward and one pending starting-point block summarizing the years before.
- `trajectree summarize psca` with no model fails with `no model was chosen`; `trajectree summarize psca --model gpt-6.1-sol` runs on Codex; the web Summarize control is disabled until a model is picked from the backend menu.
- The owner's Codex sessions in project folders appear in the activity strip.

## 9. Builder check outcome

A fresh builder read this document on 2026-10-23 and raised four questions. Three were settled as calls (starting-point date and input, remotes at registration, Codex titles; see sections 5 and 7). The fourth went to the owner:

**Runs reading the instance's secrets.** Moving state out of the projects root does not close two routes the code still has: (1) the Claude read-deny rules do not cover the mounted backend profiles (`/host/claude-backends`, which hold API keys); (2) Codex runs use only `--sandbox read-only`, which reads the whole filesystem, including `/cfg` (OAuth, connector tokens, Codex `auth.json`) and `/data/token`. Route 1 is closed by adding the profiles path to the Claude deny rules (my call). For route 2 the owner asked to verify Codex's own path deny rules and to pause if they do not work.

**Codex sandbox check (2026-10-23, Codex 0.163.0, image built from this branch with `INSTALL_CODEX=1`):**
- Codex supports deny entries in a permission profile (`[permissions.<name>.filesystem]`, `"/cfg" = "deny"`), enforced on Linux only by bubblewrap.
- The image ships no `bwrap`, and the musl release asset bundles none: `bubblewrap is unavailable`.
- With Debian's `bubblewrap` added, under the compose hardening (`read_only`, `cap_drop: ALL`, `no-new-privileges`, uid 1000): `No permissions to create a new namespace` (Docker's default seccomp profile blocks user namespaces).
- Diagnostic only, with `seccomp=unconfined` as well: `Failed to make / slave: Permission denied` (mount propagation is blocked too). Codex's own remote-test setup runs bubblewrap in a `--privileged` container.
- Codex rejects its legacy Landlock mode for filesystem-restricted policies, and turning off its shell tool leaves other built-in tools; neither is a boundary.

Result: Codex cannot confine its reads inside the hardened container.

**Follow-up research and tests (2026-10-23).**
- **History:**
  - Codex's Linux sandbox started as Landlock plus seccomp.
  - Bubblewrap became an option in January 2026 (PR #9938) and the default in March 2026 (PR #13996, from about 0.115).
  - Legacy Landlock was deprecated in April 2026 (PR #17971).
  - Stated reasons: one consistent isolation path with namespaces and network, fail-closed behavior, nested deny carve-outs (Landlock can only allow), and isolating Codex's app-server Unix sockets (PR #45984).
  - In 0.163, legacy Landlock is refused for any filesystem-restricted policy.
- **Missing or unusable bwrap:** every model shell command fails with the panic as tool output, and `codex exec` still exits 0 (reproduced with a mock model; matches open issue openai/codex#46246). v1's Codex runs therefore could not run any shell command, silently.
- **Official guidance** (OpenAI's Codex security docs): when the container blocks bubblewrap, isolate at the container level and run Codex with `--sandbox danger-full-access`. Their warning is that a malicious project can then read anything in the container. Their secure devcontainer instead turns off seccomp and AppArmor and adds capabilities.
- **Outer Landlock test:** in the hardened container, Landlock ABI 10 is available under Docker's default seccomp profile. Codex 0.163 ran under a Landlock wrapper with `--sandbox danger-full-access`, driven by a local mock Responses server that requested shell commands:
  - the project file was readable;
  - `/data/token`, the Claude OAuth token, `/proc/1/root/data/token`, another process's `/proc/<pid>/environ`, listing `/cfg` and writing to `/cfg` were all refused;
  - the run completed.

  `codex doctor` under the wrapper reported only an expected missing login and missing `ripgrep`.

Decision (owner): confine every run with Landlock and run Codex with `danger-full-access` (section 4, "Run confinement with Landlock"). The separate runner container stays available as a stronger option later.

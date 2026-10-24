# Trajectree

Self-hosted project tracker (Python backend, React frontend).

## Install an instance

An instance is a directory that holds only `compose.yaml`, `.env`, `data/` and
`cfg/`; the image is built from the git repository.

```bash
# 1. Create the instance directory and copy in the two files from deploy/
#    (env.example becomes .env).
mkdir ~/trajectree-instance && cd ~/trajectree-instance
curl -fsSLO https://raw.githubusercontent.com/yu17/Trajectree/main/deploy/compose.yaml
curl -fsSL https://raw.githubusercontent.com/yu17/Trajectree/main/deploy/env.example -o .env

# 2. Create data and cfg yourself (Docker would make them root-owned), keep cfg
#    private, then fill in .env (at least TRAJECTREE_PROJECTS_ROOT, UID and GID).
mkdir -p data cfg && chmod 700 cfg

# 3. Build and start, then give the host CLI the token.
docker compose up -d --build
mkdir -p ~/.config/trajectree && cp data/token ~/.config/trajectree/token

# 4. Install the host CLI.
uv tool install git+https://github.com/yu17/Trajectree@<ref>

# 5. Log in the runners.
claude setup-token
trajectree login
docker compose exec -e CODEX_HOME=/cfg/codex trajectree codex login --device-auth
trajectree backends sync

# 6. Refresh the model catalog (from Task 8), then connect a project folder
#    (the interactive form is from Task 7).
trajectree models --refresh
trajectree init-project <folder>
```

Instance state must never live inside `TRAJECTREE_PROJECTS_ROOT`.

## Development compose

The `compose.yaml` at the repository root is the development stack. It is named
`trajectree-dev`, publishes port 8743 on 127.0.0.1 and builds from the working
tree, so it cannot replace a running instance.

```bash
# Create the data and cfg dirs yourself first: if Docker creates them, they are
# root-owned and the non-root container cannot write to them. cfg holds the
# runner's credentials, so keep it private to you. UID/GID are not exported by
# bash by default, so `export UID GID=$(id -g)` or put them in a .env file.
mkdir -p data cfg && chmod 700 cfg

# Folder that holds your project folders; mounted read-only at /projects.
# docker compose refuses to start while this is unset.
export TRAJECTREE_PROJECTS_ROOT=$HOME/Projects
docker compose up -d --build

# Login URL (open it in a browser; replace 0.0.0.0 with 127.0.0.1)
docker compose logs trajectree | grep ready

# Host CLI and MCP shim token
mkdir -p ~/.config/trajectree && cp data/token ~/.config/trajectree/token
```

The container runs as a non-root user with a read-only root filesystem, and
publishes its port on 127.0.0.1 only (8742 for an instance, 8743 for the
development stack). It mounts `~/.claude/projects` (only the
transcripts, not the rest of the Claude home), `~/.config/claude-backends` and
`$TRAJECTREE_PROJECTS_ROOT` read-only, `./data` read-write, and `./cfg`
read-write at `/cfg` for the runner's Claude Code config and credentials.
The image includes Claude Code (installed natively, auto-update disabled).

The `trajectree` CLI and the `trajectree mcp` shim run on the host. Install
them with `uv tool install .` (or `uv sync` and `uv run trajectree`). They talk
to `http://127.0.0.1:8742` and read the token from `~/.config/trajectree/token`.

## Connect a project folder

```bash
trajectree init-project ~/path/to/project
```

This registers the folder as a Trajectree project (`--slug`, `--title` and
`--kind` override the defaults derived from the folder name) and writes:

- a `folder` resource, plus a `git_local` resource if the folder has a `.git`;
- `.claude/rules/trajectree.md` in the folder: guidance telling Claude Code
  when and how to record decisions, milestones, blockers and follow-ups;
- the line `.claude/rules/trajectree.md` in `.git/info/exclude`, so the rules
  file is not committed (`--no-exclude` skips this);
- the user-scope MCP server `trajectree` in Claude Code, if `claude` is on the
  PATH and it is not already configured.

The CLI needs the token at `~/.config/trajectree/token` (copied from
`data/token`, see above) or in `TRAJECTREE_TOKEN`.

## MCP

`init-project` registers the stdio shim, equivalent to:

```bash
claude mcp add --scope user trajectree -- trajectree mcp
```

The shim forwards to the daemon and defaults the project from
`CLAUDE_PROJECT_DIR`. To skip the shim and talk to the daemon over HTTP:

```bash
claude mcp add --transport http --scope user trajectree http://127.0.0.1:8742/mcp --header "Authorization: Bearer <token>"
```

The endpoint is `/mcp` and accepts only the bearer token. Everything a session
writes lands as pending in the Inbox tab, where the owner accepts or rejects it.

## Ingest

A background loop reads Claude Code transcripts under `TRAJECTREE_CLAUDE_HOME`
and `TRAJECTREE_BACKENDS_ROOT`, and the git history of each project's
`git_local` resources. A transcript is attached to the project whose
registered folder equals the session's `cwd` (a host path) or is an ancestor
of it; the deepest registered folder wins.

- `TRAJECTREE_INGEST_EVERY` sets the loop period in seconds (300; 0 disables).
- The live marker is derived at scan time, so `TRAJECTREE_LIVE_WINDOW` must be
  at least `TRAJECTREE_INGEST_EVERY` plus a margin, otherwise running sessions
  are missed between scans.
- In Docker, the git connector reads the repositories through
  `TRAJECTREE_PATH_MAP`, which compose sets to
  `$TRAJECTREE_PROJECTS_ROOT=/projects`: host paths under that root are read
  from `/projects` inside the container. `TRAJECTREE_PROJECTS_ROOT` must be the
  physical path (no symlinks): `init-project` stores the resolved folder path,
  and the path map is a prefix match on it.
- `trajectree ingest` runs one pass now.
- Each project's summary is exported to `data/exports/projects/<slug>/SUMMARY.md`.

## Runs (summaries, ask, quick-add)

A run spawns the Claude Code CLI inside the container, headless, over a
token-budgeted context pack built from the tracker.

- `trajectree summarize <project>` writes a project summary; the result lands
  as a pending transaction in the Inbox. `trajectree summarize --session <id>`
  (or `Summarize session` in the activity panel) summarizes one Claude Code
  session from a digest of its transcript.
- `trajectree ask "<question>" [--projects a,b] [--days 90] [--budget 4000]`
  answers over project context and returns proposals.
- `trajectree add "<free text>" [--project slug]` (quick-add) turns text into
  proposals.
- `ask` and `add` apply nothing by themselves. The owner accepts a proposal in
  the web UI (Quick capture, Ask panel, Inbox) or with
  `trajectree accept <run_id> <n>` (n is 1-based).
- All three take `--runner` (`claude`, `codex` or `ccx:<backend>`), `--model`
  and `--no-wait`.
- `trajectree runs [--limit 20]` lists recent runs, `trajectree usage [--days 30]`
  shows spend.
- `trajectree caps` shows the limits; `--usd-per-run`, `--usd-per-day` and
  `--timeout` change them, and `--pause` / `--resume` is the pause switch that
  stops new runs. Claude Code 2.1.309 has no `--max-turns`, so a run is bounded
  by `--max-budget-usd` and the daemon timeout.
- The prompts are seeded at `data/prompts/<kind>.md` and are yours to edit.
  Each run's log is `data/runs/<run_id>.log`, and its working files are in
  `data/runs/<run_id>/`.
- A prompt is seeded once and never overwritten, so delete a prompt file to
  get the shipped version back at the next start. Existing deployments only get
  the activity section added to `summarize.md` in phase 4 this way.

## Log in the runner

The runner needs its own Claude credentials inside the container. The device
login cannot complete there, so use a long-lived token from the host:

```bash
claude setup-token        # on the host; prints a token
trajectree login          # paste the token when asked
trajectree runner status  # shows whether the runner is ready
```

The token is stored in `./cfg` (`/cfg/claude/oauth_token` in the container),
never in `./data`. To run through your ccx backends (`CLAUDE_CONFIG_DIR=<profile>
claude ...`), run `trajectree backends sync`: it copies each backend's
`.claude.json` and the `env`, `model` and `apiKeyHelper` keys of its
`settings.json` (nothing else, so no hooks, permissions or status line) from
`~/.config/claude-backends` into `/cfg/backends/<name>/`, and runs then use
`--runner ccx:<backend>`.

Codex is optional, see [Codex](#codex).

## Live sessions (hooks)

```bash
trajectree hooks install [--yes]
trajectree hooks show
trajectree hooks uninstall
```

`install` registers http hooks for three Claude Code events, `SessionStart`,
`Stop` and `SessionEnd`, so the timeline shows live sessions without waiting
for an ingest pass. It edits `~/.claude/settings.json` (or
`$CLAUDE_CONFIG_DIR/settings.json`) only after printing the change and getting
`--yes` or a typed `y`. The first backup is kept as `settings.json.trajectree.bak`.
The hooks carry a dedicated liveness-only token, one per settings file, that can
announce a session but cannot read or write the tracker; a settings file the
command creates is made private (0600). `show` prints the hooks currently installed;
`uninstall` removes only Trajectree's entries. All three accept
`--settings-file` to target another file.

## Routines

A routine is a scheduled run: a cron trigger in a time zone, a prompt, a scope,
caps and a delivery. Two are seeded on first start, both disabled:

| Routine | Trigger | What it does |
| --- | --- | --- |
| `cross-project-review` | `0 7 * * 1` (Mondays 07:00) | Looks back 14 days over all projects: stale projects, overdue todos, suggested next steps |
| `meeting-prep` | `0 7 * * 1-5` (weekdays 07:00) | Prepares the day's meeting agenda from the last 7 days of activity |

```bash
trajectree routines                          # name, on/off, trigger, tz, next fire, last run
trajectree routine enable cross-project-review
trajectree routine disable cross-project-review
trajectree review [--projects a,b] [--no-wait]   # run cross-project-review now
trajectree prep [--projects a,b] [--no-wait]     # run meeting-prep now
trajectree run <routine> [--projects a,b] [--no-wait]
trajectree scheduler [tick]                  # show the scheduler, or tick it now
```

- The seeds take their time zone from `TRAJECTREE_TZ` when they are created;
  compose passes `TZ` from your shell or `.env` (default `UTC`). Changing it
  later does not move existing routines: patch their `tz` instead.
- The scheduler ticks every `TRAJECTREE_SCHEDULER_EVERY` seconds. A fire missed
  by more than `TRAJECTREE_SCHEDULER_GRACE_S` (the daemon was down) is skipped
  and noted as the routine's last error, not run late.
- Each routine has its own caps on top of the global ones (`caps.usd_per_run`
  1, `caps.usd_per_day` 2 by default); a routine that reaches its daily cap is
  paused until the next UTC midnight. Change them with
  `PATCH /api/routines/<name>` and a body like `{"caps": {"usd_per_run": 0.5, "usd_per_day": 1}}`.
- An infrastructure failure (the CLI could not start, was killed, timed out or
  printed no parsable result, or the daemon restarted mid-run) is retried
  after 60 s and then 300 s, three attempts in all. A run the CLI itself ends
  with an error, such as the budget cap, is not retried.
- The result is delivered to the Inbox as a pending note in the `inbox`
  project (created at startup), titled `<routine> · <day>`. A routine can
  instead deliver to `log`, `none` or `chatmaid` (see below).
- The prompts are `data/prompts/cross_project_review.md` and
  `data/prompts/meeting_prep.md`, editable like the others.
- The Runs tab lists the routines with `Enable`/`Disable` and `Run now`.

## Connectors

The connectors run inside the ingest loop (`TRAJECTREE_INGEST_EVERY`); each
one is skipped until it is due. The Settings tab shows each one's last run and
errors.

### Folders

- Every `folder` resource is walked once every `TRAJECTREE_FOLDERS_EVERY_H`
  hours (24) and gets one `files_changed` signal per day with changed files.
- The walk honours the folder's root `.gitignore` (nested ones are not read),
  never follows symlinks and skips `.git`, `node_modules`, `.venv`, `venv`,
  `__pycache__`, `.cache`, `.mypy_cache`, `.pytest_cache`, `dist`, `build`
  and `.tox`.
- A directory whose own files add up to more than `TRAJECTREE_BIG_DIR_MB` (500)
  is not descended into; it is reported as a big directory on the signal.

### GitHub and GitLab

```bash
trajectree connectors login github   # paste a token, or pipe it on stdin
trajectree connectors login gitlab
```

- Use a read-only token (GitHub: read access to the repositories' contents,
  issues and pull requests; GitLab: `read_api`). It is stored in
  `cfg/connectors/<name>_token` (0600) and never logged.
- The ingest reads the fetch remotes of each `git_local` checkout and suggests
  each one as a `git_remote` resource, pending in the Inbox. A remote is polled
  only after you accept it; a rejected one is not suggested again.
- `github.com` remotes use the GitHub API with the `github` token. GitLab
  polling needs `TRAJECTREE_GITLAB_HOST` (for example `gitlab.uni.edu`, with
  `:<port>` if the remote URL has one): remotes on that host use
  `https://<host>/api/v4` with the `gitlab` token. Remotes on any other host
  are still suggested and listed, but never polled (no request, no token); the
  Settings tab reports them as `unknown host, not polled`.
- Accepted remotes are polled every `TRAJECTREE_REMOTE_EVERY_M` minutes (30):
  one `remote` signal per repository and day of PR/MR, issue and release
  activity, and a pending `Release <tag>` milestone for each new release.
- Without a token the requests are anonymous, and GitHub allows 60 per hour; a
  repository that hits a rate limit, a bad token or a timeout is reported as an
  error and tried again at the next poll.

### Slurm

```bash
export TRAJECTREE_SLURM_KEY=$HOME/.ssh/trajectree_slurm   # private key on the host
export TRAJECTREE_SLURM_HOST=login.cluster.example.org
export TRAJECTREE_SLURM_USER=you                           # optional
docker compose -f compose.yaml -f compose.slurm.yaml up -d
```

- The overlay mounts the key read-only at `/run/slurm_key`. Use a dedicated
  key, readable only by you (0600), and restrict it on the login node to what
  the connector runs (`squeue --me --json` and `sacct --json -X -S <time>`), for
  example with a `command=` wrapper in `authorized_keys`.
- ssh runs with `BatchMode=yes` and `StrictHostKeyChecking=accept-new`: the
  login node's host key is accepted on first connect and kept in
  `cfg/home/.ssh/known_hosts`; check it there, and remove the line if the
  host key changes legitimately.
- ssh needs a passwd entry for the container's uid, so run the container as
  uid 1000 (the image's `trajectree` user) when Slurm is on.
- Jobs are polled every `TRAJECTREE_SLURM_EVERY_M` minutes (15). Each job is
  attached to the project whose `folder` or `git_local` resource holds its
  working directory, else a project whose `slurm_workdirs` prop (a list of
  cluster paths) holds it, else the project whose slug (3 characters or more)
  starts the job name, else the `inbox` project.

### Recurrence

Set `recurrence` on a project (typically one of kind `routine`) to an RRULE,
and optionally the props `instance_kind` (default `meeting`),
`instance_title` (default the project title) and `instance_minutes` (default
60):

```bash
TOKEN=$(cat ~/.config/trajectree/token)
curl -s http://127.0.0.1:8742/api/projects -H "Authorization: Bearer $TOKEN"   # find the project's id
curl -X PATCH http://127.0.0.1:8742/api/projects/<project_id> \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"recurrence": "FREQ=WEEKLY;BYDAY=TU;BYHOUR=10;BYMINUTE=0", "props": {"instance_kind": "meeting", "instance_title": "Group meeting", "instance_minutes": 60}}'
```

`props` replaces the project's props as a whole, so include any you already
set (such as `slurm_workdirs`). Each ingest pass creates the planned instances
for the next 8 weeks, expanded in `TRAJECTREE_TZ`. Changing the rule drops the
future planned instances of the old rule; past ones stay as history.

## Reports

Reports are Marp Markdown documents, kind `deck` (slides) or `memo`.

- Draft one from the Reports tab (`Draft with agent`) or with
  `trajectree report draft --kind deck|memo --title "<title>" --projects a,b [--days 90]`;
  `New report` starts an empty one. `trajectree report list` lists them.
- Edit the source in the Reports tab, with a live preview. Each `Save report`
  is a transaction, so it shows in the Ledger and can be reverted.
- Export from the tab or with
  `trajectree report export <id> --format md|html|pdf|pptx [--out <file>]`.
  The server always keeps its copy in `data/exports/reports/<id>.<format>`.
  Without `--out`, the CLI prints `md` and `html` to stdout and writes `pdf`
  and `pptx` to `./<id>.<format>` in the current directory.
- `md` and `html` always work. `pdf` and `pptx` need marp-cli and Chromium,
  which only the `reports` image has:

  ```bash
  TRAJECTREE_IMAGE_TARGET=reports docker compose up -d --build
  ```

- During a marp export Chromium fetches remote images referenced in the deck
  from inside the container. Local files are never read (no
  `--allow-local-files`).
- The `reports` image sets `CHROME_NO_SANDBOX=1`: with all capabilities
  dropped Chromium's own sandbox cannot start, so the container is the
  sandbox.

## chatmaid

chatmaid (email and messaging) posts message signals to Trajectree, and routines
can deliver their results to chatmaid. The full contract is in
[docs/chatmaid-contract.md](docs/chatmaid-contract.md).

- Inbound: mint a token with the `chatmaid` preset in the Settings tab (or the
  `curl` in the contract) and give its secret to chatmaid.
- Outbound: set `TRAJECTREE_CHATMAID_URL` to chatmaid's base URL, store the
  token chatmaid issued with `trajectree connectors login chatmaid`, and set a
  routine's `delivery` to `chatmaid`. A failed delivery falls back to the Inbox.

## Settings

The Settings tab shows:

- Tokens: the scoped tokens with their scope and expiry, `Revoke`, and a form
  to create one from a preset (`chatmaid`, `hooks`, `read-only`, `full`); the
  secret is shown once.
- Connectors: whether each one is configured, its last run, its latest error
  and a short detail.
- Runner: whether Claude Code is installed, its version and whether the
  runner is logged in.
- Mounts: the Claude home, backends root, path map, and the data, config and
  exports directories as the daemon sees them.
- Scheduler: the last tick, the interval and the upcoming fires.

## Codex

The image does not include Codex unless you ask for it:

```bash
docker compose build --build-arg INSTALL_CODEX=1
docker compose up -d
```

This installs the pinned Codex release (`CODEX_VERSION`, 0.163.0, checked
against `CODEX_SHA256`; override both together with `--build-arg`) at
`/opt/codex/bin/codex`. `TRAJECTREE_CODEX_BIN` points there in every image,
which is harmless without Codex: it is only used when a run picks codex. Copy
your Codex `auth.json` to `cfg/codex/auth.json`, then use `--runner codex`.

## What gets written where

| Where | What |
| --- | --- |
| Container, `./data` | `trajectree.db`, `token`, `exports/` (project summaries), `runs/*.log` and `runs/<run_id>/` (run logs and working files; `runs/<run_id>/mcp.json` holds the run's MCP token, 0600, only while the run is running), `prompts/*.md` (editable prompts) |
| Container, `./cfg` (`/cfg`) | Claude Code config for the runner, `claude/oauth_token`, `claude/settings.json`, `claude/.claude.json`, backend profile copies in `backends/<name>/`, `home/` (the run home), `codex/` |
| Container, `./cfg/connectors/` | `<name>_token` for `github`, `gitlab` and `chatmaid` (0600), written by `trajectree connectors login` |
| Container, `./data/exports/reports/` | `<id>.md`, `<id>.html`, `<id>.pdf`, `<id>.pptx`, written on export |
| Container, `./cfg/home/.ssh/` | `known_hosts`: the Slurm login node's host key, accepted on first connect |
| Container, `./cfg/home/.config/chromium/`, `./cfg/home/.local/share/pki/` | `reports` image only: Chromium's `Crash Reports/` settings and an empty NSS database, created on the first PDF/PPTX export (a few KB). Chromium's per-export profile goes to a private temporary directory under `/tmp` that is removed after the export |
| Host CLI, `init-project` | `<project>/.claude/rules/trajectree.md`, `<project>/.git/info/exclude`, Claude Code's user MCP config via `claude mcp add` |
| Host CLI, `hooks install` | `~/.claude/settings.json` after confirmation, with `settings.json.trajectree.bak` |
| Host, anything else | Nothing |

The container mounts your Claude home read-only and only `~/.claude/projects`
of it. The project folders mount read-only.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `TRAJECTREE_DATA_DIR` | `~/.local/share/trajectree` (`/data` in Docker) | Database, token and exports directory |
| `TRAJECTREE_HOST` | `127.0.0.1` (`0.0.0.0` in Docker) | Address the daemon binds |
| `TRAJECTREE_PORT` | `8742` | Port the daemon binds |
| `TRAJECTREE_CLAUDE_HOME` | `~/.claude` if it exists (`/host/claude` in Docker) | Claude Code home to scan for transcripts; empty string disables |
| `TRAJECTREE_BACKENDS_ROOT` | `~/.config/claude-backends` if it exists (`/host/claude-backends` in Docker) | Extra Claude homes to scan; empty string disables |
| `TRAJECTREE_PATH_MAP` | none | `host=container;host2=container2`: where host paths live in the container |
| `TRAJECTREE_INGEST_EVERY` | `300` | Seconds between ingest passes; `0` disables the loop |
| `TRAJECTREE_LIVE_WINDOW` | `900` | Seconds since the last transcript activity during which a session counts as live |
| `TRAJECTREE_STALE_SETTLE_H` | `24` | Hours after a project's first activity since its last summary before it is flagged stale (summary due) |
| `TRAJECTREE_SCAN_SINCE_DAYS` | `365` | Ignore transcripts and commits older than this many days |
| `TRAJECTREE_CFG_DIR` | `<data dir>/cfg` (`/cfg`, bind-mounted from `./cfg`, in Docker) | Container-private runner config: OAuth token, synced backend profiles, runner home |
| `TRAJECTREE_CLAUDE_BIN` | `claude` | Claude Code binary the runner spawns |
| `TRAJECTREE_CODEX_BIN` | `codex` (`/opt/codex/bin/codex` in Docker) | Codex binary the runner spawns |
| `TRAJECTREE_DEFAULT_RUNNER` | `claude` | Runner used when none is chosen (`claude`, `codex`, or `ccx:<backend>`) |
| `TRAJECTREE_RUN_ENV_PASSTHROUGH` | none | Comma-separated environment variable names passed through to runner processes |
| `TRAJECTREE_TZ` | `UTC` (compose: `$TZ`, else `UTC`) | Time zone given to the seeded routines and used to expand recurrence rules; an unknown zone falls back to `UTC` with a warning |
| `TRAJECTREE_SCHEDULER_EVERY` | `30` | Seconds between scheduler ticks; `0` disables the loop |
| `TRAJECTREE_SCHEDULER_GRACE_S` | `3600` | A routine fire missed by more than this many seconds is skipped, not run late |
| `TRAJECTREE_DIGEST_MAX_CHARS` | `12000` | Maximum length of the transcript digest a session summary works from |
| `TRAJECTREE_FOLDERS_EVERY_H` | `24` | Hours between folder scans; `0` disables them |
| `TRAJECTREE_BIG_DIR_MB` | `500` | A directory whose own files exceed this many MB is not descended into by the folder scan |
| `TRAJECTREE_REMOTE_EVERY_M` | `30` | Minutes between GitHub/GitLab polls; `0` disables them |
| `TRAJECTREE_GITLAB_HOST` | none | The one host polled as GitLab and sent the `gitlab` token (`host` or `host:port`); while unset, no GitLab remote is polled |
| `TRAJECTREE_SLURM_HOST` | none | Slurm login node; the Slurm connector is off while unset |
| `TRAJECTREE_SLURM_USER` | none (ssh's default) | User on the login node |
| `TRAJECTREE_SLURM_KEY` | none (`/run/slurm_key` with `compose.slurm.yaml`) | Private key for ssh to the login node; the overlay reads the host path from this variable |
| `TRAJECTREE_SLURM_EVERY_M` | `15` | Minutes between Slurm polls; `0` disables them |
| `TRAJECTREE_SSH_BIN` | `ssh` | ssh binary the Slurm connector runs |
| `TRAJECTREE_CHATMAID_URL` | none | Base URL of chatmaid; routine results with the `chatmaid` delivery are POSTed to `<url>/deliver` |
| `TRAJECTREE_MARP_BIN` | `marp` on the PATH (`/usr/local/bin/marp` in the `reports` image) | marp-cli for HTML, PDF and PPTX exports; without it, HTML uses the built-in renderer and PDF/PPTX are unavailable |
| `TRAJECTREE_CHROME_BIN` | none (`/usr/bin/chromium` in the `reports` image) | Browser marp-cli uses (`--browser-path`) |
| `TRAJECTREE_IMAGE_TARGET` | `app` | Image variant compose builds (`app` or `reports`); used by compose only |
| `TRAJECTREE_PROJECTS_ROOT` | none (required by compose) | Host folder mounted read-only at `/projects`; used by compose only |
| `TRAJECTREE_URL` | `http://127.0.0.1:8742` | Daemon URL for the host CLI and shim |
| `TRAJECTREE_TOKEN` | contents of `~/.config/trajectree/token` | Bearer token for the host CLI and shim |

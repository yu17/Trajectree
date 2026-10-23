# Trajectree

Self-hosted project tracker (Python backend, React frontend).

## Run with Docker

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
publishes port 8742 on 127.0.0.1 only. It mounts `~/.claude/projects` (only the
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
  as a pending transaction in the Inbox.
- `trajectree ask "<question>" [--projects a,b] [--days 90] [--budget 4000]`
  answers over project context and returns proposals.
- `trajectree add "<free text>" [--project slug]` (quick-add) turns text into
  proposals.
- `ask` and `add` apply nothing by themselves. The owner accepts a proposal in
  the web UI (Quick capture, Ask panel, Inbox) or with
  `trajectree accept <run_id> <n>` (n is 1-based).
- All three take `--runner` (`claude` or `ccx:<backend>`), `--model` and
  `--no-wait`.
- `trajectree runs [--limit 20]` lists recent runs, `trajectree usage [--days 30]`
  shows spend.
- `trajectree caps` shows the limits; `--usd-per-run`, `--usd-per-day` and
  `--timeout` change them, and `--pause` / `--resume` is the pause switch that
  stops new runs. Claude Code 2.1.309 has no `--max-turns`, so a run is bounded
  by `--max-budget-usd` and the daemon timeout.
- The prompts are seeded at `data/prompts/<kind>.md` and are yours to edit.
  Each run's log is `data/runs/<run_id>.log`, and its working files are in
  `data/runs/<run_id>/`.

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

Codex is optional: install the binary into the image yourself and set
`TRAJECTREE_CODEX_BIN`.

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

## chatmaid

Trajectree has a small contract with chatmaid (inbound message signals, outbound
routine delivery); see [docs/chatmaid-contract.md](docs/chatmaid-contract.md).

## What gets written where

| Where | What |
| --- | --- |
| Container, `./data` | `trajectree.db`, `token`, `exports/` (project summaries), `runs/*.log` and `runs/<run_id>/` (run logs and working files; `runs/<run_id>/mcp.json` holds the run's MCP token, 0600, only while the run is running), `prompts/*.md` (editable prompts) |
| Container, `./cfg` (`/cfg`) | Claude Code config for the runner, `claude/oauth_token`, `claude/settings.json`, `claude/.claude.json`, backend profile copies in `backends/<name>/`, `home/` (the run home), `codex/` |
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
| `TRAJECTREE_CODEX_BIN` | `codex` | Codex binary the runner spawns |
| `TRAJECTREE_DEFAULT_RUNNER` | `claude` | Runner used when none is chosen (`claude`, or `ccx:<backend>`) |
| `TRAJECTREE_RUN_ENV_PASSTHROUGH` | none | Comma-separated environment variable names passed through to runner processes |
| `TRAJECTREE_CHATMAID_URL` | none | Base URL of chatmaid; routine results with the `chatmaid` delivery are POSTed to `<url>/deliver` |
| `TRAJECTREE_PROJECTS_ROOT` | none (required by compose) | Host folder mounted read-only at `/projects`; used by compose only |
| `TRAJECTREE_URL` | `http://127.0.0.1:8742` | Daemon URL for the host CLI and shim |
| `TRAJECTREE_TOKEN` | contents of `~/.config/trajectree/token` | Bearer token for the host CLI and shim |

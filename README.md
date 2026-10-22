# Trajectree

Self-hosted project tracker (Python backend, React frontend).

## Run with Docker

```bash
# Create the data dir first: if Docker creates it, it is root-owned and the
# non-root container cannot write to it. UID/GID are not exported by bash
# by default, so `export UID GID=$(id -g)` or put them in a .env file.
mkdir -p data

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
publishes port 8742 on 127.0.0.1 only. It mounts `~/.claude`,
`~/.config/claude-backends` and `$TRAJECTREE_PROJECTS_ROOT` read-only, and
`./data` read-write.

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
| `TRAJECTREE_PROJECTS_ROOT` | none (required by compose) | Host folder mounted read-only at `/projects`; used by compose only |
| `TRAJECTREE_URL` | `http://127.0.0.1:8742` | Daemon URL for the host CLI and shim |
| `TRAJECTREE_TOKEN` | contents of `~/.config/trajectree/token` | Bearer token for the host CLI and shim |

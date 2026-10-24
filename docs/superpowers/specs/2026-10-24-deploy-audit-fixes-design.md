# Deploy audit fixes: design

Status: agreed with the owner in chat on 2026-10-24, during the first instance install
(`~/.local/share/trajectree`, published on `127.0.0.1:40210`). This document records those
decisions so the plan can argue from them.

## Why

The first real install surfaced 16 findings. Some block a working instance: every Codex run
fails, and Codex cannot log in before its first run. Others leak the wrong address, or leave
data readable by other users. The owner also revised the model-choice rules from the previous
round, and chose a way for a local backend (LM Studio) to reach the host.

## Decisions

### Image and startup (findings 4, 5, 6, 13)

- `/opt/codex/bin` is on the image's `PATH`, so `codex …` works in `docker compose exec`.
- The Codex home (`/cfg/codex`) is created at startup with the other cfg dirs, so `codex login`
  works before any run.
- The daemon makes the data dir and the cfg dir `0700` at startup, and the entrypoint sets
  `umask 077`. The README's setup line becomes `chmod 700 data cfg`.
- The image ships `codex-code-mode-host` (pinned digest) next to `codex`, because Codex logs an
  error on every run without it and the `-c features.code_mode_host=false` flag only changed the
  error text.

### Server address (findings 1, 2, 3, 7)

- The instance's published port comes from `.env`: `TRAJECTREE_HOST_PORT` (default 8742).
  Compose also passes `TRAJECTREE_PUBLIC_URL=http://127.0.0.1:${TRAJECTREE_HOST_PORT}` into the
  container, and the startup "ready" line prints that URL.
- Host clients (the CLI, the `trajectree mcp` shim, and `hooks install`) find the server in this order:
  `TRAJECTREE_URL`, then the first line of `~/.config/trajectree/url`, then
  `http://127.0.0.1:8742`. The URL file sits next to the token file, so a non-default port needs
  no shell profile and no `-e` on the MCP entry.
- The hooks treat any handler whose URL path is `/api/hooks/claude` as theirs: install
  replaces old ones, and uninstall removes them all, whatever host or port they name.
- The README's install steps gain the URL file, the hooks step, and the MCP note. It also says
  that a changed `.env` takes effect with `docker compose up -d`, not `restart`.

### Codex output format (finding 12)

- Every run's JSON schema is valid in OpenAI strict mode. Every object lists all of its
  properties in `required` and has `additionalProperties: false`. A field that may be absent
  is nullable instead of optional.
- Parsing treats `null` as absent, so the existing defaults (`event_kind` "note" and so on)
  still apply.
- A failed Codex run records Codex's own error message (from `turn.failed` or `error` events),
  not just "no last message".

### Model choice (the owner's proposals 1–4)

1. **A runner without a model is a valid choice.** That runner is started without a model flag,
   so it uses its own default: Claude Code's, Codex's, or the ccx profile's `ANTHROPIC_MODEL`.
   Choosing neither stays an error. Routines may store a runner without a model. The web picker
   offers "default" as the first entry under each available runner. For Claude-based runs, the
   run records the model the runner reports using (from `modelUsage`).
2. **Refresh** reads each runner's own source. Claude keeps using the Anthropic models API (the
   owner's decision), Codex reads `models_cache.json`, and a ccx backend reads its profile plus
   its models endpoint. Refresh reports any problem per runner, with a hint that names the real
   fix.
3. **Manual edits.** In the CLI: `trajectree models --runner R --add M --delete M`, both
   repeatable, with `--runner` required (runner names contain `:`, model names contain `/`).
   In the web: a × on each model in Settings, and an add box per runner. Manually added models
   have source `manual`.
4. **Refresh merge.** If a runner's fetch succeeded with a non-empty list, its models are replaced
   by the fetched list, including manual ones. If it failed or came back empty, the stored list
   stays untouched. A runner with no stored list yet takes whatever the fetch found, so ccx
   lists are seeded from the profile's models (the owner's decision) and Claude from the
   built-in list. For a ccx backend, success means the models endpoint answered; with no
   `ANTHROPIC_BASE_URL`, the profile is the source and counts as success. A runner that is no
   longer discovered is dropped.

### Local backends through a socket (findings 8, 15, 16)

- Claude Code 2.1.310 supports `ANTHROPIC_UNIX_SOCKET` natively. This was tested live in the
  instance container against LM Studio, both unconfined and under the Landlock wrapper. No
  in-container forwarder is needed.
- On the host, the systemd user units `trajectree-backend@<profile>.socket` and `.service`
  (`systemd-socket-proxyd`) listen on `<instance>/sockets/<profile>.sock` with mode 0600, and
  forward to the TCP address named in `~/.config/trajectree/backends/<profile>.env`
  (`TARGET=127.0.0.1:40090`).
- Compose mounts `./sockets` read-only at `/host/sockets` (`TRAJECTREE_SOCKETS_DIR`). When
  `<sockets>/<profile>.sock` exists and is a socket, ccx runs of that profile get
  `ANTHROPIC_UNIX_SOCKET`, and catalog discovery calls the models endpoint over that socket. The
  host profile is never changed, so `ccx <profile>` on the host still uses its TCP URL.
- Compose names the bridge `trajectree0`. The README gives the one-time firewall rule
  `sudo ufw insert 1 deny in on trajectree0`, after which the container reaches no host port at
  all. The socket is its only way to the host, and internet egress is unaffected.
- The README states that every run can connect to every socket in `./sockets` (Landlock does
  not restrict connecting to a pathname socket).
- A smoke script checks, against the built image, that Claude Code still sends its API
  requests to `ANTHROPIC_UNIX_SOCKET`.

### Run inspection (finding 14)

- `trajectree runs` shows the runner, the model ("default" when none), and a short error.
- `trajectree runs <id>` shows one run: status, runner, model, model warning, error, log path,
  and the result.
- `trajectree run ""` is refused with a usage error.

## Non-goals

- No in-container TCP forwarder, and no change to LM Studio's listen address.
- No Landlock network rules for runs (an optional idea only, left out).
- Finding 3 (`TRAJECTREE_INGEST_EVERY=0`) was the owner's intent; it only gets the README note.

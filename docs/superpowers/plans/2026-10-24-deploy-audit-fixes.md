# Deploy Audit Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 16 findings from the first instance install. Adopt the revised model-choice rules and a socket path from the container to local backends.

**Architecture:** Seven independent-ish tasks, each touching one area: image/startup,
client URL, run output schema, runner-only choice, catalog edit/merge, backend sockets plus
network isolation, and CLI run inspection. Every task updates the README sections it changes.

**Tech Stack:** Python 3.12 (FastAPI, httpx, pydantic, apsw), pytest; React + TypeScript
(vitest) in `web/`; Docker Compose; systemd user units.

**Spec:** `docs/superpowers/specs/2026-10-24-deploy-audit-fixes-design.md`

## Global Constraints

- No new Python or npm dependencies.
- Python: `uv run pytest -q` must stay green; follow the surrounding style (type hints,
  `from __future__ import annotations`, short docstrings that say why).
- Web: run `npm test` and `npm run build` from `web/`. `npm` on this host is a pnpm shim; use
  `npm exec`, never `npx`.
- Error and hint text never contains a token, a key or a full URL with userinfo (catalog
  errors name only a status or exception class and a host).
- Hint text names the real fix as a runnable command where one exists.
- README: update every section whose behaviour a task changes, in the README's existing voice.
- Commits end with the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A catalog stored by the previous version (no `manual` source; Claude's `builtin` entries)
   must load and merge without error after the upgrade. The test goes in Task 5.
2. A routine stored before Task 4 (model set, runner null) still validates and runs. A
   runner-only routine survives a restart (`disable_modelless_routines` leaves it enabled).
   The test goes in Task 4.
3. A sockets dir that exists but holds no socket, or a stale socket nobody listens on, gives a
   plain catalog error for that backend, keeps its list, and never crashes discovery. The test
   goes in Task 6.
4. A URL file with a trailing newline, surrounding spaces or a trailing slash still yields a
   working base URL, and `TRAJECTREE_URL` wins over the file. The test goes in Task 2.
5. Proposals that come back with every optional field `null` (strict-mode output) apply with
   the old defaults (`event_kind` "note", `body_md` "", `status` "happened"). The test goes in
   Task 3.

---

### Task 1: Image and startup hygiene

**Files:**
- Modify: `Dockerfile` (ENV `PATH`), `docker/entrypoint.sh`
- Modify: `src/trajectree/runner/credentials.py` (`ensure_cfg_dirs`)
- Modify: `src/trajectree/api/app.py` (startup) or `src/trajectree/main.py`, whichever already runs once before serving
- Modify: `src/trajectree/runner/argv.py` (`codex_argv`)
- Modify: `README.md` (install step 2, Codex section)
- Test: `tests/test_runner_argv.py`, the existing credentials/config tests, `tests/test_app*.py`

**Interfaces:**
- Produces: `ensure_cfg_dirs(settings)` also creates `settings.codex_home` (0700). It is called
  once at daemon startup, as well as before each run.

- [ ] **Step 1: Write failing tests**
  - `test_codex_argv_disables_code_mode_host`: `codex_argv(...)` contains the adjacent pair
    `"-c", "features.code_mode_host=false"`.
  - `test_ensure_cfg_dirs_creates_codex_home`: after `ensure_cfg_dirs(settings)`,
    `settings.codex_home.is_dir()` and its mode is `0o700`.
  - `test_startup_makes_state_dirs_private`: start the app (the TestClient lifespan) with a
    data dir and a cfg dir created at `0o755`. Afterwards both are `0o700`, and
    `settings.codex_home` exists.
- [ ] **Step 2: Run them; expect FAIL.**
- [ ] **Step 3: Implement**
  - `Dockerfile`: put `/opt/codex/bin` on `PATH`, after `/opt/claude/.local/bin`. The directory
    may be absent when `INSTALL_CODEX=0`; that is harmless.
  - `docker/entrypoint.sh`: add `umask 077` before `exec trajectreed`.
  - Startup: call `ensure_cfg_dirs(settings)`, then `os.chmod(settings.data_dir, 0o700)`, then
    `os.chmod(settings.cfg_root, 0o700)`. Log, don't crash, on `OSError`. A bind mount owned by
    the container user can be chmodded.
  - `codex_argv`: add `-c features.code_mode_host=false` after the two `mcp_servers` `-c` pairs.
    The comment says why: the image ships no `codex-code-mode-host`.
  - README install step 2: change it to `mkdir -p data cfg && chmod 700 data cfg`. Fix the
    Codex login commands (install step 5 and the Codex section) to use plain `codex`, which now
    works.
- [ ] **Step 4: Run `uv run pytest -q`; expect PASS.**
- [ ] **Step 5: Commit** `fix(image): codex on PATH, codex home created at startup, private state dirs, code-mode host off`

### Task 2: Server URL for the ready line and host clients

**Files:**
- Modify: `src/trajectree/config.py` (`Settings.public_url`, env `TRAJECTREE_PUBLIC_URL`)
- Modify: `src/trajectree/main.py` (ready line)
- Modify: `src/trajectree/cli/main.py` (`DEFAULT_URL` use, new `base_url`), `src/trajectree/cli/shim.py`
- Modify: `src/trajectree/cli/hooks.py` (`remove_hooks` / `merge_hooks` matching)
- Modify: `deploy/compose.yaml`, `deploy/env.example`, `compose.yaml` (dev: `TRAJECTREE_PUBLIC_URL: http://127.0.0.1:8743`)
- Modify: `README.md` (Install an instance, MCP, Live sessions, Environment variables table)
- Test: `tests/test_cli*.py`, `tests/test_hooks*.py`, `tests/test_config*.py` or wherever `Settings` env loading is tested

**Interfaces:**
- Produces: `base_url(env: Mapping[str, str]) -> str` in `trajectree.cli.main`. It returns
  `TRAJECTREE_URL` if set and non-empty. Otherwise it reads the first line of
  `<config>/url`, where `<config>` is the same directory the token file uses
  (`~/.config/trajectree/`, honouring the same `HOME` handling as `_token`). Otherwise it
  returns `DEFAULT_URL`. The result is stripped of whitespace and trailing `/`. The CLI's
  `main` and `run_shim` both use it.
- Produces: `Settings.public_url: str | None`.

- [ ] **Step 1: Write failing tests**
  - `test_base_url_precedence`: env beats file, file beats default. A file containing
    `"  http://127.0.0.1:40210/ \n"` yields `http://127.0.0.1:40210`. An empty env value falls
    through to the file.
  - `test_shim_uses_base_url_file`: with the file only, the shim connects to
    `<file url>/mcp`. Assert on the URL handed to the connector factory, monkeypatched.
  - `test_ready_line_prints_public_url`: with `TRAJECTREE_PUBLIC_URL=http://127.0.0.1:40210`,
    the printed line starts `Trajectree ready: http://127.0.0.1:40210/?token=`. Without it, the
    old `http://{host}:{port}` form is kept.
  - `test_hooks_install_replaces_other_port`: the settings file holds a handler for
    `http://127.0.0.1:8742/api/hooks/claude`. Installing with base `http://127.0.0.1:40210`
    leaves exactly one Trajectree handler per event, the new URL. Handlers with other URLs
    are untouched.
  - `test_hooks_uninstall_removes_any_port`: uninstalling with base 40210 removes 8742-URL
    handlers too.
- [ ] **Step 2: Run them; expect FAIL.**
- [ ] **Step 3: Implement**
  - The hooks match on the URL path: a handler is ours when `urlsplit(url).path ==
    "/api/hooks/claude"`.
  - `deploy/compose.yaml`: `ports: ["127.0.0.1:${TRAJECTREE_HOST_PORT:-8742}:8742"]`; environment
    `TRAJECTREE_PUBLIC_URL: http://127.0.0.1:${TRAJECTREE_HOST_PORT:-8742}`.
  - `deploy/env.example`: `TRAJECTREE_HOST_PORT=8742`, with a comment saying the host clients
    read the URL from `~/.config/trajectree/url`.
  - README install steps:
    - Step 3 writes the URL file next to the token
      (`echo http://127.0.0.1:${TRAJECTREE_HOST_PORT:-8742} > ~/.config/trajectree/url`).
    - A new step after the model refresh runs `trajectree hooks install`.
    - The MCP section says the shim reads the URL file.
    - A short note says that a changed `.env` takes effect with `docker compose up -d`; that
      `restart` keeps the old values; and that `TRAJECTREE_INGEST_EVERY=0` pauses ingest during
      a first import, with `trajectree ingest` running one pass by hand.
    - The env table gains `TRAJECTREE_HOST_PORT` (compose only), `TRAJECTREE_PUBLIC_URL`, and
      the URL file for `TRAJECTREE_URL`.
- [ ] **Step 4: Run `uv run pytest -q` and `docker compose -f deploy/compose.yaml config -q` (with `TRAJECTREE_PROJECTS_ROOT=/tmp` set); expect PASS.**
- [ ] **Step 5: Commit** `fix(cli): one server URL from env or ~/.config/trajectree/url; ready line shows the published URL; hooks own their path on any port`

### Task 3: Strict-mode output schema and Codex errors

**Files:**
- Modify: `src/trajectree/runner/kinds.py` (`PROPOSAL_SCHEMA` and any other `json_schema` a plan passes)
- Modify: `src/trajectree/runner/service.py` (`_proposals`)
- Modify: `src/trajectree/runner/parse.py` (`parse_codex`)
- Test: `tests/test_runner_kinds*.py` (or the file testing plans), `tests/test_runner_parse*.py`, `tests/test_runner_service*.py`

**Interfaces:**
- Produces: `strict_ok(schema: dict) -> list[str]` in `tests/` as a helper (not product code).
  It returns the violations: an object without `additionalProperties: false`, or with
  `required` not equal to its property keys.

- [ ] **Step 1: Write failing tests**
  - `test_every_plan_schema_is_strict`: for each run kind, build a plan with minimal valid
    inputs; `strict_ok(plan.json_schema)` is `[]` for every schema that is not None.
  - `test_null_optional_fields_take_defaults`: a parsed result whose structured output has a
    proposal with `event_kind`, `when`, `due`, `body_md` and `status` all `None` yields a
    `Proposal` with `event_kind == "note"`, `body_md == ""`, `status == "happened"`,
    `when is None`, `due is None`.
  - `test_parse_codex_reports_turn_failure`: stdout holds the two lines from the real failure
    (an `error` event, then `turn.failed` with
    `{"error":{"message":"Invalid schema for response_format ..."}}`), and no last message.
    `parsed.error` contains `Invalid schema for response_format`, and is at most 500 characters.
- [ ] **Step 2: Run them; expect FAIL.**
- [ ] **Step 3: Implement**
  - In the schema, every property is required. The nullable ones are
    `{"type": ["string", "null"]}`, and the enums gain `null`
    (`{"enum": ["happened", "planned", "suggested", None]}`).
  - `_proposals` drops keys whose value is `None` before `Proposal.model_validate`.
  - `parse_codex`, when there is no last message, takes the message of the last `turn.failed`
    event (its `error.message`), else of the last `error` event, truncated to 500 chars. The
    error falls back to "no last message".
- [ ] **Step 4: Run `uv run pytest -q`; expect PASS.**
- [ ] **Step 5: Commit** `fix(runner): output schemas valid in OpenAI strict mode; codex failures keep codex's message`

### Task 4: A runner without a model

**Files:**
- Modify: `src/trajectree/runner/catalog.py` (`resolve_model`, `Resolved.model: str | None`)
- Modify: `src/trajectree/runner/service.py` (`validate`; after parsing, fill `run.model` from the output when it was None)
- Modify: `src/trajectree/runner/parse.py` (`ParsedResult.model`, `parse_claude_json`)
- Modify: `src/trajectree/models.py` (`NO_MODEL_CHOSEN` text)
- Modify: `src/trajectree/api/routes_routines.py` (`_needs_model`), `src/trajectree/repo/routines.py` (`disable_modelless_routines`), `src/trajectree/scheduler.py` (the two `NO_MODEL_CHOSEN` sites)
- Modify: `src/trajectree/cli/main.py` (pre-check at the `MODEL_REQUIRED` site; `routine set` drops `required=True` on `--model`)
- Modify: `web/src/api/types.ts` (`ModelChoice.model: string | null`), `web/src/components/ModelPicker.tsx`, `web/src/components/RoutinesSection.tsx`, and any view that prints a run's or routine's model
- Modify: `README.md` (Runs, Routines, Model catalog)
- Test: `tests/test_catalog*.py`, `tests/test_runner_service*.py`, `tests/test_routines*.py`, `tests/test_scheduler*.py`, `tests/test_cli*.py`, `web/src/components/ModelPicker.test.tsx`, `RoutinesSection.test.tsx`

**Interfaces:**
- Produces: `resolve_model(catalog, model: str | None, runner: str | None) -> Resolved`.
  - Neither given: raises `NoModelChosen`.
  - Runner only: requires that runner to be available, else `RunnerUnavailable`; returns
    `Resolved(runner=runner, model=None)`.
  - Model given: unchanged.
- Produces: `NO_MODEL_CHOSEN = "no model or runner was chosen"`.
- Produces: `ParsedResult.model: str | None`. For Claude it is the key of `modelUsage` with the
  largest `outputTokens`, or None. Codex always gives None.
- Produces: a routine is "chosen" when it has a runner or a model. The routine model rules
  (`_needs_model`, the scheduler, `disable_modelless_routines`) use that test.
- Produces: in the web, `ModelChoice = { runner: string; model: string | null }`. The picker's
  "default" option for runner R encodes as `R|` and decodes to `{runner: R, model: null}`. A
  held `{runner, model: null}` is stale only when R is no longer available.

- [ ] **Step 1: Write failing tests**
  - `test_resolve_runner_only`: an available `codex` with an empty model list gives
    `Resolved(runner="codex", model=None)`. An unavailable runner raises `RunnerUnavailable`.
    Neither raises `NoModelChosen`.
  - `test_enqueue_runner_only_omits_model_flag`: an ask with `runner="ccx:x"` and no model
    creates a run with `model is None`, and the built claude argv contains no `--model`.
  - `test_run_model_filled_from_model_usage`: a mock Claude result whose `modelUsage` has
    `{"a": {"outputTokens": 1}, "b": {"outputTokens": 9}}` leaves the finished run with
    `model == "b"`.
  - `test_runner_only_routine_survives_startup`: a routine with a runner and no model,
    enabled, is still enabled after `disable_modelless_routines`. A routine with neither is
    disabled.
  - `test_legacy_model_only_routine_runs`: a routine with model `claude-haiku-5-5` and runner
    None validates against a catalog offering it.
  - `test_cli_routine_set_runner_only`: `routine set --runner codex` (no `--model`) is
    accepted. `ask` with neither flag fails with exit 2 and the new message.
  - Web: the picker lists "default" first under each available runner. Choosing it calls
    `onChange({runner, model: null})`. A held runner-only choice stays after a refresh while
    the runner is available.
- [ ] **Step 2: Run them; expect FAIL.**
- [ ] **Step 3: Implement.** Wherever the web or the CLI prints a run's or routine's model and
  it is None, show "default". `validate` keeps its routine fallback: a request without a model
  takes the routine's runner and model.
- [ ] **Step 4: Run `uv run pytest -q`, then from `web/` `npm test` and `npm run build`; expect PASS.**
- [ ] **Step 5: Commit** `feat(models): a runner alone is a choice; it runs on its own default model`

### Task 5: Editable catalog and the refresh merge

**Files:**
- Modify: `src/trajectree/runner/catalog.py` (`Source` gains `"manual"`; `discover` messages; `merge_failed` → `merge_refresh`; new `edit_models`)
- Modify: `src/trajectree/api/routes_models.py` (`POST /api/models/edit`)
- Modify: `src/trajectree/cli/main.py` (`models --runner R --add M --delete M`)
- Modify: `web/src/api/client.ts`, `web/src/api/types.ts`, `web/src/views/SettingsView.tsx` (ModelsSection)
- Modify: `README.md` (Model catalog)
- Test: `tests/test_catalog*.py`, `tests/test_api_models*.py` (or the file testing `/api/models`), `tests/test_cli*.py`, `web/src/views/SettingsView.test.tsx`

**Interfaces:**
- Produces: `merge_refresh(previous: Catalog, fresh: Catalog) -> Catalog` replaces
  `merge_failed`. For each runner in `fresh`:
  - No previous entry: take fresh as is.
  - `fresh.error is None` and its models are non-empty: take fresh. This replaces manual
    entries too.
  - Otherwise: keep the previous `models` and `refreshed_at`; take fresh's `available` and
    `error`.

  Runners missing from `fresh` are dropped.
- Produces: discovery that marks success through `error`:
  - Claude: an API list sets `error=None`. A failure returns the built-in models with an error,
    so they seed a first list only.
  - ccx: an answering endpoint, or no base URL, sets `error=None`. A failure returns the
    profile models with an error.
- Produces: `edit_models(catalog: Catalog, runner: str, add: list[str], delete: list[str]) -> Catalog`.
  - It raises `KeyError` for a runner not in the catalog.
  - Additions are `CatalogModel(source="manual")`, skipped when the model is already present.
  - Deletions remove any source.
  - The order is the existing order, then the additions.
- Produces: `POST /api/models/edit` with body `{runner: str, add: list[str] = [], delete: list[str] = []}`,
  under the ledger lock. It returns the `Catalog`, or 404 for an unknown runner, and 422 when
  both lists are empty or a name is blank.
- Produces: the hint texts, verbatim:
  - ccx without credentials: `the profile has no API key or token; add one to ~/.config/claude-backends/<name>/settings.json, then run: trajectree backends sync`
  - ccx endpoint failure: `no model list from <host> (<status or error>); edit the list with: trajectree models --runner ccx:<name> --add <model>`
  - Codex without a cache, logged in: `no model cache yet; run one Codex run (trajectree ask --runner codex "Reply with OK."), then refresh`
  - Claude, empty or failed API list: `live list unavailable: <status or "empty list">; list unchanged`
- Web: in Settings Models, each model row has a × button (aria-label `Remove <model>`), and
  each runner has a text input plus an `Add` button. Both call `api.editModels` and update the
  `["models"]` query.

- [ ] **Step 1: Write failing tests**
  - `test_merge_success_replaces_manual`: previous `codex` has `[x(cache), m(manual)]`, fresh
    succeeds with `[y]`; the result is `[y]`.
  - `test_merge_failure_keeps_list`: previous `ccx:d` has `[a(profile), m(manual)]`, fresh has
    `error="no model list…"` and `[a]`; the result keeps `[a, m]` with the new error.
  - `test_merge_first_seen_seeds`: no previous `ccx:d`; fresh fails with `[a(profile)]`; the
    result is `[a]`.
  - `test_merge_empty_success_keeps_list`: fresh `error=None` with `[]` keeps the previous
    list.
  - `test_legacy_catalog_loads_and_merges`: a stored JSON written by the old code (sources
    `api`/`builtin`/`profile`, no `manual`) loads and merges.
  - `test_edit_models_add_delete`, `test_edit_unknown_runner_404`, `test_edit_empty_422`.
  - `test_cli_models_add_delete`: `models --runner ccx:d --add p --add q --delete a` sends one
    edit request and prints the catalog. `--add` without `--runner` exits 2. `--add` with
    `--refresh` exits 2.
  - `test_ccx_no_credentials_hint` and `test_codex_no_cache_hint` assert the verbatim texts
    above.
  - Web: clicking × calls `editModels` with `{runner, delete:[model]}`. Typing and clicking
    `Add` sends `{runner, add:[name]}`.
- [ ] **Step 2: Run them; expect FAIL.**
- [ ] **Step 3: Implement.** Update the README Model catalog section with the merge rule and
  the edit commands.
- [ ] **Step 4: Run `uv run pytest -q`, then from `web/` `npm test` and `npm run build`; expect PASS.**
- [ ] **Step 5: Commit** `feat(models): edit the catalog by hand; a refresh replaces a runner's list only when its fetch succeeded`

### Task 6: Backend sockets and network isolation

**Files:**
- Modify: `src/trajectree/config.py` (`Settings.sockets_dir: Path | None`, env `TRAJECTREE_SOCKETS_DIR`)
- Create: `src/trajectree/runner/sockets.py` (`backend_socket`)
- Modify: `src/trajectree/runner/argv.py` (`run_env`), `src/trajectree/runner/catalog.py` (`_ccx`, `discover` parameter)
- Modify: `deploy/compose.yaml` (mount, env, network bridge name)
- Create: `deploy/systemd/trajectree-backend@.socket`, `deploy/systemd/trajectree-backend@.service`
- Create: `scripts/smoke-backend-socket.sh`
- Modify: `README.md` (new "Local backends through a socket" subsection under Connectors or Runs; instance install mkdir of `sockets`; firewall; what gets written where; env table)
- Test: `tests/test_runner_argv.py`, `tests/test_catalog*.py`, `tests/test_sockets.py`

**Interfaces:**
- Produces: `backend_socket(settings: Settings, name: str) -> Path | None`. It returns
  `settings.sockets_dir / f"{name}.sock"` when `sockets_dir` is set and that path is a socket
  (`stat.S_ISSOCK` via `os.stat`, following no further checks), else None. `OSError` gives
  None.
- Produces: `run_env` for a `ccx:<name>` runner sets `ANTHROPIC_UNIX_SOCKET=<path>` when
  `backend_socket` returns one.
- Produces: `discover(..., uds_client: Callable[[Path], httpx.Client] = <real>)`. For a ccx
  profile with a socket, `_ccx` makes its models request through `uds_client(path)` and
  closes that client afterwards. The request URL keeps the profile's base URL, since its host
  and path still matter.
- Compose additions:
  - the volume `./sockets:/host/sockets:ro`;
  - env `TRAJECTREE_SOCKETS_DIR: /host/sockets`;
  - a top-level `networks: default: driver_opts: com.docker.network.bridge.name: trajectree0`.
- systemd templates:
  - `.socket` has `ListenStream=%h/.local/share/trajectree/sockets/%i.sock`, `SocketMode=0600`,
    `DirectoryMode=0700`, `[Install] WantedBy=sockets.target`.
  - `.service` has `Requires=` and `After=` the matching socket,
    `EnvironmentFile=%h/.config/trajectree/backends/%i.env`, and
    `ExecStart=/usr/lib/systemd/systemd-socket-proxyd ${TARGET}`.
  - A comment in each file says the path assumes the instance dir `~/.local/share/trajectree`.

- [ ] **Step 1: Write failing tests**
  - `test_backend_socket_detects_socket`: given a real `socket.AF_UNIX` bound in `tmp_path`,
    it returns the path. A regular file at the same name gives None, a missing file gives None,
    and an unset dir gives None.
  - `test_run_env_sets_unix_socket_for_ccx`: with a bound socket named after the backend, the
    env has `ANTHROPIC_UNIX_SOCKET`. Without one, it does not. The `claude` and `codex` runners
    never get it.
  - `test_ccx_models_over_socket`: `uds_client` is monkeypatched to return an
    `httpx.Client(transport=httpx.MockTransport(...))` that records the request. Discovery
    lists the endpoint's models, and the shared `http` client is never called for that profile.
  - `test_ccx_stale_socket_keeps_list` (Review Focus 3): the socket path exists but nobody
    listens. Use a real `uds_client` against a bound-then-closed socket, or a transport
    raising `httpx.ConnectError`. Discovery returns an error naming `ConnectError`, merging
    keeps the old list, and discovery does not raise.
- [ ] **Step 2: Run them; expect FAIL.**
- [ ] **Step 3: Implement**
  - `scripts/smoke-backend-socket.sh` follows the style of `scripts/smoke-confined-codex.sh`.
    It runs the built image with the instance hardening (read-only, cap_drop ALL,
    no-new-privileges). Inside, a Python stdlib HTTP server on a Unix socket in `/tmp` records
    the first request line and answers 500. The script runs `claude -p hi` with
    `ANTHROPIC_UNIX_SOCKET` set to that socket, `ANTHROPIC_BASE_URL=http://127.0.0.1:9`, and a
    dummy `ANTHROPIC_AUTH_TOKEN`, under `python -I -m trajectree.runner.confine` with the run's
    usual system paths. It then asserts that the server saw `POST /v1/messages`, printing
    `backend socket ok`.
  - README (the new subsection):
    1. Create `sockets/` in the instance dir.
    2. Write `~/.config/trajectree/backends/lmstudio.env` with `TARGET=127.0.0.1:40090`.
    3. Copy the two units to `~/.config/systemd/user/`, then run
       `systemctl --user daemon-reload && systemctl --user enable --now trajectree-backend@lmstudio.socket`.
    4. Run `docker compose down && docker compose up -d`. A changed network needs `down`.
    5. Run `sudo ufw insert 1 deny in on trajectree0`.
    6. Run `trajectree models --refresh`.

    It also states that every run can connect to every socket in `sockets/`, and that the host
    profile is unchanged.
- [ ] **Step 4: Run `uv run pytest -q` and `docker compose -f deploy/compose.yaml config -q` (with `TRAJECTREE_PROJECTS_ROOT=/tmp`); expect PASS. Build the image from the worktree (`docker build --build-arg INSTALL_CODEX=1 -t trajectree:audit .`) and run `scripts/smoke-backend-socket.sh trajectree:audit`; expect `backend socket ok`.**
- [ ] **Step 5: Commit** `feat(backends): local backends reach the host through a socket; the container's bridge is named for one firewall rule`

### Task 7: Run inspection in the CLI

**Files:**
- Modify: `src/trajectree/cli/main.py` (`runs` parser and `_cmd_runs`; the `run` routine argument)
- Modify: `README.md` (Runs)
- Test: `tests/test_cli*.py`

**Interfaces:**
- Produces: `trajectree runs [--limit N] [id]`.
  - Without an id, each line shows id, created, kind, status, runner, model or `default`,
    cost, and the first 60 characters of the error (or `-`).
  - With an id, it prints `GET /api/runs/{id}` as labelled lines: status, kind, runner, model,
    model warning, created, finished, cost, error, log path, then a blank line and
    `result_md`. A 404 prints `no run <id>` and exits 1.
- Produces: `trajectree run ""` (or whitespace) exits 2 with `routine name required` before
  any request is made.

- [ ] **Step 1: Write failing tests**
  - `test_runs_list_shows_runner_model_error`: a failed codex run with model None shows
    `codex`, `default` and the start of its error.
  - `test_runs_detail`: it shows the labelled fields and the result.
  - `test_runs_detail_404`: it exits 1 with `no run X`.
  - `test_run_empty_routine_refused`: it exits 2 with no HTTP call.
- [ ] **Step 2: Run them; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `uv run pytest -q`; expect PASS.**
- [ ] **Step 5: Commit** `feat(cli): runs shows runner, model and error; runs <id> shows one run`

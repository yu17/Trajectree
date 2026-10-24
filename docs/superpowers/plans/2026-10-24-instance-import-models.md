# Instances, Project Import, Model Choice, Codex Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a Trajectree instance a self-contained directory built from git, confine every agent run with Landlock, ask per project what history to import (condensing the rest into a starting-point block), require an explicit model for every run from a refreshable catalog, and ingest Codex sessions.

**Architecture:** A: a compose template under `deploy/`, a stdlib-only Landlock wrapper module that every runner process is started through, Codex switched to `danger-full-access` inside that wrapper. B: per-project import choices stored in `project.props["import"]` and honoured by the existing connectors, a daemon-side probe and import endpoint, a new `starting_point` run kind, and an interactive `init-project`. C: a model catalog kept in the daemon's state table, refreshed on demand, and enforced at enqueue time; a two-level picker in the web UI. D: a Codex transcript connector that emits the same `session` signals as the Claude one.

**Tech Stack:** Python 3.12, FastAPI, apsw/SQLite, pydantic 2, httpx, pathspec; React 19 + TanStack Query + vitest + Playwright; Docker Compose; Linux Landlock (raw syscalls via `ctypes`).

**Spec:** `docs/superpowers/specs/2026-10-23-instance-import-models-design.md` (approved 2026-10-24), building on `docs/superpowers/specs/2026-10-21-trajectree-architecture-design.md`.

## Global Constraints

- Secrets (OAuth token, run tokens `trj_…`, connector tokens, API keys found in backend profiles, Codex `auth.json`, ssh key) never appear in logs, run rows, API responses, error details, signal payloads, catalog entries or argv.
- No subprocess, HTTP or ssh call while the ledger lock is held (`ledger.locked()`); collect outside, write inside.
- The container stays `read_only: true`, non-root, `cap_drop: [ALL]`, `no-new-privileges:true`; host folders mount read-only.
- The host is written only by `init-project` and `hooks install`; any new host write is added to the README's "What gets written where" table.
- Every agent write lands pending in the Inbox, attributed to its run.
- Every runner process (`claude`, `codex`, `ccx:<backend>`) runs under Landlock; if Landlock is unavailable the run fails, never runs unconfined.
- After Task 9, no run starts without a model the owner chose. Exact error strings: `no model was chosen`; `unknown model <name>; run: trajectree models --refresh`; `model <name> is offered by <runner>, <runner>; pass --runner`.
- No new Python or npm dependencies (Landlock via `ctypes`; YAML in tests via the already-locked `pyyaml`, imported with `pytest.importorskip("yaml")`).
- Host tooling: `npm` is a pnpm shim (no `npx`): use `npm test`, `npm exec tsc --noEmit`, `npm run build`, `npm run e2e`, all from `web/`. Python: `uv run pytest -q`.
- The worktree command guard refuses shell loops, heredocs, `git -C`, awk and variable-built commands: run plain commands one at a time; write files with the Write tool.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never force-push; never push main.

## Review Focus

1. **Garbage at the interactive prompts** (`1,,3`, `0`, `9` with 5 sources, `all` when some sources are empty, a malformed or future date, an empty answer): `init-project` re-asks with a one-line reason; it never crashes and never fills in a default. Test in Task 7.
2. **Prefix-colliding folders** (`/mnt/Apps/Projects/transcriptor` and `/mnt/Apps/Projects/transcriptor-android`): the probe counts each transcript only for the folder its `cwd` really lies in. A `cwd` of `/x/transcriptor-android` must not count for `/x/transcriptor`. Test in Task 6.
3. **An unreachable or hanging backend during catalog refresh** (for example `ccx:lmstudio` pointing at `127.0.0.1:40090`, unreachable from the container): the refresh finishes within the per-call timeout, the other runners still refresh, and that runner keeps its profile models and shows the error. Test in Task 8.
4. **A registered project whose folder is missing or not mapped** when a run starts: its rule is skipped, the run is still confined, and the run never starts unconfined. Test in Task 3.
5. **A Codex rollout file truncated mid-line, or with unknown record types from a newer Codex**: the parser skips what it does not recognise, keeps its byte offset before the partial line, and picks the line up whole on the next pass. Test in Task 11.

## File Map

| Area | Create | Modify |
| --- | --- | --- |
| A | `deploy/compose.yaml`, `deploy/env.example`, `src/trajectree/runner/confine.py`, `src/trajectree/runner/sandbox.py`, `tests/test_deploy_template.py`, `tests/test_confine.py`, `tests/test_sandbox.py`, `tests/bin/mock-responses`, `scripts/smoke-confined-codex.sh` | `compose.yaml`, `Dockerfile`, `README.md`, `src/trajectree/config.py`, `src/trajectree/runner/argv.py`, `src/trajectree/runner/service.py`, `src/trajectree/runner/kinds.py`, `tests/conftest.py`, `web/playwright.config.ts` |
| B | `src/trajectree/importing.py`, `src/trajectree/context/history.py`, `src/trajectree/connectors/probe.py`, `src/trajectree/api/routes_import.py`, `src/trajectree/runner/prompts/starting_point.md`, tests per task | `src/trajectree/models.py`, `src/trajectree/connectors/{local_git,folders,remote,claude_sessions}.py`, `src/trajectree/ingest/job.py`, `src/trajectree/runner/{kinds,service}.py`, `src/trajectree/api/app.py`, `src/trajectree/cli/{init_project,main}.py` |
| C | `src/trajectree/runner/catalog.py`, `src/trajectree/api/routes_models.py`, `web/src/components/ModelPicker.tsx`, tests | `src/trajectree/runner/{service,kinds,seeds}.py`, `src/trajectree/scheduler.py`, `src/trajectree/api/{app,routes_runs,routes_routines,routes_reports}.py`, `src/trajectree/cli/main.py`, `src/trajectree/config.py`, web views and components that start runs, `web/e2e/smoke.spec.ts` |
| D | `src/trajectree/connectors/codex_sessions.py`, `tests/fixtures/codex/`, `tests/test_codex_sessions.py` | `src/trajectree/config.py`, `src/trajectree/ingest/job.py`, `src/trajectree/runner/{kinds,service}.py`, `src/trajectree/connectors/probe.py`, `src/trajectree/context/history.py`, `compose.yaml`, `deploy/compose.yaml`, `README.md` |

---

### Task 1: Instance template and dev compose

**Files:**
- Create: `deploy/compose.yaml`, `deploy/env.example`, `tests/test_deploy_template.py`
- Modify: `compose.yaml`, `Dockerfile` (add `ripgrep` to the app stage's `apt-get install` line), `README.md`

**Interfaces:**
- Produces: the instance layout later tasks extend (Task 11 adds the Codex sessions mount to both compose files).

- [ ] **Step 1: Write the failing test** `tests/test_deploy_template.py` (module-level `yaml = pytest.importorskip("yaml")`; load both files with `yaml.safe_load`):
  - `test_instance_template_is_self_contained`: `doc["name"] == "trajectree"`; service `trajectree` has `build.context == "${TRAJECTREE_GIT_URL:-https://github.com/yu17/Trajectree.git}#${TRAJECTREE_REF:-main}"`, `build.target == "reports"`, `build.args["INSTALL_CODEX"] == "1"`; `"./data:/data"` and `"./cfg:/cfg"` in `volumes`; `mem_limit == "4g"`; `read_only is True`; `cap_drop == ["ALL"]`; `"no-new-privileges:true"` in `security_opt`; `ports == ["127.0.0.1:8742:8742"]`; for each of `TRAJECTREE_INGEST_EVERY`, `TRAJECTREE_SCAN_SINCE_DAYS`, `TRAJECTREE_CHATMAID_URL`, `TRAJECTREE_GITLAB_HOST`, `TRAJECTREE_TZ`, the key is in `environment`.
  - `test_dev_compose_cannot_replace_instance`: root `compose.yaml` has `name == "trajectree-dev"` and `ports == ["127.0.0.1:8743:8742"]`.
  - `test_env_example_names_every_template_variable`: every `${VAR` referenced in `deploy/compose.yaml` other than `HOME`, `UID` and `GID` appears as `VAR=` in `deploy/env.example`.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_deploy_template.py -q`
Expected: FAIL (file not found).

- [ ] **Step 3: Write `deploy/compose.yaml`**: the v1 hardening, mounts and environment from the root `compose.yaml`, with the decisions from spec §4:
  - **Top level and build:** `name: trajectree`; `build: {context: <the string above>, target: reports, args: {INSTALL_CODEX: "1"}}`.
  - **Volumes:** `./data:/data`, `./cfg:/cfg`, the three read-only host mounts, and `${TRAJECTREE_PROJECTS_ROOT:?…}:/projects:ro`.
  - **Environment:** pass-throughs as `${VAR:-}`, plus `TRAJECTREE_TZ: ${TZ:-UTC}` and `TRAJECTREE_INGEST_EVERY: ${TRAJECTREE_INGEST_EVERY:-300}`.
  - **Limits:** `mem_limit: 4g`; keep `pids_limit: 256` unless the PDF export check in Step 6 fails.

  `deploy/env.example` lists `TRAJECTREE_PROJECTS_ROOT`, `TZ`, `UID`, `GID`, `TRAJECTREE_GIT_URL`, `TRAJECTREE_REF`, `TRAJECTREE_INGEST_EVERY`, `TRAJECTREE_SCAN_SINCE_DAYS`, `TRAJECTREE_CHATMAID_URL` and `TRAJECTREE_GITLAB_HOST`, each with a one-line comment. Root `compose.yaml`: add `name: trajectree-dev` and change the port to `127.0.0.1:8743:8742`.

- [ ] **Step 4: README.** Add an "Install an instance" section before "Run with Docker", with the sequence from spec §4:
  1. Create the instance directory and copy the two files in (`env.example` as `.env`).
  2. `mkdir -p data cfg && chmod 700 cfg`, then fill `.env`.
  3. `docker compose up -d --build`, then copy `data/token` to `~/.config/trajectree/token`.
  4. Install the host CLI with `uv tool install git+https://github.com/yu17/Trajectree@<ref>`.
  5. Log in the runners:
     - `claude setup-token` + `trajectree login`;
     - `docker compose exec -e CODEX_HOME=/cfg/codex trajectree codex login --device-auth`;
     - `trajectree backends sync`.
  6. Run `trajectree models --refresh`, then `trajectree init-project <folder>`.

  Mark the commands added by later tasks "(from Task 8)" or "(from Task 7)" until those land. Rename "Run with Docker" to "Development compose": it says the dev stack is `trajectree-dev` on port 8743, and that instance state must never live inside `TRAJECTREE_PROJECTS_ROOT`.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_deploy_template.py -q`, then `docker compose -f deploy/compose.yaml --env-file deploy/env.example config --quiet`, then `docker compose config --quiet` (with `TRAJECTREE_PROJECTS_ROOT=/tmp` exported for both).
Expected: 3 passed; both config commands exit 0.

- [ ] **Step 6: Build check.** Run `docker build --target reports --build-arg INSTALL_CODEX=1 -t trajectree-plan-check .`, then `docker run --rm --entrypoint rg trajectree-plan-check --version`.
Expected: the build succeeds and ripgrep prints its version. Remove the image afterwards (`docker rmi trajectree-plan-check`).

- [ ] **Step 7: Commit**

```bash
git add deploy/ compose.yaml Dockerfile README.md tests/test_deploy_template.py
git commit -m "feat(deploy): instance compose template built from git; dev compose renamed"
```

### Task 2: Landlock wrapper module

**Files:**
- Create: `src/trajectree/runner/confine.py`, `tests/test_confine.py`

**Interfaces:**
- Produces (stdlib only, no `trajectree` imports, so it stays cheap to start as `python -m`):
  - `CONFINE_FAILED_EXIT = 97`
  - `class LandlockUnavailable(OSError)`
  - `landlock_abi() -> int` (raises `LandlockUnavailable`)
  - `system_ro_paths() -> list[Path]`: `/usr /lib /lib64 /bin /sbin /etc /opt /app /proc /sys` plus `Path(sys.prefix).resolve()` and `Path(sys.base_prefix).resolve()`
  - `restrict(ro: Sequence[Path], rw: Sequence[Path]) -> int`: restricts the calling process, returns the ABI
  - `confine_argv(ro: Sequence[Path], rw: Sequence[Path], argv: Sequence[str], *, python: str = sys.executable) -> list[str]`, returning `[python, "-m", "trajectree.runner.confine", "--ro", *ro, "--rw", *rw, "--", *argv]`
  - `main(argv: list[str]) -> NoReturn`

- [ ] **Step 1: Write the failing tests** (a module-level fixture skips every real-kernel test when `landlock_abi()` raises; each test runs the module in a subprocess with `--ro` = `system_ro_paths()` plus its own tmp dirs):
  - `test_denied_file_is_unreadable`: `cat <tmp>/secret/token` under `--ro <tmp>/allowed` → return code != 0 and `"Permission denied"` in stderr.
  - `test_allowed_file_is_readable`: `cat <tmp>/allowed/f` prints its content.
  - `test_rw_dir_is_writable_ro_dir_is_not`: `sh -c 'echo x > <rw>/a && echo x > <ro>/b'` → `<rw>/a` exists and `<ro>/b` does not.
  - `test_children_inherit_the_restriction`: `sh -c 'sh -c "cat <secret>"'` → denied.
  - `test_missing_paths_are_skipped`: an `--ro` entry that does not exist does not fail the wrapper.
  - `test_proc_escape_is_denied`: `cat /proc/1/root<secret>` → denied (skip if `/proc/1/root` is not readable even without Landlock).
  - `test_unavailable_exits_97` (no kernel needed): `main` with `landlock_abi` monkeypatched to raise `LandlockUnavailable("not supported")` → `SystemExit` with code 97, and stderr starts with `trajectree-confine: landlock unavailable: not supported`.
  - `test_confine_argv_shape`: exact list for `ro=[Path("/a")]`, `rw=[Path("/b")]`, `argv=["x", "--y"]`, `python="/py"`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_confine.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `confine.py`.**
  - **Syscalls:** `landlock_create_ruleset`/`add_rule`/`restrict_self` (444/445/446 on x86_64) via `ctypes.CDLL(None, use_errno=True).syscall`, plus `prctl(PR_SET_NO_NEW_PRIVS)`.
  - **Handled access per ABI:**
    - ABI 1: bits 0–12;
    - ABI 2 adds `REFER` (bit 13);
    - ABI 3 adds `TRUNCATE` (bit 14);
    - ABI 5 adds `IOCTL_DEV` (bit 15).
  - **Scoping:** ABI ≥ 6 sets `scoped = LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET | LANDLOCK_SCOPE_SIGNAL` (1 | 2) and passes the 24-byte attr; below 6, 16 bytes.
  - **Rights:**
    - read-only: `EXECUTE|READ_FILE|READ_DIR`;
    - read-write: every handled bit;
    - a non-directory path gets only the file rights (`EXECUTE|WRITE_FILE|READ_FILE|TRUNCATE|IOCTL_DEV`, masked to handled).
  - **Failures:** any failure in `main` prints `trajectree-confine: landlock unavailable: <reason>` to stderr and exits 97; on success, `os.execvp`. The working throwaway version (verified in the hardened container) is recorded in spec §9; this module is its tested form.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_confine.py -q`
Expected: all pass on this host (Landlock ABI 10); kernel tests are skipped elsewhere.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/runner/confine.py tests/test_confine.py
git commit -m "feat(runner): stdlib Landlock wrapper for runner processes"
```

### Task 3: Confine every run; Codex in danger-full-access

**Files:**
- Create: `src/trajectree/runner/sandbox.py`, `tests/test_sandbox.py`, `tests/bin/mock-responses`, `scripts/smoke-confined-codex.sh`
- Modify: `src/trajectree/runner/service.py`, `src/trajectree/runner/argv.py`, `src/trajectree/runner/kinds.py` (`_deny_read` list), `src/trajectree/config.py`, `tests/conftest.py`, `web/playwright.config.ts`, `tests/test_run_service.py`, `tests/test_runner_argv.py`, `README.md`

**Interfaces:**
- Consumes: `confine_argv`, `landlock_abi`, `system_ro_paths`, `LandlockUnavailable` and `CONFINE_FAILED_EXIT` from Task 2.
- Produces:
  - `Settings.confine_extra_ro: list[Path]` (env `TRAJECTREE_CONFINE_EXTRA_RO`, colon-separated; for dev and tests only, so that binaries outside the system dirs, such as `tests/bin`, stay executable).
  - `class ConfinePaths(BaseModel): ro: list[Path]; rw: list[Path]`.
  - `run_paths(conn, settings, *, runner: str, backend: str, run_dir: Path, tmp_dir: Path) -> ConfinePaths`.
  - Run props `landlock_abi: int`.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_sandbox.py`:
    - `test_ro_has_every_registered_folder`: two projects with `folder` resources whose local paths exist → both are in `ro`.
    - `test_missing_folder_is_skipped`: a `folder` resource whose path does not exist is absent, and no error is raised (Review Focus 4).
    - `test_rw_is_run_scoped`: `rw` is exactly `[Path("/dev"), tmp_dir, run_dir, <runner cfg dir>]`, where the runner cfg dir is:
      - `claude` → `settings.claude_config_dir`;
      - `codex` → `settings.codex_home`;
      - `ccx:kimi` → `settings.backends_cfg_dir / "kimi"`.
    - `test_never_whole_data_or_cfg`: neither `settings.data_dir` nor `settings.cfg_root` nor anything under `settings.claude_home`/`settings.backends_root` appears in `ro` or `rw`.
  - `tests/test_runner_argv.py`: `test_codex_argv_uses_danger_full_access`: `codex_argv(...)` contains `"--sandbox", "danger-full-access"` and not `"read-only"`.
  - `tests/test_run_service.py`:
    - `test_launch_argv_is_confined`: the argv passed to `launch` starts with `[sys.executable, "-m", "trajectree.runner.confine"]` and its part after `--` is the runner argv.
    - `test_env_has_private_tmp_and_home`: `env["TMPDIR"]` is a fresh directory, removed after the run; `env["HOME"] == str(run_dir / "home")`.
    - `test_landlock_unavailable_fails_closed`: with `landlock_abi` raising, the run ends `failed`, `props.failure == "infra"`, error starts with `landlock unavailable`, and `launch` is never called.
    - `test_wrapper_exit_97_is_infra`: an outcome with `exit_code == 97` and stderr `trajectree-confine: landlock unavailable: x` → `failed`, infra.
    - `test_abi_recorded`: `run.props["landlock_abi"]` equals the stub's ABI.
  - `tests/test_runner_kinds.py`: `test_summarize_denies_backend_profiles`: when `settings.backends_root` is set, `plan.tools.disallowed` contains `Read(//<backends_root>/**)`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_sandbox.py tests/test_runner_argv.py tests/test_run_service.py tests/test_runner_kinds.py -q`
Expected: the new tests fail.

- [ ] **Step 3: Implement.**
  - **`sandbox.run_paths`:**
    - lists every `folder` resource (any project), maps it through `to_local` and keeps existing directories;
    - `ro` = `system_ro_paths()` + `settings.confine_extra_ro` + those folders;
    - `rw` as in the test above.
  - **In `RunService.execute`**, after `_prepare` returns and before `launch`, with the lock released:
    1. Call `landlock_abi()` (on `LandlockUnavailable`, `self._abort`-style failure with error `landlock unavailable: <reason>`, infra).
    2. `tmp_dir = Path(tempfile.mkdtemp(prefix=f"trajectree-run-{run_id}-"))`, and create `run_dir/"home"`.
    3. Compute `run_paths` under a short lock.
    4. Wrap the argv with `confine_argv`.
    5. Set `env["TMPDIR"]` and `env["HOME"]`.
    6. Store `props.landlock_abi`.
    7. Remove `tmp_dir` in the existing `finally`.
  - **In `_finalize`:** treat `exit_code == CONFINE_FAILED_EXIT` with stderr starting `trajectree-confine:` as an infra failure carrying that line.
  - **`codex_argv`:** `--sandbox danger-full-access`.
  - **`run_env`:** stops setting `HOME` to `settings.run_home`; the service sets it.
  - **`_deny_read`:** add `settings.backends_root` when set.
  - **Test configuration:** in `tests/conftest.py`'s `settings` fixture and in `web/playwright.config.ts`, set `confine_extra_ro` / `TRAJECTREE_CONFINE_EXTRA_RO` to the repo's `tests/bin` and the repo root's `.venv`.

- [ ] **Step 4: Container smoke test.**
  - `tests/bin/mock-responses` is a stdlib Responses-API mock: the first POST returns one `exec_command` function call with the command from env `MOCK_CMD`; a POST whose `input` contains `function_call_output` returns an assistant message echoing those outputs. It is the throwaway mock from spec §9.
  - `scripts/smoke-confined-codex.sh`:
    1. builds the `app` image with `INSTALL_CODEX=1`;
    2. creates fake `cfg/claude/oauth_token` and `data/token` in a temp dir, plus a Codex home with a `mock` provider config (`requires_openai_auth = false`, `supports_websockets = false`);
    3. runs the container with the compose hardening, starting `mock-responses` and then `python -m trajectree.runner.confine` (ro: system dirs and `/projects`; rw: `/tmp`, `/dev`, the Codex home) around `codex exec --sandbox danger-full-access --ephemeral --skip-git-repo-check`;
    4. asserts that the echoed output contains the readable file's content and `Permission denied` for both fake secrets and for `/proc/1/root/data/token`.

  Run: `bash scripts/smoke-confined-codex.sh`
  Expected: final line `smoke: confined codex ok`.

- [ ] **Step 5: Run the whole suite and e2e**

Run: `uv run pytest -q`, then `npm run e2e` from `web/`.
Expected: all pass (e2e runs `fake-claude` through the wrapper).

- [ ] **Step 6: README.** Add a "Run confinement" subsection under "Runs":
  - what a run can read and write;
  - fail-closed behaviour;
  - Codex in `danger-full-access` and why;
  - `TRAJECTREE_CONFINE_EXTRA_RO` added to the env table (dev/test only).

- [ ] **Step 7: Commit**

```bash
git add src/trajectree/runner/ src/trajectree/config.py tests/ scripts/smoke-confined-codex.sh web/playwright.config.ts README.md
git commit -m "feat(runner): confine every run with Landlock; codex runs in danger-full-access"
```

### Task 4: Per-project import choices honoured by the connectors

**Files:**
- Create: `src/trajectree/importing.py`, `tests/test_importing.py`
- Modify: `src/trajectree/connectors/local_git.py`, `src/trajectree/connectors/folders.py`, `src/trajectree/connectors/remote.py`, `src/trajectree/connectors/claude_sessions.py`, `src/trajectree/ingest/job.py`, and their tests

**Interfaces:**
- Produces (`trajectree.importing`):
  - `IMPORT_SOURCES = ("git", "claude", "codex", "folder", "remote")`.
  - `SESSION_SOURCE_OF_BACKEND`: `"codex"` → `"codex"`, any other backend → `"claude"`.
  - `class SourceChoice(BaseModel): picked: bool; since: date | Literal["all"] | None = None`. `None` means the default cutoff; `"all"` means everything.
  - `class ImportRecord(BaseModel): sources: dict[str, SourceChoice]; recorded_at: AwareDatetime; start_run_id: str | None = None`, stored as `project.props["import"]`.
  - `import_record(project) -> ImportRecord | None`.
  - `source_picked(project, source) -> bool`: `True` when there is no record, preserving v1 behaviour for the `inbox` project and API-created projects.
  - `source_start(project, source, *, now, default_days, tz) -> datetime | None`:
    - `None` when the choice is `"all"`;
    - a date → local midnight of that date in `tz`, as UTC;
    - no record or `since is None` → `now - default_days` at UTC midnight;
    - `"remote"` uses the `"git"` choice.
  - Connector signatures gain `starts: Mapping[str, datetime | None]` keyed by resource id (`collect_git`, `collect_folders`, `collect_remote`). The first poll of a resource with no cursor uses `starts[resource.id]`, where `None` means `datetime(1970, 1, 1, tzinfo=timezone.utc)`. A resource with a cursor ignores `starts`.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_importing.py`:
    - `test_no_record_means_v1_behaviour`;
    - `test_all_means_none`;
    - `test_date_is_local_midnight` (2025-01-15 in `America/Chicago` → `2025-01-15T06:00Z`);
    - `test_remote_follows_git`;
    - `test_unpicked_source`.
  - `tests/test_local_git.py`:
    - `test_first_poll_uses_project_start`: a repo with commits in 2023 and 2026, start 2023-01-01 → both years' signals;
    - `test_cursor_wins_over_start`.
  - `tests/test_folders.py`: `test_unpicked_folder_is_not_scanned`: `run_ingest` skips a folder resource whose project did not pick `folder`.
  - `tests/test_claude_sessions.py`:
    - `test_unpicked_sessions_not_attached`;
    - `test_session_before_start_not_attached`: a session ending 2026-03-01 with a claude start of 2026-05-01 gets no signal;
    - `test_all_lifts_transcript_cutoff`: a transcript file older than `scan_since_days` is read when some project's claude choice is `"all"`.
  - `tests/test_remote.py`: `test_first_poll_uses_git_start`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_importing.py tests/test_local_git.py tests/test_folders.py tests/test_claude_sessions.py tests/test_remote.py -q`
Expected: the new tests fail.

- [ ] **Step 3: Implement.**
  - **`ingest/job.py`:** loads the projects of the listed resources under the lock, builds `starts` with `source_start`, and filters resource lists with `source_picked` (`git` for `git_local`, `folder` for `folder` scans, `remote` for `git_remote`). It passes both into the existing collect calls.
  - **`claude_sessions`:**
    - `_session_signals` skips a summary when its project has not picked `SESSION_SOURCE_OF_BACKEND[backend]`, or when `t_end or seen` is before that source's start.
    - `_refresh_summaries`' mtime cutoff becomes the earliest claude start over all projects. That is `now - scan_since_days` when nobody asked for more, and there is no cutoff when any project chose `"all"`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/importing.py src/trajectree/connectors/ src/trajectree/ingest/job.py tests/
git commit -m "feat(ingest): per-project import sources and start dates"
```

### Task 5: The `starting_point` run kind

**Files:**
- Create: `src/trajectree/context/history.py`, `src/trajectree/runner/prompts/starting_point.md`, `tests/test_context_history.py`
- Modify: `src/trajectree/models.py`, `src/trajectree/runner/kinds.py`, `src/trajectree/runner/service.py`, `tests/test_runner_kinds.py`, `tests/test_run_service.py`

**Interfaces:**
- Consumes: `ImportRecord` and `IMPORT_SOURCES` (Task 4).
- Produces:
  - `RunKind` gains `"starting_point"`, and `SLOT_OF["starting_point"] = "interactive"`.
  - `class StartingPointRequest(BaseModel): project: str; starts: dict[str, date]; runner: str | None = None; model: str | None = None`. `starts` maps each source that skipped history to its chosen start, for example `{"git": date(2025, 1, 1)}`.
  - `block_date(starts) -> date`: the latest value.
  - `collect_history(settings, *, folders: list[Path], starts: Mapping[str, date], tz: str, budget_chars: int) -> HistoryInput`. It runs `git log` and walks folders, so it is called with no lock.
  - `class HistoryInput(BaseModel): git_md: str; folder_md: str; truncated: bool`.
  - `session_history(conn, settings, project, *, starts, budget_chars) -> str` (DB plus transcript digests).
  - The prompt variables are `$project_title`, `$project_slug`, `$block_date`, `$git_history`, `$session_history`, `$folder_snapshot` and `$sources`.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_context_history.py`:
    - `test_git_before_start_only`: a repo with commits 2024-06 and 2025-03 and start 2025-01-01 → `git_md` names the 2024 subject, not the 2025 one;
    - `test_large_history_is_condensed_by_month`: 600 commits → output under `budget_chars`, one line per month with its count, `truncated` true;
    - `test_folder_snapshot_lists_top_level`: top-level entries with sizes and the newest modification date, honouring the folder connector's skip list;
    - `test_session_history_before_start`: only sessions ending before the claude start are included.
  - `tests/test_runner_kinds.py`:
    - `test_starting_point_plan`: the scope is `Scope(projects=[pid], tools=["record_event"], write=True)`, the rendered prompt contains `<data name="git_history">` and the block date, and the tools are `Read`/`Grep`/`Glob`/`Bash(git log *)` with the same deny rules as summarize;
    - `test_block_date_is_latest_start`: `{"git": 2025-01-01, "claude": 2026-01-01}` → `2026-01-01`.
  - `tests/test_run_service.py`:
    - `test_history_collected_outside_lock`: a `collect_history` stub asserts that the ledger lock is not held when it is called;
    - `test_no_history_fails_cleanly`: empty input → the run ends `failed` with error `no history before the chosen start`, and no token stays live.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_context_history.py tests/test_runner_kinds.py tests/test_run_service.py -q`
Expected: the new tests fail.

- [ ] **Step 3: Implement.**
  - **`RunService.execute`:** for `starting_point` runs, calls `collect_history` after the version probe and before `_prepare`, and passes the result to `plan_run` through a new keyword `prelocked: dict[str, Any] | None` (`plan_run(..., prelocked={"history": HistoryInput})`).
  - **Plan:** `_plan_starting_point` renders the seeded prompt and fences each input in `<data name=…>` with the v1 Rules sentence. The system prompt names exactly one `record_event` call with `kind: "starting_point"`, `status: "happened"` and `when: <block_date>`, writes nothing else, and sends anything else to `proposals`.
  - **Enqueue:** `service.enqueue` resolves the project, as it does for summarize.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/ tests/
git commit -m "feat(runner): starting_point run kind summarising history before the import start"
```

### Task 6: Probe and import endpoints

**Files:**
- Create: `src/trajectree/connectors/probe.py`, `src/trajectree/api/routes_import.py`, `tests/test_probe.py`, `tests/test_api_import.py`
- Modify: `src/trajectree/api/app.py`

**Interfaces:**
- Consumes: Tasks 4 and 5; `discover_remotes`; `encode_project_dir`; the folder connector's skip list and big-directory rule.
- Produces:
  - `class SourceProbe(BaseModel): source: str; count: int; oldest: AwareDatetime | None; detail: str = ""; imported: bool = False`
  - `class RemoteProbe(BaseModel): url: str; title: str; declined: bool = False`
  - `class ProbeReport(BaseModel): path: str; default_since: date; sources: list[SourceProbe]; remotes: list[RemoteProbe]`. `default_since` is the default cutoff as a local date (`now - scan_since_days` in `settings.tz`), so the CLI can offer it and compute concrete `starts` without knowing the setting. `sources` is in `IMPORT_SOURCES` order without `"remote"`; until Task 11, `codex` is listed with count 0 and detail `not connected`.
  - `probe_folder(conn_reader, settings, host_path, *, now) -> ProbeReport`. The heavy work (git, folder walk, transcript reads) runs with no lock; `conn_reader` is a callable that briefly takes the lock to read registered folders and states.
  - `POST /api/import/probe {"path": str}` → `ProbeReport` (owner only).
  - `class StartBlockChoice(BaseModel): runner: str | None = None; model: str`.
  - `class ImportRequest(BaseModel): sources: dict[str, SourceChoice]; remotes: list[str] = []; start_block: StartBlockChoice | None = None; starts: dict[str, date] = {}`.
  - `POST /api/projects/{project_id}/import` → `{"project": Project, "resources": list[Resource], "starting_point_run": Run | None}`.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_probe.py`:
    - `test_git_counts_and_oldest`;
    - `test_claude_sessions_by_cwd`: transcripts under the encoded directory whose recorded `cwd` is the folder or below it count;
    - `test_prefix_collision` (Review Focus 2): with folders `/x/transcriptor` and `/x/transcriptor-android`, a transcript with `cwd=/x/transcriptor-android` counts only for the second;
    - `test_folder_count_respects_skips`;
    - `test_remotes_listed_with_declined_flag`;
    - `test_imported_flag_from_record`.
  - `tests/test_api_import.py`:
    - `test_import_saves_record_and_resources`: picking `git` creates `git_local`; the `folder` resource always exists;
    - `test_picked_remote_is_accepted_without_inbox`: the resource exists and its txn review is not `pending`;
    - `test_unpicked_remote_recorded_declined`: it is added to `remote:suggested` for that project, and a following `run_ingest` creates no suggestion;
    - `test_reimport_of_picked_source_is_409` with detail `already imported: git`;
    - `test_start_block_enqueues_starting_point` (the run's request carries `starts` and the model);
    - `test_probe_is_owner_only` (a scoped run token gets 403).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_probe.py tests/test_api_import.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement.**
  - **Git probe:** `git -c safe.directory=* -C <local> rev-list --count --all` and `git log --all --reverse --format=%cI --max-count=1`, with a 30 s timeout.
  - **Claude probe:** candidate directories are those named `encode_project_dir(path)` or starting with `encode_project_dir(path) + "-"`. Each candidate transcript's `cwd` is read from its first records (stop after 50 lines), and the deepest-registered-folder rule is applied with the folder being probed treated as registered.
  - **Folder probe:** stops counting at 200,000 files, with detail `at least 200000 files`.
  - **Import endpoint:** remote acceptance writes `upsert_resource` as a human actor (`Actor(kind="human", ref="owner")`).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/connectors/probe.py src/trajectree/api/ tests/
git commit -m "feat(api): import probe and per-project import endpoint"
```

### Task 7: Interactive `init-project`

**Files:**
- Modify: `src/trajectree/cli/init_project.py`, `src/trajectree/cli/main.py`, `tests/test_init_project.py`, `README.md` ("Connect a project folder")

**Interfaces:**
- Consumes: the Task 6 endpoints.
- Produces:
  - Flags: `--sources <list|all>`, `--since <source>=<YYYY-MM-DD|all|default>` (repeatable), `--start-model`, `--start-runner`, `--no-start-block`, `--yes`.
  - `init_project(...)` gains `ask: Callable[[str], str]`, `interactive: bool` and those options.

- [ ] **Step 1: Write the failing tests** (fake `api` and `ask`; assertions on printed text and API calls):
  - `test_lists_sources_numbered`: output contains `1. git`, the count, and `oldest 2023-03-14`; empty sources are printed with `(nothing found)` and cannot be picked;
  - `test_since_asked_only_when_older_than_cutoff`;
  - `test_start_model_asked_when_history_skipped` and `test_no_model_question_without_skipped_history`;
  - `test_summary_then_confirm` (answer `n` → no import call, exit 0, `nothing imported`);
  - `test_bad_answers_reprompt` (Review Focus 1): `1,,3`, `0`, `9`, `all` with an empty source, `2026-13-01`, a future date and an empty line each produce a one-line reason and a new prompt; the test ends with a valid answer and asserts no default was sent;
  - `test_non_tty_missing_choices_errors`: exit 2 and stderr `missing choices: --sources` (and `--since git=…` when git predates the cutoff, and `--start-model or --no-start-block` when history is skipped);
  - `test_flags_skip_prompts`;
  - `test_rerun_offers_only_new_sources` (imported sources show `(imported)` and are not selectable);
  - `test_remote_choice_sent`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_init_project.py -q`
Expected: the new tests fail.

- [ ] **Step 3: Implement.** The order is:
  1. register (as today);
  2. probe;
  3. list and pick;
  4. dates per source;
  5. remotes (y/n each);
  6. start model (a typed model name, plus a runner only if the owner adds one; Task 9 replaces this with the catalog list);
  7. summary;
  8. confirm;
  9. `POST …/import`;
  10. print the queued run id and `next ingest pass imports the history (trajectree ingest to run it now)`.

  The rules file, exclude and MCP shim steps stay as they are.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/cli/ tests/test_init_project.py README.md
git commit -m "feat(cli): interactive import choices in init-project"
```

### Task 8: Model catalog

**Files:**
- Create: `src/trajectree/runner/catalog.py`, `src/trajectree/api/routes_models.py`, `tests/test_catalog.py`, `tests/test_api_models.py`
- Modify: `src/trajectree/api/app.py`, `src/trajectree/api/routes_runs.py` (`POST /backends/sync` refreshes afterwards), `src/trajectree/cli/main.py`, `tests/test_cli_runs.py`, `README.md`

**Interfaces:**
- Produces:
  - `class CatalogModel(BaseModel): runner: str; model: str; display: str | None = None; source: Literal["api", "cache", "profile", "builtin"]`
  - `class RunnerCatalog(BaseModel): runner: str; available: bool; models: list[CatalogModel]; refreshed_at: AwareDatetime | None; error: str | None`
  - `class Catalog(BaseModel): runners: list[RunnerCatalog]`
  - `BUILTIN_CLAUDE_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")`
  - `discover(settings, *, now, http: httpx.Client, which=shutil.which, timeout_s: float = 5.0) -> Catalog`, with no DB access
  - `store_catalog(conn, catalog)` and `load_catalog(conn) -> Catalog`, under state key `models:catalog`
  - `merge_failed(previous: Catalog, fresh: Catalog) -> Catalog`: a runner whose fresh refresh failed keeps its previous models
  - `GET /api/models` → `Catalog`; `POST /api/models/refresh` → `Catalog`
  - CLI `trajectree models [--refresh]`

- [ ] **Step 1: Write the failing tests** (`httpx.MockTransport`; temporary Codex home and backend profiles):
  - `test_codex_from_cache_hides_hidden`: a cache with `gpt-6.1-sol` (`list`) and `codex-auto-review` (`hide`) → only the first, `source == "cache"`, `display == "GPT-6.1-Sol"`;
  - `test_codex_missing_cache_error`: error contains `codex login --device-auth`;
  - `test_ccx_profile_models`: env `ANTHROPIC_MODEL=deepseek-flash[1m]` and `ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-flash` → both, verbatim, deduplicated, `source == "profile"`;
  - `test_ccx_endpoint_adds_models`: the mock `GET <base>/v1/models` returns `{"data": [{"id": "deepseek-pro"}]}` → added with `source == "api"`; the request carries the profile's key header;
  - `test_unreachable_backend_keeps_profile_models` (Review Focus 3): the transport raises `httpx.ConnectTimeout` for `127.0.0.1:40090` → that runner has its profile models and an error, the other runners refresh, and the test finishes in under 2 s with `timeout_s=0.5`;
  - `test_claude_live_list`: the OAuth token is sent as a Bearer token with `anthropic-beta: oauth-2025-04-20` → ids from the response;
  - `test_claude_falls_back_to_builtin`: a 401 → `BUILTIN_CLAUDE_MODELS`, `source == "builtin"`, error `live list unavailable: 401`;
  - `test_no_secret_in_catalog`: the serialised catalog contains neither the token nor the profile key;
  - `test_refresh_endpoint_stores` and `test_backends_sync_refreshes` (API);
  - `test_cli_models_prints_and_refreshes` (CLI: without `--refresh` no POST is made; with it, the POST comes first).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_catalog.py tests/test_api_models.py tests/test_cli_runs.py -q`
Expected: the new tests fail.

- [ ] **Step 3: Implement.**
  - **ccx keys:** `ANTHROPIC_MODEL`, every key matching `ANTHROPIC_DEFAULT_[A-Z]+_MODEL`, and `CLAUDE_CODE_SUBAGENT_MODEL`, read from `settings.backends_cfg_dir/<name>/settings.json` `env`.
  - **Endpoint auth:** `x-api-key` from `ANTHROPIC_API_KEY`, or `Authorization: Bearer` from `ANTHROPIC_AUTH_TOKEN`, plus `anthropic-version: 2023-06-01`.
  - **Claude:** a runner is `available` when its binary is found; for `claude` the OAuth token must also be stored.
  - **Startup:** the app's lifespan starts one background refresh when `load_catalog` is empty, using no lock during HTTP.
  - **CLI output:** one header line per runner (`<runner>  refreshed <local time>` plus `, <error>` when set), then one indented line per model (`  <model>  <display>`).
  - **Verification:** check the Claude live list against a real token if one is available; otherwise note in the report that only the fallback path was exercised.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/ tests/ README.md
git commit -m "feat(runner): model catalog with refresh, API and CLI"
```

### Task 9: Require an explicit model everywhere

**Files:**
- Modify: `src/trajectree/runner/catalog.py`, `src/trajectree/runner/service.py`, `src/trajectree/config.py` (remove `default_runner` and `TRAJECTREE_DEFAULT_RUNNER`), `src/trajectree/api/app.py` (error handler), `src/trajectree/api/routes_routines.py`, `src/trajectree/repo/routines.py`, `src/trajectree/scheduler.py`, `src/trajectree/runner/seeds.py`, `src/trajectree/cli/main.py`, `src/trajectree/cli/init_project.py`, tests (`test_config`, `test_run_service`, `test_api_runs`, `test_api_routines`, `test_scheduler`, `test_seeds`, `test_cli_runs`, `test_init_project`), `README.md`

**Interfaces:**
- Consumes: `load_catalog` (Task 8).
- Produces:
  - `class ModelChoiceError(ValueError)` and its subclasses `NoModelChosen`, `UnknownModel`, `AmbiguousModel`, all mapped to HTTP 422 with the exact Global Constraints strings.
  - `class Resolved(BaseModel): runner: str; model: str; warning: str | None = None`.
  - `resolve_model(catalog, model: str | None, runner: str | None) -> Resolved`.
  - Run props `model_warning`.
  - `disable_modelless_routines(conn, *, now) -> list[str]`.
  - CLI `trajectree routine set <name> --model M [--runner R]`.

- [ ] **Step 1: Write the failing tests.**
  - `resolve_model`:
    - none → `NoModelChosen("no model was chosen")`;
    - a unique name → its runner;
    - a name listed by two runners → `AmbiguousModel` with message `model x is offered by ccx:a, ccx:b; pass --runner`;
    - unknown without a runner → `unknown model x; run: trajectree models --refresh`;
    - unknown with an explicit runner → accepted, with warning `model x is not in the catalog for codex`.
  - `test_enqueue_without_model_is_422` for each run endpoint (summarize, session summarize, ask, quick-add, report draft, routine run, import start block);
  - `test_routine_run_uses_stored_model`;
  - `test_enable_routine_without_model_is_422`;
  - `test_create_enabled_routine_without_model_is_422`;
  - `test_scheduler_skips_modelless_routine` (records `last_error` `no model was chosen`);
  - `test_startup_disables_modelless_enabled_routines` (`last_error` `disabled: no model was chosen`);
  - `test_seeds_have_no_model`;
  - `test_default_runner_setting_removed` (setting `TRAJECTREE_DEFAULT_RUNNER` has no effect);
  - `test_cli_requires_model` (exit 2, `no model was chosen; pass --model (see: trajectree models)`; no HTTP call);
  - `test_cli_routine_set`;
  - `test_cli_prints_model_warning` (`_print_run` shows `warning: <model_warning>` when the run has one);
  - `test_init_project_start_model_from_catalog` (a numbered list `1. claude/claude-opus-5-5` …; the answer is a number or a name);
  - `test_model_with_suffix_passes_verbatim` (`deepseek-flash[1m]` reaches `claude_argv` unchanged).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q`
Expected: the new tests fail; nothing else does.

- [ ] **Step 3: Implement.** `RunService.enqueue`:
  1. loads the catalog under its existing lock (a state read);
  2. for routine runs, takes the model and runner from the routine when the request has none;
  3. calls `resolve_model`;
  4. records the warning in props;
  5. creates the run with the resolved runner and model.

  Remove every fallback to a default runner. `PATCH /routines/{ref}` with `enabled: true` checks that a model is set. The README env table drops `TRAJECTREE_DEFAULT_RUNNER`, and the run sections say that `--model` is required.

- [ ] **Step 4: Run the tests and e2e**

Run: `uv run pytest -q`
Expected: all pass. e2e will fail until Task 10; do not run it here.

- [ ] **Step 5: Commit**

```bash
git add src/trajectree/ tests/ README.md
git commit -m "feat(runner): every run needs an explicit model; no default runner"
```

### Task 10: Web model picker and Models settings

**Files:**
- Create: `web/src/components/ModelPicker.tsx`, `web/src/components/ModelPicker.test.tsx`
- Modify: `web/src/api/types.ts`, `web/src/api/client.ts`, `web/src/views/TimelineView.tsx`, `web/src/views/ProjectView.tsx`, `web/src/components/SessionSummary.tsx`, `web/src/components/AskPanel.tsx`, `web/src/components/QuickCapture.tsx`, `web/src/views/ReportsView.tsx`, `web/src/components/RoutinesSection.tsx`, `web/src/views/SettingsView.tsx`, their tests, `web/e2e/smoke.spec.ts`

**Interfaces:**
- Consumes: `GET /api/models`, `POST /api/models/refresh`, `PATCH /api/routines/{ref}` with `runner`/`model` (Tasks 8 and 9).
- Produces:
  - `type ModelChoice = { runner: string; model: string }`.
  - `<ModelPicker value: ModelChoice | null, onChange, label = "Model" />`: a native `<select>` with one `<optgroup label={runner}>` per runner and its models as options. The first option is a disabled placeholder `Choose a model…`, so nothing is preselected and nothing is remembered. A runner with no models is shown as a disabled optgroup with its error.
  - `api.models()` and `api.refreshModels()`; every run-starting client call takes a `ModelChoice`.

- [ ] **Step 1: Write the failing tests** (vitest + Testing Library):
  - `ModelPicker`: renders optgroups per runner; starts at the placeholder; `onChange` receives `{runner, model}`; the select has the accessible name `Model`.
  - For each run control (the timeline stale-chip Summarize, Project tab Summarize, Summarize session, Ask, Quick capture, Draft report, routine Run now): the button is disabled until a model is picked, and the request body carries `runner` and `model`.
  - `RoutinesSection`: a `Model` picker per routine saves through `patchRoutine`; enabling a routine without a model shows the API's `no model was chosen`.
  - `SettingsView`: a `Models` heading lists runners with their models, last refresh and error; the `Refresh models` button posts and re-renders.

- [ ] **Step 2: Run them to verify they fail**

Run: `npm test` from `web/`
Expected: the new tests fail.

- [ ] **Step 3: Implement.** Each control keeps its own `ModelChoice` state, and the picker works at phone width (a full-width select under 600 px). Update `web/e2e/smoke.spec.ts`: after storing the fake OAuth token, call `POST /api/models/refresh`, then pick `claude` → `claude-fable-5-1` wherever the test starts a run.

- [ ] **Step 4: Run the checks**

Run from `web/`: `npm test`, `npm exec tsc --noEmit`, `npm run build`, `npm run e2e`.
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web/
git commit -m "feat(web): two-level model picker on every run control; Models in Settings"
```

### Task 11: Codex sessions

**Files:**
- Create: `src/trajectree/connectors/codex_sessions.py`, `tests/fixtures/codex/` (synthetic rollouts), `tests/test_codex_sessions.py`
- Modify: `src/trajectree/config.py`, `src/trajectree/ingest/job.py`, `src/trajectree/runner/kinds.py`, `src/trajectree/runner/service.py`, `src/trajectree/connectors/probe.py`, `src/trajectree/context/history.py`, `compose.yaml`, `deploy/compose.yaml`, `tests/test_deploy_template.py`, `README.md`

**Interfaces:**
- Consumes: `SessionSummary`/`LiveSession` shapes and the session-attachment gating from Task 4; `SESSION_SOURCE_OF_BACKEND`.
- Produces:
  - `Settings.codex_sessions_root: Path | None` (env `TRAJECTREE_CODEX_SESSIONS`; the default is `~/.codex/sessions` if it exists; an empty value disables it).
  - `scan_codex(conn, settings, *, now) -> ScanReport`, which emits `session` signals with `dedupe_key = f"codex_session:{id}"` and payload `backend: "codex"`.
  - `digest_codex(path, *, max_chars) -> str`.
  - `session_signal(conn, session_id) -> Signal | None`, which looks up the `claude_session:` key and then the `codex_session:` key.
  - The Compose mount `${HOME}/.codex/sessions:/host/codex/sessions:ro`, with env `TRAJECTREE_CODEX_SESSIONS: /host/codex/sessions`.

- [ ] **Step 1: Check the record layout.** Read the keys of a few real rollouts under `~/.codex/sessions`: record `type` values, payload keys, and where `timestamp`, the user prompt text and token counts live. Never copy message text. Write synthetic fixtures that reproduce that structure with invented content:
  - `rollout-…-basic.jsonl`;
  - `…-truncated.jsonl` (the last line cut in half);
  - `…-unknown.jsonl` (an extra record type `future_thing`).

- [ ] **Step 2: Write the failing tests.**
  - `test_session_from_meta`: `id`, `cwd`, `model` from `turn_context`, `t_start`/`t_end`;
  - `test_title_is_first_prompt_shortened` (80 characters with an ellipsis);
  - `test_truncated_line_is_kept_for_next_pass` (Review Focus 5: the offset stops before the partial line; appending the rest yields the full summary);
  - `test_unknown_records_ignored`;
  - `test_signal_attaches_by_cwd_and_honours_codex_choice`;
  - `test_digest_codex_caps_length`;
  - `test_summarize_codex_session` (enqueue by a codex id; the plan uses `digest_codex`);
  - `test_probe_counts_codex_sessions`;
  - `test_history_includes_codex_sessions`;
  - `test_template_mounts_codex_sessions` (both compose files; read-only).

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_codex_sessions.py tests/test_deploy_template.py -q`
Expected: the new tests fail.

- [ ] **Step 4: Implement**, mirroring `claude_sessions`:
  - incremental byte offsets in state `scan:<path>`;
  - an mtime cutoff using the earliest codex start;
  - the same live-window rule.

  It runs in the same locked phase as the Claude scan in `run_ingest`. Its files are found with `rglob("rollout-*.jsonl")` under the root.

- [ ] **Step 5: Run the suite and both compose checks**

Run: `uv run pytest -q`, `docker compose config --quiet`, and `docker compose -f deploy/compose.yaml --env-file deploy/env.example config --quiet` (with `TRAJECTREE_PROJECTS_ROOT=/tmp`).
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/trajectree/ tests/ compose.yaml deploy/compose.yaml README.md
git commit -m "feat(ingest): codex sessions as session signals, digests and import source"
```

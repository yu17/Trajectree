import asyncio, json, os, sys
from pathlib import Path
import pytest
from trajectree.auth.scopes import Scope
from trajectree.auth.tokens import resolve_token
from trajectree.models import Actor, Caps, ProjectIn, Proposal
from trajectree.repo import NotFound, UnknownProject
from trajectree.repo.caps import set_caps
from trajectree.repo.events import list_events
from trajectree.repo.projects import create_project
from trajectree.repo.runs import get_run, update_run
from trajectree.runner.confine import LandlockUnavailable
from trajectree.runner.kinds import AskRequest, QuickAddRequest, SummarizeRequest
from trajectree.runner.launch import RunOutcome
from trajectree.runner.service import RunService
from trajectree.runner.spec import AlreadyApplied, RunsPaused
import trajectree.runner.service as service_mod
from tests.test_catalog import MODEL, seed_catalog


@pytest.fixture(autouse=True)
def _catalog(ledger):
    seed_catalog(ledger)


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
    run = s.enqueue(AskRequest(model=MODEL, question="q"))
    assert run.status == "queued" and run.kind == "ask" and run.runner == "claude"
    done = s.execute(run.id)
    assert done.status == "ok" and done.est_usd == 0.0123 and (done.tokens_in, done.tokens_out, done.tokens_cache) == (100, 20, 12)
    assert done.turns == 2 and done.session_id == "fake-sess" and done.result_md == "A"
    assert [p.title for p in done.proposals] == ["Decided X", "Email Bob"]   # the bogus item was dropped
    assert done.token_id and resolve_token(ledger.conn, "trj_x") is None
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0
    log = (settings.runs_dir / f"{run.id}.log").read_text()
    assert "--mcp-config" in log and "Bearer" not in log and "fake-oauth" not in log and "trj_" not in log

def test_summarize_mints_scoped_token_with_run_actor(ledger, human, settings, logged_in, monkeypatch):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    seen = {}
    real = service_mod.mint_token
    def spy(conn, **kw):
        seen.update(kw)
        return real(conn, **kw)
    monkeypatch.setattr(service_mod, "mint_token", spy)
    s = svc(ledger, settings)
    run = s.execute(s.enqueue(SummarizeRequest(model=MODEL, project="a")).id)
    assert run.status == "ok" and run.project_id == p.id
    assert seen["scope"] == Scope(projects=[p.id], tools=["set_project_summary"], write=True)
    a = seen["actor"]
    assert (a.kind, a.ref, a.run_id, a.tool, a.backend, a.tool_version) == ("agent", run.id, run.id, "claude", "claude", "2.1.309")

def test_run_failure_revokes_token(ledger, human, settings, logged_in):
    s = svc(ledger, settings, mode="garbage")
    run = s.execute(s.enqueue(AskRequest(model=MODEL, question="q")).id)
    assert run.status == "failed" and "boom" in run.error and run.est_usd == 0 and run.tokens_in == 0
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0 and (settings.runs_dir / f"{run.id}.log").exists()

def test_budget_subtype_is_capped(ledger, human, settings, logged_in):
    run = svc(ledger, settings, mode="budget").execute(svc(ledger, settings).enqueue(AskRequest(model=MODEL, question="q")).id)
    assert run.status == "capped" and run.est_usd == 0.0123

def test_daily_cap_marks_capped_without_launch(ledger, human, settings, logged_in):
    set_caps(ledger.conn, Caps(usd_per_run=0.5, usd_per_day=1.0))
    s = svc(ledger, settings)
    first = update_run(ledger.conn, s.enqueue(AskRequest(model=MODEL, question="q")).id, status="ok", est_usd=0.6, started=s.now())
    run = s.execute(s.enqueue(AskRequest(model=MODEL, question="q")).id)
    assert run.status == "capped" and "daily cap" in run.error and run.token_id is None
    assert not (settings.runs_dir / f"{run.id}.log").exists()

def test_paused_enqueue_raises(ledger, settings):
    set_caps(ledger.conn, Caps(paused=True))
    with pytest.raises(RunsPaused):
        svc(ledger, settings).enqueue(AskRequest(model=MODEL, question="q"))

def test_missing_credentials_fails_fast(ledger, settings):
    run = svc(ledger, settings).execute(svc(ledger, settings).enqueue(AskRequest(model=MODEL, question="q")).id)
    assert run.status == "failed" and "trajectree login" in run.error and not (settings.runs_dir / f"{run.id}.log").exists()
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0

def test_invalid_runner_and_unknown_project_rejected_at_enqueue(ledger, human, settings):
    with pytest.raises(ValueError, match="invalid runner"):
        svc(ledger, settings).enqueue(AskRequest(model=MODEL, question="q", runner="evil; rm -rf"))
    with pytest.raises(UnknownProject):
        svc(ledger, settings).enqueue(SummarizeRequest(model=MODEL, project="zzz"))

@pytest.mark.anyio
async def test_worker_continues_after_timeout(ledger, human, settings, logged_in, tmp_path):
    set_caps(ledger.conn, Caps(timeout_s=1))
    mode = settings.claude_config_dir / "mode"   # inside what a confined claude run may read
    mode.write_text("sleep")
    s = svc(ledger, settings, FAKE_CLAUDE_MODE_FILE=str(mode))
    real_launch = s.launch

    def launch_then_unstick(*a, **kw):
        outcome = real_launch(*a, **kw)
        mode.write_text("ok")   # flipped before the worker can start the next run
        return outcome

    s.launch = launch_then_unstick
    r1 = s.enqueue(AskRequest(model=MODEL, question="slow"))
    r2 = s.enqueue(AskRequest(model=MODEL, question="fast"))
    stop = asyncio.Event()
    task = asyncio.create_task(s.worker(stop, poll_s=0.05))
    for _ in range(200):
        await asyncio.sleep(0.05)
        if get_run(ledger.conn, r2.id).status == "ok":
            break
    stop.set()
    await asyncio.wait_for(task, 5)
    assert get_run(ledger.conn, r1.id).status == "failed" and get_run(ledger.conn, r1.id).error == "timeout after 1s"
    assert get_run(ledger.conn, r2.id).status == "ok", get_run(ledger.conn, r2.id).error

def test_accept_proposal_creates_human_txn_with_provenance(ledger, human, settings, logged_in):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = svc(ledger, settings)
    run = s.execute(s.enqueue(QuickAddRequest(model=MODEL, text="x")).id)
    web = Actor(kind="human", ref="owner", tool="web")
    after = s.apply_proposal(run.id, 0, actor=web)
    ev = list_events(ledger.conn, project_ids=[p.id])[0]
    assert ev.title == "Decided X" and ev.kind == "decision" and ev.source == "agent" and after.proposals[0].applied_id == ev.id
    t = ledger.get_txn(after.proposals[0].applied_txn)
    assert (t.actor_kind, t.proposed_by_run, t.model, t.backend, t.review, t.intent, t.tool) == ("human", run.id, MODEL, "claude", "accepted", "decision taken", "web")
    after = s.apply_proposal(run.id, 1, actor=web)
    assert after.proposals[1].applied_txn is not None and ledger.get_txn(after.proposals[1].applied_txn).intent == "follow-up owed"

def test_accept_proposal_twice_is_409(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = svc(ledger, settings)
    run = s.execute(s.enqueue(QuickAddRequest(model=MODEL, text="x")).id)
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
    run = s.execute(s.enqueue(QuickAddRequest(model=MODEL, text="x")).id)
    n = len(ledger.list_txns())
    with pytest.raises(UnknownProject, match="known projects: b"):
        s.apply_proposal(run.id, 0, actor=human)
    assert len(ledger.list_txns()) == n


def test_execute_on_finished_run_is_a_noop(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = svc(ledger, settings)
    done = s.execute(s.enqueue(AskRequest(model=MODEL, question="q")).id)
    log = settings.runs_dir / f"{done.id}.log"
    log.unlink()
    again = s.execute(done.id)
    assert again == done and not log.exists()
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0


def test_version_probe_runs_without_the_ledger_lock(ledger, human, settings, logged_in):
    owned = []
    def version(binary):
        owned.append(ledger._lock._is_owned())
        return "2.1.309"
    env = {"PATH": os.environ["PATH"], "FAKE_CLAUDE_MODE": "ok", "FAKE_CLAUDE_STRUCTURED": STRUCTURED}
    s = RunService(ledger, settings, version=version, base_env=env)
    assert s.execute(s.enqueue(AskRequest(model=MODEL, question="q")).id).status == "ok"
    assert owned == [False]


def test_launch_exception_marks_failed_and_revokes_token(ledger, human, settings, logged_in):
    def boom(*a, **kw):
        raise RuntimeError("launch exploded")
    s = RunService(ledger, settings, launch=boom, base_env={"PATH": os.environ["PATH"]})
    run = s.enqueue(AskRequest(model=MODEL, question="q"))
    with pytest.raises(RuntimeError):
        s.execute(run.id)
    after = get_run(ledger.conn, run.id)
    assert after.status == "failed" and after.error == "launch exploded" and after.finished is not None
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0


def test_execute_keeps_token_and_prompt_off_argv_and_removes_mcp_config(ledger, human, settings, logged_in, tmp_path, monkeypatch):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    # The confined fake-claude may write only to its own config dir (and the run's dirs).
    argv_file, prompt_file = settings.claude_config_dir / "argv.json", settings.claude_config_dir / "prompt.txt"
    seen = {}
    real = service_mod.mint_token
    def spy(conn, **kw):
        token_id, secret = real(conn, **kw)
        seen["secret"] = secret
        return token_id, secret
    monkeypatch.setattr(service_mod, "mint_token", spy)
    real_launch = service_mod.launch
    def launch_and_look(argv, **kw):
        cfg_path = argv[argv.index("--mcp-config") + 1]
        seen["mode"] = os.stat(cfg_path).st_mode & 0o777
        seen["cfg"] = json.loads(open(cfg_path).read())
        return real_launch(argv, **kw)
    s = svc(ledger, settings, FAKE_CLAUDE_ARGV_FILE=str(argv_file), FAKE_CLAUDE_PROMPT_FILE=str(prompt_file))
    s.launch = launch_and_look
    run = s.execute(s.enqueue(AskRequest(model=MODEL, question="What is blocked?")).id)
    assert run.status == "ok", run.error
    argv = json.loads(argv_file.read_text())
    secret = seen["secret"]
    assert not any(secret in a for a in argv) and not any("What is blocked?" in a for a in argv)
    assert "What is blocked?" in prompt_file.read_text()
    assert seen["mode"] == 0o600 and seen["cfg"]["mcpServers"]["trajectree"]["headers"]["Authorization"] == f"Bearer {secret}"
    log = (settings.runs_dir / f"{run.id}.log").read_text()
    assert secret not in log and "fake-oauth" not in log
    cfg_path = settings.runs_dir / run.id / "mcp.json"
    assert argv[argv.index("--mcp-config") + 1] == str(cfg_path) and not cfg_path.exists()


def test_mcp_config_removed_when_launch_raises(ledger, human, settings, logged_in):
    def boom(argv, **kw):
        assert os.path.exists(argv[argv.index("--mcp-config") + 1])
        raise RuntimeError("launch exploded")
    s = RunService(ledger, settings, launch=boom, base_env={"PATH": os.environ["PATH"]})
    run = s.enqueue(AskRequest(model=MODEL, question="q"))
    with pytest.raises(RuntimeError):
        s.execute(run.id)
    assert not (settings.runs_dir / run.id / "mcp.json").exists()


def test_unparseable_stdout_error_is_masked(ledger, human, settings, logged_in):
    from trajectree.runner.launch import RunOutcome
    def leaky(argv, *, secrets, log_path, **kw):
        log_path.write_text("")
        return RunOutcome(exit_code=1, timed_out=False, stdout=f"oops {secrets[0]} and {secrets[1]}", stderr_tail="",
                          log_path=str(log_path), duration_ms=1)
    s = RunService(ledger, settings, launch=leaky, base_env={"PATH": os.environ["PATH"]})
    run = s.execute(s.enqueue(AskRequest(model=MODEL, question="q")).id)
    assert run.status == "failed" and run.error == "oops *** and ***"


def test_launch_argv_is_confined(ledger, human, settings, logged_in):
    argv_file = settings.claude_config_dir / "argv.json"
    seen = {}
    real_launch = service_mod.launch
    def spy(argv, **kw):
        seen["argv"] = argv
        return real_launch(argv, **kw)
    s = svc(ledger, settings, FAKE_CLAUDE_ARGV_FILE=str(argv_file))
    s.launch = spy
    run = s.execute(s.enqueue(AskRequest(model=MODEL, question="q")).id)
    assert run.status == "ok", run.error
    argv = seen["argv"]
    assert argv[:4] == [sys.executable, "-I", "-m", "trajectree.runner.confine"]
    runner_argv = argv[argv.index("--") + 1:]
    assert runner_argv[:2] == [settings.claude_bin, "-p"] and "--mcp-config" in runner_argv
    assert json.loads(argv_file.read_text()) == runner_argv[1:]   # what the confined fake-claude received


def test_confined_run_cannot_write_data_dir(ledger, human, settings, logged_in):
    leak = settings.data_dir / "leak.json"   # outside the run's own directory
    run = svc(ledger, settings, FAKE_CLAUDE_ARGV_FILE=str(leak)).execute(
        svc(ledger, settings).enqueue(AskRequest(model=MODEL, question="q")).id)
    assert run.status == "failed" and not leak.exists()
    log = (settings.runs_dir / f"{run.id}.log").read_text()
    assert "Permission denied" in log


def test_env_has_private_tmp_and_home(ledger, human, settings, logged_in):
    seen = {}
    real_launch = service_mod.launch
    def spy(argv, *, env, **kw):
        tmp = Path(env["TMPDIR"])
        seen.update(env=env, tmp_mode=tmp.stat().st_mode & 0o777, home_is_dir=Path(env["HOME"]).is_dir())
        return real_launch(argv, env=env, **kw)
    s = svc(ledger, settings)
    s.launch = spy
    run = s.execute(s.enqueue(AskRequest(model=MODEL, question="q")).id)
    assert run.status == "ok", run.error
    env = seen["env"]
    assert env["HOME"] == str(settings.runs_dir / run.id / "home") and seen["home_is_dir"]
    assert seen["tmp_mode"] == 0o700 and run.id in Path(env["TMPDIR"]).name
    assert not Path(env["TMPDIR"]).exists()   # removed after the run


def test_landlock_unavailable_fails_closed(ledger, human, settings, logged_in):
    def no_landlock():
        raise LandlockUnavailable(38, "landlock abi: Function not implemented")
    def never(*a, **kw):
        raise AssertionError("launch must not be called")
    s = RunService(ledger, settings, launch=never, landlock_abi=no_landlock, base_env={"PATH": os.environ["PATH"]})
    run = s.execute(s.enqueue(AskRequest(model=MODEL, question="q")).id)
    assert run.status == "failed" and run.props["failure"] == "infra"
    assert run.error.startswith("landlock unavailable") and "Function not implemented" in run.error
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0


def _wrapper_outcome(exit_code, stderr, seen):
    def fake(argv, *, env, log_path, **kw):
        seen["tmp"] = Path(env["TMPDIR"])
        log_path.write_text("")
        return RunOutcome(exit_code=exit_code, timed_out=False, stdout="", stderr_tail=stderr,
                          log_path=str(log_path), duration_ms=1)
    return fake


def test_wrapper_exit_97_is_infra(ledger, human, settings, logged_in):
    seen = {}
    fake = _wrapper_outcome(97, "trajectree-confine: landlock unavailable: x\n", seen)
    s = RunService(ledger, settings, launch=fake, base_env={"PATH": os.environ["PATH"]})
    run = s.execute(s.enqueue(AskRequest(model=MODEL, question="q")).id)
    assert run.status == "failed" and run.props["failure"] == "infra"
    assert run.error == "trajectree-confine: landlock unavailable: x"
    assert not seen["tmp"].exists()   # removed when the run fails too


def test_wrapper_exit_127_cannot_run_is_infra(ledger, human, settings, logged_in):
    fake = _wrapper_outcome(127, "trajectree-confine: cannot run /opt/claude: No such file or directory\n", {})
    s = RunService(ledger, settings, launch=fake, base_env={"PATH": os.environ["PATH"]})
    run = s.execute(s.enqueue(AskRequest(model=MODEL, question="q")).id)
    assert run.status == "failed" and run.props["failure"] == "infra"
    assert run.error == "trajectree-confine: cannot run /opt/claude: No such file or directory"


def test_abi_recorded(ledger, human, settings, logged_in):
    s = RunService(ledger, settings, landlock_abi=lambda: 7,
                   base_env={"PATH": os.environ["PATH"], "FAKE_CLAUDE_MODE": "ok", "FAKE_CLAUDE_STRUCTURED": STRUCTURED})
    run = s.execute(s.enqueue(AskRequest(model=MODEL, question="q")).id)
    assert run.status == "ok", run.error
    assert run.props["landlock_abi"] == 7 and "request" in run.props


@pytest.mark.anyio
async def test_pause_holds_queued_runs_until_resumed(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = svc(ledger, settings)
    run = s.enqueue(AskRequest(model=MODEL, question="q"))
    set_caps(ledger.conn, Caps(paused=True))
    polls = []
    real_next = s._next_queued
    def counting_next(*a):
        polls.append(1)
        return real_next(*a)
    s._next_queued = counting_next
    stop = asyncio.Event()
    task = asyncio.create_task(s.worker(stop, poll_s=0.02))
    await asyncio.sleep(0.3)
    assert get_run(ledger.conn, run.id).status == "queued" and not (settings.runs_dir / f"{run.id}.log").exists()
    assert polls == []   # paused: the worker does not even pick a run
    set_caps(ledger.conn, Caps(paused=False))
    for _ in range(200):
        await asyncio.sleep(0.05)
        if get_run(ledger.conn, run.id).status == "ok":
            break
    stop.set()
    await asyncio.wait_for(task, 5)
    assert get_run(ledger.conn, run.id).status == "ok"


def test_recover_fails_orphaned_running_runs(ledger, human, settings):
    from trajectree.auth.tokens import mint_token
    s = svc(ledger, settings)
    orphan = s.enqueue(AskRequest(model=MODEL, question="q"))
    queued = s.enqueue(AskRequest(model=MODEL, question="q2"))
    token_id, _ = mint_token(ledger.conn, label=f"run:{orphan.id}", scope=Scope(write=False),
                             actor=Actor(kind="agent", ref=orphan.id), ttl=None)
    update_run(ledger.conn, orphan.id, status="running", started=s.now(), token_id=token_id)
    leftover = settings.runs_dir / orphan.id / "mcp.json"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("{}")
    ghost = s.enqueue(AskRequest(model=MODEL, question="q3"))
    update_run(ledger.conn, ghost.id, status="running", started=s.now(), token_id="tok-gone")
    assert s.recover() == 2
    after = get_run(ledger.conn, orphan.id)
    assert after.status == "failed" and after.error == "daemon restarted" and after.finished is not None
    assert get_run(ledger.conn, ghost.id).status == "failed"
    assert get_run(ledger.conn, queued.id).status == "queued"
    assert ledger.conn.execute("select count(*) from token where id = ?", (token_id,)).fetchone()[0] == 0
    assert not leftover.exists()
    assert s.recover() == 0


def test_summarize_enqueue_is_deduplicated_while_active(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    create_project(ledger, human, ProjectIn(slug="b", title="B", kind="research"))
    s = svc(ledger, settings)
    first = s.enqueue(SummarizeRequest(model=MODEL, project="a"))
    assert s.enqueue(SummarizeRequest(model=MODEL, project="a")).id == first.id
    assert s.enqueue(SummarizeRequest(model=MODEL, project="b")).id != first.id
    update_run(ledger.conn, first.id, status="running")
    assert s.enqueue(SummarizeRequest(model=MODEL, project="a")).id == first.id
    update_run(ledger.conn, first.id, status="ok")
    assert s.enqueue(SummarizeRequest(model=MODEL, project="a")).id != first.id


def _run_with(ledger, settings, proposals):
    s = svc(ledger, settings, FAKE_CLAUDE_STRUCTURED=json.dumps({"answer": "A", "proposals": proposals}))
    return s, s.execute(s.enqueue(QuickAddRequest(model=MODEL, text="x")).id)


def test_worker_mark_failed_keeps_abort_error(ledger, human, settings, logged_in):
    def boom(argv, **kw):
        cfg = json.loads(open(argv[argv.index("--mcp-config") + 1]).read())
        token = cfg["mcpServers"]["trajectree"]["headers"]["Authorization"].split()[1]
        raise RuntimeError(f"boom {token}")
    s = RunService(ledger, settings, launch=boom, base_env={"PATH": os.environ["PATH"]})
    run = s.enqueue(AskRequest(model=MODEL, question="q"))
    with pytest.raises(RuntimeError) as info:
        s.execute(run.id)
    s._mark_failed(run.id, str(info.value))   # what the worker does with the unmasked text
    stored = get_run(ledger.conn, run.id)
    assert "trj_" not in (stored.error or "") and stored.props["failure"] == "infra" and "request" in stored.props


def test_failure_flag_is_set_for_infra_only(ledger, human, settings, logged_in):
    s = svc(ledger, settings)
    run = s.enqueue(AskRequest(model=MODEL, question="q"))
    with ledger.locked() as conn:
        s._fail(conn, run.id, "no")
    assert "failure" not in get_run(ledger.conn, run.id).props
    other = s.enqueue(AskRequest(model=MODEL, question="q"))
    update_run(ledger.conn, other.id, status="running")
    s.recover()
    assert get_run(ledger.conn, other.id).props["failure"] == "infra"


def test_dismissed_proposal_409_text(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s, run = _run_with(ledger, settings, [{"kind": "todo", "project": "a", "title": "T1", "reason": "r"}])
    s.dismiss_proposal(run.id, 0)
    with pytest.raises(AlreadyApplied, match="was dismissed"):
        s.apply_proposal(run.id, 0, actor=human)


def test_lenient_proposal_dates_apply_and_unparseable_ones_are_dropped(ledger, human, settings, logged_in):
    from trajectree.repo.todos import list_todos
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s, run = _run_with(ledger, settings, [
        {"kind": "event", "project": "a", "title": "Talk", "reason": "r", "when": "2026-10-24"},
        {"kind": "todo", "project": "a", "title": "Slides", "reason": "r", "due": "2026-10-24T09:00:00"},
        {"kind": "todo", "project": "a", "title": "Vague", "reason": "r", "due": "Friday"},
        {"kind": "event", "project": "a", "title": "Offset", "reason": "r", "when": "2026-10-24T09:00:00+02:00"}])
    assert [p.title for p in run.proposals] == ["Talk", "Slides", "Offset"]
    assert [(p.when, p.due) for p in run.proposals] == [("2026-10-24T00:00:00Z", None), (None, "2026-10-24T09:00:00Z"),
                                                        ("2026-10-24T07:00:00Z", None)]
    s.apply_proposal(run.id, 0, actor=human)
    s.apply_proposal(run.id, 1, actor=human)
    ev = list_events(ledger.conn, project_ids=[p.id])[0]
    assert ev.t_start.isoformat() == "2026-10-24T00:00:00+00:00"
    todo = [t for t in list_todos(ledger.conn, project_ids=[p.id]) if t.title == "Slides"][0]
    assert todo.due.isoformat() == "2026-10-24T09:00:00+00:00"


def test_apply_proposal_tolerates_stored_naive_and_date_only_values(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s, run = _run_with(ledger, settings, [])
    update_run(ledger.conn, run.id, proposals=[
        Proposal(kind="todo", project="a", title="T", reason="r", due="2026-10-24"),
        Proposal(kind="event", project="a", title="E", reason="r", when="2026-10-24T09:00:00")])
    assert s.apply_proposal(run.id, 0, actor=human).proposals[0].applied_txn
    assert s.apply_proposal(run.id, 1, actor=human).proposals[1].applied_txn


def test_todo_applied_txn_is_the_txn_that_inserted_it(ledger, human, settings, logged_in, monkeypatch):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s, run = _run_with(ledger, settings, [{"kind": "todo", "project": "a", "title": "Email Bob", "reason": "r"}])
    real = service_mod.todos.create_todo
    def create_then_other_txn(ledger_, actor, data, **kw):
        todo = real(ledger_, actor, data, **kw)
        create_project(ledger_, human, ProjectIn(slug="later", title="Later", kind="admin"))   # a newer txn
        return todo
    monkeypatch.setattr(service_mod.todos, "create_todo", create_then_other_txn)
    after = s.apply_proposal(run.id, 0, actor=human)
    applied = after.proposals[0]
    changes = ledger.changes_for(applied.applied_txn)
    assert any(c.table_name == "todo" and c.op == "INSERT" and c.pk == [applied.applied_id] for c in changes)


def test_dismiss_proposal(ledger, human, settings, logged_in):
    from trajectree.repo.runs import list_runs
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s, run = _run_with(ledger, settings, [{"kind": "todo", "project": "a", "title": "T1", "reason": "r"},
                                          {"kind": "todo", "project": "a", "title": "T2", "reason": "r", "dismissed": True, "applied_txn": "forged"}])
    assert [(p.dismissed, p.applied_txn) for p in run.proposals] == [(False, None), (False, None)]   # agent cannot preset these
    assert [r.id for r in list_runs(ledger.conn, with_open_proposals=True)] == [run.id]
    n = len(ledger.list_txns())
    after = s.dismiss_proposal(run.id, 0)
    assert after.proposals[0].dismissed and not after.proposals[1].dismissed and len(ledger.list_txns()) == n
    with pytest.raises(AlreadyApplied):
        s.dismiss_proposal(run.id, 0)
    with pytest.raises(AlreadyApplied):
        s.apply_proposal(run.id, 0, actor=human)
    with pytest.raises(NotFound):
        s.dismiss_proposal(run.id, 9)
    with pytest.raises(NotFound):
        s.dismiss_proposal("nope", 0)
    assert [r.id for r in list_runs(ledger.conn, with_open_proposals=True)] == [run.id]   # T2 still open
    s.apply_proposal(run.id, 1, actor=human)
    with pytest.raises(AlreadyApplied):
        s.dismiss_proposal(run.id, 1)
    assert list_runs(ledger.conn, with_open_proposals=True) == []


# -- routines ------------------------------------------------------------------------------

from datetime import datetime, timezone
from trajectree.models import RoutineCaps, RoutineIn, RoutineScope
from trajectree.repo.routines import create_routine, get_routine
from trajectree.repo.runs import create_run, oldest_queued
from trajectree.runner.kinds import RoutineRequest

R_NOW = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)


def _routine(ledger, **over):
    data = {"name": "rev", "trigger": "@daily", "prompt_ref": "cross_project_review.md", "model": MODEL, **over}
    return create_routine(ledger.conn, RoutineIn(**data), now=R_NOW)


def test_enqueue_routine_lands_in_scheduled_slot(ledger, human, settings):
    r = _routine(ledger)
    s = svc(ledger, settings)
    run = s.enqueue(RoutineRequest(routine="rev"))
    assert (run.slot, run.kind, run.routine_id, run.attempt) == ("scheduled", "routine", r.id, 1)
    assert oldest_queued(ledger.conn, slot="interactive") is None
    assert run.props["request"]["routine"] == "rev" and run.project_id is None
    assert s.enqueue(AskRequest(model=MODEL, question="q")).slot == "interactive"
    with pytest.raises(NotFound):
        s.enqueue(RoutineRequest(routine="nope"))
    with pytest.raises(UnknownProject):
        s.enqueue(RoutineRequest(routine="rev", projects=["zzz"]))


def test_enqueue_routine_runner_and_model_fall_back_to_routine(ledger, human, settings):
    _routine(ledger, runner="ccx:kimi", model="k2")
    s = svc(ledger, settings)
    run = s.enqueue(RoutineRequest(routine="rev", attempt=2))
    assert (run.runner, run.backend, run.model, run.attempt) == ("ccx:kimi", "kimi", "k2", 2)
    run = s.enqueue(RoutineRequest(routine="rev", runner="claude", model="m"))
    assert (run.runner, run.model) == ("claude", "m")


def test_routine_daily_cap_marks_capped_and_pauses_routine(ledger, human, settings, logged_in):
    r = _routine(ledger, caps=RoutineCaps(usd_per_run=0.01, usd_per_day=0.02))
    s = svc(ledger, settings)
    prior = s.enqueue(RoutineRequest(routine="rev"))
    update_run(ledger.conn, prior.id, status="ok", est_usd=0.0199, started=s.now())
    run = s.execute(s.enqueue(RoutineRequest(routine="rev")).id)
    assert run.status == "capped" and "routine daily cap 0.02 USD reached" in run.error and run.token_id is None
    assert not (settings.runs_dir / f"{run.id}.log").exists()
    after = get_routine(ledger.conn, r.id)
    assert after.paused_until > s.now() and after.paused_until.hour == 0 and after.paused_until.minute == 0
    assert after.last_run_id == run.id and after.last_error == run.error


def test_routine_run_cap_is_the_smaller_of_routine_and_global(ledger, human, settings, logged_in, monkeypatch):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    _routine(ledger, caps=RoutineCaps(usd_per_run=0.25, usd_per_day=2.0))
    seen = []
    real = service_mod.run_env
    def spy(spec, *a, **kw):
        seen.append(spec.usd_cap)
        return real(spec, *a, **kw)
    monkeypatch.setattr(service_mod, "run_env", spy)
    s = svc(ledger, settings)
    s.execute(s.enqueue(RoutineRequest(routine="rev")).id)
    set_caps(ledger.conn, Caps(usd_per_run=0.1, usd_per_day=5.0))
    s.execute(s.enqueue(RoutineRequest(routine="rev")).id)
    s.execute(s.enqueue(AskRequest(model=MODEL, question="q")).id)
    assert seen == [0.25, 0.1, 0.1]


def test_routine_ok_run_delivers_to_inbox_and_updates_routine(ledger, human, settings, logged_in, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "Agenda")
    monkeypatch.delenv("FAKE_CLAUDE_STRUCTURED", raising=False)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    r = _routine(ledger, delivery="inbox", scope=RoutineScope(projects=["a"]))
    s = RunService(ledger, settings)
    run = s.execute(s.enqueue(RoutineRequest(routine="rev")).id)
    assert run.status == "ok" and run.result_md == "Agenda", run.error
    notes = list_events(ledger.conn, project_ids=[p.id], kinds=["note"])
    assert [(e.title, e.body_md, e.source) for e in notes] == [(f"rev · {s.today()}", "Agenda", "agent")]
    txn = ledger.get_txn(notes[0].created_txn)
    assert (txn.review, txn.actor_kind, txn.run_id, txn.intent) == ("pending", "agent", run.id, "deliver rev result")
    assert run.props["delivery"]["ok"] is True and run.props["delivery"]["event_id"] == notes[0].id
    after = get_routine(ledger.conn, r.id)
    assert after.last_run_id == run.id and after.last_error is None and after.last_fired == run.started


def test_routine_failed_run_records_last_error_without_delivery(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    r = _routine(ledger, delivery="inbox", scope=RoutineScope(projects=["a"]))
    s = svc(ledger, settings, mode="garbage")
    run = s.execute(s.enqueue(RoutineRequest(routine="rev")).id)
    assert run.status == "failed" and "delivery" not in run.props
    assert list_events(ledger.conn, kinds=["note"]) == []
    after = get_routine(ledger.conn, r.id)
    assert after.last_run_id == run.id and after.last_error == run.error


@pytest.mark.anyio
async def test_interactive_worker_ignores_scheduled_runs(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    _routine(ledger)
    s = svc(ledger, settings)
    routine_run = s.enqueue(RoutineRequest(routine="rev"))
    ask = s.enqueue(AskRequest(model=MODEL, question="q"))
    stop = asyncio.Event()
    task = asyncio.create_task(s.worker(stop, poll_s=0.02))
    for _ in range(200):
        await asyncio.sleep(0.05)
        if get_run(ledger.conn, ask.id).status == "ok":
            break
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert get_run(ledger.conn, ask.id).status == "ok"
    assert get_run(ledger.conn, routine_run.id).status == "queued"
    stop = asyncio.Event()
    task = asyncio.create_task(s.worker(stop, slot="scheduled", poll_s=0.02))
    for _ in range(200):
        await asyncio.sleep(0.05)
        if get_run(ledger.conn, routine_run.id).status == "ok":
            break
    stop.set()
    await asyncio.wait_for(task, 5)
    assert get_run(ledger.conn, routine_run.id).status == "ok"


@pytest.mark.anyio
async def test_worker_crash_is_logged_without_exception_text(ledger, human, settings, logged_in, caplog):
    def boom(*a, **kw):
        raise RuntimeError("boom trj_secret")
    s = RunService(ledger, settings, launch=boom, base_env={"PATH": os.environ["PATH"]})
    run = s.enqueue(AskRequest(model=MODEL, question="q"))
    stop = asyncio.Event()
    task = asyncio.create_task(s.worker(stop, poll_s=0.02))
    for _ in range(200):
        await asyncio.sleep(0.05)
        if get_run(ledger.conn, run.id).status == "failed":
            break
    stop.set()
    await asyncio.wait_for(task, 5)
    assert get_run(ledger.conn, run.id).status == "failed"
    assert "trj_secret" not in caplog.text and "RuntimeError" in caplog.text and "Traceback" not in caplog.text


def test_mark_failed_is_an_infra_failure(ledger, human, settings):
    s = svc(ledger, settings)
    run = s.enqueue(AskRequest(model=MODEL, question="q"))
    update_run(ledger.conn, run.id, status="running")
    s._mark_failed(run.id, "planner crashed")
    after = get_run(ledger.conn, run.id)
    assert after.status == "failed" and after.props["failure"] == "infra"


def test_infra_failure_of_routine_run_schedules_retry(ledger, human, settings):
    from datetime import timedelta
    from trajectree.timeutil import to_db
    _routine(ledger)
    s = RunService(ledger, settings, now=lambda: R_NOW)
    runs = [s.enqueue(RoutineRequest(routine="rev", attempt=n)) for n in (1, 2, 3)]
    with ledger.locked() as conn:
        failed = [s._fail(conn, r.id, "timeout after 1s", infra=True) for r in runs]
    assert [f.props.get("retry_at") for f in failed] == [
        to_db(R_NOW + timedelta(seconds=60)), to_db(R_NOW + timedelta(seconds=300)), None]
    assert all(f.props["failure"] == "infra" for f in failed)


def test_routine_delivery_runs_without_the_ledger_lock(ledger, human, settings, logged_in, monkeypatch):
    import threading
    import trajectree.runner.routines as routines_mod
    from trajectree.delivery import DeliveryResult
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "Agenda")
    monkeypatch.delenv("FAKE_CLAUDE_STRUCTURED", raising=False)
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    _routine(ledger, delivery="log")
    free = []
    def probe(*a, **kw):
        t = threading.Thread(target=lambda: ledger._lock.acquire(timeout=2) and (free.append(1), ledger._lock.release()))
        t.start()
        t.join(5)
        return DeliveryResult(channel="log", ok=True)
    monkeypatch.setattr(routines_mod, "deliver", probe)
    s = RunService(ledger, settings)
    run = s.execute(s.enqueue(RoutineRequest(routine="rev")).id)
    assert run.status == "ok" and run.props["delivery"] == {"channel": "log", "ok": True, "detail": None, "event_id": None}
    assert free == [1]   # another thread could take the lock while delivering
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0
    assert get_routine(ledger.conn, run.routine_id).last_run_id == run.id


def test_routine_delivery_title_uses_routine_tz(ledger, human, settings, logged_in, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT", "Agenda")
    monkeypatch.delenv("FAKE_CLAUDE_STRUCTURED", raising=False)
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    _routine(ledger, delivery="inbox", tz="Pacific/Kiritimati", scope=RoutineScope(projects=["a"]))
    s = RunService(ledger, settings, now=lambda: R_NOW)   # 12:00 UTC is 02:00 the next day at UTC+14
    run = s.execute(s.enqueue(RoutineRequest(routine="rev")).id)
    assert run.status == "ok", run.error
    assert [e.title for e in list_events(ledger.conn, project_ids=[p.id], kinds=["note"])] == ["rev · 2026-01-11"]


def test_routine_without_projects_fails_without_a_token(ledger, human, settings, logged_in):
    _routine(ledger)
    s = svc(ledger, settings)
    run = s.execute(s.enqueue(RoutineRequest(routine="rev")).id)
    assert run.status == "failed" and run.error == "no projects in scope" and run.token_id is None
    assert "failure" not in run.props


@pytest.mark.anyio
async def test_worker_crash_stores_masked_error(ledger, human, settings):
    s = svc(ledger, settings)
    run = s.enqueue(AskRequest(model=MODEL, question="q"))
    def crash(run_id):
        raise RuntimeError("planner died holding trj_abc-DEF_123 and more")
    s.execute = crash
    stop = asyncio.Event()
    task = asyncio.create_task(s.worker(stop, poll_s=0.02))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if get_run(ledger.conn, run.id).status == "failed":
            break
    stop.set()
    await asyncio.wait_for(task, 5)
    assert get_run(ledger.conn, run.id).error == "RuntimeError: planner died holding *** and more"


import threading
from datetime import date
from trajectree.context.history import HistoryInput
from trajectree.models import ResourceIn
from trajectree.repo.resources import upsert_resource
from trajectree.runner.kinds import StartingPointRequest


def _lock_free(ledger):
    """True when another thread can take the ledger lock right now."""
    got = []
    def probe():
        ok = ledger._lock.acquire(blocking=False)
        got.append(ok)
        if ok:
            ledger._lock.release()
    t = threading.Thread(target=probe)
    t.start(); t.join()
    return got[0]


def test_history_collected_outside_lock(ledger, human, settings, logged_in, monkeypatch, tmp_path):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    folder = tmp_path / "proj"
    folder.mkdir()
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=str(folder)))
    seen = {}
    def stub(settings_, *, folders, starts, tz, budget_chars):
        seen.update(lock_free=_lock_free(ledger), folders=folders, starts=starts)
        return HistoryInput(git_md="- 2024-06-10 abc12345 first commit")
    monkeypatch.setattr(service_mod, "collect_history", stub)
    s = svc(ledger, settings)
    run = s.enqueue(StartingPointRequest(model=MODEL, project="a", starts={"git": date(2025, 1, 1)}))
    assert run.kind == "starting_point" and run.slot == "interactive" and run.project_id == p.id
    done = s.execute(run.id)
    assert seen["lock_free"] is True and seen["folders"] == [folder] and seen["starts"] == {"git": date(2025, 1, 1)}
    assert done.status == "ok"


def test_no_history_fails_cleanly(ledger, human, settings, logged_in, monkeypatch):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    monkeypatch.setattr(service_mod, "collect_history", lambda *a, **k: HistoryInput())
    s = svc(ledger, settings)
    run = s.execute(s.enqueue(StartingPointRequest(model=MODEL, project="a", starts={"git": date(2025, 1, 1)})).id)
    assert run.status == "failed" and run.error == "no history before the chosen start"
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0


def test_session_history_collected_outside_lock(ledger, human, settings, logged_in, monkeypatch, tmp_path):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator="/work/a"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator="/work/b"))
    seen = {}
    def stub(settings_, folders, registered, *, starts, budget_chars):
        seen.update(lock_free=_lock_free(ledger), folders=folders, registered=registered)
        return "### 2024-06-01 Old session (2 prompts)\nbody"
    monkeypatch.setattr(service_mod, "session_history", stub)
    monkeypatch.setattr(service_mod, "collect_history", lambda *a, **k: HistoryInput())
    s = svc(ledger, settings)
    done = s.execute(s.enqueue(StartingPointRequest(model=MODEL, project="a", starts={"claude": date(2025, 1, 1)})).id)
    assert seen["lock_free"] is True and seen["folders"] == ["/work/a", "/work/b"] == seen["registered"]
    assert done.status == "ok"  # the sessions alone are enough history to run


def test_starting_point_runs_when_version_probe_is_none(ledger, human, settings, logged_in, monkeypatch):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    monkeypatch.setattr(service_mod, "collect_history", lambda *a, **k: HistoryInput(git_md="- 2024-06-10 abc12345 x"))
    env = {"PATH": os.environ["PATH"], "FAKE_CLAUDE_MODE": "ok", "FAKE_CLAUDE_STRUCTURED": STRUCTURED}
    s = RunService(ledger, settings, base_env=env, version=lambda binary: None)
    done = s.execute(s.enqueue(StartingPointRequest(model=MODEL, project="a", starts={"git": date(2025, 1, 1)})).id)
    assert done.status == "ok"


# -- model choice ------------------------------------------------------------------------------

from trajectree.runner.catalog import NoModelChosen, UnknownModel


def test_enqueue_without_model_is_refused(ledger, human, settings):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    _routine(ledger, model=None)
    s = svc(ledger, settings)
    for request in (AskRequest(question="q"), SummarizeRequest(project="a"),
                    QuickAddRequest(text="t"), RoutineRequest(routine="rev"),
                    StartingPointRequest(project="a", starts={"git": date(2025, 1, 1)})):
        with pytest.raises(NoModelChosen, match="^no model or runner was chosen$"):
            s.enqueue(request)
    with pytest.raises(UnknownModel):
        s.check(AskRequest(question="q", model="nope"))
    assert ledger.conn.execute("select count(*) from run").fetchone()[0] == 0


def test_enqueue_resolves_the_runner_and_records_a_warning(ledger, settings):
    s = svc(ledger, settings)
    run = s.enqueue(AskRequest(question="q", model="gpt-6.1-sol"))
    assert (run.runner, run.backend, run.model) == ("codex", "codex", "gpt-6.1-sol") and "model_warning" not in run.props
    run = s.enqueue(AskRequest(question="q", runner="codex", model="gpt-7"))
    assert (run.runner, run.model) == ("codex", "gpt-7")
    assert run.props["model_warning"] == "model gpt-7 is not in the catalog for codex"


def test_routine_run_uses_stored_model(ledger, settings):
    _routine(ledger, model="gpt-6.1-sol")
    s = svc(ledger, settings)
    run = s.enqueue(RoutineRequest(routine="rev"))
    assert (run.runner, run.model) == ("codex", "gpt-6.1-sol")
    run = s.enqueue(RoutineRequest(routine="rev", model="k2"))  # a model given with the request wins
    assert (run.runner, run.model) == ("ccx:kimi", "k2")


def test_model_with_suffix_passes_verbatim(ledger, settings):
    profile = settings.backends_cfg_dir / "ds"
    profile.mkdir(parents=True)
    (profile / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "k"}}))
    seen = {}
    def capture(argv, *, log_path, **kw):
        seen["argv"] = argv
        return RunOutcome(exit_code=0, timed_out=False, stdout="{}", stderr_tail="", log_path=str(log_path), duration_ms=1)
    s = RunService(ledger, settings, launch=capture, base_env={"PATH": os.environ["PATH"]})
    run = s.enqueue(AskRequest(question="q", model="deepseek-flash[1m]"))
    assert (run.runner, run.model) == ("ccx:ds", "deepseek-flash[1m]")
    s.execute(run.id)
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "deepseek-flash[1m]"


def test_socket_run_masks_the_exported_profile_secrets(ledger, settings, tmp_path):
    import socket
    profile = settings.backends_cfg_dir / "ds"
    profile.mkdir(parents=True)
    (profile / "settings.json").write_text(json.dumps({"env": {
        "ANTHROPIC_AUTH_TOKEN": "lm-secret", "ANTHROPIC_API_KEY": "lm-key", "ANTHROPIC_CUSTOM_HEADERS": "X-Auth: hdr-secret"}}))
    d = tmp_path / "sockets"
    d.mkdir()
    s_settings = settings.model_copy(update={"sockets_dir": d})
    def capture(argv, *, log_path, **kw):  # stdout is stored unmasked by launch, so the service masks it
        out = "boom lm-secret lm-key X-Auth: hdr-secret"
        return RunOutcome(exit_code=1, timed_out=False, stdout=out, stderr_tail="", log_path=str(log_path), duration_ms=1)
    s = RunService(ledger, s_settings, launch=capture, base_env={"PATH": os.environ["PATH"]})
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(str(d / "ds.sock"))
        run = s.execute(s.enqueue(AskRequest(question="q", runner="ccx:ds")).id)
    assert run.status == "failed" and "boom" in run.error
    assert not any(x in run.error for x in ("lm-secret", "lm-key", "hdr-secret")), run.error


# -- files a confined run may replace (S1) ----------------------------------------------------


def _codex_planting(plant):
    """A launch that, like a Codex run with a shell, leaves `plant(last_md)` in its run dir."""
    def fake(argv, *, log_path, **kw):
        plant(Path(argv[argv.index("-o") + 1]))
        log_path.write_text("")
        return RunOutcome(exit_code=0, timed_out=False, stdout="", stderr_tail="", log_path=str(log_path), duration_ms=1)
    return fake


def test_codex_last_message_symlink_to_a_secret_is_not_read(ledger, settings):
    secret = settings.data_dir / "token"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("owner-secret-token")
    s = RunService(ledger, settings, launch=_codex_planting(lambda p: p.symlink_to(secret)),
                   version=lambda b: None, base_env={"PATH": os.environ["PATH"]})
    run = s.execute(s.enqueue(AskRequest(question="q", model="gpt-6.1-sol")).id)
    assert run.status == "failed" and run.error == "no last message"
    assert "owner-secret-token" not in json.dumps(run.model_dump(mode="json"))


def test_codex_last_message_fifo_returns_promptly(ledger, settings):
    import time
    s = RunService(ledger, settings, launch=_codex_planting(os.mkfifo),
                   version=lambda b: None, base_env={"PATH": os.environ["PATH"]})
    t0 = time.monotonic()
    run = s.execute(s.enqueue(AskRequest(question="q", model="gpt-6.1-sol")).id)
    assert run.status == "failed" and time.monotonic() - t0 < 5


def test_codex_prompt_goes_on_stdin_whole_and_confined(ledger, settings, monkeypatch):
    big = "x" * (200 * 1024)  # over Linux's 128 KiB limit for one argv element
    real_plan = service_mod.plan_run
    monkeypatch.setattr(service_mod, "plan_run", lambda *a, **kw: (p := real_plan(*a, **kw)).model_copy(update={"prompt": p.prompt + big}))
    seen = {}
    def capture(argv, *, log_path, stdin_text=None, **kw):
        seen.update(argv=argv, stdin=stdin_text)
        Path(argv[argv.index("-o") + 1]).write_text('{"answer": "A", "proposals": []}')
        log_path.write_text("")
        return RunOutcome(exit_code=0, timed_out=False, stdout="", stderr_tail="", log_path=str(log_path), duration_ms=1)
    s = RunService(ledger, settings, launch=capture, version=lambda b: None, base_env={"PATH": os.environ["PATH"]})
    run = s.execute(s.enqueue(AskRequest(question="the question", model="gpt-6.1-sol")).id)
    assert run.status == "ok", run.error
    argv = seen["argv"]
    assert argv[:4] == [sys.executable, "-I", "-m", "trajectree.runner.confine"]
    runner = argv[argv.index("--") + 1:]
    assert runner[:2] == [settings.codex_bin, "exec"] and runner[-1] == "-"
    assert not any("the question" in a or "xxxx" in a for a in argv)
    assert "the question" in seen["stdin"] and seen["stdin"].endswith(big)


def test_codex_regular_last_message_is_the_result(ledger, settings):
    s = RunService(ledger, settings, launch=_codex_planting(lambda p: p.write_text('{"answer": "A", "proposals": []}')),
                   version=lambda b: None, base_env={"PATH": os.environ["PATH"]})
    run = s.execute(s.enqueue(AskRequest(question="q", model="gpt-6.1-sol")).id)
    assert run.status == "ok" and run.result_md == "A"


def test_null_optional_fields_take_defaults():
    from trajectree.runner.parse import ParsedResult
    from trajectree.runner.service import _proposals
    item = {"kind": "todo", "project": "a", "title": "T", "reason": "r",
            "event_kind": None, "when": None, "due": None, "body_md": None, "status": None}
    (p,) = _proposals(ParsedResult(ok=True, subtype="success", structured={"answer": "", "proposals": [item]}))
    assert (p.event_kind, p.body_md, p.status, p.when, p.due) == ("note", "", "happened", None, None)


def test_enqueue_runner_only_omits_model_flag(ledger, settings, logged_in):
    argv_file = settings.claude_config_dir / "argv.json"
    s = svc(ledger, settings, FAKE_CLAUDE_ARGV_FILE=str(argv_file))
    run = s.enqueue(AskRequest(question="q", runner="claude"))
    assert (run.runner, run.model) == ("claude", None)
    assert s.execute(run.id).status == "ok"
    assert "--model" not in json.loads(argv_file.read_text())


def test_legacy_model_only_routine_runs(ledger, settings):
    _routine(ledger, model="claude-sonnet-5-5", runner=None)
    s = svc(ledger, settings)
    run = s.enqueue(RoutineRequest(routine="rev"))
    assert (run.runner, run.model) == ("claude", "claude-sonnet-5-5")


def test_runner_only_routine_runs_without_a_model(ledger, settings):
    _routine(ledger, model=None, runner="ccx:kimi")
    run = svc(ledger, settings).enqueue(RoutineRequest(routine="rev"))
    assert (run.runner, run.model) == ("ccx:kimi", None)


def test_run_model_filled_from_model_usage(ledger, settings, logged_in):
    usage = json.dumps({"a": {"outputTokens": 1}, "b": {"outputTokens": 9}})
    s = svc(ledger, settings, FAKE_CLAUDE_MODEL_USAGE=usage)
    assert s.execute(s.enqueue(AskRequest(question="q", runner="claude")).id).model == "b"
    chosen = s.execute(s.enqueue(AskRequest(question="q", model=MODEL)).id)
    assert chosen.model == MODEL  # a chosen model is never overwritten


def test_apply_proposal_sets_tier(ledger, human, settings, logged_in):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = svc(ledger, settings)
    run = s.execute(s.enqueue(QuickAddRequest(model=MODEL, text="x")).id)
    base = {"kind": "event", "project": "a", "reason": "r", "event_kind": "note"}
    update_run(ledger.conn, run.id, proposals=[
        Proposal(**base, title="Moment", tier="pill"), Proposal(**base, title="Checkpoint", tier=None),
    ])
    s.apply_proposal(run.id, 0, actor=human)
    s.apply_proposal(run.id, 1, actor=human)
    assert {e.title: e.tier for e in list_events(ledger.conn, project_ids=[p.id])} == {"Moment": "pill", "Checkpoint": "block"}


# -- inspections -------------------------------------------------------------------------------

from datetime import timedelta
from trajectree.models import SignalIn, Watermark
from trajectree.repo.inspections import due_projects, get_watermark, set_watermark, uninspected_stretches
from trajectree.repo.runs import find_active
from trajectree.repo.signals import upsert_signals
from trajectree.runner.kinds import InspectRequest
from trajectree.runner.spec import InspectionActive, NothingToInspect
from trajectree.timeutil import utcnow

UTC = timezone.utc
PROPOSALS_ANSWER = json.dumps({"answer": "done", "proposals": []})


def _at(day, hour=0, minute=0):
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC)


def _seg(pid, day, h1, h2, key, kind="session"):
    return SignalIn(project_id=pid, kind=kind, t_start=_at(day, h1), t_end=_at(day, h2),
                    payload={"title": key, "prompts": 2}, dedupe_key=key)


def _inspect_project(ledger, human, tmp_path, slug="a"):
    p = create_project(ledger, human, ProjectIn(slug=slug, title=slug.upper(), kind="research"))
    folder = tmp_path / f"proj-{slug}"
    folder.mkdir()
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator=str(folder)))
    return p


def isvc(ledger, settings, structured=PROPOSALS_ANSWER, mode="ok"):
    env = {"PATH": os.environ["PATH"], "FAKE_CLAUDE_MODE": mode, "FAKE_CLAUDE_STRUCTURED": structured}
    return RunService(ledger, settings, base_env=env)


def _set_structured(s, structured, mode="ok"):
    s.base_env = {**s.base_env, "FAKE_CLAUDE_STRUCTURED": structured, "FAKE_CLAUDE_MODE": mode}


def _stretches(ledger, settings, p):
    from trajectree.repo.projects import get_project
    return uninspected_stretches(ledger.conn, settings, get_project(ledger.conn, p.id), now=utcnow())


def _due_slugs(ledger, settings):
    return [d.slug for d in due_projects(ledger.conn, settings, now=utcnow())]


def _queued_inspect_runs(ledger):
    from trajectree.repo.runs import list_runs
    return [r for r in list_runs(ledger.conn, limit=50) if r.kind.startswith("inspect") and r.status == "queued"]


def test_inspect_small_material_runs_once_and_advances_watermark(ledger, human, settings, logged_in, tmp_path):
    p = _inspect_project(ledger, human, tmp_path)
    upsert_signals(ledger.conn, [_seg(p.id, 5, 10, 11, "s1"), _seg(p.id, 5, 12, 13, "s2")])
    [stretch] = _stretches(ledger, settings, p)
    s = isvc(ledger, settings)
    run = s.inspect(InspectRequest(project="a", model=MODEL))
    assert run.kind == "inspect" and run.project_id == p.id
    assert run.props["series_id"] and run.props["episode_no"] == 1 and run.props["episodes"] == 1
    done = s.execute(run.id)
    assert done.status == "ok"
    wm = get_watermark(ledger.conn, p.id)
    assert wm.inspected_at == stretch.t_end and wm.series_id == run.props["series_id"]
    assert wm.started_at == done.started
    assert set(wm.covered) == {"s1", "s2"}
    assert "a" not in _due_slugs(ledger, settings)
    assert _queued_inspect_runs(ledger) == []


def test_inspect_material_is_rendered_outside_the_lock(ledger, human, settings, logged_in, tmp_path, monkeypatch):
    p = _inspect_project(ledger, human, tmp_path)
    upsert_signals(ledger.conn, [_seg(p.id, 5, 10, 11, "s1")])
    seen = []
    real = service_mod.render_stretches
    def spy(*a, **kw):
        seen.append(_lock_free(ledger))
        return real(*a, **kw)
    monkeypatch.setattr(service_mod, "render_stretches", spy)
    prompt_file = settings.claude_config_dir / "prompt.txt"  # the confined fake may write only there
    s = isvc(ledger, settings)
    s.base_env = {**s.base_env, "FAKE_CLAUDE_PROMPT_FILE": str(prompt_file)}
    run = s.inspect(InspectRequest(project="a", model=MODEL))
    done = s.execute(run.id)
    assert done.status == "ok", done.error
    assert seen == [True]  # rendered once, in the run; `inspect` only sizes it from the digest sizes
    assert "### Stretch 1: 2026-01-05" in prompt_file.read_text()


def test_inspect_mints_scope_with_five_tools(ledger, human, settings, logged_in, tmp_path, monkeypatch):
    p = _inspect_project(ledger, human, tmp_path)
    upsert_signals(ledger.conn, [_seg(p.id, 5, 10, 11, "s1")])
    seen = {}
    real = service_mod.mint_token
    def spy(conn, **kw):
        seen.update(kw)
        return real(conn, **kw)
    monkeypatch.setattr(service_mod, "mint_token", spy)
    s = isvc(ledger, settings)
    assert s.execute(s.inspect(InspectRequest(project="a", model=MODEL)).id).status == "ok"
    assert seen["scope"] == Scope(
        projects=[p.id], tools=["record_event", "update_event", "add_todo", "link", "set_project_summary"], write=True
    )


def test_inspect_refuses_a_second_active_inspection(ledger, human, settings, logged_in, tmp_path):
    p = _inspect_project(ledger, human, tmp_path)
    upsert_signals(ledger.conn, [_seg(p.id, 5, 10, 11, "s1")])
    s = isvc(ledger, settings)
    s.inspect(InspectRequest(project="a", model=MODEL))
    with pytest.raises(InspectionActive, match="an inspection is already running for a"):
        s.inspect(InspectRequest(project="a", model=MODEL))
    with pytest.raises(InspectionActive):
        s.enqueue(InspectRequest(project="a", model=MODEL))


def test_inspect_nothing_due_raises(ledger, human, settings, logged_in, tmp_path):
    _inspect_project(ledger, human, tmp_path)
    s = isvc(ledger, settings)
    with pytest.raises(NothingToInspect, match="nothing to inspect for a"):
        s.inspect(InspectRequest(project="a", model=MODEL))


def test_inspect_with_a_bad_model_writes_nothing(ledger, human, settings, logged_in, tmp_path):
    p = _inspect_project(ledger, human, tmp_path)
    upsert_signals(ledger.conn, [_seg(p.id, 5, 10, 11, "s1")])
    set_watermark(ledger.conn, p.id, Watermark(inspected_at=_at(30)))
    s = isvc(ledger, settings)
    with pytest.raises(ValueError):
        s.inspect(InspectRequest(project="a", model="no-such-model", since=date(2026, 1, 1)))
    assert get_watermark(ledger.conn, p.id).inspected_at == _at(30)  # `since` was not applied


def _three_stretches(ledger, p):
    upsert_signals(ledger.conn, [
        _seg(p.id, 5, 10, 11, "s1"), _seg(p.id, 5, 12, 13, "s2"),
        _seg(p.id, 10, 9, 10, "s3"),
        _seg(p.id, 20, 14, 15, "s4"),
    ])


def _render_size(ledger, settings, stretches):
    """The un-cut size of the inspection material for `stretches` (no events in these tests)."""
    from trajectree.context.stretches import render_stretches
    from trajectree.repo.signals import list_signals
    signals = {sig.id: sig for sig in list_signals(ledger.conn)}
    return render_stretches(stretches, signals, blocks=[], edges=[], settings=settings, budget_chars=10**9)[1]


def _episode_budget(ledger, settings, p):
    """A budget that fits episode 1 (stretches 1-2) and episode 2 (stretch 3), but not all three."""
    st1, st2, st3 = _stretches(ledger, settings, p)
    budget = max(_render_size(ledger, settings, [st1, st2]), _render_size(ledger, settings, [st3]))
    assert _render_size(ledger, settings, [st1, st2, st3]) > budget
    settings.inspect_budget_chars = budget
    return st1, st2, st3


TWO_EPISODES = json.dumps({"answer": "two", "episodes": [
    {"start": "2026-01-05", "end": "2026-01-10", "label": "first"},
    {"start": "2026-01-20", "end": "2026-01-20", "label": "second"},
]})


def test_inspect_large_material_runs_boundaries_then_episodes_in_order(ledger, human, settings, logged_in, tmp_path):
    p = _inspect_project(ledger, human, tmp_path)
    _three_stretches(ledger, p)
    st1, st2, st3 = _episode_budget(ledger, settings, p)  # each episode renders whole
    s = isvc(ledger, settings, structured=TWO_EPISODES)
    b = s.inspect(InspectRequest(project="a", model=MODEL))
    assert b.kind == "inspect_boundaries" and b.project_id == p.id
    assert set(b.props["sizes"]) == {sid for st in (st1, st2, st3) for sid in st.signal_ids}
    series = b.props["series_id"]
    assert s.execute(b.id).status == "ok"
    b = get_run(ledger.conn, b.id)
    assert [e["label"] for e in b.props["episodes_json"]] == ["first", "second"]
    assert b.props["episodes"] == 2
    [ep1] = _queued_inspect_runs(ledger)
    assert ep1.kind == "inspect" and ep1.props["episode_no"] == 1 and ep1.props["episodes"] == 2
    req1 = InspectRequest.model_validate(ep1.props["request"])
    assert req1.episode_start == st1.t_start and req1.episode_end == st2.t_end and req1.model == MODEL
    assert get_watermark(ledger.conn, p.id).inspected_at is None  # the boundary pass moves nothing
    _set_structured(s, PROPOSALS_ANSWER)
    assert s.execute(ep1.id).status == "ok"
    assert get_watermark(ledger.conn, p.id).inspected_at == st2.t_end
    [ep2] = _queued_inspect_runs(ledger)
    assert ep2.props["episode_no"] == 2 and ep2.props["episodes"] == 2
    assert InspectRequest.model_validate(ep2.props["request"]).episode_start == st3.t_start
    assert s.execute(ep2.id).status == "ok"
    wm = get_watermark(ledger.conn, p.id)
    assert wm.inspected_at == st3.t_end and set(wm.covered) == {"s1", "s2", "s3", "s4"}
    assert {r.props["series_id"] for r in (b, ep1, ep2)} == {series}
    assert _queued_inspect_runs(ledger) == [] and "a" not in _due_slugs(ledger, settings)


def test_inspect_failed_episode_stops_series_and_keeps_watermark(ledger, human, settings, logged_in, tmp_path):
    settings.inspect_budget_chars = 200
    p = _inspect_project(ledger, human, tmp_path)
    _three_stretches(ledger, p)
    s = isvc(ledger, settings, structured=TWO_EPISODES)
    s.execute(s.inspect(InspectRequest(project="a", model=MODEL)).id)
    [ep1] = _queued_inspect_runs(ledger)
    _set_structured(s, PROPOSALS_ANSWER, mode="garbage")
    assert s.execute(ep1.id).status == "failed"
    assert _queued_inspect_runs(ledger) == []
    assert get_watermark(ledger.conn, p.id).inspected_at is None
    assert "a" in _due_slugs(ledger, settings)


@pytest.mark.parametrize("episodes, reason", [
    ([{"start": "2026-01-20", "end": "2026-01-20", "label": "x"}, {"start": "2026-01-05", "end": "2026-01-10", "label": "y"}], "out of order"),
    ([{"start": "2026-01-05", "end": "2026-01-10", "label": "x"}, {"start": "2026-01-10", "end": "2026-01-20", "label": "y"}], "overlap"),
    ([], "no episodes"),
    ([{"start": "Jan 5", "end": "2026-01-10", "label": "x"}], "unparsable"),
    ([{"start": "2025-12-01", "end": "2026-01-10", "label": "x"}], "outside"),
    ([{"start": "2026-01-12", "end": "2026-01-15", "label": "x"}], "no stretch"),
    (None, "no episodes"),
])
def test_bad_boundaries_fail_the_series_and_keep_the_watermark(ledger, human, settings, logged_in, tmp_path, episodes, reason):
    settings.inspect_budget_chars = 200
    p = _inspect_project(ledger, human, tmp_path)
    _three_stretches(ledger, p)
    body = {"answer": "bad"} if episodes is None else {"answer": "bad", "episodes": episodes}
    s = isvc(ledger, settings, structured=json.dumps(body))
    done = s.execute(s.inspect(InspectRequest(project="a", model=MODEL)).id)
    stored = get_run(ledger.conn, done.id)
    assert done.status == stored.status == "failed"
    assert stored.error.startswith("boundaries invalid:") and reason in stored.error
    assert _queued_inspect_runs(ledger) == []
    assert get_watermark(ledger.conn, p.id).inspected_at is None
    assert "a" in _due_slugs(ledger, settings)


ONE_EPISODE = json.dumps({"answer": "one", "episodes": [{"start": "2026-01-05", "end": "2026-01-20", "label": "all"}]})


def test_segments_cut_from_an_over_budget_episode_stay_due(ledger, human, settings, logged_in, tmp_path):
    p = _inspect_project(ledger, human, tmp_path)
    _three_stretches(ledger, p)
    # Just under the whole material: the one episode's render loses its last segment (s4).
    settings.inspect_budget_chars = _render_size(ledger, settings, _stretches(ledger, settings, p)) - 5
    s = isvc(ledger, settings, structured=ONE_EPISODE)
    s.execute(s.inspect(InspectRequest(project="a", model=MODEL)).id)
    [ep] = _queued_inspect_runs(ledger)
    _set_structured(s, PROPOSALS_ANSWER)
    assert s.execute(ep.id).status == "ok"
    wm = get_watermark(ledger.conn, p.id)
    assert wm.inspected_at == _at(20, 15)
    assert set(wm.covered) == {"s1", "s2", "s3"}  # the cut segment was never shown, so not covered
    assert "a" in _due_slugs(ledger, settings)
    from trajectree.repo.signals import list_signals
    key_of = {sig.id: sig.dedupe_key for sig in list_signals(ledger.conn, project_ids=[p.id])}
    [st] = _stretches(ledger, settings, p)
    assert [key_of[sid] for sid in st.signal_ids] == ["s4"]  # its stretch is only the cut segment


def test_signal_ingested_during_inspection_stays_due(ledger, human, settings, logged_in, tmp_path):
    p = _inspect_project(ledger, human, tmp_path)
    upsert_signals(ledger.conn, [_seg(p.id, 5, 10, 11, "s1"), _seg(p.id, 5, 12, 13, "s2")])
    s = isvc(ledger, settings)
    real_launch = s.launch
    def launch_and_ingest(argv, **kw):
        # Ingest lands a segment inside the episode while the agent runs (no lock held here).
        late = SignalIn(project_id=p.id, kind="session", t_start=_at(5, 11, 20), t_end=_at(5, 11, 40),
                        payload={"title": "late"}, dedupe_key="late")
        upsert_signals(ledger.conn, [late])
        return real_launch(argv, **kw)
    s.launch = launch_and_ingest
    run = s.inspect(InspectRequest(project="a", model=MODEL))
    assert s.execute(run.id).status == "ok"
    wm = get_watermark(ledger.conn, p.id)
    assert wm.inspected_at == _at(5, 13) and set(wm.covered) == {"s1", "s2"}
    assert "a" in _due_slugs(ledger, settings)
    [st] = _stretches(ledger, settings, p)
    # Re-flagged by the late segment, the stretch is read whole: s1 and s2 come back for context.
    assert (st.t_start, st.t_end, st.segments, st.inspected_up_to) == (_at(5, 10), _at(5, 13), 3, _at(5, 13))


def test_series_that_cannot_queue_its_next_episode_records_why(ledger, human, settings, logged_in, tmp_path):
    settings.inspect_budget_chars = 200
    p = _inspect_project(ledger, human, tmp_path)
    _three_stretches(ledger, p)
    s = isvc(ledger, settings, structured=TWO_EPISODES)
    b = s.inspect(InspectRequest(project="a", model=MODEL))
    set_caps(ledger.conn, Caps(paused=True))  # the owner pauses runs while the boundary pass runs
    done = s.execute(b.id)
    assert done.status == "ok" and done.props["series_stopped"] == "runs are paused"
    assert _queued_inspect_runs(ledger) == [] and "a" in _due_slugs(ledger, settings)


def test_boundaries_snap_episode_dates_to_stretch_edges(ledger, human, settings, logged_in, tmp_path):
    settings.inspect_budget_chars = 200
    p = _inspect_project(ledger, human, tmp_path)
    _three_stretches(ledger, p)
    st1, st2, st3 = _stretches(ledger, settings, p)
    # Day boundaries that fall between stretches snap to the stretches inside them.
    body = {"answer": "x", "episodes": [
        {"start": "2026-01-05", "end": "2026-01-07", "label": "a"},
        {"start": "2026-01-08", "end": "2026-01-20", "label": "b"},
    ]}
    s = isvc(ledger, settings, structured=json.dumps(body))
    b = s.execute(s.inspect(InspectRequest(project="a", model=MODEL)).id)
    eps = get_run(ledger.conn, b.id).props["episodes_json"]
    assert [(e["start"], e["end"]) for e in eps] == [
        (st1.t_start.isoformat(), st1.t_end.isoformat()), (st2.t_start.isoformat(), st3.t_end.isoformat()),
    ]


def test_inspect_since_resets_watermark(ledger, human, settings, logged_in, tmp_path):
    p = _inspect_project(ledger, human, tmp_path)
    upsert_signals(ledger.conn, [_seg(p.id, 1, 10, 11, "s1"), _seg(p.id, 20, 10, 11, "s2")])
    set_watermark(ledger.conn, p.id, Watermark(inspected_at=_at(30), covered={"s1": _at(1, 11).isoformat(), "s2": _at(20, 11).isoformat()}))
    assert "a" not in _due_slugs(ledger, settings)
    s = isvc(ledger, settings)
    run = s.inspect(InspectRequest(project="a", model=MODEL, since=date(2026, 1, 1)))
    req = InspectRequest.model_validate(run.props["request"])
    assert req.episode_start.date() == date(2026, 1, 1) and req.episode_start == _at(1, 10)
    assert get_watermark(ledger.conn, p.id).inspected_at == _at(1)


def test_inspect_since_keeps_earlier_history_inspected(ledger, human, settings, logged_in, tmp_path):
    p = _inspect_project(ledger, human, tmp_path)
    upsert_signals(ledger.conn, [_seg(p.id, 1, 10, 11, "old"), _seg(p.id, 20, 10, 11, "new")])
    s = isvc(ledger, settings)  # never inspected before
    run = s.inspect(InspectRequest(project="a", model=MODEL, since=date(2026, 1, 15)))
    wm = get_watermark(ledger.conn, p.id)
    assert wm.inspected_at == _at(15) and set(wm.covered) == {"old"}
    assert InspectRequest.model_validate(run.props["request"]).episode_start == _at(20, 10)


def test_segment_changed_during_inspection_is_due_again(ledger, human, settings, logged_in, tmp_path):
    p = _inspect_project(ledger, human, tmp_path)
    upsert_signals(ledger.conn, [_seg(p.id, 5, 10, 11, "s1"), _seg(p.id, 5, 12, 13, "s2")])
    s = isvc(ledger, settings)
    assert s.execute(s.inspect(InspectRequest(project="a", model=MODEL)).id).status == "ok"
    assert "a" not in _due_slugs(ledger, settings)
    upsert_signals(ledger.conn, [_seg(p.id, 5, 12, 15, "s2")])  # the same segment, extended
    assert "a" in _due_slugs(ledger, settings)
    [st] = _stretches(ledger, settings, p)
    assert st.inspected_up_to == _at(5, 13)


def test_inspections_report_the_active_run_and_episode(ledger, human, settings, logged_in, tmp_path):
    p = _inspect_project(ledger, human, tmp_path)
    upsert_signals(ledger.conn, [_seg(p.id, 5, 10, 11, "s1")])
    run = isvc(ledger, settings).inspect(InspectRequest(project="a", model=MODEL))
    [due] = due_projects(ledger.conn, settings, now=utcnow())
    assert (due.active_run, due.episode, due.episodes) == (run.id, 1, 1)
    assert find_active(ledger.conn, p.id, "inspect").id == run.id


def _with_prompt_file(s, settings):
    prompt_file = settings.claude_config_dir / "prompt.txt"  # the confined fake may write only there
    s.base_env = {**s.base_env, "FAKE_CLAUDE_PROMPT_FILE": str(prompt_file)}
    return prompt_file


def test_series_validates_episodes_in_local_time(ledger, human, settings, logged_in, tmp_path):
    settings.tz = "America/Chicago"
    settings.inspect_budget_chars = 150  # the timeline fits, the whole material does not
    p = _inspect_project(ledger, human, tmp_path)
    t = datetime(2026, 9, 4, 0, 30, tzinfo=UTC)  # 19:30 on Sep 3 in Chicago
    upsert_signals(ledger.conn, [SignalIn(project_id=p.id, kind="session", t_start=t, t_end=t + timedelta(minutes=20),
                                          payload={"title": "evening", "prompts": 1}, dedupe_key="s1")])
    [due] = due_projects(ledger.conn, settings, now=utcnow())
    assert (due.first_day, due.last_day) == ("2026-09-03", "2026-09-03")
    answer = json.dumps({"answer": "one", "episodes": [{"start": "2026-09-03", "end": "2026-09-03", "label": "evening"}]})
    s = isvc(ledger, settings, structured=answer)
    prompt_file = _with_prompt_file(s, settings)
    b = s.inspect(InspectRequest(project="a", model=MODEL))
    assert b.kind == "inspect_boundaries"
    assert s.execute(b.id).status == "ok"
    text = prompt_file.read_text()
    assert "- 2026-09-03 19:30–19:50 claude session" in text and "2026-09-04" not in text
    stored = get_run(ledger.conn, b.id)
    assert stored.status == "ok", stored.error
    assert stored.props["episodes_json"][0]["start"] == t.isoformat()
    [ep] = _queued_inspect_runs(ledger)
    _set_structured(s, PROPOSALS_ANSWER)
    assert s.execute(ep.id).status == "ok"
    assert "### Stretch 1: 2026-09-03 → 2026-09-03" in prompt_file.read_text()  # its segment is cut by the tiny budget


def test_boundary_pass_planned_while_live_gets_its_segment_lines(ledger, human, settings, logged_in, tmp_path):
    from trajectree.repo.sessions import start_session
    p = _inspect_project(ledger, human, tmp_path)
    _three_stretches(ledger, p)
    _episode_budget(ledger, settings, p)
    s = isvc(ledger, settings, structured=TWO_EPISODES)
    prompt_file = _with_prompt_file(s, settings)
    b = s.inspect(InspectRequest(project="a", model=MODEL))
    assert b.kind == "inspect_boundaries"
    # The owner starts a session in the project before the boundary pass is planned.
    start_session(ledger.conn, session_id="x", cwd="/w/a", transcript_path=None, project_id=p.id, source="hook", now=utcnow())
    assert s.execute(b.id).status == "ok"
    text = prompt_file.read_text()
    assert '- 2026-01-05 10:00–11:00 claude session "s1"' in text and '"s4"' in text


def test_inspect_reads_each_transcript_once(ledger, human, settings, logged_in, tmp_path, monkeypatch):
    from tests.test_claude_sessions import CWD, SID, rec_assistant, rec_user, write_transcript
    p = _inspect_project(ledger, human, tmp_path)
    path = write_transcript(tmp_path / "home", CWD, SID, [
        rec_user("2026-01-05T10:00:00.000Z", text="first"), rec_assistant("2026-01-05T10:30:00.000Z"),
        rec_user("2026-01-05T13:00:00.000Z", text="second"), rec_assistant("2026-01-05T13:30:00.000Z"),
    ])
    payload = {"title": "t", "prompts": 1, "transcript_path": str(path), "backend": "claude"}
    upsert_signals(ledger.conn, [
        SignalIn(project_id=p.id, kind="session", t_start=_at(5, 10), t_end=_at(5, 10, 30), payload=payload, dedupe_key="a"),
        SignalIn(project_id=p.id, kind="session", t_start=_at(5, 13), t_end=_at(5, 13, 30), payload=payload, dedupe_key="b"),
    ])
    opened = []
    real_open = Path.open
    def spy(self, *a, **kw):
        if self == path:
            opened.append(1)
        return real_open(self, *a, **kw)
    monkeypatch.setattr(Path, "open", spy)
    run = isvc(ledger, settings).inspect(InspectRequest(project="a", model=MODEL))
    assert run.kind == "inspect" and len(opened) == 1


def test_inspect_while_live_says_so(ledger, human, settings, logged_in, tmp_path):
    from trajectree.repo.sessions import start_session
    p = _inspect_project(ledger, human, tmp_path)
    upsert_signals(ledger.conn, [_seg(p.id, 5, 10, 11, "s1")])
    start_session(ledger.conn, session_id="x", cwd="/w/a", transcript_path=None, project_id=p.id, source="hook", now=utcnow())
    with pytest.raises(NothingToInspect, match="^a session is live in a; inspect once it has been quiet for 120 minutes$"):
        isvc(ledger, settings).inspect(InspectRequest(project="a", model=MODEL))

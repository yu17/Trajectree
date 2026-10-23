import asyncio, json, os
import pytest
from trajectree.auth.scopes import Scope
from trajectree.auth.tokens import resolve_token
from trajectree.models import Actor, Caps, ProjectIn, Proposal
from trajectree.repo import NotFound, UnknownProject
from trajectree.repo.caps import set_caps
from trajectree.repo.events import list_events
from trajectree.repo.projects import create_project
from trajectree.repo.runs import get_run, update_run
from trajectree.runner.kinds import AskRequest, QuickAddRequest, SummarizeRequest
from trajectree.runner.service import RunService
from trajectree.runner.spec import AlreadyApplied, RunsPaused
import trajectree.runner.service as service_mod

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
    run = s.enqueue(AskRequest(question="q"))
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
    run = s.execute(s.enqueue(SummarizeRequest(project="a")).id)
    assert run.status == "ok" and run.project_id == p.id
    assert seen["scope"] == Scope(projects=[p.id], tools=["set_project_summary"], write=True)
    a = seen["actor"]
    assert (a.kind, a.ref, a.run_id, a.tool, a.backend, a.tool_version) == ("agent", run.id, run.id, "claude", "claude", "2.1.309")

def test_run_failure_revokes_token(ledger, human, settings, logged_in):
    s = svc(ledger, settings, mode="garbage")
    run = s.execute(s.enqueue(AskRequest(question="q")).id)
    assert run.status == "failed" and "boom" in run.error and run.est_usd == 0 and run.tokens_in == 0
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0 and (settings.runs_dir / f"{run.id}.log").exists()

def test_budget_subtype_is_capped(ledger, human, settings, logged_in):
    run = svc(ledger, settings, mode="budget").execute(svc(ledger, settings).enqueue(AskRequest(question="q")).id)
    assert run.status == "capped" and run.est_usd == 0.0123

def test_daily_cap_marks_capped_without_launch(ledger, human, settings, logged_in):
    set_caps(ledger.conn, Caps(usd_per_run=0.5, usd_per_day=1.0))
    s = svc(ledger, settings)
    first = update_run(ledger.conn, s.enqueue(AskRequest(question="q")).id, status="ok", est_usd=0.6, started=s.now())
    run = s.execute(s.enqueue(AskRequest(question="q")).id)
    assert run.status == "capped" and "daily cap" in run.error and run.token_id is None
    assert not (settings.runs_dir / f"{run.id}.log").exists()

def test_paused_enqueue_raises(ledger, settings):
    set_caps(ledger.conn, Caps(paused=True))
    with pytest.raises(RunsPaused):
        svc(ledger, settings).enqueue(AskRequest(question="q"))

def test_missing_credentials_fails_fast(ledger, settings):
    run = svc(ledger, settings).execute(svc(ledger, settings).enqueue(AskRequest(question="q")).id)
    assert run.status == "failed" and "trajectree login" in run.error and not (settings.runs_dir / f"{run.id}.log").exists()
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0

def test_invalid_runner_and_unknown_project_rejected_at_enqueue(ledger, human, settings):
    with pytest.raises(ValueError, match="invalid runner"):
        svc(ledger, settings).enqueue(AskRequest(question="q", runner="evil; rm -rf"))
    with pytest.raises(UnknownProject):
        svc(ledger, settings).enqueue(SummarizeRequest(project="zzz"))

@pytest.mark.anyio
async def test_worker_continues_after_timeout(ledger, human, settings, logged_in, tmp_path):
    set_caps(ledger.conn, Caps(timeout_s=1))
    mode = tmp_path / "mode"
    mode.write_text("sleep")
    s = svc(ledger, settings, FAKE_CLAUDE_MODE_FILE=str(mode))
    real_launch = s.launch

    def launch_then_unstick(*a, **kw):
        outcome = real_launch(*a, **kw)
        mode.write_text("ok")   # flipped before the worker can start the next run
        return outcome

    s.launch = launch_then_unstick
    r1 = s.enqueue(AskRequest(question="slow"))
    r2 = s.enqueue(AskRequest(question="fast"))
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
    run = s.execute(s.enqueue(QuickAddRequest(text="x")).id)
    web = Actor(kind="human", ref="owner", tool="web")
    after = s.apply_proposal(run.id, 0, actor=web)
    ev = list_events(ledger.conn, project_ids=[p.id])[0]
    assert ev.title == "Decided X" and ev.kind == "decision" and ev.source == "agent" and after.proposals[0].applied_id == ev.id
    t = ledger.get_txn(after.proposals[0].applied_txn)
    assert (t.actor_kind, t.proposed_by_run, t.model, t.backend, t.review, t.intent, t.tool) == ("human", run.id, None, "claude", "accepted", "decision taken", "web")
    after = s.apply_proposal(run.id, 1, actor=web)
    assert after.proposals[1].applied_txn is not None and ledger.get_txn(after.proposals[1].applied_txn).intent == "follow-up owed"

def test_accept_proposal_twice_is_409(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = svc(ledger, settings)
    run = s.execute(s.enqueue(QuickAddRequest(text="x")).id)
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
    run = s.execute(s.enqueue(QuickAddRequest(text="x")).id)
    n = len(ledger.list_txns())
    with pytest.raises(UnknownProject, match="known projects: b"):
        s.apply_proposal(run.id, 0, actor=human)
    assert len(ledger.list_txns()) == n


def test_execute_on_finished_run_is_a_noop(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = svc(ledger, settings)
    done = s.execute(s.enqueue(AskRequest(question="q")).id)
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
    assert s.execute(s.enqueue(AskRequest(question="q")).id).status == "ok"
    assert owned == [False]


def test_launch_exception_marks_failed_and_revokes_token(ledger, human, settings, logged_in):
    def boom(*a, **kw):
        raise RuntimeError("launch exploded")
    s = RunService(ledger, settings, launch=boom, base_env={"PATH": os.environ["PATH"]})
    run = s.enqueue(AskRequest(question="q"))
    with pytest.raises(RuntimeError):
        s.execute(run.id)
    after = get_run(ledger.conn, run.id)
    assert after.status == "failed" and after.error == "launch exploded" and after.finished is not None
    assert ledger.conn.execute("select count(*) from token").fetchone()[0] == 0


def test_execute_keeps_token_and_prompt_off_argv_and_removes_mcp_config(ledger, human, settings, logged_in, tmp_path, monkeypatch):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    argv_file, prompt_file = tmp_path / "argv.json", tmp_path / "prompt.txt"
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
    run = s.execute(s.enqueue(AskRequest(question="What is blocked?")).id)
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
    run = s.enqueue(AskRequest(question="q"))
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
    run = s.execute(s.enqueue(AskRequest(question="q")).id)
    assert run.status == "failed" and run.error == "oops *** and ***"


@pytest.mark.anyio
async def test_pause_holds_queued_runs_until_resumed(ledger, human, settings, logged_in):
    create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    s = svc(ledger, settings)
    run = s.enqueue(AskRequest(question="q"))
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
    orphan = s.enqueue(AskRequest(question="q"))
    queued = s.enqueue(AskRequest(question="q2"))
    token_id, _ = mint_token(ledger.conn, label=f"run:{orphan.id}", scope=Scope(write=False),
                             actor=Actor(kind="agent", ref=orphan.id), ttl=None)
    update_run(ledger.conn, orphan.id, status="running", started=s.now(), token_id=token_id)
    leftover = settings.runs_dir / orphan.id / "mcp.json"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("{}")
    ghost = s.enqueue(AskRequest(question="q3"))
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
    first = s.enqueue(SummarizeRequest(project="a"))
    assert s.enqueue(SummarizeRequest(project="a")).id == first.id
    assert s.enqueue(SummarizeRequest(project="b")).id != first.id
    update_run(ledger.conn, first.id, status="running")
    assert s.enqueue(SummarizeRequest(project="a")).id == first.id
    update_run(ledger.conn, first.id, status="ok")
    assert s.enqueue(SummarizeRequest(project="a")).id != first.id


def _run_with(ledger, settings, proposals):
    s = svc(ledger, settings, FAKE_CLAUDE_STRUCTURED=json.dumps({"answer": "A", "proposals": proposals}))
    return s, s.execute(s.enqueue(QuickAddRequest(text="x")).id)


def test_worker_mark_failed_keeps_abort_error(ledger, human, settings, logged_in):
    def boom(argv, **kw):
        cfg = json.loads(open(argv[argv.index("--mcp-config") + 1]).read())
        token = cfg["mcpServers"]["trajectree"]["headers"]["Authorization"].split()[1]
        raise RuntimeError(f"boom {token}")
    s = RunService(ledger, settings, launch=boom, base_env={"PATH": os.environ["PATH"]})
    run = s.enqueue(AskRequest(question="q"))
    with pytest.raises(RuntimeError) as info:
        s.execute(run.id)
    s._mark_failed(run.id, str(info.value))   # what the worker does with the unmasked text
    stored = get_run(ledger.conn, run.id)
    assert "trj_" not in (stored.error or "") and stored.props["failure"] == "infra" and "request" in stored.props


def test_failure_flag_is_set_for_infra_only(ledger, human, settings, logged_in):
    s = svc(ledger, settings)
    run = s.enqueue(AskRequest(question="q"))
    with ledger.locked() as conn:
        s._fail(conn, run.id, "no")
    assert "failure" not in get_run(ledger.conn, run.id).props
    other = s.enqueue(AskRequest(question="q"))
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
    data = {"name": "rev", "trigger": "@daily", "prompt_ref": "cross_project_review.md", **over}
    return create_routine(ledger.conn, RoutineIn(**data), now=R_NOW)


def test_enqueue_routine_lands_in_scheduled_slot(ledger, human, settings):
    r = _routine(ledger)
    s = svc(ledger, settings)
    run = s.enqueue(RoutineRequest(routine="rev"))
    assert (run.slot, run.kind, run.routine_id, run.attempt) == ("scheduled", "routine", r.id, 1)
    assert oldest_queued(ledger.conn, slot="interactive") is None
    assert run.props["request"]["routine"] == "rev" and run.project_id is None
    assert s.enqueue(AskRequest(question="q")).slot == "interactive"
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
    s.execute(s.enqueue(AskRequest(question="q")).id)
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
    ask = s.enqueue(AskRequest(question="q"))
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
    run = s.enqueue(AskRequest(question="q"))
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
    run = s.enqueue(AskRequest(question="q"))
    update_run(ledger.conn, run.id, status="running")
    s._mark_failed(run.id, "planner crashed")
    after = get_run(ledger.conn, run.id)
    assert after.status == "failed" and after.props["failure"] == "infra"

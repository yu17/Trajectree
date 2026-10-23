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
    assert "Bearer ***" in log and "fake-oauth" not in log and "trj_" not in log

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
    r1 = s.enqueue(AskRequest(question="slow"))
    r2 = s.enqueue(AskRequest(question="fast"))
    stop = asyncio.Event()
    task = asyncio.create_task(s.worker(stop, poll_s=0.05))
    for _ in range(200):
        await asyncio.sleep(0.05)
        if get_run(ledger.conn, r1.id).status not in ("queued", "running"):
            mode.write_text("ok")
        if get_run(ledger.conn, r2.id).status == "ok":
            break
    stop.set()
    await asyncio.wait_for(task, 5)
    assert get_run(ledger.conn, r1.id).status == "failed" and get_run(ledger.conn, r1.id).error == "timeout after 1s"
    assert get_run(ledger.conn, r2.id).status == "ok"

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

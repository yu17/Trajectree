import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trajectree.connectors.slurm import (
    SlurmCollection,
    SlurmJob,
    _epoch,
    _num,
    _states,
    collect_slurm,
    parse_sacct,
    parse_squeue,
    project_for_job,
    slurm_due,
    ssh_command,
    write_slurm,
)
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.projects import create_project
from trajectree.repo.resources import list_resources, upsert_resource
from trajectree.repo.signals import list_signals
from trajectree.repo.state import get_state, set_state

UTC = timezone.utc
NOW = datetime(2026, 1, 6, 12, tzinfo=UTC)
FAKE_SSH = Path(__file__).parent / "bin" / "fake-ssh"
FIXTURES = Path(__file__).parent / "fixtures"


def fake_settings(settings, tmp_path, **extra):
    return settings.model_copy(
        update={"slurm_host": "login.hpc", "slurm_user": "u", "ssh_bin": str(FAKE_SSH), **extra}
    )


def job(job_id="1", state="RUNNING", workdir="/home/u/proj", name="j", **kw):
    return SlurmJob(
        job_id=job_id, name=name, state=state, workdir=workdir, partition="gpu", user="u",
        submit=kw.get("submit"), start=kw.get("start"), end=kw.get("end"),
    )  # fmt: skip


def test_num_epoch_states_helpers():
    assert _num(5) == 5 and _num("7") == 7 and _num({"set": True, "number": 9}) == 9
    assert _num({"set": False, "number": 0}) is None and _num({"set": True, "infinite": True, "number": 1}) is None
    assert _num("abc") is None and _num(None) is None and _num(True) is None
    assert _epoch(0) is None and _epoch(None) is None
    assert _epoch(1767607200) == datetime(2026, 1, 5, 10, tzinfo=UTC)
    assert _states(None) == [] and _states("RUNNING") == ["RUNNING"] and _states(["A", "B"]) == ["A", "B"]


def test_parse_squeue_handles_both_number_shapes():
    jobs = parse_squeue((FIXTURES / "squeue.json").read_text())
    assert [(j.job_id, j.state, j.workdir) for j in jobs] == [
        ("123", "RUNNING", "/home/u/proj"),
        ("124", "PENDING", "/home/u/other"),
    ]
    assert jobs[0].start == datetime(2026, 1, 5, 10, tzinfo=UTC) and jobs[1].start is None
    assert jobs[0].end is None and jobs[1].submit == datetime.fromtimestamp(1767610000, UTC)


def test_parse_sacct_state_list_and_times():
    (j,) = parse_sacct((FIXTURES / "sacct.json").read_text())
    assert (j.job_id, j.state, j.workdir, j.partition, j.user) == ("99", "COMPLETED", "/home/u/proj", "cpu", "u")
    assert j.end == datetime.fromtimestamp(1767523700, UTC) and j.start == datetime.fromtimestamp(1767520100, UTC)


def test_parse_is_defensive():
    assert parse_squeue('{"jobs": [{"job_id": 1}, "junk", {"name": "no id"}]}')[0].state == "UNKNOWN"
    assert len(parse_squeue('{"jobs": [{"job_id": 1}, "junk", {"name": "no id"}]}')) == 1
    for bad in ("not json", "[]", '{"jobs": 3}', '{"x": 1}'):
        with pytest.raises(ValueError):
            parse_sacct(bad)


def test_ssh_command_shape(settings):
    s = settings.model_copy(update={"slurm_host": "h", "ssh_bin": "ssh"})
    assert ssh_command(s, ["squeue"]) == [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10", "h", "squeue",
    ]  # fmt: skip
    s = s.model_copy(update={"slurm_user": "u", "slurm_key": Path("/k")})
    assert ssh_command(s, ["x"])[-4:] == ["-i", "/k", "u@h", "x"]


def test_collect_slurm_with_fake_ssh(settings, tmp_path, monkeypatch):
    s = fake_settings(settings, tmp_path, slurm_key=tmp_path / "k")
    monkeypatch.setenv("FAKE_SSH_SQUEUE", str(FIXTURES / "squeue.json"))
    monkeypatch.setenv("FAKE_SSH_SACCT", str(FIXTURES / "sacct.json"))
    monkeypatch.setenv("FAKE_SSH_ARGV_FILE", str(tmp_path / "argv"))
    col = collect_slurm(s, since=NOW - timedelta(days=1))
    assert {j.job_id for j in col.jobs} == {"123", "124", "99"} and col.errors == []
    argv = json.loads((tmp_path / "argv").read_text())
    assert argv[:2] == ["-o", "BatchMode=yes"] and "u@login.hpc" in argv and "-i" in argv


def test_collect_slurm_squeue_wins_on_merge(settings, tmp_path, monkeypatch):
    sacct = tmp_path / "sacct.json"
    sacct.write_text(json.dumps({"jobs": [{"job_id": 123, "name": "x", "state": {"current": ["COMPLETED"]}}]}))
    monkeypatch.setenv("FAKE_SSH_SQUEUE", str(FIXTURES / "squeue.json"))
    monkeypatch.setenv("FAKE_SSH_SACCT", str(sacct))
    col = collect_slurm(fake_settings(settings, tmp_path), since=NOW)
    assert {j.job_id: j.state for j in col.jobs} == {"123": "RUNNING", "124": "PENDING"}


def test_collect_slurm_ssh_failure_is_reported(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_EXIT", "255")
    col = collect_slurm(fake_settings(settings, tmp_path), since=NOW)
    assert col.jobs == [] and len(col.errors) == 2
    assert col.errors[0].startswith("squeue: ") and "fake failure" in col.errors[0]
    assert col.errors[1].startswith("sacct: ")


def test_collect_slurm_one_command_failing_keeps_the_other(settings, tmp_path):
    def run(cmd, **kw):
        if "sacct" in cmd:
            raise subprocess.TimeoutExpired(cmd, 30)
        return subprocess.CompletedProcess(cmd, 0, (FIXTURES / "squeue.json").read_text(), "")

    col = collect_slurm(fake_settings(settings, tmp_path), since=NOW, run=run)
    assert {j.job_id for j in col.jobs} == {"123", "124"} and len(col.errors) == 1
    assert col.errors[0].startswith("sacct: ")


def test_collect_slurm_bad_json_and_oserror(settings, tmp_path):
    def run(cmd, **kw):
        if "sacct" in cmd:
            raise OSError("no ssh")
        return subprocess.CompletedProcess(cmd, 0, "garbage", "")

    col = collect_slurm(fake_settings(settings, tmp_path), since=NOW, run=run)
    assert col.jobs == [] and [e.split(":")[0] for e in col.errors] == ["squeue", "sacct"]


def test_project_for_job_fallbacks(ledger, human):
    a = create_project(ledger, human, ProjectIn(slug="alpha", title="A", kind="research"))
    b = create_project(ledger, human, ProjectIn(slug="beta", title="B", kind="research", props={"slurm_workdirs": ["/scratch/b"]}))
    inbox = create_project(ledger, human, ProjectIn(slug="inbox", title="Inbox", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=a.id, kind="folder", locator="/home/u/alpha"))
    conn = ledger.conn
    assert project_for_job(conn, job(workdir="/home/u/alpha/run1")).id == a.id
    assert project_for_job(conn, job(workdir="/scratch/b/x")).id == b.id
    assert project_for_job(conn, job(workdir="/scratch/bb", name="Alpha-sweep")).id == a.id
    assert project_for_job(conn, job(workdir=None, name="zzz")).id == inbox.id


def test_project_for_job_no_match_is_none(ledger, human):
    create_project(ledger, human, ProjectIn(slug="alpha", title="A", kind="research"))
    assert project_for_job(ledger.conn, job(workdir="/nowhere", name="zzz")) is None


def test_write_slurm_maps_by_workdir_and_auto_accepts(ledger, human, settings, tmp_path):
    s = fake_settings(settings, tmp_path)
    p = create_project(ledger, human, ProjectIn(slug="proj", title="P", kind="research"))
    upsert_resource(ledger, human, ResourceIn(project_id=p.id, kind="folder", locator="/home/u/proj"))
    col = SlurmCollection(
        jobs=[
            job("123", "RUNNING", start=datetime(2026, 1, 5, 10, tzinfo=UTC)),
            job("99", "COMPLETED", end=datetime(2026, 1, 5, 11, tzinfo=UTC), submit=datetime(2026, 1, 5, 8, tzinfo=UTC)),
            job("5", "PENDING", workdir="/elsewhere", name="zzz"),
        ],
        errors=[],
    )
    report = write_slurm(ledger, s, col, now=NOW)
    assert (report.jobs, report.resources, report.signals_written) == (3, 2, 2)
    res = {r.locator: r for r in list_resources(ledger.conn, kinds=["slurm_job"])}
    assert set(res) == {"login.hpc:123", "login.hpc:99"} and res["login.hpc:123"].props["state"] == "RUNNING"
    txns = [t for t in ledger.list_txns() if t.intent.startswith("slurm job")]
    assert {t.intent for t in txns} == {"slurm job 123 RUNNING", "slurm job 99 COMPLETED"}
    assert ledger.list_txns(review="pending") == []
    sigs = {x.dedupe_key: x for x in list_signals(ledger.conn, project_ids=[p.id], kinds=["job_state"])}
    assert set(sigs) == {"slurm:login.hpc:123:RUNNING", "slurm:login.hpc:99:COMPLETED"}
    running, done = sigs["slurm:login.hpc:123:RUNNING"], sigs["slurm:login.hpc:99:COMPLETED"]
    assert running.t_start == datetime(2026, 1, 5, 10, tzinfo=UTC) and running.resource_id == res["login.hpc:123"].id
    assert done.t_start == datetime(2026, 1, 5, 11, tzinfo=UTC)
    assert running.payload == {"job_id": "123", "name": "j", "state": "RUNNING", "partition": "gpu", "workdir": "/home/u/proj"}
    assert get_state(ledger.conn, "slurm:last")["at"]
    write_slurm(ledger, s, col, now=NOW)
    assert len(list_signals(ledger.conn, project_ids=[p.id], kinds=["job_state"])) == 2


def test_write_slurm_t_start_falls_back_to_now(ledger, human, settings, tmp_path):
    p = create_project(ledger, human, ProjectIn(slug="inbox", title="I", kind="research"))
    write_slurm(ledger, fake_settings(settings, tmp_path), SlurmCollection(jobs=[job("7", "PENDING", workdir=None)], errors=[]), now=NOW)
    (sig,) = list_signals(ledger.conn, project_ids=[p.id], kinds=["job_state"])
    assert sig.t_start == NOW


def test_slurm_due(ledger):
    assert slurm_due(ledger.conn, now=NOW, every_m=15)
    assert not slurm_due(ledger.conn, now=NOW, every_m=0)
    with ledger.conn:
        set_state(ledger.conn, "slurm:last", {"at": NOW.isoformat()})
    assert not slurm_due(ledger.conn, now=NOW, every_m=15)
    assert slurm_due(ledger.conn, now=NOW + timedelta(minutes=16), every_m=15)

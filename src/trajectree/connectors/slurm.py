"""Slurm connector: running and recent jobs from `squeue`/`sacct` over ssh on the login node.

Three phases, like the other connectors, so nothing slow runs while the ledger lock is held:
`collect_slurm` runs the two ssh commands with no connection, `write_slurm` attaches each job
to a project and writes an auto-accepted `slurm_job` resource plus one `job_state` signal per
(job, state). A failing command becomes one `errors` entry; the other still counts. The JSON
shapes differ between Slurm versions (plain ints vs `{"set", "number"}`, string vs list
states), so parsing is defensive and every key is optional.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

import apsw
from pydantic import AwareDatetime, BaseModel

from trajectree.config import Settings
from trajectree.db.ledger import Ledger
from trajectree.models import Actor, Project, ResourceIn, SignalIn
from trajectree.repo.projects import get_project_by_slug, list_projects
from trajectree.repo.resources import project_for_path, upsert_resource
from trajectree.repo.signals import upsert_signals
from trajectree.repo.state import get_state, set_state
from trajectree.timeutil import from_db, to_db

SLURM_ACTOR = Actor(kind="connector", ref="slurm", tool="slurm")
TERMINAL_STATES = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED"}
)
COMMAND_TIMEOUT_S = 30


class SlurmJob(BaseModel):
    job_id: str
    name: str
    state: str
    workdir: str | None
    partition: str | None
    user: str | None
    submit: AwareDatetime | None
    start: AwareDatetime | None
    end: AwareDatetime | None


class SlurmCollection(BaseModel):
    jobs: list[SlurmJob]
    errors: list[str]


class SlurmReport(BaseModel):
    jobs: int
    resources: int
    signals_written: int
    errors: list[str]


def _num(value: object) -> int | None:
    """An int from a plain number, a `{"set": true, "number": n}` dict or a digit string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        if not value.get("set") or value.get("infinite"):
            return None
        return _num(value.get("number"))
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _states(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _epoch(value: object) -> datetime | None:
    n = _num(value)
    if not n:
        return None
    try:
        return datetime.fromtimestamp(n, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _state(value: object) -> str:
    states = _states(value)
    words = states[0].split() if states else []
    return words[0].upper() if words else "UNKNOWN"  # "CANCELLED by 1000" -> CANCELLED


def _jobs(text: str) -> list[dict]:
    data = json.loads(text)  # JSONDecodeError is a ValueError
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError("not a Slurm job listing")
    return [j for j in data["jobs"] if isinstance(j, dict) and _num(j.get("job_id")) is not None]


def parse_squeue(text: str) -> list[SlurmJob]:
    return [
        SlurmJob(
            job_id=str(_num(j.get("job_id"))),
            name=str(j.get("name") or ""),
            state=_state(j.get("job_state")),
            workdir=_text(j.get("current_working_directory")),
            partition=_text(j.get("partition")),
            user=_text(j.get("user_name")),
            submit=_epoch(j.get("submit_time")),
            start=_epoch(j.get("start_time")),
            end=_epoch(j.get("end_time")),
        )  # fmt: skip
        for j in _jobs(text)
    ]


def parse_sacct(text: str) -> list[SlurmJob]:
    out: list[SlurmJob] = []
    for j in _jobs(text):
        state = j.get("state")
        times = j.get("time")
        times = times if isinstance(times, dict) else {}
        out.append(
            SlurmJob(
                job_id=str(_num(j.get("job_id"))),
                name=str(j.get("name") or ""),
                state=_state(state.get("current") if isinstance(state, dict) else state),
                workdir=_text(j.get("working_directory")),
                partition=_text(j.get("partition")),
                user=_text(j.get("user")),
                submit=_epoch(times.get("submission")),
                start=_epoch(times.get("start")),
                end=_epoch(times.get("end")),
            )  # fmt: skip
        )
    return out


def ssh_command(settings: Settings, remote: list[str]) -> list[str]:
    target = f"{settings.slurm_user}@{settings.slurm_host}" if settings.slurm_user else settings.slurm_host
    return [
        settings.ssh_bin, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        *(["-i", str(settings.slurm_key)] if settings.slurm_key else []),
        str(target), *remote,
    ]  # fmt: skip


def collect_slurm(settings: Settings, *, since: datetime, run=subprocess.run) -> SlurmCollection:
    """Run squeue and sacct over ssh (never under the ledger lock) and merge the jobs by id."""
    commands = (
        ("squeue", ["squeue", "--me", "--json"], parse_squeue),
        ("sacct", ["sacct", "--json", "-X", "-S", since.strftime("%Y-%m-%dT%H:%M:%S")], parse_sacct),
    )
    merged: dict[str, SlurmJob] = {}
    errors: list[str] = []
    for name, remote, parse in commands:
        try:
            proc = run(
                ssh_command(settings, remote), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=COMMAND_TIMEOUT_S,
            )  # fmt: skip
            if proc.returncode != 0:
                tail = (proc.stderr or "").strip()[-200:]
                errors.append(f"{name}: {tail or f'exit {proc.returncode}'}")
                continue
            jobs = parse(proc.stdout)
        except subprocess.TimeoutExpired:
            errors.append(f"{name}: timed out after {COMMAND_TIMEOUT_S}s")
            continue
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: {(str(exc) or type(exc).__name__)[:200]}")
            continue
        for job in jobs:
            if name == "squeue" or job.job_id not in merged:  # squeue is first, so it wins
                merged[job.job_id] = job
    return SlurmCollection(jobs=list(merged.values()), errors=errors)


def _under(path: str, prefix: str) -> bool:
    target, root = PurePosixPath(path), PurePosixPath(prefix)
    return target == root or target.is_relative_to(root)


def project_for_job(conn: apsw.Connection, job: SlurmJob) -> Project | None:
    """The project a job belongs to: workdir under a folder/git_local resource, then under a
    project's `slurm_workdirs`, then a project whose slug starts the job name, then `inbox`."""
    if job.workdir:
        found = project_for_path(conn, job.workdir)
        if found is not None:
            return found
    projects = list_projects(conn)
    if job.workdir:
        for project in projects:
            prefixes = project.props.get("slurm_workdirs")
            if isinstance(prefixes, list) and any(
                isinstance(p, str) and p and _under(job.workdir, p) for p in prefixes
            ):
                return project
    name = job.name.lower()
    by_slug = [p for p in projects if name.startswith(p.slug)]
    if by_slug:
        return max(by_slug, key=lambda p: len(p.slug))
    return get_project_by_slug(conn, "inbox")


def _t_start(job: SlurmJob, now: datetime) -> datetime:
    if job.state in TERMINAL_STATES:
        return job.end or now
    if job.state == "RUNNING":
        return job.start or now
    return job.submit or now


def write_slurm(
    ledger: Ledger, settings: Settings, collection: SlurmCollection, *, now: datetime
) -> SlurmReport:
    """Upsert an auto-accepted `slurm_job` resource per attachable job, then its signals."""
    host = settings.slurm_host
    with ledger.locked() as conn:
        placed = [(job, project_for_job(conn, job)) for job in collection.jobs]
    signals: list[SignalIn] = []
    resources = 0
    for job, project in placed:
        if project is None:
            continue
        resource = upsert_resource(
            ledger,
            SLURM_ACTOR,
            ResourceIn(
                project_id=project.id, kind="slurm_job", locator=f"{host}:{job.job_id}", title=job.name,
                props={"state": job.state, "workdir": job.workdir, "partition": job.partition},
            ),
            intent=f"slurm job {job.job_id} {job.state}",
            auto_accept=True,
        )  # fmt: skip
        resources += 1
        signals.append(
            SignalIn(
                project_id=project.id,
                resource_id=resource.id,
                kind="job_state",
                t_start=_t_start(job, now),
                dedupe_key=f"slurm:{host}:{job.job_id}:{job.state}",
                payload={
                    "job_id": job.job_id, "name": job.name, "state": job.state,
                    "partition": job.partition, "workdir": job.workdir,
                },  # fmt: skip
            )
        )
    with ledger.locked() as conn:
        written = upsert_signals(conn, signals)
        with conn:
            set_state(conn, "slurm:last", {"at": to_db(now)})
    return SlurmReport(
        jobs=len(collection.jobs), resources=resources, signals_written=written, errors=collection.errors
    )


def slurm_due(conn: apsw.Connection, *, now: datetime, every_m: int) -> bool:
    """True when no poll is recorded or the last one is `every_m` minutes old (0 = never)."""
    if every_m <= 0:
        return False
    state = get_state(conn, "slurm:last")
    if not isinstance(state, dict) or not isinstance(state.get("at"), str):
        return True
    try:
        return now - from_db(state["at"]) >= timedelta(minutes=every_m)
    except ValueError:
        return True

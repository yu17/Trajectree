"""GitHub and GitLab connector: one `remote` signal per (repository, UTC day) of PR/MR, issue
and release activity, plus a pending milestone for each published release.

Three phases, like the git connector, so no HTTP request runs while the ledger lock is held:
read the resources and cursors (`read_remote_states`), fetch with no connection
(`collect_remote`), then write signals, cursors and milestones (`write_remote`). A failing
repository (bad token, rate limit, timeout) becomes one `errors` entry and is not marked polled;
it never stops the others. Tokens come from `runner.credentials.load_connector_token` and are
never logged or put in an error message.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote, urlparse

import apsw
import httpx
from pydantic import AwareDatetime, BaseModel

from trajectree.config import Settings
from trajectree.connectors.local_git import _since
from trajectree.db.ledger import Ledger
from trajectree.models import Actor, EventIn, Resource, SignalIn
from trajectree.repo.events import create_event, list_events
from trajectree.repo.signals import upsert_signals
from trajectree.repo.state import get_state, set_state
from trajectree.runner.credentials import load_connector_token
from trajectree.timeutil import from_db, to_db

REMOTE_ACTOR = Actor(kind="connector", ref="remote", tool="remote")
MAX_LISTED = 20  # items of each kind kept in one day's signal payload
_SCP = re.compile(r"^[\w.-]+@([\w.-]+):(.+)$")


class RemoteRepo(BaseModel):
    kind: Literal["github", "gitlab"]
    host: str
    path: str
    api_base: str
    web_url: str


class RemoteItem(BaseModel):
    kind: Literal["pr", "issue", "release"]
    number: int | None
    title: str
    state: str
    url: str
    updated: AwareDatetime
    tag: str | None = None


class ReleaseFound(BaseModel):
    project_id: str
    resource_id: str
    tag: str
    title: str
    url: str
    published: AwareDatetime


class RemoteCollection(BaseModel):
    signals: list[SignalIn]
    releases: list[ReleaseFound]
    polled: list[str]  # resource ids whose cursor advances
    errors: list[str]


class RemoteReport(BaseModel):
    repos: int
    signals_written: int
    milestones_created: int
    errors: list[str]


def parse_remote_url(url: str) -> RemoteRepo | None:
    """The repository behind a git remote URL, or None for local paths and unparsable strings.

    Any host other than github.com is treated as GitLab.
    """
    url = url.strip()
    if "://" in url:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https", "ssh", "git"):
            return None
        host, path = parsed.hostname or "", parsed.path
    else:
        match = _SCP.match(url)
        if match is None:
            return None
        host, path = match.group(1), match.group(2)
    path = path.strip("/").removesuffix(".git").strip("/")
    host = host.lower()
    if not host or len(path.split("/")) < 2 or not all(path.split("/")):
        return None
    if host == "github.com":
        return RemoteRepo(
            kind="github", host=host, path=path,
            api_base="https://api.github.com", web_url=f"https://{host}/{path}",
        )  # fmt: skip
    return RemoteRepo(
        kind="gitlab", host=host, path=path,
        api_base=f"https://{host}/api/v4", web_url=f"https://{host}/{path}",
    )  # fmt: skip


def normalize_remote(url: str) -> str | None:
    repo = parse_remote_url(url)
    return repo.web_url if repo else None


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _rows(client: httpx.Client, url: str, params: dict[str, object], headers: dict[str, str]) -> list[dict]:
    response = client.get(url, params=params, headers=headers)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("unexpected response body")
    return [row for row in data if isinstance(row, dict)]


def _item(
    kind: Literal["pr", "issue", "release"], row: dict, *, number: str, url: str, updated: str,
    since: datetime, tag: str | None = None, title: str | None = None,
) -> RemoteItem | None:  # fmt: skip
    when = _when(row.get(updated))
    n = row.get(number)
    link = row.get(url)
    if when is None or when < since or not isinstance(link, str):
        return None
    return RemoteItem(
        kind=kind,
        number=n if isinstance(n, int) and not isinstance(n, bool) else None,
        title=str(row.get("title") or "") if title is None else title,
        state=str(row.get("state") or ""),
        url=link,
        updated=when,
        tag=tag,
    )


def _release(row: dict, *, url: str | None, stamp: str, since: datetime) -> RemoteItem | None:
    tag = row.get("tag_name")
    when = _when(row.get(stamp))
    if not isinstance(tag, str) or not url or when is None or when < since:
        return None
    return RemoteItem(
        kind="release", number=None, title=str(row.get("name") or tag), state="released",
        url=url, updated=when, tag=tag,
    )  # fmt: skip


def fetch_github(
    client: httpx.Client, repo: RemoteRepo, token: str | None, *, since: datetime
) -> list[RemoteItem]:
    """Recent pull requests, issues (pull requests removed) and releases of a GitHub repository."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    base = f"{repo.api_base}/repos/{repo.path}"
    items: list[RemoteItem | None] = []
    pulls = _rows(
        client, f"{base}/pulls",
        {"state": "all", "sort": "updated", "direction": "desc", "per_page": 50}, headers,
    )  # fmt: skip
    items += [
        _item("pr", r, number="number", url="html_url", updated="updated_at", since=since) for r in pulls
    ]
    issues = _rows(
        client, f"{base}/issues", {"state": "all", "since": since.isoformat(), "per_page": 50}, headers
    )  # fmt: skip
    items += [
        _item("issue", r, number="number", url="html_url", updated="updated_at", since=since)
        for r in issues
        if "pull_request" not in r
    ]
    for r in _rows(client, f"{base}/releases", {"per_page": 20}, headers):
        url = r.get("html_url")
        items.append(
            _release(r, url=url if isinstance(url, str) else None, stamp="published_at", since=since)
        )
    return [i for i in items if i is not None]


def fetch_gitlab(
    client: httpx.Client, repo: RemoteRepo, token: str | None, *, since: datetime
) -> list[RemoteItem]:
    """Recent merge requests, issues and releases of a GitLab project."""
    headers = {"PRIVATE-TOKEN": token} if token else {}
    base = f"{repo.api_base}/projects/{quote(repo.path, safe='')}"
    after = since.isoformat()
    items: list[RemoteItem | None] = []
    mrs = _rows(client, f"{base}/merge_requests", {"state": "all", "updated_after": after, "per_page": 50}, headers)
    items += [_item("pr", r, number="iid", url="web_url", updated="updated_at", since=since) for r in mrs]
    issues = _rows(client, f"{base}/issues", {"updated_after": after, "per_page": 50}, headers)
    items += [_item("issue", r, number="iid", url="web_url", updated="updated_at", since=since) for r in issues]
    for r in _rows(client, f"{base}/releases", {"per_page": 20}, headers):
        links = r.get("_links")
        url = links.get("self") if isinstance(links, dict) else None
        if not isinstance(url, str):
            url = f"{repo.web_url}/-/releases/{r.get('tag_name')}"
        items.append(_release(r, url=url, stamp="released_at", since=since))
    return [i for i in items if i is not None]


def _state_key(resource_id: str) -> str:
    return f"remote:{resource_id}"


def read_remote_states(conn: apsw.Connection, resources: list[Resource]) -> dict[str, dict | None]:
    """Each resource's stored poll cursor, keyed by resource id."""
    return {r.id: get_state(conn, _state_key(r.id)) for r in resources}


def _signals(resource: Resource, items: list[RemoteItem]) -> list[SignalIn]:
    days: dict[str, list[RemoteItem]] = {}
    for item in items:
        days.setdefault(item.updated.astimezone(timezone.utc).date().isoformat(), []).append(item)
    out: list[SignalIn] = []
    for day, group in sorted(days.items()):
        stamps = [i.updated for i in group]
        by_kind = {k: [i for i in group if i.kind == k] for k in ("pr", "issue", "release")}
        out.append(
            SignalIn(
                project_id=resource.project_id,
                resource_id=resource.id,
                kind="remote",
                t_start=min(stamps),
                t_end=max(stamps),
                dedupe_key=f"remote:{resource.id}:{day}",
                payload={
                    "count": len(group),
                    "prs": [
                        {"number": i.number, "title": i.title, "state": i.state, "url": i.url}
                        for i in by_kind["pr"][:MAX_LISTED]
                    ],
                    "issues": [
                        {"number": i.number, "title": i.title, "state": i.state, "url": i.url}
                        for i in by_kind["issue"][:MAX_LISTED]
                    ],
                    "releases": [
                        {"tag": i.tag, "title": i.title, "url": i.url} for i in by_kind["release"][:MAX_LISTED]
                    ],
                },
            )
        )
    return out


def collect_remote(
    settings: Settings,
    resources: list[Resource],
    states: dict[str, dict | None],
    *,
    now: datetime,
    since_days: int,
    client: httpx.Client | None = None,
) -> RemoteCollection:
    """Fetch each git_remote resource's recent activity and build its signals; no DB access."""
    signals: list[SignalIn] = []
    releases: list[ReleaseFound] = []
    polled: list[str] = []
    errors: list[str] = []
    tokens: dict[str, str | None] = {}
    own_client = client is None
    http = client if client is not None else httpx.Client(timeout=20)
    try:
        for resource in resources:
            if resource.kind != "git_remote":
                continue
            repo = parse_remote_url(resource.locator)
            if repo is None:
                errors.append(f"{resource.locator}: not a recognised git remote URL")
                continue
            if repo.kind not in tokens:
                tokens[repo.kind] = load_connector_token(settings, repo.kind)
            since = _since(states.get(resource.id), now, since_days)
            fetch = fetch_github if repo.kind == "github" else fetch_gitlab
            try:
                items = fetch(http, repo, tokens[repo.kind], since=since)
            except httpx.HTTPStatusError as exc:
                errors.append(f"{resource.locator}: {exc.response.status_code}")
                continue
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{resource.locator}: {str(exc) or type(exc).__name__}")
                continue
            signals += _signals(resource, items)
            releases += [
                ReleaseFound(
                    project_id=resource.project_id, resource_id=resource.id, tag=i.tag or i.title,
                    title=i.title, url=i.url, published=i.updated,
                )  # fmt: skip
                for i in items
                if i.kind == "release"
            ]
            polled.append(resource.id)
    finally:
        if own_client:
            http.close()
    return RemoteCollection(signals=signals, releases=releases, polled=polled, errors=errors)


def write_remote(ledger: Ledger, collection: RemoteCollection, *, now: datetime) -> tuple[int, int]:
    """Upsert signals, advance cursors and stamp `remote:last` in one transaction, then add one
    pending milestone per release not already recorded. Returns (signals written, milestones)."""
    created = 0
    with ledger.locked() as conn:
        with conn:
            written = upsert_signals(conn, collection.signals)
            for rid in collection.polled:
                set_state(conn, _state_key(rid), {"last_poll": to_db(now)})
            set_state(conn, "remote:last", {"at": to_db(now)})
        known: dict[str, set[str]] = {}
        for rel in collection.releases:
            if rel.project_id not in known:
                known[rel.project_id] = {
                    str(e.props.get("release_url"))
                    for e in list_events(conn, project_ids=[rel.project_id], kinds=["milestone"])
                    if e.props.get("release_url")
                }
            if rel.url in known[rel.project_id]:
                continue
            create_event(
                ledger,
                REMOTE_ACTOR,
                EventIn(
                    project_id=rel.project_id,
                    kind="milestone",
                    title=f"Release {rel.tag}",
                    body_md=rel.title,
                    t_start=rel.published,
                    status="happened",
                    source="connector",
                    props={"release_url": rel.url, "tag": rel.tag},
                ),
                intent=f"release {rel.tag} published",
            )
            known[rel.project_id].add(rel.url)
            created += 1
    return written, created


def remote_due(conn: apsw.Connection, *, now: datetime, every_m: int) -> bool:
    """True when no poll is recorded or the last one is `every_m` minutes old (0 = never)."""
    if every_m <= 0:
        return False
    state = get_state(conn, "remote:last")
    if not isinstance(state, dict) or not isinstance(state.get("at"), str):
        return True
    try:
        return now - from_db(state["at"]) >= timedelta(minutes=every_m)
    except ValueError:
        return True

from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx

from trajectree.connectors.remote import (
    ReleaseFound,
    RemoteCollection,
    RemoteRepo,
    collect_remote,
    fetch_github,
    fetch_gitlab,
    normalize_remote,
    parse_remote_url,
    read_remote_states,
    remote_due,
    write_remote,
)
from trajectree.models import ProjectIn, ResourceIn
from trajectree.repo.events import list_events
from trajectree.repo.projects import create_project
from trajectree.repo.resources import list_resources, upsert_resource
from trajectree.repo.signals import list_signals
from trajectree.repo.state import get_state
from trajectree.runner.credentials import store_connector_token

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def start_30d(resources):
    return {r.id: datetime(2026, 9, 22, tzinfo=UTC) for r in resources}


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_remote_urls():
    assert parse_remote_url("git@github.com:yu17/trajectree.git") == RemoteRepo(
        kind="github", host="github.com", path="yu17/trajectree",
        api_base="https://api.github.com", web_url="https://github.com/yu17/trajectree",
    )  # fmt: skip
    assert parse_remote_url("https://github.com/o/r").path == "o/r"
    assert parse_remote_url("https://github.com/o/r.git").path == "o/r"
    lab = parse_remote_url("https://gitlab.uni.edu/lab/sub/proj.git", gitlab_host="gitlab.uni.edu")
    assert lab.kind == "gitlab" and lab.api_base == "https://gitlab.uni.edu/api/v4"
    # without a configured GitLab host (or on another host) a non-GitHub remote is `other`
    unknown = parse_remote_url("https://gitlab.uni.edu/lab/sub/proj.git")
    assert unknown.kind == "other" and unknown.api_base is None and unknown.web_url == "https://gitlab.uni.edu/lab/sub/proj"
    assert parse_remote_url("https://evil.example/a/b", gitlab_host="gitlab.uni.edu").kind == "other"
    assert parse_remote_url("git@github.com:o/r.git", gitlab_host="gitlab.uni.edu").kind == "github"
    ssh = parse_remote_url("ssh://git@host/g/sub/r.git", gitlab_host="host")
    assert ssh.kind == "gitlab" and ssh.path == "g/sub/r" and ssh.web_url == "https://host/g/sub/r"
    ported = parse_remote_url("https://gitlab.lab:8443/g/r", gitlab_host="gitlab.lab:8443")
    assert ported.kind == "gitlab"
    assert ported.api_base == "https://gitlab.lab:8443/api/v4" and ported.web_url == "https://gitlab.lab:8443/g/r"
    assert normalize_remote("https://evil.example/a/b.git") == "https://evil.example/a/b"  # still suggested
    assert parse_remote_url("https://h:abc/x") is None and normalize_remote("https://h:abc/x") is None
    assert parse_remote_url("https://[bad/x") is None
    assert parse_remote_url("file:///tmp/x") is None
    assert parse_remote_url("/tmp/x") is None
    assert parse_remote_url("nonsense") is None
    assert normalize_remote("git@github.com:o/r.git") == "https://github.com/o/r"
    assert normalize_remote("/tmp/x") is None


def test_fetch_github_filters_prs_from_issues():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer ghp_x"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        q = parse_qs(request.url.query.decode())
        if request.url.path == "/repos/o/r/pulls":
            assert q["state"] == ["all"] and q["sort"] == ["updated"] and q["direction"] == ["desc"]
            assert q["per_page"] == ["50"]
            return httpx.Response(200, json=[
                {"number": 1, "title": "PR one", "state": "open", "html_url": "https://github.com/o/r/pull/1", "updated_at": "2026-10-20T10:00:00Z"},
                {"number": 0, "title": "old", "state": "closed", "html_url": "u", "updated_at": "2020-01-01T00:00:00Z"},
            ])  # fmt: skip
        if request.url.path == "/repos/o/r/issues":
            assert q["since"] == ["2026-10-01T00:00:00+00:00"] and q["state"] == ["all"]
            return httpx.Response(200, json=[
                {"number": 2, "title": "Issue two", "state": "open", "html_url": "https://github.com/o/r/issues/2", "updated_at": "2026-10-21T10:00:00Z"},
                {"number": 1, "title": "PR one", "state": "open", "html_url": "x", "updated_at": "2026-10-20T10:00:00Z", "pull_request": {}},
            ])  # fmt: skip
        if request.url.path == "/repos/o/r/releases":
            assert q["per_page"] == ["20"]
            return httpx.Response(200, json=[
                {"tag_name": "v1", "name": "One", "html_url": "https://github.com/o/r/releases/tag/v1", "published_at": "2026-10-19T08:00:00Z"},
            ])  # fmt: skip
        raise AssertionError(request.url)

    repo = parse_remote_url("https://github.com/o/r")
    since = datetime(2026, 10, 1, tzinfo=UTC)
    items = fetch_github(client_for(handler), repo, "ghp_x", since=since)
    assert sorted(seen) == ["/repos/o/r/issues", "/repos/o/r/pulls", "/repos/o/r/releases"]
    by_kind = {(i.kind, i.number or i.tag): i for i in items}
    assert set(by_kind) == {("pr", 1), ("issue", 2), ("release", "v1")}
    assert by_kind[("release", "v1")].title == "One" and by_kind[("issue", 2)].url.endswith("/issues/2")
    assert by_kind[("pr", 1)].updated == datetime(2026, 10, 20, 10, tzinfo=UTC)


def test_fetch_github_without_token_sends_no_authorization():
    def handler(request):
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=[])

    assert fetch_github(client_for(handler), parse_remote_url("https://github.com/o/r"), None, since=NOW) == []


def test_fetch_gitlab_uses_private_token_and_encoded_path():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode()
        seen.append(raw.split("?")[0])
        assert request.headers["PRIVATE-TOKEN"] == "glpat"
        assert raw.startswith("/api/v4/projects/lab%2Fsub%2Fproj/")
        q = parse_qs(request.url.query.decode())
        if raw.split("?")[0].endswith("/merge_requests"):
            assert q["state"] == ["all"] and q["updated_after"] == ["2026-10-01T00:00:00+00:00"] and q["per_page"] == ["50"]
            return httpx.Response(200, json=[{"iid": 4, "title": "MR", "state": "merged", "web_url": "https://h/mr/4", "updated_at": "2026-10-20T10:00:00.000Z"}])
        if raw.split("?")[0].endswith("/issues"):
            assert q["updated_after"] == ["2026-10-01T00:00:00+00:00"]
            return httpx.Response(200, json=[{"iid": 5, "title": "Iss", "state": "opened", "web_url": "https://h/i/5", "updated_at": "2026-10-20T11:00:00Z"}])
        assert raw.split("?")[0].endswith("/releases")
        return httpx.Response(200, json=[
            {"tag_name": "v2", "name": "Two", "_links": {"self": "https://h/lab/sub/proj/-/releases/v2"}, "released_at": "2026-10-18T00:00:00Z"},
            {"tag_name": "v3", "name": "Three", "released_at": "2026-10-17T00:00:00Z"},
        ])  # fmt: skip

    repo = parse_remote_url("https://gitlab.uni.edu/lab/sub/proj.git", gitlab_host="gitlab.uni.edu")
    items = fetch_gitlab(client_for(handler), repo, "glpat", since=datetime(2026, 10, 1, tzinfo=UTC))
    assert len(seen) == 3
    by = {(i.kind, i.number or i.tag): i for i in items}
    assert set(by) == {("pr", 4), ("issue", 5), ("release", "v2"), ("release", "v3")}
    assert by[("release", "v2")].url == "https://h/lab/sub/proj/-/releases/v2"
    assert by[("release", "v3")].url == "https://gitlab.uni.edu/lab/sub/proj/-/releases/v3"


def mk_remote(ledger, human, project, url):
    return upsert_resource(ledger, human, ResourceIn(project_id=project.id, kind="git_remote", locator=url))


def test_collect_remote_isolates_failures(ledger, human, settings):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    r401 = mk_remote(ledger, human, p, "https://github.com/o/a")
    r403 = mk_remote(ledger, human, p, "https://github.com/o/b")
    rto = mk_remote(ledger, human, p, "https://github.com/o/c")
    rok = mk_remote(ledger, human, p, "https://github.com/o/d")
    rbad = mk_remote(ledger, human, p, "file:///tmp/x")
    store_connector_token(settings, "github", "ghp_x")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/repos/o/a/"):
            return httpx.Response(401, json={})
        if path.startswith("/repos/o/b/"):
            return httpx.Response(403, json={})
        if path.startswith("/repos/o/c/"):
            raise httpx.ReadTimeout("slow", request=request)
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[
                {"number": 1, "title": "P1", "state": "open", "html_url": "u1", "updated_at": "2026-10-20T10:00:00Z"},
                {"number": 2, "title": "P2", "state": "open", "html_url": "u2", "updated_at": "2026-10-20T15:00:00Z"},
            ])  # fmt: skip
        if path.endswith("/issues"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{"tag_name": "v1", "name": "One", "html_url": "ru", "published_at": "2026-10-21T08:00:00Z"}])

    resources = list_resources(ledger.conn, kinds=["git_remote", "git_local"])
    states = read_remote_states(ledger.conn, resources)
    col = collect_remote(settings, resources, states, now=NOW, starts=start_30d(resources), client=client_for(handler))
    assert len(col.errors) == 4
    joined = "\n".join(col.errors)
    assert "https://github.com/o/a: 401" in joined and "https://github.com/o/b: 403" in joined
    assert "https://github.com/o/c:" in joined and "file:///tmp/x:" in joined
    assert col.polled == [rok.id]
    days = {s.dedupe_key: s for s in col.signals}
    assert set(days) == {f"remote:{rok.id}:2026-10-20", f"remote:{rok.id}:2026-10-21"}
    d20 = days[f"remote:{rok.id}:2026-10-20"]
    assert d20.kind == "remote" and d20.payload["count"] == 2 and [x["number"] for x in d20.payload["prs"]] == [1, 2]
    assert d20.t_start == datetime(2026, 10, 20, 10, tzinfo=UTC) and d20.t_end == datetime(2026, 10, 20, 15, tzinfo=UTC)
    d21 = days[f"remote:{rok.id}:2026-10-21"]
    assert d21.payload["releases"] == [{"tag": "v1", "title": "One", "url": "ru"}]
    assert [r.tag for r in col.releases] == ["v1"] and col.releases[0].resource_id == rok.id
    assert rbad.id not in col.polled and r401.id not in col.polled
    assert "ghp_x" not in joined and all("ghp_x" not in str(s.payload) for s in col.signals)


def test_collect_remote_sends_gitlab_token_only_to_the_configured_host(ledger, human, settings):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    evil = mk_remote(ledger, human, p, "https://evil.example/o/r.git")
    lab = mk_remote(ledger, human, p, "https://gitlab.uni.edu/lab/proj.git")
    store_connector_token(settings, "gitlab", "glpat-secret")
    hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        assert request.url.host == "gitlab.uni.edu" and request.headers["PRIVATE-TOKEN"] == "glpat-secret"
        return httpx.Response(200, json=[])

    resources = list_resources(ledger.conn, kinds=["git_remote"])
    configured = settings.model_copy(update={"gitlab_host": "gitlab.uni.edu"})
    col = collect_remote(configured, resources, {}, now=NOW, starts=start_30d(resources), client=client_for(handler))
    assert set(hosts) == {"gitlab.uni.edu"} and len(hosts) == 3
    assert col.errors == ["https://evil.example/o/r.git: unknown host, not polled"]
    assert col.polled == [lab.id] and evil.id not in col.polled
    # with no GitLab host configured, nothing at all is requested
    hosts.clear()
    col = collect_remote(settings, resources, {}, now=NOW, starts=start_30d(resources), client=client_for(handler))
    assert hosts == [] and col.polled == [] and len(col.errors) == 2
    assert all(e.endswith(": unknown host, not polled") for e in col.errors)


def test_first_poll_uses_git_start(ledger, human, settings):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    r = mk_remote(ledger, human, p, "https://github.com/o/a")
    sinces = []

    def handler(request):
        if request.url.path.endswith("/issues"):
            sinces.append(parse_qs(request.url.query.decode())["since"][0])
        return httpx.Response(200, json=[])

    resources = list_resources(ledger.conn, kinds=["git_remote"])
    start = datetime(2024, 3, 1, 6, tzinfo=UTC)
    collect_remote(settings, resources, {}, now=NOW, starts={r.id: start}, client=client_for(handler))
    collect_remote(settings, resources, {}, now=NOW, starts={}, client=client_for(handler))
    assert sinces == ["2024-03-01T06:00:00+00:00", "1970-01-01T00:00:00+00:00"]


def test_collect_remote_since_uses_cursor_and_caps(ledger, human, settings):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    r = mk_remote(ledger, human, p, "https://github.com/o/a")
    sinces = []

    def handler(request):
        if request.url.path.endswith("/issues"):
            sinces.append(parse_qs(request.url.query.decode())["since"][0])
            return httpx.Response(200, json=[
                {"number": n, "title": f"I{n}", "state": "open", "html_url": "u", "updated_at": "2026-10-20T10:00:00Z"} for n in range(1, 26)
            ])  # fmt: skip
        return httpx.Response(200, json=[])

    resources = list_resources(ledger.conn, kinds=["git_remote"])
    col = collect_remote(settings, resources, {r.id: None}, now=NOW, starts=start_30d(resources), client=client_for(handler))
    assert sinces == ["2026-09-22T00:00:00+00:00"]
    assert col.signals[0].payload["count"] == 25 and len(col.signals[0].payload["issues"]) == 20
    collect_remote(settings, resources, {r.id: {"last_poll": "2026-10-20T12:00:00+00:00"}}, now=NOW, starts=start_30d(resources), client=client_for(handler))
    assert sinces[1] == "2026-10-19T00:00:00+00:00"


def test_write_remote_creates_pending_milestone_once(ledger, human, settings):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    r = mk_remote(ledger, human, p, "https://github.com/o/a")
    rel = ReleaseFound(project_id=p.id, resource_id=r.id, tag="v1", title="First", url="https://x/v1", published=datetime(2026, 10, 19, tzinfo=UTC))
    col = RemoteCollection(signals=[], releases=[rel], polled=[r.id], errors=[])
    assert write_remote(ledger, col, now=NOW) == (0, 1)
    events = list_events(ledger.conn, project_ids=[p.id], kinds=["milestone"])
    assert len(events) == 1 and events[0].title == "Release v1" and events[0].props["release_url"] == "https://x/v1"
    assert events[0].source == "connector" and events[0].body_md == "First"
    assert ledger.list_txns(review="pending")[0].intent == "release v1 published"
    assert get_state(ledger.conn, f"remote:{r.id}")["last_poll"]
    assert write_remote(ledger, col, now=NOW) == (0, 0)
    assert len(list_events(ledger.conn, project_ids=[p.id], kinds=["milestone"])) == 1


def test_write_remote_upserts_signals(ledger, human, settings):
    p = create_project(ledger, human, ProjectIn(slug="a", title="A", kind="research"))
    r = mk_remote(ledger, human, p, "https://github.com/o/a")

    def handler(request):
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[{"number": 1, "title": "P", "state": "open", "html_url": "u", "updated_at": "2026-10-20T10:00:00Z"}])
        return httpx.Response(200, json=[])

    resources = list_resources(ledger.conn, kinds=["git_remote"])
    col = collect_remote(settings, resources, {}, now=NOW, starts=start_30d(resources), client=client_for(handler))
    assert write_remote(ledger, col, now=NOW) == (1, 0)
    assert len(list_signals(ledger.conn, project_ids=[p.id], kinds=["remote"])) == 1


def test_remote_due(ledger):
    from trajectree.repo.state import set_state

    assert remote_due(ledger.conn, now=NOW, every_m=30)
    assert not remote_due(ledger.conn, now=NOW, every_m=0)
    with ledger.conn:
        set_state(ledger.conn, "remote:last", {"at": NOW.isoformat()})
    assert not remote_due(ledger.conn, now=NOW, every_m=30)
    later = datetime(2026, 10, 22, 12, 31, tzinfo=UTC)
    assert remote_due(ledger.conn, now=later, every_m=30)

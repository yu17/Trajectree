from types import SimpleNamespace

from trajectree.cli.init_project import slug_from_path
from trajectree.cli.main import main


class FakeClaude:
    def __init__(self, registered=False):
        self.registered = registered
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:3] == ["claude", "mcp", "get"]:
            return SimpleNamespace(returncode=0 if self.registered else 1, stdout="", stderr="")
        self.registered = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def env_for(app):
    return {"TRAJECTREE_TOKEN": app.state.token, "TRAJECTREE_URL": "http://testserver"}


def make_repo(tmp_path):
    repo = tmp_path / "My Proj"
    (repo / ".git" / "info").mkdir(parents=True)
    return repo


def test_slug_from_path(tmp_path):
    assert slug_from_path(tmp_path / "My Proj_v2") == "my-proj-v2"
    assert slug_from_path(tmp_path / "---x---") == "x"


def test_init_project_creates_project_resources_rules_and_mcp(tmp_path, client, app, capsys):
    repo = make_repo(tmp_path)
    fake = FakeClaude()
    assert main(["init-project", str(repo)], client=client, env=env_for(app), runner=fake) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("project: my-proj (") and out[1] == "resources: folder, git_local" and out[3] == "mcp: configured"
    assert client.get("/api/projects").json()[0]["slug"] == "my-proj"
    kinds = {r["kind"]: r["locator"] for r in client.get("/api/resources").json()}
    assert kinds == {"folder": str(repo.resolve()), "git_local": str(repo.resolve())}
    rules = (repo / ".claude" / "rules" / "trajectree.md").read_text()
    assert rules.startswith("<!-- managed by trajectree init-project; edits are overwritten -->") and "my-proj" in rules and "record_event" in rules
    assert (repo / ".git" / "info" / "exclude").read_text().count(".claude/rules/trajectree.md") == 1
    assert fake.calls == [["claude", "mcp", "get", "trajectree"],
                          ["claude", "mcp", "add", "--scope", "user", "trajectree", "--", "trajectree", "mcp"]]


def test_init_project_twice_is_idempotent(tmp_path, client, app, capsys):
    repo = make_repo(tmp_path)
    fake = FakeClaude()
    main(["init-project", str(repo)], client=client, env=env_for(app), runner=fake)
    (repo / ".claude" / "rules" / "trajectree.md").write_text("edited")
    assert main(["init-project", str(repo)], client=client, env=env_for(app), runner=fake) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "mcp: already configured"
    assert len(client.get("/api/projects").json()) == 1 and len(client.get("/api/resources").json()) == 2
    assert (repo / ".git" / "info" / "exclude").read_text().count(".claude/rules/trajectree.md") == 1
    assert (repo / ".claude" / "rules" / "trajectree.md").read_text() != "edited"
    assert fake.calls[-1] == ["claude", "mcp", "get", "trajectree"]


def test_init_project_without_git_or_claude(tmp_path, client, app, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()

    def missing(args):
        raise FileNotFoundError("claude")

    assert main(["init-project", str(plain), "--slug", "plain-one", "--title", "Plain", "--kind", "tooling"], client=client, env=env_for(app), runner=missing) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "resources: folder" and out[3].startswith("mcp: claude not found; run: claude mcp add --scope user trajectree")
    p = client.get("/api/projects").json()[0]
    assert (p["slug"], p["title"], p["kind"]) == ("plain-one", "Plain", "tooling")
    assert not (plain / ".git").exists()


def test_init_project_rejects_bad_slug(tmp_path, client, app, capsys):
    assert main(["init-project", str(tmp_path), "--slug", "Bad Slug"], client=client, env=env_for(app), runner=FakeClaude()) == 2
    assert "invalid slug" in capsys.readouterr().err


def test_init_project_reports_failed_mcp_add(tmp_path, client, app, capsys):
    def failing(args):
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    assert main(["init-project", str(tmp_path), "--slug", "xx"], client=client, env=env_for(app), runner=failing) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("mcp: failed (claude mcp add exited 1); run: claude mcp add")

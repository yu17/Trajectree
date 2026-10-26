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


def user_projects(client):
    """Projects other than the `inbox` the daemon seeds at startup."""
    return [p for p in client.get("/api/projects").json() if p["slug"] != "inbox"]


def test_slug_from_path(tmp_path):
    assert slug_from_path(tmp_path / "My Proj_v2") == "my-proj-v2"
    assert slug_from_path(tmp_path / "---x---") == "x"


def test_init_project_without_guidance_writes_no_rules_and_skips_mcp(tmp_path, client, app, capsys):
    repo = make_repo(tmp_path)
    fake = FakeClaude()
    assert main(["init-project", str(repo)], client=client, env=env_for(app), runner=fake) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("project: my-proj (") and out[1] == "resources: folder, git_local"
    assert out[2] == "guidance: not installed (trajectree guidance install <path> to add it)"
    assert user_projects(client)[0]["slug"] == "my-proj"
    kinds = {r["kind"]: r["locator"] for r in client.get("/api/resources").json()}
    assert kinds == {"folder": str(repo.resolve()), "git_local": str(repo.resolve())}
    assert not (repo / ".claude").exists()
    assert "trajectree" not in (repo / ".git" / "info" / "exclude").read_text() if (repo / ".git" / "info" / "exclude").exists() else True
    assert fake.calls == []


def test_init_project_guidance_flag_writes_rules_and_configures_mcp(tmp_path, client, app, capsys):
    repo = make_repo(tmp_path)
    fake = FakeClaude()
    assert main(["init-project", str(repo), "--guidance"], client=client, env=env_for(app), runner=fake) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("project: my-proj (") and out[1] == "resources: folder, git_local" and out[3] == "mcp: configured"
    rules = (repo / ".claude" / "rules" / "trajectree.md").read_text()
    assert rules.startswith("<!-- managed by trajectree init-project; edits are overwritten -->") and "my-proj" in rules and "record_event" in rules
    assert (repo / ".git" / "info" / "exclude").read_text().count(".claude/rules/trajectree.md") == 1
    assert fake.calls == [["claude", "mcp", "get", "trajectree"],
                          ["claude", "mcp", "add", "--scope", "user", "trajectree", "--", "trajectree", "mcp"]]


def test_init_project_interactive_asks_about_guidance_default_no(tmp_path, capsys):
    api = FakeApi(make_probe())
    code, out, err, ask = dialogue(tmp_path, capsys, api, "4", "y")  # import answered; list exhausted at guidance
    assert code == 0 and ask.prompts[-1] == (
        "Install session guidance so Claude Code sessions in this folder record milestones through Trajectree? [y/N]: ")
    assert "guidance: not installed" in out and not (tmp_path / "proj" / ".claude").exists()
    assert api.imports()  # the import was posted before the question
    code, out, err, ask = dialogue(tmp_path, capsys, FakeApi(make_probe()), "4", "n", "")
    assert code == 0 and "nothing imported" in out and "guidance: not installed" in out
    code, out, err, ask = dialogue(tmp_path, capsys, FakeApi(make_probe()), "4", "y", "y")
    assert code == 0 and "mcp: configured" in out and (tmp_path / "proj" / ".claude" / "rules" / "trajectree.md").is_file()


def test_init_project_twice_is_idempotent(tmp_path, client, app, capsys):
    repo = make_repo(tmp_path)
    fake = FakeClaude()
    main(["init-project", str(repo), "--guidance"], client=client, env=env_for(app), runner=fake)
    (repo / ".claude" / "rules" / "trajectree.md").write_text("edited")
    assert main(["init-project", str(repo), "--guidance"], client=client, env=env_for(app), runner=fake) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "mcp: already configured"
    assert len(user_projects(client)) == 1 and len(client.get("/api/resources").json()) == 2
    assert (repo / ".git" / "info" / "exclude").read_text().count(".claude/rules/trajectree.md") == 1
    assert (repo / ".claude" / "rules" / "trajectree.md").read_text() != "edited"
    assert (repo / ".claude" / "rules" / "trajectree.md").read_text().startswith("<!-- managed by trajectree init-project; edits are overwritten -->")
    assert fake.calls[-1] == ["claude", "mcp", "get", "trajectree"]


def test_init_project_without_git_or_claude(tmp_path, client, app, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()

    def missing(args):
        raise FileNotFoundError("claude")

    assert main(["init-project", str(plain), "--slug", "plain-one", "--title", "Plain", "--kind", "tooling", "--guidance"], client=client, env=env_for(app), runner=missing) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "resources: folder" and out[3].startswith("mcp: claude not found; run: claude mcp add --scope user trajectree")
    p = user_projects(client)[0]
    assert (p["slug"], p["title"], p["kind"]) == ("plain-one", "Plain", "tooling")
    assert not (plain / ".git").exists()


def test_init_project_rejects_bad_slug(tmp_path, client, app, capsys):
    assert main(["init-project", str(tmp_path), "--slug", "Bad Slug"], client=client, env=env_for(app), runner=FakeClaude()) == 2
    assert "invalid slug" in capsys.readouterr().err


def test_init_project_reports_failed_mcp_add(tmp_path, client, app, capsys):
    def failing(args):
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    assert main(["init-project", str(tmp_path), "--slug", "xx", "--sources", "none", "--guidance"], client=client, env=env_for(app), runner=failing) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("mcp: failed (claude mcp add exited 1); run: claude mcp add")


def test_init_project_rejects_missing_or_file_path(tmp_path, client, app, capsys):
    afile = tmp_path / "f.txt"
    afile.write_text("x")
    for bad in (tmp_path / "nope", afile):
        assert main(["init-project", str(bad), "--slug", "zz"], client=client, env=env_for(app), runner=FakeClaude()) == 2
        assert "not a directory" in capsys.readouterr().err
    assert user_projects(client) == []


def test_init_project_handles_git_file_pointer(tmp_path, client, app, capsys):
    gitdir = tmp_path / "gitdir"
    gitdir.mkdir()
    folder = tmp_path / "wt"
    folder.mkdir()
    (folder / ".git").write_text(f"gitdir: {gitdir}\n")
    assert main(["init-project", str(folder), "--guidance"], client=client, env=env_for(app), runner=FakeClaude()) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 4 and out[1] == "resources: folder, git_local"
    assert (gitdir / "info" / "exclude").read_text().count(".claude/rules/trajectree.md") == 1
    assert "git_local" in {r["kind"] for r in client.get("/api/resources").json()}


def test_init_project_worktree_exclude_goes_to_common_dir(tmp_path, client, app, capsys):
    common = tmp_path / "main" / ".git"
    gitdir = common / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    folder = tmp_path / "wt"
    folder.mkdir()
    (folder / ".git").write_text(f"gitdir: {gitdir}\n")
    assert main(["init-project", str(folder), "--guidance"], client=client, env=env_for(app), runner=FakeClaude()) == 0
    assert (common / "info" / "exclude").read_text().count(".claude/rules/trajectree.md") == 1
    assert not (gitdir / "info" / "exclude").exists()
    # an absolute commondir works too
    other = tmp_path / "other-common"
    other.mkdir()
    (gitdir / "commondir").write_text(f"{other}\n")
    assert main(["init-project", str(folder), "--guidance"], client=client, env=env_for(app), runner=FakeClaude()) == 0
    assert (other / "info" / "exclude").read_text().count(".claude/rules/trajectree.md") == 1


# --- the import dialogue -------------------------------------------------------------------

import io

import pytest

from trajectree.cli.init_project import init_project
from trajectree.cli.main import _Fail

DEFAULT_SINCE = "2026-07-26"


def _source(name, count=0, oldest=None, detail="", imported=False):
    return {"source": name, "count": count, "oldest": oldest, "detail": detail, "imported": imported}


def make_probe(**overrides):
    sources = {
        "git": _source("git", 312, "2023-03-14T09:00:00Z"),
        "claude": _source("claude", 5, "2026-09-01T09:00:00Z"),
        "codex": _source("codex", 0, None, "not connected"),
        "folder": _source("folder", 40, "2026-10-01T09:00:00Z"),
    }
    sources.update(overrides.pop("sources", {}))
    return {"path": "/x", "default_since": DEFAULT_SINCE, "tz": overrides.pop("tz", "UTC"), "sources": list(sources.values()),
            "remotes": overrides.pop("remotes", [])}


def _runner(name, models, available=True):
    return {"runner": name, "available": available, "refreshed_at": None, "error": None,
            "models": [{"runner": name, "model": m, "display": None, "source": "profile"} for m in models]}


CATALOG = {"runners": [
    _runner("claude", ["claude-opus-5-5", "claude-sonnet-5-5"]),
    _runner("codex", ["gpt-hidden"], available=False),
    _runner("ccx:a", ["x"]),
    _runner("ccx:b", ["x"]),
]}


class FakeApi:
    def __init__(self, probe, catalog=CATALOG):
        self.probe = probe
        self.catalog = catalog
        self.calls = []

    def get(self, path):
        return self.catalog if path == "/api/models" else []

    def post(self, path, body):
        self.calls.append((path, body))
        if path == "/api/projects":
            return {"id": "prj_1", "slug": body["slug"], "title": body["title"]}
        if path == "/api/import/probe":
            if isinstance(self.probe, str):
                raise _Fail(self.probe)
            return self.probe
        if path.endswith("/import"):
            return {"project": {}, "resources": [], "starting_point_run": {"id": "run_9"}}
        return {}

    def imports(self):
        return [b for p, b in self.calls if p.endswith("/import")]


class Answers:
    def __init__(self, *answers):
        self.left = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.left:
            raise EOFError
        return self.left.pop(0)


def dialogue(tmp_path, capsys, api, *answers, interactive=True, **kw):
    ask = Answers(*answers)
    folder = tmp_path / "proj"
    folder.mkdir(exist_ok=True)
    out = io.StringIO()
    code = init_project(api, folder, slug="proj", title=None, kind="research", runner=FakeClaude(),
                        out=out, ask=ask, interactive=interactive, **kw)
    return code, out.getvalue(), capsys.readouterr().err, ask


def test_lists_sources_numbered(tmp_path, capsys):
    api = FakeApi(make_probe())
    code, out, _, _ = dialogue(tmp_path, capsys, api, "2", "n")
    assert "1. git  312 found, oldest 2023-03-14" in out
    assert "3. codex  (nothing found) not connected" in out


def test_empty_source_cannot_be_picked(tmp_path, capsys):
    api = FakeApi(make_probe())
    code, out, _, ask = dialogue(tmp_path, capsys, api, "3", "2", "n")
    assert "codex has nothing to import" in out
    assert code == 0 and api.imports() == []


def test_since_asked_only_when_older_than_cutoff(tmp_path, capsys):
    api = FakeApi(make_probe())
    _, _, _, ask = dialogue(tmp_path, capsys, api, "1,2,4", "default", "none", "y")
    asked = [p for p in ask.prompts if "Import from" in p]
    assert len(asked) == 1 and asked[0].startswith("git reaches back to 2023-03-14")


def test_start_model_asked_when_history_skipped(tmp_path, capsys):
    api = FakeApi(make_probe())
    code, out, _, ask = dialogue(tmp_path, capsys, api, "1", "default", "claude-sonnet-5-5", "y")
    assert code == 0
    (body,) = api.imports()
    assert body["start_block"] == {"runner": "claude", "model": "claude-sonnet-5-5"}
    assert body["starts"] == {"git": DEFAULT_SINCE}
    assert body["sources"] == {"git": {"picked": True, "since": None}}
    assert "starting-point run run_9 queued" in out
    assert "history imports on the next ingest pass, then the project appears in the Inbox as due for inspection (trajectree ingest to run the pass now)" in out


def test_no_model_question_without_skipped_history(tmp_path, capsys):
    api = FakeApi(make_probe())
    _, _, _, ask = dialogue(tmp_path, capsys, api, "1", "all", "y")
    assert not any("Model to summarise" in p for p in ask.prompts)
    (body,) = api.imports()
    assert body["start_block"] is None and body["starts"] == {}
    assert body["sources"]["git"] == {"picked": True, "since": "all"}
    _, _, _, ask = dialogue(tmp_path, capsys, FakeApi(make_probe()), "2,4", "y")
    assert not any("Model to summarise" in p or "Import from" in p for p in ask.prompts)


def test_date_that_skips_nothing_sends_no_start(tmp_path, capsys):
    api = FakeApi(make_probe())
    dialogue(tmp_path, capsys, api, "1", "2020-01-01", "y")
    (body,) = api.imports()
    assert body["starts"] == {} and body["sources"]["git"]["since"] == "2020-01-01"


def test_summary_then_confirm(tmp_path, capsys):
    api = FakeApi(make_probe())
    code, out, _, _ = dialogue(tmp_path, capsys, api, "2", "n")
    assert code == 0 and "import summary:" in out and "claude: from 2026-07-26 (default)" in out
    assert "nothing imported" in out and api.imports() == []


def test_bad_answers_reprompt(tmp_path, capsys):
    api = FakeApi(make_probe(sources={"claude": _source("claude", 0)}))
    code, out, _, ask = dialogue(
        tmp_path, capsys, api,
        "1,,3", "0", "9", "2", "",  # picks: gap, zero, out of range, empty source, empty line
        "1,4",
        "2026-13-01", "2099-01-01", "", "yesterday", "2024-05-01",  # git's date
        "none", "x", "y",
    )
    assert code == 0
    for reason in ("'' is not a source number", "0 is not between 1 and 4", "9 is not between 1 and 4",
                   "claude has nothing to import", "'2026-13-01' is not a real date", "2099-01-01 is in the future",
                   "'' is not a date", "'yesterday' is not a date", "answer y or n"):
        assert reason in out
    (body,) = api.imports()
    assert body["sources"] == {"git": {"picked": True, "since": "2024-05-01"}, "folder": {"picked": True, "since": None}}
    # the owner chose none: no default model, but the skipped history is recorded for a later block
    assert body["start_block"] is None and body["starts"] == {"git": "2024-05-01"}
    assert ("  history before 2024-05-01 will be neither imported nor summarised"
            f" (later: trajectree init-project {(tmp_path / "proj").resolve()} --start-block-only)") in out


def test_all_with_nothing_pickable_is_not_offered(tmp_path, capsys):
    sources = {n: _source(n) for n in ("git", "claude", "folder")}
    api = FakeApi(make_probe(sources=sources))
    code, out, _, ask = dialogue(tmp_path, capsys, api)
    assert code == 0 and [p[:7] for p in ask.prompts] == ["Install"] and "history found" not in out


def test_non_tty_missing_choices_errors(tmp_path, capsys):
    api = FakeApi(make_probe())
    code, _, err, _ = dialogue(tmp_path, capsys, api, interactive=False)
    assert code == 2 and api.imports() == []
    assert err.strip() == ("missing choices: --sources, --since git=<YYYY-MM-DD|all|default>, "
                           "--start-runner, --start-model or --no-start-block")
    code, _, err, _ = dialogue(tmp_path, capsys, api, interactive=False, sources="claude,folder")
    assert code == 0 and api.imports()[0]["sources"].keys() == {"claude", "folder"}
    code, _, err, _ = dialogue(tmp_path, capsys, FakeApi(make_probe()), interactive=False, sources="git",
                               since=["git=2020-01-01"])
    assert code == 0


def test_flags_skip_prompts(tmp_path, capsys):
    api = FakeApi(make_probe())
    code, _, _, ask = dialogue(tmp_path, capsys, api, interactive=False, sources="git,claude",
                               since=["git=default"], start_model="m1", start_runner="claude")
    assert code == 0 and ask.prompts == []
    (body,) = api.imports()
    assert body["start_block"] == {"runner": "claude", "model": "m1"}
    assert body["starts"] == {"git": DEFAULT_SINCE}
    api = FakeApi(make_probe())
    dialogue(tmp_path, capsys, api, interactive=False, sources="all", since=["git=all"], no_start_block=True)
    (body,) = api.imports()
    assert set(body["sources"]) == {"git", "claude", "folder"} and body["starts"] == {} and body["start_block"] is None


def test_bad_flags_exit_2(tmp_path, capsys):
    for kw, text in (({"sources": "nope"}, "unknown source: nope"),
                     ({"sources": "codex"}, "cannot import codex: nothing found"),
                     ({"sources": "git", "since": ["claude=all"]}, "claude, which is not picked"),
                     ({"sources": "git", "since": ["git=2099-01-01"]}, "in the future"),
                     ({"sources": "git", "since": ["git"]}, "expected <source>=")):
        api = FakeApi(make_probe())
        code, _, err, _ = dialogue(tmp_path, capsys, api, interactive=False, **kw)
        assert code == 2 and text in err and api.imports() == []


def test_rerun_offers_only_new_sources(tmp_path, capsys):
    api = FakeApi(make_probe(sources={"git": _source("git", 312, "2023-03-14T09:00:00Z", imported=True)}))
    code, out, _, ask = dialogue(tmp_path, capsys, api, "1", "2", "n")
    assert "1. git  (imported)" in out and "git is already imported" in out
    code, _, err, _ = dialogue(tmp_path, capsys, api, interactive=False, sources="git")
    assert code == 2 and "already imported" in err


def test_remote_choice_sent(tmp_path, capsys):
    remotes = [{"url": "https://github.com/a/b", "title": "a/b", "declined": False},
               {"url": "https://github.com/a/old", "title": "a/old", "declined": True}]
    api = FakeApi(make_probe(remotes=remotes))
    _, out, _, ask = dialogue(tmp_path, capsys, api, "2", "y", "y")
    assert "https://github.com/a/old (declined before)" in out
    assert sum("Also follow remote" in p for p in ask.prompts) == 1
    assert api.imports()[0]["remotes"] == ["https://github.com/a/b"]


def test_probe_outside_mapped_folders_stops_before_host_writes(tmp_path, capsys):
    api = FakeApi("path /x is outside every mapped folder")
    code, out, err, _ = dialogue(tmp_path, capsys, api)
    assert code == 2 and out == ""
    assert err.strip() == ("project registered, but the daemon cannot see this folder: "
                           "path /x is outside every mapped folder")
    assert not (tmp_path / "proj" / ".claude").exists()


def test_other_probe_errors_also_stop_before_host_writes(tmp_path, capsys):
    class Broken(FakeApi):
        def post(self, path, body):
            if path == "/api/import/probe":
                raise RuntimeError("daemon down")
            return super().post(path, body)

    with pytest.raises(RuntimeError):
        dialogue(tmp_path, capsys, Broken(make_probe()))
    assert not (tmp_path / "proj" / ".claude").exists()


REMOTE = {"url": "https://github.com/a/b", "title": "a/b", "declined": False}
OLD_REMOTE = {"url": "https://github.com/a/old", "title": "a/old", "declined": True}


def test_non_tty_undecided_remotes_are_missing_choices(tmp_path, capsys):
    api = FakeApi(make_probe(remotes=[REMOTE, OLD_REMOTE]))
    code, _, err, _ = dialogue(tmp_path, capsys, api, interactive=False, sources="claude")
    assert code == 2 and api.imports() == []
    assert err.strip() == "missing choices: --remote <url> or --no-remotes"
    # a remote declined before is not undecided
    api = FakeApi(make_probe(remotes=[OLD_REMOTE]))
    code, _, _, _ = dialogue(tmp_path, capsys, api, interactive=False, sources="claude")
    assert code == 0 and api.imports()[0]["remotes"] == []


def test_remote_flags(tmp_path, capsys):
    api = FakeApi(make_probe(remotes=[REMOTE]))
    code, _, _, ask = dialogue(tmp_path, capsys, api, interactive=False, sources="claude", no_remotes=True)
    assert code == 0 and api.imports()[0]["remotes"] == []
    api = FakeApi(make_probe(remotes=[REMOTE]))
    code, _, _, _ = dialogue(tmp_path, capsys, api, interactive=False, sources="claude", remote=[REMOTE["url"]])
    assert code == 0 and api.imports()[0]["remotes"] == [REMOTE["url"]]
    # flags also skip the remote question in an interactive run
    api = FakeApi(make_probe(remotes=[REMOTE]))
    _, _, _, ask = dialogue(tmp_path, capsys, api, "2", "y", no_remotes=True)
    assert not any("remote" in p for p in ask.prompts) and api.imports()[0]["remotes"] == []
    # contradictory flags
    code, _, err, _ = dialogue(tmp_path, capsys, FakeApi(make_probe()), interactive=False, sources="claude",
                               remote=["u"], no_remotes=True)
    assert code == 2 and "exclude each other" in err


def starts_for(tz, oldest, capsys, tmp_path):
    api = FakeApi(make_probe(tz=tz, sources={"git": _source("git", 3, oldest)}))
    code, _, _, _ = dialogue(tmp_path, capsys, api, interactive=False, sources="git",
                             since=["git=2024-05-01"], start_model="m1")
    assert code == 0
    return api.imports()[0]["starts"]


def test_starts_compare_against_local_midnight_of_the_daemon_zone(tmp_path, capsys):
    # 2024-05-01T02:00Z is still April 30th in New York: history exists before the chosen midnight
    assert starts_for("America/New_York", "2024-05-01T02:00:00Z", capsys, tmp_path) == {"git": "2024-05-01"}
    # ... and already past midnight on the 1st in Tokyo: nothing is skipped
    assert starts_for("Asia/Tokyo", "2024-05-01T02:00:00Z", capsys, tmp_path) == {}
    # oldest exactly at that midnight is not older
    assert starts_for("America/New_York", "2024-05-01T04:00:00Z", capsys, tmp_path) == {}


def test_all_at_the_prompt_picks_exactly_the_pickable_sources(tmp_path, capsys):
    api = FakeApi(make_probe(sources={"git": _source("git", 312, "2023-03-14T09:00:00Z", imported=True)}))
    code, _, _, ask = dialogue(tmp_path, capsys, api, "7", "1", "3", "all", "y")
    assert code == 0
    assert sum(p.startswith("Sources to import") for p in ask.prompts) == 4
    (body,) = api.imports()
    assert set(body["sources"]) == {"claude", "folder"}


def test_eof_at_the_model_prompt_imports_nothing(tmp_path, capsys):
    api = FakeApi(make_probe())
    code, _, err, ask = dialogue(tmp_path, capsys, api, "1", "default")
    assert ask.prompts[-1].startswith("Model") and code == 2
    assert "nothing imported" in err and api.imports() == []


def test_init_project_start_model_from_catalog(tmp_path, capsys):
    api = FakeApi(make_probe())
    code, out, _, ask = dialogue(tmp_path, capsys, api, "1", "default", "", "9", "x", "gpt-hidden", "3", "y")
    assert code == 0
    assert ("  1. claude/default\n  2. claude/claude-opus-5-5\n  3. claude/claude-sonnet-5-5\n"
            "  4. ccx:a/default\n  5. ccx:a/x\n  6. ccx:b/default\n  7. ccx:b/x\n") in out
    assert "codex/" not in out  # an unavailable runner offers nothing
    for reason in ("give a number or a model name, or none to skip", "no model with number 9",
                   "model x is offered by ccx:a, ccx:b; answer with its number",
                   "unknown model gpt-hidden; answer with a number from the list"):
        assert reason in out
    assert sum(p.startswith("Model") for p in ask.prompts) == 5
    assert api.imports()[0]["start_block"] == {"runner": "claude", "model": "claude-sonnet-5-5"}
    for answer, block in (("ccx:b/x", {"runner": "ccx:b", "model": "x"}), ("5", {"runner": "ccx:a", "model": "x"}),
                          ("claude/default", {"runner": "claude", "model": None}),
                          ("4", {"runner": "ccx:a", "model": None}),
                          ("claude-opus-5-5", {"runner": "claude", "model": "claude-opus-5-5"})):
        api = FakeApi(make_probe())
        dialogue(tmp_path, capsys, api, "1", "default", answer, "y")
        assert api.imports()[0]["start_block"] == block, answer


def test_empty_catalog_says_how_to_fill_it(tmp_path, capsys):
    api = FakeApi(make_probe(), catalog={"runners": []})
    code, out, _, _ = dialogue(tmp_path, capsys, api, "1", "default", "1", "none", "y")
    assert code == 0 and "no models in the catalog; run: trajectree models --refresh" in out
    assert "no model with number 1" in out
    assert api.imports()[0]["start_block"] is None


def test_input_ending_imports_nothing(tmp_path, capsys):
    api = FakeApi(make_probe())
    code, _, err, _ = dialogue(tmp_path, capsys, api, "1")
    assert code == 2 and "nothing imported" in err and api.imports() == []


def test_new_project_is_created_with_an_empty_import_record(tmp_path):
    api = FakeApi(make_probe())
    folder = tmp_path / "fresh"
    folder.mkdir()
    init_project(api, folder, slug="fresh", title=None, kind="research", runner=FakeClaude(),
                 out=io.StringIO(), sources="none")
    (create,) = [b for p, b in api.calls if p == "/api/projects"]
    assert create["props"]["import"]["sources"] == {} and create["props"]["import"]["recorded_at"]


def test_existing_project_is_not_modified(tmp_path):
    class Existing(FakeApi):
        def get(self, path):
            return [{"id": "prj_1", "slug": "fresh", "title": "Fresh"}]

    api = Existing(make_probe())
    folder = tmp_path / "fresh"
    folder.mkdir()
    init_project(api, folder, slug="fresh", title=None, kind="research", runner=FakeClaude(),
                 out=io.StringIO(), sources="none")
    assert not [p for p, _ in api.calls if p == "/api/projects"]


def test_flags_import_through_the_daemon(tmp_path, client, app, capsys):
    plain = tmp_path / "work"
    plain.mkdir()
    (plain / "notes.txt").write_text("x")
    args = ["init-project", str(plain), "--sources", "folder"]
    assert main(args, client=client, env=env_for(app), runner=FakeClaude()) == 0
    assert "history imports on the next ingest pass" in capsys.readouterr().out
    (p,) = user_projects(client)
    # "default" is stored as the concrete day it meant at import
    from trajectree.importing import default_since
    from trajectree.timeutil import utcnow
    settings = app.state.settings
    day = default_since(utcnow(), settings.scan_since_days, settings.tz).isoformat()
    assert p["props"]["import"]["sources"] == {"folder": {"picked": True, "since": day}}
    assert main(args, client=client, env=env_for(app), runner=FakeClaude()) == 0  # now imported
    assert "4. folder  (imported)" in capsys.readouterr().out


def test_created_project_has_empty_import_record_in_the_daemon(tmp_path, client, app):
    plain = tmp_path / "fresh"
    plain.mkdir()
    assert main(["init-project", str(plain), "--sources", "none"], client=client, env=env_for(app), runner=FakeClaude()) == 0
    (p,) = user_projects(client)
    assert p["props"]["import"]["sources"] == {}


def test_no_start_block_prints_what_is_left_out(tmp_path, capsys):
    api = FakeApi(make_probe())
    code, out, _, _ = dialogue(tmp_path, capsys, api, interactive=False, sources="git", since=["git=default"],
                               no_start_block=True)
    assert code == 0
    folder = (tmp_path / "proj").resolve()
    assert (f"  history before {DEFAULT_SINCE} will be neither imported nor summarised"
            f" (later: trajectree init-project {folder} --start-block-only)") in out.splitlines()
    (body,) = api.imports()
    assert body["start_block"] is None and body["starts"] == {"git": DEFAULT_SINCE}   # kept for a later block
    # with a block, no such line
    _, out, _, _ = dialogue(tmp_path, capsys, FakeApi(make_probe()), interactive=False, sources="git",
                            since=["git=default"], start_model="m1")
    assert "neither imported nor summarised" not in out


FOLLOWED = {"url": "https://github.com/a/followed", "title": "a/followed", "declined": False, "followed": True}


def test_followed_remotes_are_shown_never_asked_and_not_missing(tmp_path, capsys):
    api = FakeApi(make_probe(remotes=[FOLLOWED]))
    code, out, err, _ = dialogue(tmp_path, capsys, api, interactive=False, sources="claude")
    assert code == 0 and err == "" and api.imports()[0]["remotes"] == []
    assert f"remote {FOLLOWED['url']} (following)" in out
    api = FakeApi(make_probe(remotes=[FOLLOWED, REMOTE]))
    _, out, _, ask = dialogue(tmp_path, capsys, api, "2", "n", "y")
    asked = [p for p in ask.prompts if "Also follow remote" in p]
    assert asked == [f"Also follow remote {REMOTE['url']}? [y/n]: "] and "(following)" in out


class WithStarts(FakeApi):
    def __init__(self, starts, catalog=CATALOG):
        super().__init__(make_probe(), catalog)
        self.starts = starts

    def get(self, path):
        if path == "/api/projects":
            return [{"id": "prj_1", "slug": "proj", "title": "Proj", "props": {"import": {"starts": self.starts}}}]
        return super().get(path)

    def post(self, path, body):
        if path.endswith("/import/start-block"):
            self.calls.append((path, body))
            return {"id": "run_7"}
        return super().post(path, body)


def test_start_block_only_asks_for_the_model_and_queues(tmp_path, capsys):
    api = WithStarts({"git": "2025-01-01"})
    code, out, _, ask = dialogue(tmp_path, capsys, api, "claude-sonnet-5-5", start_block_only=True)
    assert code == 0 and "skipped history: git before 2025-01-01" in out and "starting-point run run_7 queued" in out
    assert api.calls == [("/api/projects/prj_1/import/start-block", {"runner": "claude", "model": "claude-sonnet-5-5"})]
    assert any("Model to summarise the skipped history of git" in p for p in ask.prompts)
    assert not (tmp_path / "proj" / ".claude").exists()   # no host write in this mode
    # none queues nothing
    api = WithStarts({"git": "2025-01-01"})
    code, out, _, _ = dialogue(tmp_path, capsys, api, "none", start_block_only=True)
    assert code == 0 and api.calls == [] and "no starting-point run queued" in out


def test_start_block_only_non_interactive_needs_a_model(tmp_path, capsys):
    api = WithStarts({"git": "2025-01-01"})
    code, _, err, _ = dialogue(tmp_path, capsys, api, interactive=False, start_block_only=True)
    assert code == 2 and err.strip() == "missing choices: --start-runner or --start-model" and api.calls == []
    code, _, _, ask = dialogue(tmp_path, capsys, api, interactive=False, start_block_only=True,
                               start_model="m1", start_runner="ccx:a")
    assert code == 0 and ask.prompts == []
    assert api.calls == [("/api/projects/prj_1/import/start-block", {"runner": "ccx:a", "model": "m1"})]


def test_start_block_only_accepts_a_runner_alone(tmp_path, capsys):
    api = WithStarts({"git": "2025-01-01"})
    code, _, _, ask = dialogue(tmp_path, capsys, api, interactive=False, start_block_only=True,
                               start_runner="codex")
    assert code == 0 and ask.prompts == []
    assert api.calls == [("/api/projects/prj_1/import/start-block", {"runner": "codex", "model": None})]
    # the interactive pick of <runner>/default sends the runner with no model
    api = WithStarts({"git": "2025-01-01"})
    code, _, _, _ = dialogue(tmp_path, capsys, api, "ccx:a/default", start_block_only=True)
    assert code == 0 and api.calls == [("/api/projects/prj_1/import/start-block", {"runner": "ccx:a", "model": None})]


def test_import_with_a_runner_alone_prints_default(tmp_path, capsys):
    api = FakeApi(make_probe())
    code, out, _, _ = dialogue(tmp_path, capsys, api, interactive=False, sources="git", since=["git=default"],
                               start_runner="codex")
    assert code == 0 and api.imports()[0]["start_block"] == {"runner": "codex", "model": None}
    assert "starting point: default via codex" in out


def test_start_block_only_refusals(tmp_path, capsys):
    code, _, err, _ = dialogue(tmp_path, capsys, FakeApi(make_probe()), interactive=False, start_block_only=True,
                               start_model="m1")
    assert code == 2 and "no project proj" in err and "run: trajectree init-project" in err
    code, _, err, _ = dialogue(tmp_path, capsys, WithStarts({}), interactive=False, start_block_only=True,
                               start_model="m1")
    assert code == 2 and "no skipped history recorded" in err
    code, _, err, _ = dialogue(tmp_path, capsys, WithStarts({"git": "2025-01-01"}), interactive=False,
                               start_block_only=True, no_start_block=True)
    assert code == 2 and "--start-block-only excludes --no-start-block" in err


def test_start_block_only_through_the_daemon(tmp_path, client, app, capsys):
    work = tmp_path / "work"
    work.mkdir()
    (work / "notes.txt").write_text("x")
    assert main(["init-project", str(work), "--sources", "folder"], client=client, env=env_for(app), runner=FakeClaude()) == 0
    capsys.readouterr()
    args = ["init-project", str(work), "--start-block-only", "--start-model", "claude-opus-5-5"]
    assert main(args, client=client, env=env_for(app), runner=FakeClaude(), interactive=False) == 2
    assert "no skipped history recorded" in capsys.readouterr().err   # the folder's history was all imported
    (p,) = user_projects(client)
    body = {"sources": {"claude": {"picked": True}}, "starts": {"claude": "2025-01-01"}}
    assert client.post(f"/api/projects/{p['id']}/import", json=body).status_code == 200
    assert main(args, client=client, env=env_for(app), runner=FakeClaude(), interactive=False) == 0
    out = capsys.readouterr().out
    run_id = out.split("starting-point run ")[1].split()[0]
    assert user_projects(client)[0]["props"]["import"]["start_run_id"] == run_id

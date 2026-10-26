from tests.test_init_project import FakeClaude, env_for, make_repo
from trajectree.cli.main import main


def run(argv, client, app, fake, capsys):
    code = main(argv, client=client, env=env_for(app), runner=fake)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_guidance_install_requires_a_registered_folder(tmp_path, client, app, capsys):
    repo = make_repo(tmp_path)
    fake = FakeClaude()
    code, _, err = run(["guidance", "install", str(repo)], client, app, fake, capsys)
    assert code != 0 and "folder is not a registered project: run trajectree init-project <path>" in err
    assert not (repo / ".claude").exists() and fake.calls == []


def test_guidance_install_remove_show_round_trip(tmp_path, client, app, capsys):
    repo = make_repo(tmp_path)
    fake = FakeClaude()
    assert run(["init-project", str(repo), "--sources", "none"], client, app, fake, capsys)[0] == 0
    rules = repo / ".claude" / "rules" / "trajectree.md"
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("keep-me\n")
    assert run(["guidance", "show", str(repo)], client, app, fake, capsys)[1].splitlines() == ["rules: absent", "mcp: absent"]

    code, out, _ = run(["guidance", "install", str(repo)], client, app, fake, capsys)
    assert code == 0 and out.splitlines()[-1] == "mcp: configured"
    assert rules.read_text().startswith("<!-- managed by trajectree init-project; edits are overwritten -->")
    assert exclude.read_text().splitlines() == ["keep-me", ".claude/rules/trajectree.md"]
    assert run(["guidance", "show", str(repo)], client, app, fake, capsys)[1].splitlines() == ["rules: present", "mcp: configured"]

    calls = list(fake.calls)
    code, out, _ = run(["guidance", "remove", str(repo)], client, app, fake, capsys)
    assert code == 0 and out.strip() == "guidance: removed"
    assert not rules.exists() and exclude.read_text() == "keep-me\n"
    assert fake.calls == calls  # remove leaves the MCP registration alone


def test_guidance_remove_refuses_a_file_it_does_not_manage(tmp_path, client, app, capsys):
    repo = make_repo(tmp_path)
    rules = repo / ".claude" / "rules" / "trajectree.md"
    rules.parent.mkdir(parents=True)
    rules.write_text("mine\n")
    code, _, err = run(["guidance", "remove", str(repo)], client, app, FakeClaude(), capsys)
    assert code == 1 and "not managed" in err and rules.read_text() == "mine\n"

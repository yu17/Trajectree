import subprocess
from pathlib import Path

import pytest

from trajectree.models import ReportIn
from trajectree.reports.export import MarpUnavailable, builtin_html, export_report, marp_binary, split_slides
from trajectree.repo.reports import create_report

DECK = "---\nmarp: true\n---\n\n# One\n\n<!-- note -->\n\n---\n\n# Two"


def test_export_without_marp(settings, ledger, human):
    r = create_report(ledger, human, ReportIn(title="Deck", kind="deck", source_md=DECK))
    html = export_report(settings, r, "html", which=lambda name: None).read_text()
    assert html.count('<section class="slide">') == 2 and "<aside class=\"notes\">" in html and "<title>Deck</title>" in html
    for fmt in ("pdf", "pptx"):
        with pytest.raises(MarpUnavailable):
            export_report(settings, r, fmt, which=lambda name: None)
    assert not (settings.reports_dir / f"{r.id}.pdf").exists() and (settings.reports_dir / f"{r.id}.md").exists()


def test_export_with_marp_invokes_cli(settings, ledger, human, tmp_path):
    report = create_report(ledger, human, ReportIn(title="Deck", kind="deck", source_md=DECK))
    calls, kwargs = [], []

    def run(argv, **kw):
        calls.append(argv)
        kwargs.append(kw)
        Path(argv[argv.index("-o") + 1]).write_bytes(b"%PDF")
        (Path(kw["env"]["TMPDIR"]) / "marp-cli-profile").mkdir()  # what marp leaves behind
        return subprocess.CompletedProcess(argv, 0, "", "")

    s = settings.model_copy(update={"marp_bin": "/opt/marp", "chrome_bin": "/usr/bin/chromium"})
    out = export_report(s, report, "pdf", run=run)
    assert out.suffix == ".pdf" and calls[0][0] == "/opt/marp" and "--browser-path" in calls[0] and "--allow-local-files" not in calls[0]
    # marp writes a temporary file beside the target, which then replaces it in one step
    tmp_out = Path(calls[0][3])
    assert calls[0][:3] == ["/opt/marp", str(s.reports_dir / f"{report.id}.md"), "-o"] and calls[0][4:] == ["--browser-path", "/usr/bin/chromium"]
    assert tmp_out.parent == s.reports_dir and tmp_out.suffix == ".pdf" and tmp_out != out and not tmp_out.exists()
    assert kwargs[0]["timeout"] == 120 and kwargs[0]["cwd"] == s.reports_dir and out == s.reports_dir / f"{report.id}.pdf"
    assert out.read_bytes() == b"%PDF" and sorted(p.name for p in s.reports_dir.iterdir()) == [f"{report.id}.md", f"{report.id}.pdf"]
    # each marp run gets its own TMPDIR (Chromium's profile lands there), removed afterwards
    scratch = Path(kwargs[0]["env"]["TMPDIR"])
    assert scratch.name.startswith("trajectree-marp-") and not scratch.exists()
    assert kwargs[0]["env"]["PATH"]  # the rest of the environment is inherited


def test_export_marp_failure_and_md(settings, ledger, human):
    report = create_report(ledger, human, ReportIn(title="Deck", kind="deck", source_md=DECK))

    def run(argv, **kw):
        Path(argv[3]).write_text("half")  # a partial file marp leaves behind
        return subprocess.CompletedProcess(argv, 1, "", "x" * 600 + "boom")

    with pytest.raises(RuntimeError, match="boom") as err:
        export_report(settings, report, "html", run=run, which=lambda name: "/usr/bin/marp")
    assert len(str(err.value)) == 500
    assert [p.name for p in settings.reports_dir.iterdir()] == [f"{report.id}.md"]   # no temp or partial output
    md = export_report(settings, report, "md", which=lambda name: None)
    assert md == settings.reports_dir / f"{report.id}.md" and md.read_text() == DECK


def test_marp_binary_prefers_setting(settings):
    assert marp_binary(settings, which=lambda name: "/usr/bin/marp") == "/usr/bin/marp"
    assert marp_binary(settings, which=lambda name: None) is None
    assert marp_binary(settings.model_copy(update={"marp_bin": "/opt/marp"}), which=lambda name: None) == "/opt/marp"


def test_split_slides():
    meta, slides = split_slides("---\nmarp: true\ntheme: default\n---\n\n# A\n\n---\n\n# B\n")
    assert meta == {"marp": "true", "theme": "default"} and [s.strip() for s in slides] == ["# A", "# B"]
    assert split_slides("# Only") == ({}, ["# Only"])


def test_split_slides_keeps_fenced_rules():
    source = "# A\n\n```yaml\n---\nkey: v\n---\n```\n\n~~~\n---\n~~~\n\n---\n\n# B"
    meta, slides = split_slides(source)
    assert meta == {} and len(slides) == 2 and slides[0].count("---") == 3 and slides[1].strip() == "# B"


def test_split_slides_unclosed_front_matter_is_plain():
    meta, slides = split_slides("---\n# A\n\nbody")
    assert meta == {} and slides == ["---\n# A\n\nbody"]
    meta, slides = split_slides("---\n# A\n\n---\n\n# B")   # closed: front matter of one line
    assert meta == {} and [s.strip() for s in slides] == ["# B"]


def test_builtin_memo_single_article(settings, ledger, human):
    memo = create_report(ledger, human, ReportIn(title="<Memo & co>", kind="memo", source_md="---\nmarp: true\n---\n\n# Summary\n\nAll *good*.\n\n<!-- a <b> -->\n"))
    html = builtin_html(memo)
    assert html.startswith("<!doctype html>") and html.count("<article") == 1 and '<section class="slide">' not in html
    assert "<title>&lt;Memo &amp; co&gt;</title>" in html and "<em>good</em>" in html and "prefers-color-scheme: dark" in html
    assert '<aside class="notes">a &lt;b&gt;</aside>' in html and "marp: true" not in html

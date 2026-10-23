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
        return subprocess.CompletedProcess(argv, 0, "", "")

    s = settings.model_copy(update={"marp_bin": "/opt/marp", "chrome_bin": "/usr/bin/chromium"})
    out = export_report(s, report, "pdf", run=run)
    assert out.suffix == ".pdf" and calls[0][0] == "/opt/marp" and "--browser-path" in calls[0] and "--allow-local-files" not in calls[0]
    assert calls[0] == ["/opt/marp", str(s.reports_dir / f"{report.id}.md"), "-o", str(out), "--browser-path", "/usr/bin/chromium"]
    assert kwargs[0]["timeout"] == 120 and kwargs[0]["cwd"] == s.reports_dir and out.read_bytes() == b"%PDF"


def test_export_marp_failure_and_md(settings, ledger, human):
    report = create_report(ledger, human, ReportIn(title="Deck", kind="deck", source_md=DECK))

    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "x" * 600 + "boom")

    with pytest.raises(RuntimeError, match="boom") as err:
        export_report(settings, report, "html", run=run, which=lambda name: "/usr/bin/marp")
    assert len(str(err.value)) == 500
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


def test_builtin_memo_single_article(settings, ledger, human):
    memo = create_report(ledger, human, ReportIn(title="<Memo & co>", kind="memo", source_md="---\nmarp: true\n---\n\n# Summary\n\nAll *good*.\n\n<!-- a <b> -->\n"))
    html = builtin_html(memo)
    assert html.startswith("<!doctype html>") and html.count("<article") == 1 and '<section class="slide">' not in html
    assert "<title>&lt;Memo &amp; co&gt;</title>" in html and "<em>good</em>" in html and "prefers-color-scheme: dark" in html
    assert '<aside class="notes">a &lt;b&gt;</aside>' in html and "marp: true" not in html

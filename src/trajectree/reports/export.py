"""Report export: Markdown as stored, HTML via marp-cli or the built-in renderer, PDF/PPTX via marp-cli.

marp-cli runs as a subprocess with no ledger lock held; callers load the report first.
"""

from __future__ import annotations

import html
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from markdown_it import MarkdownIt

from trajectree.config import Settings
from trajectree.models import Report

ExportFormat = Literal["md", "html", "pdf", "pptx"]

MARP_TIMEOUT_S = 120

_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
# Marp directives written as comments (`<!-- _class: lead -->`) are not speaker notes.
_DIRECTIVE_RE = re.compile(
    r"^\s*_?(theme|style|headingDivider|lang|size|math|title|description|author|image|keywords|url|marp"
    r"|paginate|header|footer|class|backgroundColor|backgroundImage|backgroundPosition|backgroundRepeat"
    r"|backgroundSize|color|transition)\s*:[^\n]*$"
)
# Raw HTML in the source is escaped, never passed through: exports are served from the app's origin.
_MD = MarkdownIt("commonmark", {"html": False})
# A memo keeps its `---` lines as rules; blank lines around one stop `text\n---` becoming a heading.
_RULE = "\n\n---\n\n"

_CSS = """
:root { color-scheme: light dark; --bg: #f4f4f5; --card: #ffffff; --fg: #18181b; --muted: #71717a; --line: #e4e4e7; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #18181b; --card: #27272a; --fg: #f4f4f5; --muted: #a1a1aa; --line: #3f3f46; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 2rem 1rem; background: var(--bg); color: var(--fg);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.slide, article { max-width: 60rem; margin: 0 auto 2rem; padding: 2.5rem 3rem; background: var(--card);
  border: 1px solid var(--line); border-radius: 8px; }
.slide { aspect-ratio: 16 / 9; overflow: auto; }
article { max-width: 46rem; }
h1, h2, h3 { line-height: 1.2; }
a { color: inherit; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
pre { overflow-x: auto; padding: 0.75rem; border: 1px solid var(--line); border-radius: 6px; }
.notes { margin-top: 1.5rem; padding-top: 0.75rem; border-top: 1px dashed var(--line); color: var(--muted);
  font-size: 0.875rem; white-space: pre-wrap; }
@media print { body { padding: 0; background: none; } .slide { break-after: page; border: none; } }
"""


class MarpUnavailable(Exception):
    pass


def marp_binary(settings: Settings, *, which=shutil.which) -> str | None:
    """The configured marp-cli, else `marp` on PATH, else None."""
    return settings.marp_bin or which("marp")


def split_slides(source_md: str) -> tuple[dict[str, str], list[str]]:
    """Split Marp Markdown into its front matter (`key: value` pairs) and slides (split on `---` lines
    outside fenced code). A leading `---` with no closing `---` is a plain line, not front matter."""
    lines = source_md.splitlines()
    meta: dict[str, str] = {}
    slides: list[list[str]] = [[]]
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            slides[0].append(lines.pop(0))
        else:
            for line in lines[1:end]:
                key, sep, value = line.partition(":")
                if sep and key.strip():
                    meta[key.strip()] = value.strip()
            lines = lines[end + 1 :]
    fence: str | None = None  # the opening ``` or ~~~ of the code block we are in
    for line in lines:
        marker = line.lstrip()[:3]
        if fence is None and marker in ("```", "~~~"):
            fence = marker
        elif fence is not None and marker == fence:
            fence = None
        elif fence is None and line == "---":
            slides.append([])
            continue
        slides[-1].append(line)
    return meta, ["\n".join(slide) for slide in slides]


def _render(body: str) -> str:
    """Render Markdown, turning comments (other than Marp directives) into speaker-note asides."""
    notes = [text.strip() for text in _COMMENT_RE.findall(body) if not _DIRECTIVE_RE.match(text.strip())]
    rendered = _MD.render(_COMMENT_RE.sub("", body))
    return rendered + "".join(f'<aside class="notes">{html.escape(note)}</aside>\n' for note in notes if note)


def builtin_html(report: Report) -> str:
    """A self-contained HTML page: one section per slide for a deck, one article for a memo."""
    _, slides = split_slides(report.source_md)
    if report.kind == "deck":
        body = "".join(f'<section class="slide">\n{_render(slide)}</section>\n' for slide in slides)
    else:
        body = f"<article>\n{_render(_RULE.join(slides))}</article>\n"
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(report.title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n{body}</body>\n</html>\n"
    )


def export_report(
    settings: Settings, report: Report, fmt: ExportFormat, *, run=subprocess.run, which=shutil.which
) -> Path:
    """Write `<reports_dir>/<id>.md`, then the requested format beside it; return the file's path.

    Every file is written under a temporary name and moved into place with `os.replace`, so a
    concurrent export or save never serves (or feeds marp) a half-written file."""
    out_dir = settings.reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{report.id}.md"
    _write_atomic(md_path, report.source_md)
    if fmt == "md":
        return md_path
    out_path = out_dir / f"{report.id}.{fmt}"
    marp = marp_binary(settings, which=which)
    if marp is None:
        if fmt != "html":
            raise MarpUnavailable("marp-cli not available; build the reports image variant")
        _write_atomic(out_path, builtin_html(report))
        return out_path
    tmp_out = _temp_path(out_path)
    # Never `--allow-local-files`: the Markdown is agent-written and must not read the host's files.
    argv = [marp, str(md_path), "-o", str(tmp_out)]
    if settings.chrome_bin:
        argv += ["--browser-path", settings.chrome_bin]
    try:
        try:
            result = run(argv, capture_output=True, text=True, timeout=MARP_TIMEOUT_S, cwd=out_dir)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"marp timed out after {MARP_TIMEOUT_S}s") from None
        except OSError as exc:
            raise RuntimeError(f"marp failed to start: {exc}") from None
        if result.returncode != 0:
            raise RuntimeError((result.stderr or f"marp exited with {result.returncode}")[-500:])
        if not tmp_out.is_file():
            raise RuntimeError("marp wrote no output")
        os.replace(tmp_out, out_path)
    finally:
        tmp_out.unlink(missing_ok=True)
    return out_path


def _temp_path(path: Path) -> Path:
    """A fresh hidden name beside `path` with the same extension (marp picks the format from it)."""
    return path.with_name(f".{path.stem}.{secrets.token_hex(6)}{path.suffix}")


def _write_atomic(path: Path, text: str) -> None:
    tmp = _temp_path(path)
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

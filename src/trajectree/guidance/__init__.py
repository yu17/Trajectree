from __future__ import annotations

from importlib import resources


def render_rules(slug: str, title: str) -> str:
    text = resources.files("trajectree.guidance").joinpath("rules.md").read_text(encoding="utf-8")
    return text.replace("{slug}", slug).replace("{title}", title)

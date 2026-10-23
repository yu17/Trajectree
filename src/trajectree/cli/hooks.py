from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

HOOK_EVENTS = ("SessionStart", "Stop", "SessionEnd")
HOOK_LABEL = "hooks"


def hooks_url(base: str) -> str:
    return base.rstrip("/") + "/api/hooks/claude"


def settings_path(env: Mapping[str, str]) -> Path:
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "settings.json"
    home = env.get("HOME") if env is not os.environ else None
    return (Path(home) if home else Path.home()) / ".claude" / "settings.json"


def handler(url: str, secret: str) -> dict[str, Any]:
    return {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {secret}"}, "timeout": 3}


def _strip(groups: list[dict[str, Any]], url: str) -> list[dict[str, Any]]:
    kept = []
    for group in groups:
        hooks = [h for h in group.get("hooks", []) if h.get("url") != url]
        if hooks:
            kept.append({**group, "hooks": hooks})
    return kept


def merge_hooks(settings: dict[str, Any], url: str, secret: str) -> dict[str, Any]:
    merged = copy.deepcopy(settings)
    hooks = merged.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        groups = _strip(hooks.get(event, []), url)
        groups.append({"hooks": [handler(url, secret)]})
        hooks[event] = groups
    return merged


def remove_hooks(settings: dict[str, Any], url: str) -> dict[str, Any]:
    result = copy.deepcopy(settings)
    hooks = result.get("hooks", {})
    for event in HOOK_EVENTS:
        if event in hooks:
            groups = _strip(hooks[event], url)
            if groups:
                hooks[event] = groups
            else:
                del hooks[event]
    if "hooks" in result and not result["hooks"]:
        del result["hooks"]
    return result


def redact(settings: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(settings)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            headers = node.get("headers")
            if isinstance(headers, dict) and "Authorization" in headers:
                headers["Authorization"] = "Bearer ***"
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(result)
    return result


def _default_confirm(prompt: str) -> bool:
    return sys.stdin.isatty() and input(prompt).strip().lower() == "y"


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        print(f"cannot parse {path}", file=sys.stderr)
        return None
    return data


def _write(path: Path, settings: dict[str, Any]) -> None:
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + ".trajectree.bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")


def _hook_tokens(api: Any) -> list[dict[str, Any]]:
    return [t for t in api.get("/api/tokens") if t["label"] == HOOK_LABEL]


def install(
    api: Any,
    env: Mapping[str, str],
    *,
    out: TextIO,
    confirm: Callable[[str], bool] = _default_confirm,
    yes: bool = False,
    settings_file: Path | None = None,
) -> int:
    path = settings_file or settings_path(env)
    current = _load(path)
    if current is None:
        return 1
    old_tokens = _hook_tokens(api)
    created = api.post(
        "/api/tokens",
        {"label": HOOK_LABEL, "scope": {"projects": [], "tools": ["hooks"], "write": False}},
    )
    merged = merge_hooks(current, hooks_url(api.base), created["secret"])
    shown = redact(merged)["hooks"]
    print(f"settings file: {path}", file=out)
    print(json.dumps({ev: shown[ev] for ev in HOOK_EVENTS}, indent=2), file=out)
    print(f"This writes {path} (backup at {path}.trajectree.bak).", file=out)
    if not (yes or confirm("Proceed? [y/N] ")):
        api.delete(f"/api/tokens/{created['id']}")
        print("aborted", file=out)
        return 1
    try:
        _write(path, merged)
    except OSError as exc:
        api.delete(f"/api/tokens/{created['id']}")
        print(f"cannot write {path}: {exc}", file=sys.stderr)
        return 1
    for token in old_tokens:
        api.delete(f"/api/tokens/{token['id']}")
    print(f"hooks: installed ({len(HOOK_EVENTS)} events)", file=out)
    return 0


def uninstall(
    api: Any, env: Mapping[str, str], *, out: TextIO, settings_file: Path | None = None
) -> int:
    path = settings_file or settings_path(env)
    current = _load(path)
    if current is None:
        return 1
    if path.exists():
        _write(path, remove_hooks(current, hooks_url(api.base)))
    for token in _hook_tokens(api):
        api.delete(f"/api/tokens/{token['id']}")
    print("hooks: removed", file=out)
    return 0


def show(
    api: Any, env: Mapping[str, str], *, out: TextIO, settings_file: Path | None = None
) -> int:
    path = settings_file or settings_path(env)
    current = _load(path)
    if current is None:
        return 1
    url = hooks_url(api.base)
    for event in HOOK_EVENTS:
        present = any(
            h.get("url") == url for g in current.get("hooks", {}).get(event, []) for h in g.get("hooks", [])
        )
        print(f"{event}: {'present' if present else 'absent'}", file=out)
    print(f"token: {'present' if _hook_tokens(api) else 'absent'}", file=out)
    return 0

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

HOOK_EVENTS = ("SessionStart", "Stop", "SessionEnd")
HOOK_LABEL = "hooks"
HOOK_PATH = "/api/hooks/claude"


def token_label(path: Path) -> str:
    """One hook token per settings file, so installing into one never revokes another's."""
    return f"{HOOK_LABEL}:{hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:8]}"


def hooks_url(base: str) -> str:
    return base.rstrip("/") + HOOK_PATH


def settings_path(env: Mapping[str, str]) -> Path:
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return (Path(config_dir) / "settings.json").resolve()
    home = env.get("HOME") if env is not os.environ else None
    return ((Path(home) if home else Path.home()) / ".claude" / "settings.json").resolve()


def handler(url: str, secret: str) -> dict[str, Any]:
    return {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {secret}"}, "timeout": 3}


def _ours(h: dict[str, Any]) -> bool:
    """A handler Trajectree wrote: it owns this path on whatever host and port it was installed for."""
    return urlsplit(str(h.get("url", ""))).path == HOOK_PATH


def _strip(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = []
    for group in groups:
        hooks = [h for h in group.get("hooks", []) if not _ours(h)]
        if hooks:
            kept.append({**group, "hooks": hooks})
    return kept


def merge_hooks(settings: dict[str, Any], url: str, secret: str) -> dict[str, Any]:
    merged = copy.deepcopy(settings)
    hooks = merged.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        groups = _strip(hooks.get(event, []))
        groups.append({"hooks": [handler(url, secret)]})
        hooks[event] = groups
    return merged


def remove_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(settings)
    hooks = result.get("hooks", {})
    for event in HOOK_EVENTS:
        if event in hooks:
            groups = _strip(hooks[event])
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


def _well_formed(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    if hooks is None:
        return "hooks" not in data
    if not isinstance(hooks, dict):
        return False
    for groups in hooks.values():
        if not isinstance(groups, list):
            return False
        for group in groups:
            if not isinstance(group, dict):
                return False
            handlers = group.get("hooks", [])
            if not isinstance(handlers, list) or not all(isinstance(h, dict) for h in handlers):
                return False
    return True


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not _well_formed(data):
        print(f"cannot parse {path}", file=sys.stderr)
        return None
    return data


def _write(path: Path, settings: dict[str, Any]) -> None:
    backup = path.with_name(path.name + ".trajectree.bak")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    # Write a sibling and rename it over the file, so a crash never leaves a half-written
    # settings file. A new file holds a hook token, so it starts private.
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        if existed:
            shutil.copymode(path, tmp)  # an existing file keeps its mode
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if not existed:
        path.chmod(0o600)


def _hook_tokens(api: Any, path: Path) -> list[dict[str, Any]]:
    label = token_label(path)
    return [t for t in api.get("/api/tokens") if t["label"] == label]


def install(
    api: Any,
    env: Mapping[str, str],
    *,
    out: TextIO,
    confirm: Callable[[str], bool] = _default_confirm,
    yes: bool = False,
    settings_file: Path | None = None,
) -> int:
    path = (settings_file or settings_path(env)).resolve()
    current = _load(path)
    if current is None:
        return 1
    old_tokens = _hook_tokens(api, path)
    created = api.post(
        "/api/tokens",
        {"label": token_label(path), "scope": {"projects": [], "tools": ["hooks"], "write": False}},
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
    # Tokens from before the per-file label were all named plain `hooks`.
    for token in api.get("/api/tokens"):
        if token["label"] == HOOK_LABEL:
            api.delete(f"/api/tokens/{token['id']}")
            print(f"rotated legacy token {token['id']}", file=out)
    print(f"hooks: installed ({len(HOOK_EVENTS)} events)", file=out)
    return 0


def uninstall(
    api: Any, env: Mapping[str, str], *, out: TextIO, settings_file: Path | None = None
) -> int:
    path = (settings_file or settings_path(env)).resolve()
    current = _load(path)
    if current is None:
        return 1
    cleaned = remove_hooks(current)
    if cleaned != current:
        try:
            _write(path, cleaned)
        except OSError as exc:
            print(f"cannot write {path}: {exc}", file=sys.stderr)
            return 1
    for token in _hook_tokens(api, path):
        api.delete(f"/api/tokens/{token['id']}")
    print("hooks: removed", file=out)
    return 0


def show(
    api: Any, env: Mapping[str, str], *, out: TextIO, settings_file: Path | None = None
) -> int:
    path = (settings_file or settings_path(env)).resolve()
    current = _load(path)
    if current is None:
        return 1
    url = hooks_url(api.base)
    for event in HOOK_EVENTS:
        ours = [h for g in current.get("hooks", {}).get(event, []) for h in g.get("hooks", []) if _ours(h)]
        if any(h.get("url") == url for h in ours):
            state = "present"
        elif ours:
            state = f"stale ({ours[0].get('url')}); run: trajectree hooks install"
        else:
            state = "absent"
        print(f"{event}: {state}", file=out)
    print(f"token: {'present' if _hook_tokens(api, path) else 'absent'}", file=out)
    return 0

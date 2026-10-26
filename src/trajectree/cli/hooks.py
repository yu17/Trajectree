from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import shutil
import sys
import tomllib
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

HOOK_EVENTS = ("SessionStart", "Stop", "SessionEnd")
HOOK_LABEL = "hooks"
HOOK_PATH = "/api/hooks/claude"
CODEX_HOOK_PATH = "/api/hooks/codex"
CODEX_EVENTS = ("SessionStart", "Stop", "SessionEnd")
CODEX_TRUST_EVENT = {"SessionStart": "session_start", "Stop": "stop", "SessionEnd": "session_end"}  # config.toml keys
CODEX_LABEL = "hooks-codex"
POST_TIMEOUT_S = 3


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
    home = codex_home(env)
    # Validate the Codex file before anything is minted, confirmed or written.
    if home is not None and _codex_load(codex_hooks_file(home)) is None:
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
    codex_on = codex_home(env) is not None
    if codex_on:
        _codex_plan(env, out)
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
    if codex_on:
        return install_codex(api, env, out=out, yes=True, confirm=confirm)  # the one confirmation covered it
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
    if codex_home(env) is not None:
        return uninstall_codex(api, env, out=out)
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
    if codex_home(env) is not None:
        return show_codex(api, env, out=out)
    return 0


# ---- Codex: command hooks that post to the daemon with a liveness-only token ----


def codex_home(env: Mapping[str, str]) -> Path | None:
    """$CODEX_HOME, else $HOME/.codex; the process's real home only when env is the real environment.

    None when the directory does not exist, so an env dict without HOME or CODEX_HOME never reaches
    the real ~/.codex.
    """
    if env.get("CODEX_HOME"):
        home: Path | None = Path(env["CODEX_HOME"])
    elif env.get("HOME"):
        home = Path(env["HOME"]) / ".codex"
    elif env is os.environ:
        home = Path.home() / ".codex"
    else:
        home = None
    return home if home is not None and home.is_dir() else None


def codex_hooks_file(home: Path) -> Path:
    return home / "hooks.json"


def codex_token_file(env: Mapping[str, str]) -> Path:
    from trajectree.cli.main import _config_file

    return _config_file(env, "codex-hook-token")


def codex_token_label(hooks_file: Path) -> str:
    return f"{CODEX_LABEL}:{hashlib.sha256(str(hooks_file).encode()).hexdigest()[:8]}"


def codex_handler(token_file: Path) -> dict[str, Any]:
    return {
        "type": "command",
        "command": f"trajectree hooks post --token-file {shlex.quote(str(token_file))}",
        "timeout": 3,
        "async": True,
    }


def _ours_codex(h: dict[str, Any]) -> bool:
    return h.get("type") == "command" and "trajectree hooks post" in str(h.get("command", ""))


def _codex_events(doc: dict[str, Any], *, create: bool = False) -> dict[str, Any]:
    """The event -> groups mapping: under a top-level `hooks` key when the file uses one."""
    if isinstance(doc.get("hooks"), dict):
        return doc["hooks"]
    if create and not doc:
        return doc.setdefault("hooks", {})
    return doc


def _codex_well_formed(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if "hooks" in data and not isinstance(data["hooks"], dict):
        return False
    for groups in _codex_events(data).values():
        if not isinstance(groups, list):
            return False
        for group in groups:
            if not isinstance(group, dict):
                return False
            handlers = group.get("hooks", [])
            if not isinstance(handlers, list) or not all(isinstance(h, dict) for h in handlers):
                return False
    return True


def _codex_load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not _codex_well_formed(data):
        print(f"cannot parse {path}", file=sys.stderr)
        return None
    return data


def _strip_codex(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = []
    for group in groups:
        hooks = [h for h in group.get("hooks", []) if not _ours_codex(h)]
        if hooks:
            kept.append({**group, "hooks": hooks})
    return kept


def merge_codex_hooks(doc: dict[str, Any], token_file: Path) -> dict[str, Any]:
    merged = copy.deepcopy(doc)
    events = _codex_events(merged, create=True)
    for event in CODEX_EVENTS:
        # Codex keys trust by position: replace our handler where it already sits, so no owner
        # group moves; only when ours is absent does it go last.
        groups, placed = [], False
        for group in events.get(event, []):
            hooks = []
            for h in group.get("hooks", []):
                if not _ours_codex(h):
                    hooks.append(h)
                elif not placed:
                    hooks.append(codex_handler(token_file))
                    placed = True
            if hooks:
                groups.append({**group, "hooks": hooks})
        if not placed:
            groups.append({"hooks": [codex_handler(token_file)]})
        events[event] = groups
    return merged


def remove_codex_hooks(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Without our groups, and the events where a group of the owner's came after ours (their positions shift)."""
    result = copy.deepcopy(doc)
    events = _codex_events(result)
    shifted = []
    for event in CODEX_EVENTS:
        if event not in events:
            continue
        original = events[event]
        removed = [
            i for i, g in enumerate(original)
            if g.get("hooks") and all(_ours_codex(h) for h in g["hooks"])
        ]
        groups = _strip_codex(original)
        if removed and len(original) - 1 > removed[0]:
            shifted.append(event)
        if groups:
            events[event] = groups
        else:
            del events[event]
    if "hooks" in result and not result["hooks"]:
        del result["hooks"]
    return result, shifted


def _toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def codex_trusted(config_toml: Path, hooks_file: Path, event: str, group_index: int) -> bool:
    """Whether Codex holds a trusted_hash for our (first) handler in this group."""
    state = _toml(config_toml).get("hooks", {}).get("state", {})
    snake = CODEX_TRUST_EVENT.get(event, event)
    key = f"{hooks_file.absolute()}:{snake}:{group_index}:0"
    entry = state.get(key) if isinstance(state, dict) else None
    return isinstance(entry, dict) and bool(entry.get("trusted_hash"))


def _write_secret(path: Path, secret: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)  # a stale temp may carry a broader mode
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secret + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _codex_plan(env: Mapping[str, str], out: TextIO) -> None:
    home = codex_home(env)
    assert home is not None
    hooks_file = codex_hooks_file(home)
    target = hooks_file.resolve()
    shown = f"{hooks_file} -> {target}" if target != hooks_file.absolute() else str(hooks_file)
    print(f"codex hooks file: {shown} (backup at {target}.trajectree.bak)", file=out)
    print(f"codex token file: {codex_token_file(env)}", file=out)


def _codex_tokens(api: Any, hooks_file: Path) -> list[dict[str, Any]]:
    label = codex_token_label(hooks_file)
    return [t for t in api.get("/api/tokens") if t["label"] == label]


def install_codex(
    api: Any,
    env: Mapping[str, str],
    *,
    out: TextIO,
    yes: bool = False,
    confirm: Callable[[str], bool] = _default_confirm,
) -> int:
    home = codex_home(env)
    if home is None:
        return 0
    hooks_file = codex_hooks_file(home)
    token_file = codex_token_file(env)
    current = _codex_load(hooks_file)
    if current is None:
        return 1
    if not yes:
        _codex_plan(env, out)
        if not confirm("Proceed? [y/N] "):
            print("aborted", file=out)
            return 1
    old_tokens = _codex_tokens(api, hooks_file)
    created = api.post(
        "/api/tokens",
        {"label": codex_token_label(hooks_file), "scope": {"projects": [], "tools": ["hooks"], "write": False}},
    )
    try:
        _write_secret(token_file, created["secret"])
        # Written through a symlink to its target, as the Claude settings file is; the label and
        # Codex's trust keys keep the path Codex reads.
        _write(hooks_file.resolve(), merge_codex_hooks(current, token_file))
    except OSError as exc:
        api.delete(f"/api/tokens/{created['id']}")
        print(f"cannot write codex hooks: {exc}", file=sys.stderr)
        return 1
    for token in old_tokens:
        api.delete(f"/api/tokens/{token['id']}")
    print(f"codex hooks: installed ({len(CODEX_EVENTS)} events); Codex will ask to trust them on its next start", file=out)
    config = home / "config.toml"
    if _toml(config).get("features", {}).get("hooks") is not True:
        print(f"codex hooks: enable them with [features] hooks = true in {config}", file=out)
    return 0


def uninstall_codex(api: Any, env: Mapping[str, str], *, out: TextIO) -> int:
    home = codex_home(env)
    if home is None:
        return 0
    hooks_file = codex_hooks_file(home)
    current = _codex_load(hooks_file)
    if current is None:
        return 1
    cleaned, shifted = remove_codex_hooks(current)
    if cleaned != current:
        try:
            _write(hooks_file.resolve(), cleaned)  # through a symlink, like install
        except OSError as exc:
            print(f"cannot write {hooks_file}: {exc}", file=sys.stderr)
            return 1
    for token in _codex_tokens(api, hooks_file):
        api.delete(f"/api/tokens/{token['id']}")
    codex_token_file(env).unlink(missing_ok=True)
    if shifted:
        print(f"codex hooks: removed; groups after Trajectree's shifted: re-trust {', '.join(shifted)}", file=out)
    else:
        print("codex hooks: removed", file=out)
    return 0


def show_codex(api: Any, env: Mapping[str, str], *, out: TextIO) -> int:
    home = codex_home(env)
    if home is None:
        return 0
    hooks_file = codex_hooks_file(home)
    current = _codex_load(hooks_file)
    if current is None:
        return 1
    events = _codex_events(current)
    for event in CODEX_EVENTS:
        groups = events.get(event, [])
        index = next(
            (i for i, g in enumerate(groups) if any(_ours_codex(h) for h in g.get("hooks", []))), None
        )
        if index is None:
            state = "absent"
        elif codex_trusted(home / "config.toml", hooks_file, event, index):
            state = "present (trusted)"
        else:
            state = "present (not yet trusted)"
        print(f"codex {event}: {state}", file=out)
    print(f"codex token: {'present' if _codex_tokens(api, hooks_file) else 'absent'}", file=out)
    return 0


def post(env: Mapping[str, str], *, token_file: Path, stdin: TextIO) -> int:
    """The command hook Codex runs: forward its stdin JSON to the daemon. Silent, and always 0."""
    try:
        import httpx

        from trajectree.cli.main import base_url

        secret = token_file.read_text(encoding="utf-8").strip()
        body = stdin.read()
        httpx.post(
            base_url(env) + CODEX_HOOK_PATH,
            content=body.encode("utf-8"),
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            timeout=POST_TIMEOUT_S,
        )
    except Exception:
        pass
    return 0

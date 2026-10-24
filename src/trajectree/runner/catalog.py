"""The model catalog: which models each runner offers, refreshed explicitly; `resolve_model`
picks the runner for the model a run request names.

`discover` does the network and file reads and touches no database; the caller stores the
result under a short lock (`refresh`). Errors name only an HTTP status or exception class
and a host, never a token, key or full URL.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
from datetime import datetime
from typing import Any, Callable, Literal
from urllib.parse import unquote, urlsplit, urlunsplit

import apsw
import httpx
from pydantic import AwareDatetime, BaseModel

from trajectree.config import Settings
from trajectree.db.ledger import Ledger
from trajectree.models import NO_MODEL_CHOSEN
from trajectree.repo.state import get_state, set_state
from trajectree.runner.credentials import load_oauth_token, profile_has_credentials, read_profile
from trajectree.runner.safe_io import MiB, read_regular
from trajectree.timeutil import utcnow

STATE_KEY = "models:catalog"
BUILTIN_CLAUDE_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models?limit=1000"
MODELS_CACHE_MAX_BYTES = 8 * MiB
NO_CODEX_CACHE = "no model cache yet; run one Codex run with --runner codex --model <slug> (or use Codex once) and refresh"
_PROFILE_KEY = re.compile(r"^(ANTHROPIC_MODEL|ANTHROPIC_DEFAULT_[A-Z]+_MODEL|CLAUDE_CODE_SUBAGENT_MODEL)$")

Source = Literal["api", "cache", "profile", "builtin"]


class CatalogModel(BaseModel):
    runner: str
    model: str
    display: str | None = None
    source: Source


class RunnerCatalog(BaseModel):
    runner: str
    available: bool
    models: list[CatalogModel]
    refreshed_at: AwareDatetime | None
    error: str | None


class Catalog(BaseModel):
    runners: list[RunnerCatalog]


def _get_models(
    http: httpx.Client, url: str, headers: dict[str, str], timeout_s: float
) -> tuple[list[dict[str, Any]], str | None]:
    """The `data` entries of a models endpoint, or a short error: an HTTP status or an exception class."""
    try:
        resp = http.get(url, headers=headers, timeout=timeout_s)
    except Exception as exc:
        return [], type(exc).__name__
    if resp.status_code != 200:
        return [], str(resp.status_code)
    try:
        data = resp.json().get("data")
    except Exception:
        return [], "unreadable response"
    if not isinstance(data, list):
        return [], "unreadable response"
    return [d for d in data if isinstance(d, dict) and isinstance(d.get("id"), str)], None


def _claude(settings: Settings, now: datetime, http: httpx.Client, which, timeout_s: float) -> RunnerCatalog:
    def builtin(error: str, *, available: bool) -> RunnerCatalog:
        models = [CatalogModel(runner="claude", model=m, source="builtin") for m in BUILTIN_CLAUDE_MODELS]
        return RunnerCatalog(runner="claude", available=available, models=models, refreshed_at=now, error=error)

    if which(settings.claude_bin) is None:
        return RunnerCatalog(runner="claude", available=False, models=[], refreshed_at=now, error="claude binary not found")
    token = load_oauth_token(settings)
    if token is None:
        return RunnerCatalog(runner="claude", available=False, models=[], refreshed_at=now, error="not logged in; run: trajectree login")
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20", "anthropic-version": "2023-06-01"}
    entries, error = _get_models(http, ANTHROPIC_MODELS_URL, headers, timeout_s)
    if error is None and entries:
        models = [CatalogModel(runner="claude", model=e["id"], display=e.get("display_name"), source="api") for e in entries]
        return RunnerCatalog(runner="claude", available=True, models=models, refreshed_at=now, error=None)
    return builtin(f"live list unavailable: {error or 'empty list'}", available=True)


def _codex(settings: Settings, now: datetime, which) -> RunnerCatalog:
    def result(models: list[CatalogModel], error: str | None, *, available: bool = True) -> RunnerCatalog:
        return RunnerCatalog(runner="codex", available=available, models=models, refreshed_at=now, error=error)

    if which(settings.codex_bin) is None:
        return result([], "codex binary not found", available=False)
    logged_in = (settings.codex_home / "auth.json").exists()
    try:
        # Codex runs write the Codex home: never follow a symlink or block on a FIFO there.
        raw = read_regular(settings.codex_home / "models_cache.json", max_bytes=MODELS_CACHE_MAX_BYTES)
        if raw is None:
            # Logged in, still available: a run naming --runner codex and a model creates the cache.
            return result([], NO_CODEX_CACHE if logged_in else "no model cache; log in: codex login --device-auth")
        entries = json.loads(raw)["models"]
        if not isinstance(entries, list):
            raise ValueError("models is not a list")
    except (OSError, ValueError, KeyError, TypeError):
        return result([], "model cache unreadable; run one Codex run and refresh" if logged_in
                      else "no model cache; log in: codex login --device-auth")
    models = [
        CatalogModel(runner="codex", model=e["slug"], display=e.get("display_name"), source="cache")
        for e in entries
        if isinstance(e, dict) and isinstance(e.get("slug"), str) and e.get("visibility") != "hide"
    ]
    return result(models, None)


def _profile_env(settings: Settings, name: str) -> dict[str, str]:
    env = (read_profile(settings.backends_cfg_dir / name / "settings.json") or {}).get("env")
    return {k: v for k, v in env.items() if isinstance(v, str)} if isinstance(env, dict) else {}


def _ccx(settings: Settings, name: str, now: datetime, http: httpx.Client, which, timeout_s: float) -> RunnerCatalog:
    runner = f"ccx:{name}"

    def unavailable(error: str) -> RunnerCatalog:
        return RunnerCatalog(runner=runner, available=False, models=[], refreshed_at=now, error=error)

    if which(settings.claude_bin) is None:
        return unavailable("claude binary not found")
    if not profile_has_credentials(settings.backends_cfg_dir / name / "settings.json"):
        return unavailable("no credentials in the profile; run: trajectree backends sync")
    env = _profile_env(settings, name)
    seen: dict[str, CatalogModel] = {}
    for key in sorted(env):
        if _PROFILE_KEY.match(key) and env[key] and env[key] not in seen:
            seen[env[key]] = CatalogModel(runner=runner, model=env[key], source="profile")
    error: str | None = None
    base = env.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    if base:
        headers = {"anthropic-version": "2023-06-01"}
        if env.get("ANTHROPIC_API_KEY"):
            headers["x-api-key"] = env["ANTHROPIC_API_KEY"]
        elif env.get("ANTHROPIC_AUTH_TOKEN"):
            headers["Authorization"] = f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}"
        # httpx logs the request URL, so userinfo must never be in it: it moves to a header.
        parts = urlsplit(base)
        if parts.username is not None:
            base = urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))
            if "x-api-key" not in headers and "Authorization" not in headers:
                pair = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
                headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
        url = base + ("/models"if base.endswith("/v1") else "/v1/models")
        entries, err = _get_models(http, url, headers, timeout_s)
        if err:
            try:
                host = httpx.URL(url).host
            except httpx.InvalidURL:
                host = "invalid url"
            error = f"models endpoint unavailable: {err} ({host})"
        for e in entries:
            if e["id"] not in seen:
                seen[e["id"]] = CatalogModel(runner=runner, model=e["id"], display=e.get("display_name"), source="api")
    return RunnerCatalog(runner=runner, available=True, models=list(seen.values()), refreshed_at=now, error=error)


def discover(
    settings: Settings,
    *,
    now: datetime,
    http: httpx.Client,
    which: Callable[[str], str | None] = shutil.which,
    timeout_s: float = 5.0,
) -> Catalog:
    """Ask every runner what it offers. No database access; HTTP only through `http`."""
    def guarded(runner: str, ask: Callable[[], RunnerCatalog]) -> RunnerCatalog:
        # One runner's unexpected failure (say a malformed base URL) is its own error only; the
        # class name alone, since an exception's text may carry a URL or key.
        try:
            return ask()
        except Exception as exc:
            return RunnerCatalog(runner=runner, available=False, models=[], refreshed_at=now, error=type(exc).__name__)

    runners = [
        guarded("claude", lambda: _claude(settings, now, http, which, timeout_s)),
        guarded("codex", lambda: _codex(settings, now, which)),
    ]
    if settings.backends_cfg_dir.is_dir():
        for d in sorted(settings.backends_cfg_dir.iterdir()):
            if d.is_dir():
                runners.append(guarded(f"ccx:{d.name}", lambda name=d.name: _ccx(settings, name, now, http, which, timeout_s)))
    return Catalog(runners=runners)


def store_catalog(conn: apsw.Connection, catalog: Catalog) -> None:
    set_state(conn, STATE_KEY, catalog.model_dump(mode="json"))


def load_catalog(conn: apsw.Connection) -> Catalog:
    raw = get_state(conn, STATE_KEY)
    try:
        return Catalog.model_validate(raw) if raw else Catalog(runners=[])
    except ValueError:
        return Catalog(runners=[])


def merge_failed(previous: Catalog, fresh: Catalog) -> Catalog:
    """A runner whose refresh failed keeps the models it last listed, with the new error.

    Profile models come from a local file and are always current, so only the previous live
    and cached entries are kept; a shipped fallback list never replaces a live one.
    """
    before = {r.runner: r for r in previous.runners}
    merged: list[RunnerCatalog] = []
    for r in fresh.runners:
        old = before.get(r.runner)
        kept = [m for m in old.models if m.source in ("api", "cache")] if old else []
        if r.error is None or not r.available or old is None or not kept:
            merged.append(r)
            continue
        have = {m.model for m in kept}
        profile = [m for m in r.models if m.source == "profile" and m.model not in have]
        merged.append(r.model_copy(update={"models": profile + kept, "refreshed_at": old.refreshed_at}))
    return Catalog(runners=merged)


def refresh(
    ledger: Ledger, settings: Settings, http: httpx.Client, *, which=shutil.which, timeout_s: float = 5.0
) -> Catalog:
    """Discover with no lock held, then store under a short one."""
    fresh = discover(settings, now=utcnow(), http=http, which=which, timeout_s=timeout_s)
    with ledger.locked() as conn:
        merged = merge_failed(load_catalog(conn), fresh)
        store_catalog(conn, merged)
    return merged


# -- model choice --------------------------------------------------------------------------


class ModelChoiceError(ValueError):
    """The model a request names cannot be run; the message says what to do (HTTP 422)."""


class NoModelChosen(ModelChoiceError):
    pass


class UnknownModel(ModelChoiceError):
    pass


class AmbiguousModel(ModelChoiceError):
    pass


class RunnerUnavailable(ModelChoiceError):
    pass


class Resolved(BaseModel):
    runner: str
    model: str
    warning: str | None = None


def resolve_model(catalog: Catalog, model: str | None, runner: str | None) -> Resolved:
    """The runner that runs `model`, from the runners the catalog lists as available.

    With `runner` given, a model missing from that runner's list is still accepted, with a
    warning: a shipped or cached list can lag a real new model. Model names pass verbatim.
    """
    if not model:
        raise NoModelChosen(NO_MODEL_CHOSEN)
    available = {r.runner: r for r in catalog.runners if r.available}
    if runner is not None:
        if runner not in available:
            raise RunnerUnavailable(f"runner {runner} is not available; run: trajectree models")
        if any(m.model == model for m in available[runner].models):
            return Resolved(runner=runner, model=model)
        return Resolved(runner=runner, model=model, warning=f"model {model} is not in the catalog for {runner}")
    offering = [name for name, r in available.items() if any(m.model == model for m in r.models)]
    if not offering:
        raise UnknownModel(f"unknown model {model}; run: trajectree models --refresh")
    if len(offering) > 1:
        raise AmbiguousModel(f"model {model} is offered by {', '.join(offering)}; pass --runner")
    return Resolved(runner=offering[0], model=model)

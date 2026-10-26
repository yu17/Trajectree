import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent


def _load(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text())


def test_instance_template_is_self_contained():
    doc = _load("deploy/compose.yaml")
    assert doc["name"] == "trajectree"
    svc = doc["services"]["trajectree"]
    assert svc["build"]["context"] == (
        "${TRAJECTREE_GIT_URL:-https://github.com/yu17/Trajectree.git}#${TRAJECTREE_REF:-main}"
    )
    assert svc["build"]["target"] == "reports"
    assert svc["build"]["args"]["INSTALL_CODEX"] == "1"
    assert "./data:/data" in svc["volumes"]
    assert "./cfg:/cfg" in svc["volumes"]
    assert svc["mem_limit"] == "4g"
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["ports"] == ["127.0.0.1:${TRAJECTREE_HOST_PORT:-8742}:8742"]
    assert svc["environment"]["TRAJECTREE_PUBLIC_URL"] == "http://127.0.0.1:${TRAJECTREE_HOST_PORT:-8742}"
    for key in (
        "TRAJECTREE_INGEST_EVERY",
        "TRAJECTREE_SCAN_SINCE_DAYS",
        "TRAJECTREE_CHATMAID_URL",
        "TRAJECTREE_GITLAB_HOST",
        "TRAJECTREE_TZ",
    ):
        assert key in svc["environment"]


def test_dev_compose_cannot_replace_instance():
    doc = _load("compose.yaml")
    assert doc["name"] == "trajectree-dev"
    assert doc["services"]["trajectree"]["ports"] == ["127.0.0.1:8743:8742"]
    assert doc["services"]["trajectree"]["environment"]["TRAJECTREE_PUBLIC_URL"] == "http://127.0.0.1:8743"


def test_dev_state_lives_outside_the_checkout():
    # The checkout sits under the projects root: dev tokens there would be readable by instance runs.
    volumes = _load("compose.yaml")["services"]["trajectree"]["volumes"]
    state = "${TRAJECTREE_DEV_STATE:-${HOME}/.local/share/trajectree-dev}"
    assert f"{state}/data:/data" in volumes and f"{state}/cfg:/cfg" in volumes
    assert not any(isinstance(v, str) and v.startswith(".") for v in volumes)


def test_env_example_names_every_template_variable():
    template = (ROOT / "deploy/compose.yaml").read_text()
    # ${VAR}, ${VAR:-default} and ${VAR:?msg} all name VAR up to the first ':' or '}'.
    names = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)[:}]", template)) - {"HOME", "UID", "GID"}
    example = (ROOT / "deploy/env.example").read_text()
    missing = {n for n in names if not re.search(rf"^{n}=", example, re.MULTILINE)}
    assert not missing


@pytest.mark.parametrize("path", ["compose.yaml", "deploy/compose.yaml"])
def test_template_mounts_codex_sessions(path):
    svc = _load(path)["services"]["trajectree"]
    assert "${HOME}/.codex/sessions:/host/codex/sessions:ro" in svc["volumes"]
    assert svc["environment"]["TRAJECTREE_CODEX_SESSIONS"] == "/host/codex/sessions"
    # only the transcripts: never the Codex home with auth.json and config.toml
    strs = [v for v in svc["volumes"] if isinstance(v, str)]
    assert not any(v.startswith("${HOME}/.codex:") or v.startswith("${HOME}/.codex/auth") for v in strs)


@pytest.mark.parametrize("path", ["compose.yaml", "deploy/compose.yaml"])
def test_projects_root_must_exist(path):
    # A wrong path must stop compose, not mount an empty root-owned folder Docker made for it.
    svc = _load(path)["services"]["trajectree"]
    [mount] = [v for v in svc["volumes"] if isinstance(v, dict) and v["target"] == "/projects"]
    assert mount["type"] == "bind" and mount["read_only"] is True
    assert mount["bind"] == {"create_host_path": False}
    assert mount["source"].startswith("${TRAJECTREE_PROJECTS_ROOT:?")


@pytest.mark.parametrize("path", ["compose.yaml", "deploy/compose.yaml"])
def test_segment_and_inspect_variables_pass_through(path):
    env = _load(path)["services"]["trajectree"]["environment"]
    for key in (
        "TRAJECTREE_SEGMENT_GAP_S",
        "TRAJECTREE_SESSION_IDLE_S",
        "TRAJECTREE_INSPECT_BUDGET_CHARS",
    ):
        assert key in env


def test_env_example_leaves_projects_root_empty():
    # Empty, so compose's ${TRAJECTREE_PROJECTS_ROOT:?...} stops an install that never set it.
    example = (ROOT / "deploy/env.example").read_text()
    assert re.search(r"^TRAJECTREE_PROJECTS_ROOT=$", example, re.MULTILINE)

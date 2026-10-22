from trajectree.config import load_settings


def test_runner_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAJECTREE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("TRAJECTREE_CFG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TRAJECTREE_CLAUDE_BIN", "/opt/claude")
    monkeypatch.setenv("TRAJECTREE_DEFAULT_RUNNER", "ccx:kimi")
    monkeypatch.setenv("TRAJECTREE_RUN_ENV_PASSTHROUGH", "HTTPS_PROXY, NO_PROXY")
    s = load_settings()
    assert s.cfg_root == tmp_path / "cfg" and s.claude_bin == "/opt/claude" and s.default_runner == "ccx:kimi"
    assert s.claude_config_dir == tmp_path / "cfg" / "claude" and s.runs_dir == tmp_path / "d" / "runs"
    assert s.run_env_passthrough == ["HTTPS_PROXY", "NO_PROXY"]


def test_cfg_defaults_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAJECTREE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TRAJECTREE_CFG_DIR", raising=False)
    s = load_settings()
    assert s.cfg_root == tmp_path / "cfg" and s.run_env_passthrough == [] and s.default_runner == "claude"

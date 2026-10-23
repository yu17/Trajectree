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


def test_slurm_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAJECTREE_DATA_DIR", str(tmp_path))
    for var in ("HOST", "USER", "KEY", "EVERY_M"):
        monkeypatch.delenv(f"TRAJECTREE_SLURM_{var}", raising=False)
    monkeypatch.delenv("TRAJECTREE_SSH_BIN", raising=False)
    s = load_settings()
    assert s.slurm_host is None and s.slurm_user is None and s.slurm_key is None
    assert s.slurm_every_m == 15 and s.ssh_bin == "ssh"
    monkeypatch.setenv("TRAJECTREE_SLURM_HOST", "login.hpc")
    monkeypatch.setenv("TRAJECTREE_SLURM_USER", "u")
    monkeypatch.setenv("TRAJECTREE_SLURM_KEY", "/keys/id")
    monkeypatch.setenv("TRAJECTREE_SLURM_EVERY_M", "5")
    monkeypatch.setenv("TRAJECTREE_SSH_BIN", "/usr/bin/ssh")
    s = load_settings()
    assert (s.slurm_host, s.slurm_user, s.slurm_every_m, s.ssh_bin) == ("login.hpc", "u", 5, "/usr/bin/ssh")
    assert str(s.slurm_key) == "/keys/id"
    monkeypatch.setenv("TRAJECTREE_SLURM_KEY", "")
    assert load_settings().slurm_key is None


def test_marp_and_chrome_bins_from_env(monkeypatch, tmp_path):
    from trajectree.config import load_settings

    monkeypatch.setenv("TRAJECTREE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TRAJECTREE_MARP_BIN", raising=False)
    monkeypatch.delenv("TRAJECTREE_CHROME_BIN", raising=False)
    s = load_settings()
    assert s.marp_bin is None and s.chrome_bin is None and s.reports_dir == tmp_path / "exports" / "reports"
    monkeypatch.setenv("TRAJECTREE_MARP_BIN", "/opt/marp")
    monkeypatch.setenv("TRAJECTREE_CHROME_BIN", "/usr/bin/chromium")
    s = load_settings()
    assert (s.marp_bin, s.chrome_bin) == ("/opt/marp", "/usr/bin/chromium")
    monkeypatch.setenv("TRAJECTREE_MARP_BIN", "")
    assert load_settings().marp_bin is None


def test_ensure_cfg_dirs_creates_private_ssh_dir(settings):
    from trajectree.runner.credentials import ensure_cfg_dirs

    ensure_cfg_dirs(settings)
    ssh = settings.run_home / ".ssh"
    assert ssh.is_dir() and ssh.stat().st_mode & 0o777 == 0o700

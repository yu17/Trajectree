from pathlib import Path
from trajectree.config import Settings, load_settings
from trajectree.connectors.paths import encode_project_dir, to_local

def test_to_local_maps_longest_component_prefix(tmp_path):
    s = Settings(data_dir=tmp_path, path_map=[("/mnt/apps", "/projects"), ("/mnt/apps/big", "/big")])
    assert to_local(s, "/mnt/apps/x/y") == Path("/projects/x/y")
    assert to_local(s, "/mnt/apps/big/z") == Path("/big/z")
    assert to_local(s, "/mnt/appsx") == Path("/mnt/appsx")

def test_encode_project_dir():
    assert encode_project_dir("/mnt/Apps/Projects/Trajectree") == "-mnt-Apps-Projects-Trajectree"
    assert encode_project_dir("/home/u/my proj.v2") == "-home-u-my-proj-v2"

def test_load_settings_parses_phase2_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRAJECTREE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRAJECTREE_PATH_MAP", "/mnt/apps=/projects;/data=/d")
    monkeypatch.setenv("TRAJECTREE_CLAUDE_HOME", "")
    monkeypatch.setenv("TRAJECTREE_INGEST_EVERY", "0")
    s = load_settings()
    assert s.path_map == [("/mnt/apps", "/projects"), ("/data", "/d")] and s.claude_home is None and s.ingest_interval_s == 0

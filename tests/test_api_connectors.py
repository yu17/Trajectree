def test_store_connector_credentials(client, settings):
    assert client.post("/api/connectors/github/credentials", json={"token": "ghp_x"}).json() == {"stored": True}
    path = settings.connectors_dir / "github_token"
    assert path.stat().st_mode & 0o777 == 0o600
    assert settings.connectors_dir.stat().st_mode & 0o777 == 0o700
    assert path.read_text() == "ghp_x"
    assert client.post("/api/connectors/nope/credentials", json={"token": "x"}).status_code == 404
    assert client.post("/api/connectors/gitlab/credentials", json={"token": ""}).status_code == 422
    assert client.post("/api/connectors/gitlab/credentials", json={"token": "   "}).status_code == 422

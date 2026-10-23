from tests.test_api_entities import mk_project


def test_report_crud_and_export(client):
    mk_project(client)   # slug clmi
    created = client.post("/api/reports", json={"title": "Memo", "kind": "memo", "source_md": "# Hi"})
    assert created.status_code == 201
    r = created.json()
    assert client.get("/api/ledger/txns?limit=1").json()[0]["intent"] == "new report Memo"
    assert client.patch(f"/api/reports/{r['id']}", json={"source_md": "# Hi there"}).json()["source_md"] == "# Hi there"
    txn = client.get("/api/ledger/txns?limit=1").json()[0]
    assert txn["intent"] == "update report Memo" and txn["actor_kind"] == "human"
    assert client.patch(f"/api/reports/{r['id']}", json={"title": "Memo 2"}).json()["title"] == "Memo 2"
    assert client.get("/api/ledger/txns?limit=1").json()[0]["intent"] == "update report Memo 2"
    assert client.get(f"/api/reports/{r['id']}").json()["source_md"] == "# Hi there"
    listed = client.get("/api/reports").json()
    assert [x["id"] for x in listed] == [r["id"]] and listed[0]["pending_txn"] is None
    html = client.get(f"/api/reports/{r['id']}/export?format=html")
    assert html.status_code == 200 and html.headers["content-type"].startswith("text/html") and "<h1>Hi there</h1>" in html.text
    assert html.headers["content-security-policy"] == "sandbox allow-scripts"
    md = client.get(f"/api/reports/{r['id']}/export?format=md")
    assert md.headers["content-type"].startswith("text/markdown") and md.text == "# Hi there"
    pdf = client.get(f"/api/reports/{r['id']}/export?format=pdf")
    assert pdf.status_code == 503 and "marp-cli not available" in pdf.json()["detail"]
    assert client.get(f"/api/reports/{r['id']}/export?format=docx").status_code == 422
    assert client.post("/api/reports/draft", json={"kind": "deck", "title": "D", "projects": ["clmi"]}).status_code == 202


def test_export_through_marp_when_found(client, app):
    from pathlib import Path
    fake = str(Path(__file__).parent / "bin" / "fake-marp")
    app.state.which = lambda name: fake if name == "marp" else None
    r = client.post("/api/reports", json={"title": "Deck", "kind": "deck", "source_md": "# One"}).json()
    pdf = client.get(f"/api/reports/{r['id']}/export?format=pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"FAKE MARP .pdf") and pdf.content.endswith(b"# One")
    html = client.get(f"/api/reports/{r['id']}/export?format=html")
    assert html.text.startswith("FAKE MARP .html")   # marp, not the built-in renderer
    app.state.which = lambda name: "/nonexistent/marp"
    failed = client.get(f"/api/reports/{r['id']}/export?format=pptx")
    assert failed.status_code == 502 and "marp failed to start" in failed.json()["detail"]


def test_report_errors_and_pending(client, app):
    from trajectree.models import Actor, ReportIn
    from trajectree.repo.reports import create_report
    assert client.get("/api/reports/nope").status_code == 404
    assert client.patch("/api/reports/nope", json={"title": "x"}).status_code == 404
    r = client.post("/api/reports", json={"title": "Memo", "kind": "memo"}).json()
    for field in ("title", "kind", "scope", "source_md", "props"):
        assert client.patch(f"/api/reports/{r['id']}", json={field: None}).status_code == 422, field
    assert client.get("/api/reports/nope/export?format=md").status_code == 404
    assert client.post("/api/reports/draft", json={"kind": "deck", "title": "D", "projects": ["zzz"]}).status_code == 422
    assert client.post("/api/reports/draft", json={"kind": "deck", "title": "D", "projects": []}).status_code == 422
    agent = Actor(kind="agent", ref="run_x", model="m", backend="claude")
    drafted = create_report(app.state.ledger, agent, ReportIn(title="Draft", kind="deck"), intent="draft deck")
    listed = client.get("/api/reports").json()
    assert listed[0]["id"] == drafted.id and listed[0]["pending_txn"]

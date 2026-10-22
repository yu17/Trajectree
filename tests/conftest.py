import pytest

from trajectree.db.connection import open_db
from trajectree.db.ledger import Ledger
from trajectree.models import Actor


@pytest.fixture
def conn(tmp_path):
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def ledger(conn):
    return Ledger(conn)


@pytest.fixture
def human():
    return Actor(kind="human", ref="owner", tool="test")


@pytest.fixture
def agent():
    return Actor(kind="agent", ref="run_test", model="claude-opus-5-5", backend="claude")


@pytest.fixture
def settings(tmp_path):
    from trajectree.config import Settings

    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def app(settings, ledger):
    from trajectree.api.app import create_app

    return create_app(settings, ledger=ledger)


@pytest.fixture
def anon(app):
    from fastapi.testclient import TestClient

    # Context-managed so the app lifespan (the MCP session manager) runs.
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(app, anon):
    from fastapi.testclient import TestClient

    # The MCP session manager starts once per app and serves only the event loop
    # its lifespan runs on, so this client sends on anon's portal instead of
    # entering the lifespan a second time.
    c = TestClient(app, headers={"Authorization": f"Bearer {app.state.token}"})
    c.portal = anon.portal
    yield c


@pytest.fixture
def anyio_backend():
    return "asyncio"

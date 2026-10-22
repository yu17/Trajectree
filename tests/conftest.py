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
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app, headers={"Authorization": f"Bearer {app.state.token}"})


@pytest.fixture
def anon(app):
    from fastapi.testclient import TestClient

    return TestClient(app)

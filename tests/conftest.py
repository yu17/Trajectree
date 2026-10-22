import pytest

from trajectree.db.connection import open_db


@pytest.fixture
def conn(tmp_path):
    connection = open_db(tmp_path / "test.db")
    yield connection
    connection.close()

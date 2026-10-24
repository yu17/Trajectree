from __future__ import annotations

import uvicorn

from trajectree.api.app import create_app
from trajectree.config import ensure_token, load_settings
from trajectree.db.connection import open_db
from trajectree.db.ledger import Ledger


def run() -> None:
    settings = load_settings()
    token = ensure_token(settings)
    ledger = Ledger(open_db(settings.db_path))
    app = create_app(settings, ledger=ledger)
    # Inside a container the bind address is not where the owner connects.
    url = (settings.public_url or f"http://{settings.host}:{settings.port}").rstrip("/")
    print(f"Trajectree ready: {url}/?token={token}", flush=True)
    # access_log off: the login URL carries the token in its query string.
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False)

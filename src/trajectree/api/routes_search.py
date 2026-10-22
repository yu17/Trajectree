from __future__ import annotations

from fastapi import APIRouter, Depends

from trajectree.api.deps import get_ledger
from trajectree.db import fts
from trajectree.db.ledger import Ledger

router = APIRouter()


@router.get("/search", response_model=list[fts.SearchHit])
def search(q: str, limit: int = 50, ledger: Ledger = Depends(get_ledger)):
    return fts.search(ledger.conn, q, limit=limit)

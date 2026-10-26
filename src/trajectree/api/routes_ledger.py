from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, BaseModel, Field

from trajectree.api.deps import actor_from_request, get_ledger
from trajectree.api.routes_entities import _csv
from trajectree.db.ledger import AlreadyReverted, Ledger, RevertConflict
from trajectree.models import ActorKind, Change, Conflict, Review, Txn
from trajectree.repo import NotFound

router = APIRouter()


class TxnOut(BaseModel):
    """`Txn` without the raw changeset bytes."""

    id: str
    ts: AwareDatetime
    actor_kind: ActorKind
    actor_ref: str
    model: str | None = None
    backend: str | None = None
    tool: str | None = None
    tool_version: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    proposed_by_run: str | None = None
    intent: str
    review: Review
    review_ts: AwareDatetime | None = None
    reverts_txn: str | None = None
    reverted_by: str | None = None


class TxnDetail(BaseModel):
    txn: TxnOut
    changes: list[Change]


class RevertBody(BaseModel):
    intent: str | None = None


class ReviewBody(BaseModel):
    state: Literal["accepted", "rejected"]


class ReviewManyBody(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    state: Literal["accepted", "rejected"]


class ReviewManyResult(BaseModel):
    done: list[str]
    failed: str | None = None
    conflicts: list[Conflict] = []


def _out(txn: Txn) -> TxnOut:
    return TxnOut.model_validate(txn.model_dump(exclude={"changeset"}))


def _require(ledger: Ledger, txn_id: str) -> Txn:
    txn = ledger.get_txn(txn_id)
    if txn is None:
        raise NotFound(f"transaction {txn_id} not found")
    return txn


@router.get("/ledger/txns", response_model=list[TxnOut])
def list_txns(
    limit: int = Query(default=50, ge=1, le=500),
    before: str | None = None,
    review: Literal["pending", "accepted", "rejected", "none"] | None = None,
    actor_kind: str | None = None,
    run_id: str | None = None,
    ledger: Ledger = Depends(get_ledger),
):
    with ledger.locked():
        txns = ledger.list_txns(
            limit=limit, before=before, review=review, actor_kinds=_csv(actor_kind), run_id=run_id
        )
    return [_out(t) for t in txns]


@router.get("/ledger/txns/{txn_id}", response_model=TxnDetail)
def get_txn(txn_id: str, ledger: Ledger = Depends(get_ledger)):
    with ledger.locked():
        txn = _require(ledger, txn_id)
        changes = ledger.changes_for(txn_id)
    return TxnDetail(txn=_out(txn), changes=changes)


@router.post("/ledger/txns/{txn_id}/revert", response_model=TxnOut)
def revert_txn(
    txn_id: str,
    request: Request,
    body: RevertBody | None = None,
    ledger: Ledger = Depends(get_ledger),
):
    _require(ledger, txn_id)
    intent = body.intent if body else None
    return _out(ledger.revert(txn_id, actor_from_request(request), intent))


@router.post("/ledger/txns/{txn_id}/review", response_model=TxnOut)
def review_txn(
    txn_id: str, body: ReviewBody, request: Request, ledger: Ledger = Depends(get_ledger)
):
    _require(ledger, txn_id)
    return _out(ledger.set_review(txn_id, body.state, actor_from_request(request)))


@router.post("/ledger/review-many", response_model=ReviewManyResult)
def review_many(body: ReviewManyBody, request: Request, ledger: Ledger = Depends(get_ledger)):
    """Accept or reject a group. Rejecting goes newest first so dependents revert before their target."""
    actor = actor_from_request(request)
    ids = list(dict.fromkeys(body.ids))
    if body.state == "rejected":
        with ledger.locked():
            found = {i: ledger.get_txn(i) for i in ids}
            missing = next((i for i in ids if found[i] is None), None)
            if missing is not None:
                return ReviewManyResult(done=[], failed=missing)
            # ids are ULIDs, so (ts, id) breaks ties inside one second.
            ids.sort(key=lambda i: (found[i].ts, i), reverse=True)  # type: ignore[union-attr]
    done: list[str] = []
    for txn_id in ids:
        try:
            ledger.set_review(txn_id, body.state, actor)
        except KeyError:
            return ReviewManyResult(done=done, failed=txn_id)
        except AlreadyReverted:
            if body.state == "accepted":
                return ReviewManyResult(done=done, failed=txn_id)
            # rejecting something already reverted is idempotent
        except RevertConflict as exc:
            return ReviewManyResult(done=done, failed=txn_id, conflicts=exc.conflicts)
        done.append(txn_id)
    return ReviewManyResult(done=done)

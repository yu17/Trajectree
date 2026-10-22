"""The daemon's ingest loop: run now, then every `interval_s` seconds until stopped."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from trajectree.config import Settings
from trajectree.db.ledger import Ledger
from trajectree.ingest.job import run_ingest

log = logging.getLogger("trajectree.ingest")


async def ingest_loop(
    ledger: Ledger,
    settings: Settings,
    *,
    interval_s: float,
    stop: asyncio.Event,
    run: Callable[[Ledger, Settings], Any] = run_ingest,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(run, ledger, settings)
        except Exception:
            log.exception("ingest failed")
        try:
            await asyncio.wait_for(stop.wait(), interval_s)
        except TimeoutError:
            pass

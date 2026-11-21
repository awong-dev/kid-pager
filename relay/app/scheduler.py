"""In-process scheduler: `jobs.tick()` every five minutes, `jobs.sweep()` when 23 hours old."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app import jobs
from app.routing import Routing
from app.store import settings as settings_store

logger = logging.getLogger("relay.scheduler")

FIRST_TICK_DELAY_S = 30.0
TICK_INTERVAL_S = 300.0
SWEEP_INTERVAL = timedelta(hours=23)


def sweep_due(now: datetime) -> bool:
    last = settings_store.get_meta().lastSweepAt
    return last is None or now - last >= SWEEP_INTERVAL


def run_once(routing: Routing, *, now: datetime | None = None) -> None:
    """One slot: a tick, then the sweep if due; one job's failure does not skip the other."""
    try:
        result = jobs.tick(routing)
        settings_store.mark_ticked()
        logger.info("scheduled tick: %s", result)
    except Exception:
        logger.exception("scheduled tick failed")
    try:
        if sweep_due(now or datetime.now(UTC)):
            logger.info("scheduled sweep: %s", jobs.sweep())
    except Exception:
        logger.exception("scheduled sweep failed")


async def run(
    routing: Routing,
    *,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    """Runs until cancelled (the lifespan cancels it on shutdown)."""
    await sleep(FIRST_TICK_DELAY_S)
    while True:
        await asyncio.to_thread(run_once, routing)
        await sleep(TICK_INTERVAL_S)

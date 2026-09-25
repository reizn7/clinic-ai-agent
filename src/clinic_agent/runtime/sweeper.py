"""Background idle sweeper — finalize conversations + generate analytics.

Replicates nextdim's ``TextIdleSweeper`` pattern as an in-process asyncio loop
(fine for a single instance; a multi-replica deploy would move this to a shared
worker). Each tick: close conversations idle past the timeout, then generate
summary/sentiment/language for ended ones that still lack it. Idempotent via
``analyticsStatus``; stuck ``processing`` docs are reclaimed after a cutoff.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from clinic_agent.config import settings
from clinic_agent.runtime import analytics, conversations

log = logging.getLogger("clinic_agent.runtime.sweeper")

_BATCH = 10


async def run_once() -> None:
    now = datetime.now(UTC)
    idle_cutoff = (now - timedelta(minutes=settings.IDLE_TIMEOUT_MINUTES)).isoformat()
    stuck_cutoff = (now - timedelta(minutes=settings.ANALYTICS_STUCK_MINUTES)).isoformat()

    closed = await asyncio.to_thread(conversations.close_idle_active, idle_cutoff)
    if closed:
        log.info("sweeper: closed %d idle conversation(s)", closed)

    if not settings.ANALYTICS_ENABLED:
        return

    batch = await asyncio.to_thread(conversations.claim_analytics_batch, stuck_cutoff, _BATCH)
    for external_id, transcript in batch:
        result = await asyncio.to_thread(analytics.generate_analytics, transcript)
        await asyncio.to_thread(conversations.apply_analytics, external_id, result)
        log.info(
            "sweeper: analytics %s for %s",
            "done" if result else "failed",
            external_id,
        )


async def run_forever() -> None:
    """Loop until cancelled; one tick failing never kills the loop."""
    log.info(
        "idle sweeper started (every %ss, idle %smin)",
        settings.SWEEP_SECONDS,
        settings.IDLE_TIMEOUT_MINUTES,
    )
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sweeper tick failed")
        await asyncio.sleep(settings.SWEEP_SECONDS)

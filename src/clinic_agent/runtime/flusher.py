"""Debounce flusher — the worker loop that drains due message buffers.

Replicates nextdim's ``TextDebounceFlusher``: tick every ``DEBOUNCE_TICK_SECONDS``,
claim each due buffer, join the buffered bubbles into ONE agent turn, run it, and
send the single reply. A transient failure requeues the batch for the next tick.
Started/stopped from the FastAPI lifespan alongside the idle sweeper.
"""

from __future__ import annotations

import asyncio
import logging

from clinic_agent.config import settings
from clinic_agent.runtime import debounce
from clinic_agent.runtime.turn import run_turn
from clinic_agent.whatsapp.client import send_text_message

log = logging.getLogger("clinic_agent.runtime.flusher")


async def _flush_one(phone: str, messages: list[str]) -> None:
    combined = "\n".join(messages)
    try:
        reply = await run_turn(phone, combined)
        await send_text_message(phone, reply)
        log.info("flushed %d bubble(s) for %s", len(messages), phone)
    except Exception:
        log.exception("flush failed for %s; requeueing %d bubble(s)", phone, len(messages))
        await debounce.requeue(phone, messages)


async def run_once() -> None:
    for phone, messages in await debounce.claim_due():
        await _flush_one(phone, messages)


async def run_forever() -> None:
    """Loop until cancelled; one tick failing never kills the loop."""
    log.info(
        "debounce flusher started (tick %ss, window %ss, cap %ss)",
        settings.DEBOUNCE_TICK_SECONDS,
        settings.DEBOUNCE_WINDOW_SECONDS,
        settings.DEBOUNCE_MAX_WAIT_SECONDS,
    )
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("flusher tick failed")
        await asyncio.sleep(settings.DEBOUNCE_TICK_SECONDS)

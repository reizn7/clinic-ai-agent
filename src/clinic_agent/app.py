"""FastAPI application entrypoint for Medi Desk AI: WhatsApp + browser voice calls."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI  # noqa: E402

from clinic_agent.config import settings  # noqa: E402
from clinic_agent.observability import init_tracing, shutdown_tracing  # noqa: E402
from clinic_agent.runtime import flusher, outbox, sweeper  # noqa: E402
from clinic_agent.whatsapp.webhook import router as webhook_router  # noqa: E402


@contextlib.asynccontextmanager
def _ensure_indexes() -> None:
    """Clinic-scoped indexes (idempotent). Never blocks startup: log and carry on."""
    from clinic_agent.runtime import conversations, debounce
    from clinic_agent.tools import repo

    for ensure in (
        repo.ensure_indexes,
        conversations.ensure_indexes,
        debounce.ensure_indexes,
    ):
        try:
            ensure()
        except Exception:
            logging.getLogger("clinic_agent.app").exception("index setup failed: %s", ensure)


async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await asyncio.to_thread(_ensure_indexes)
    # Background loops: idle sweeper (finalize + analytics) and debounce flusher.
    tasks = [
        asyncio.create_task(sweeper.run_forever()),
        asyncio.create_task(flusher.run_forever()),
        # Appointment follow-ups: doctor calendar sync + WhatsApp confirmations.
        asyncio.create_task(outbox.run_forever()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Flush any buffered spans to Langfuse before the process exits.
        shutdown_tracing()


def create_app() -> FastAPI:
    logging.basicConfig(level=settings.LOG_LEVEL)
    # Langfuse tracing (no-op unless LANGFUSE_ENABLED + keys). ADK's own OTel spans
    # are picked up from here on.
    init_tracing()
    app = FastAPI(title="Medi Desk AI", lifespan=lifespan)

    @app.get("/")
    async def health() -> dict[str, str]:
        return {"status": "Medi Desk AI running"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # WhatsApp webhook (GET verify + POST inbound).
    app.include_router(webhook_router, prefix="/webhook")

    # Browser voice calls: the call page (GET /voice/call) and its WebSocket
    # (WS /voice/ws), bridged to Gemini Live.
    if settings.VOICE_ENABLED:
        from clinic_agent.voice.routes import router as voice_router

        app.include_router(voice_router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "clinic_agent.app:app",
        host="0.0.0.0",
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()

"""FastAPI application entrypoint for the clinic WhatsApp agent (v2)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI  # noqa: E402

from clinic_agent.config import settings  # noqa: E402
from clinic_agent.runtime import flusher, sweeper  # noqa: E402
from clinic_agent.whatsapp.webhook import router as webhook_router  # noqa: E402


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Background loops: idle sweeper (finalize + analytics) and debounce flusher.
    tasks = [
        asyncio.create_task(sweeper.run_forever()),
        asyncio.create_task(flusher.run_forever()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    logging.basicConfig(level=settings.LOG_LEVEL)
    app = FastAPI(title="Clinic AI Agent v2", lifespan=lifespan)

    @app.get("/")
    async def health() -> dict[str, str]:
        return {"status": "Clinic AI Agent v2 running"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # WhatsApp webhook (GET verify + POST inbound).
    app.include_router(webhook_router, prefix="/webhook")
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

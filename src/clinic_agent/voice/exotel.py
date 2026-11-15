"""Exotel Voicebot WebSocket endpoint: ``wss://<host>/voice/exotel``.

Configure the Voicebot applet URL as
``wss://<host>/voice/exotel?sample-rate=16000&token=<VOICE_WS_TOKEN>``.
``sample-rate`` tells Exotel which PCM rate to stream (8000/16000/24000) and
tells us what to expect; ``token`` is checked only when ``VOICE_WS_TOKEN`` is
set. Mounted by ``app.create_app`` only when ``VOICE_ENABLED``.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, WebSocket

from clinic_agent.config import settings
from clinic_agent.voice.bridge import run_call

log = logging.getLogger("clinic_agent.voice.exotel")

router = APIRouter()

SUPPORTED_RATES = (8000, 16000, 24000)


def requested_rate(value: str | None) -> int:
    """The ``?sample-rate=`` we were connected with, else ``VOICE_SAMPLE_RATE``."""
    try:
        rate = int(value or "")
    except ValueError:
        return settings.VOICE_SAMPLE_RATE
    return rate if rate in SUPPORTED_RATES else settings.VOICE_SAMPLE_RATE


def token_ok(supplied: str | None) -> bool:
    expected = settings.VOICE_WS_TOKEN
    return not expected or hmac.compare_digest((supplied or "").encode(), expected.encode())


@router.websocket("/voice/exotel")
async def exotel_voicebot(ws: WebSocket) -> None:
    if not token_ok(ws.query_params.get("token")):
        log.warning("Rejected Exotel WS from %s: bad token", ws.client)
        await ws.close(code=1008)  # policy violation (403 before accept)
        return
    await ws.accept()
    await run_call(ws, sample_rate=requested_rate(ws.query_params.get("sample-rate")))

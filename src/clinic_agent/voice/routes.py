"""Browser voice-call routes. Mounted by ``app.create_app`` only when ``VOICE_ENABLED``.

* ``GET /voice/call`` serves the call page (``static/call.html``, read once at
  import). Open it as ``/voice/call?token=<VOICE_WS_TOKEN>``; the page passes its
  own ``token`` on to the WebSocket.
* ``WS /voice/ws?token=…`` carries one call (protocol in ``voice.protocol``). The
  token is checked only when ``VOICE_WS_TOKEN`` is set, before the socket is
  accepted.
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path

from fastapi import APIRouter, WebSocket
from fastapi.responses import HTMLResponse

from clinic_agent.config import settings
from clinic_agent.voice.bridge import run_call

log = logging.getLogger("clinic_agent.voice.routes")

router = APIRouter()

CALL_PAGE = (Path(__file__).parent / "static" / "call.html").read_text(encoding="utf-8")


def token_ok(supplied: str | None) -> bool:
    expected = settings.VOICE_WS_TOKEN
    return not expected or hmac.compare_digest((supplied or "").encode(), expected.encode())


@router.get("/voice/call", response_class=HTMLResponse)
async def call_page() -> HTMLResponse:
    return HTMLResponse(CALL_PAGE, headers={"Cache-Control": "no-cache"})


@router.websocket("/voice/ws")
async def voice_ws(ws: WebSocket) -> None:
    if not token_ok(ws.query_params.get("token")):
        log.warning("Rejected voice WS from %s: bad token", ws.client)
        await ws.close(code=1008)  # policy violation (403 before accept)
        return
    await ws.accept()
    await run_call(ws)

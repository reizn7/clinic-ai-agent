"""WhatsApp webhook — verification (GET) + inbound messages (POST).

Python port of v1's ``routes/webhook.js``. For Milestone 1 the reply is a plain
echo; Milestone 2 swaps ``_handle_message`` for the ADK agent turn.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request, Response

from clinic_agent.config import settings
from clinic_agent.runtime.turn import run_turn
from clinic_agent.whatsapp.client import send_text_message

log = logging.getLogger("clinic_agent.whatsapp.webhook")

router = APIRouter()


@router.get("")
async def verify(request: Request) -> Response:
    """Meta webhook verification handshake."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.VERIFY_TOKEN:
        log.info("Webhook verified")
        return Response(content=challenge or "", media_type="text/plain")
    return Response(status_code=403)


def _extract_message(body: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the first user message from a webhook payload, or None for status
    callbacks (delivery/read receipts)."""
    try:
        return body["entry"][0]["changes"][0]["value"]["messages"][0]
    except (KeyError, IndexError, TypeError):
        return None


async def _reply(phone: str, text: str) -> None:
    """Run the agent for one message and deliver its reply."""
    try:
        reply = await run_turn(phone, text)
        await send_text_message(phone, reply)
        log.info("Reply sent to %s", phone)
    except Exception:
        log.exception("Failed to handle message from %s", phone)


@router.post("")
async def incoming(request: Request, background: BackgroundTasks) -> Response:
    """Receive an inbound message; ack 200 immediately, reply in the background."""
    body = await request.json()
    message = _extract_message(body)

    # No user message (status callback, etc.) -> just ack.
    if not message:
        return Response(status_code=200)

    phone = message.get("from", "")
    text = (message.get("text") or {}).get("body", "")
    log.info("Message from %s: %s", phone, text)

    if phone:
        background.add_task(_reply, phone, text)
    return Response(status_code=200)

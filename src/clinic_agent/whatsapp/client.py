"""WhatsApp Cloud API client — the only place that talks to the Graph API.

Python port of v1's ``services/whatsappService.js`` (same endpoint, headers, and
payload shape).
"""

from __future__ import annotations

import logging

import httpx

from clinic_agent.config import settings

log = logging.getLogger("clinic_agent.whatsapp.client")


def _messages_url() -> str:
    return (
        f"https://graph.facebook.com/{settings.GRAPH_API_VERSION}"
        f"/{settings.PHONE_NUMBER_ID}/messages"
    )


async def send_text_message(to: str, body: str) -> dict:
    """Send a plain-text WhatsApp message. Returns the Graph API response JSON."""
    return await _send(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
    )


async def send_template_message(
    to: str, template: str, language: str, body_params: list[str]
) -> dict:
    """Send an approved template message (allowed outside the 24h window).

    ``body_params`` fill the template body's {{1}}, {{2}}, … in order.
    """
    return await _send(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": p} for p in body_params],
                    }
                ],
            },
        }
    )


async def _send(payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {settings.ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(_messages_url(), json=payload, headers=headers)

    if resp.status_code >= 400:
        log.error("WhatsApp send failed %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()
    return resp.json()

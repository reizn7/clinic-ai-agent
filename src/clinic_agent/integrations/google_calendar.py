"""Google Calendar via a service account (REST over httpx, no discovery client).

Each doctor shares their calendar with the service account's email ("Make changes
to events"); events are then written straight onto that calendar, addressed by the
doctor's ``calendarEmail`` (a calendar's id is its owner's email).

Event ids are deterministic per appointment (``event_id_for``), so every write is
idempotent: a retry after a timeout updates the same event instead of creating a
duplicate, and reschedule/cancel find it without storing anything first.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from urllib.parse import quote

import httpx

from clinic_agent.config import settings

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
_API = "https://www.googleapis.com/calendar/v3/calendars"


class CalendarError(Exception):
    """A Calendar call failed. ``permanent`` = retrying won't help (fix config)."""

    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent


def event_id_for(appointment_id: str) -> str:
    """Calendar event ids must be base32hex (a-v, 0-9); an ObjectId hex fits."""
    return f"appt{appointment_id.lower()}"


_creds_lock = threading.Lock()
_creds: Any = None


def _credentials() -> Any:
    from google.oauth2 import service_account

    if settings.GOOGLE_SERVICE_ACCOUNT_JSON:
        info = json.loads(settings.GOOGLE_SERVICE_ACCOUNT_JSON)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if settings.GOOGLE_SERVICE_ACCOUNT_FILE:
        return service_account.Credentials.from_service_account_file(
            settings.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
    raise CalendarError("No Google service account configured.", permanent=True)


def _access_token_sync() -> str:
    """A valid OAuth token for the service account (refreshed when expired)."""
    import google.auth.transport.requests

    global _creds
    with _creds_lock:
        if _creds is None:
            _creds = _credentials()
        if not _creds.valid:
            _creds.refresh(google.auth.transport.requests.Request())
        return _creds.token


async def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        token = await asyncio.to_thread(_access_token_sync)
    except CalendarError:
        raise
    except Exception as exc:  # bad key file, revoked key, network…
        raise CalendarError(f"Google auth failed: {exc}", permanent=False) from exc
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await client.request(
            method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )


def _raise_for(resp: httpx.Response, calendar_id: str) -> None:
    if resp.status_code < 400:
        return
    detail = resp.text[:300]
    if resp.status_code in (401, 403, 404):
        # 404 on the calendar itself = not shared with the service account.
        raise CalendarError(
            f"Calendar '{calendar_id}' not accessible ({resp.status_code}). Has the doctor "
            f'shared it with the service account ("Make changes to events")? {detail}',
            permanent=True,
        )
    raise CalendarError(
        f"Calendar API {resp.status_code}: {detail}",
        permanent=resp.status_code < 500 and resp.status_code != 429,
    )


async def upsert_event(calendar_id: str, event_id: str, body: dict) -> dict:
    """Create the event, or update it if it already exists (idempotent)."""
    base = f"{_API}/{quote(calendar_id, safe='')}/events"
    resp = await _request("PUT", f"{base}/{event_id}", json=body)
    if resp.status_code == 404:  # doesn't exist yet (or calendar not shared)
        resp = await _request("POST", base, json={**body, "id": event_id})
        if resp.status_code == 409:  # created concurrently / deleted id reused
            resp = await _request("PUT", f"{base}/{event_id}", json={**body, "status": "confirmed"})
    _raise_for(resp, calendar_id)
    return resp.json()


async def delete_event(calendar_id: str, event_id: str) -> None:
    """Delete the event; already gone counts as success."""
    url = f"{_API}/{quote(calendar_id, safe='')}/events/{event_id}"
    resp = await _request("DELETE", url)
    if resp.status_code in (404, 410):
        return
    _raise_for(resp, calendar_id)

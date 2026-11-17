"""Appointment follow-ups: outbox lifecycle, calendar sync, WhatsApp confirmations."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from clinic_agent.config import settings
from clinic_agent.integrations import google_calendar
from clinic_agent.runtime import followups, outbox
from clinic_agent.tools import repo

PHONE = "919999999999"


def _next_monday() -> str:
    d = date.today() + timedelta(days=1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d.isoformat()


MON = _next_monday()


@pytest.fixture
def db(_no_real_mongo, monkeypatch):
    """One fake DB for every module (the conftest safety net), seeded with a clinic."""
    fake = _no_real_mongo
    clinic = settings.CLINIC_ID
    fake["clinic"].insert_one(
        {"clinicId": clinic, "clinicName": "ABC Clinic", "address": "12 MG Road", "phone": "+91-1"}
    )
    fake["doctors"].insert_one(
        {
            "clinicId": clinic,
            "name": "Asha Mehta",
            "specialization": "General Physician",
            "isActive": True,
            "availableDays": ["Monday"],
            "workingHours": {"start": "09:00", "end": "12:00"},
            "consultationDuration": 30,
            "calendarEmail": "asha@example.com",
        }
    )
    monkeypatch.setattr(settings, "GOOGLE_CALENDAR_ENABLED", True)
    monkeypatch.setattr(settings, "WHATSAPP_CONFIRMATIONS_ENABLED", True)
    for name in (
        "WHATSAPP_TEMPLATE_BOOKED",
        "WHATSAPP_TEMPLATE_RESCHEDULED",
        "WHATSAPP_TEMPLATE_CANCELLED",
    ):
        monkeypatch.setattr(settings, name, "")
    return fake


@pytest.fixture
def sent(monkeypatch):
    """Capture WhatsApp sends instead of calling Meta."""
    calls: list[tuple] = []

    async def text(to, body):
        calls.append(("text", to, body))
        return {"messages": [{"id": "wamid.1"}]}

    async def template(to, name, lang, params):
        calls.append(("template", to, name, lang, params))
        return {"messages": [{"id": "wamid.2"}]}

    monkeypatch.setattr(followups.whatsapp, "send_text_message", text)
    monkeypatch.setattr(followups.whatsapp, "send_template_message", template)
    return calls


@pytest.fixture
def calendar(monkeypatch):
    """Capture Calendar writes instead of calling Google."""
    calls: list[tuple] = []

    async def upsert(calendar_id, event_id, body):
        calls.append(("upsert", calendar_id, event_id, body))
        return {"id": event_id}

    async def delete(calendar_id, event_id):
        calls.append(("delete", calendar_id, event_id))

    monkeypatch.setattr(followups.google_calendar, "upsert_event", upsert)
    monkeypatch.setattr(followups.google_calendar, "delete_event", delete)
    return calls


async def _drain():
    return await outbox.run_once(followups.HANDLERS)


def _book(slot="09:30"):
    res = repo.create_appointment(PHONE, "Ravi Kumar", 34, "Male", "Asha Mehta", MON, slot)
    assert res["success"], res
    return res


def _jobs(db, **q):
    return list(db["outbox"].find(q))


# ---- holding: confirmations wait for the reply / end of call ----------------------


async def test_confirmation_is_held_until_the_turn_ends(db, sent, calendar):
    with outbox.held("wa:turn1"):
        booked = _book()
        assert booked["whatsapp_confirmation"]
        assert [j["status"] for j in _jobs(db, kind=outbox.WHATSAPP_NOTIFY)] == ["held"]
        await _drain()  # calendar can go now; the confirmation can't
        assert sent == []
        assert len(calendar) == 1
    await _drain()
    assert len(sent) == 1
    kind, to, body = sent[0]
    assert (kind, to) == ("text", PHONE)
    assert "Appointment confirmed" in body
    assert "Dr. Asha Mehta (General Physician)" in body
    assert "9:30 AM" in body and "ABC Clinic, 12 MG Road" in body
    appt = db["appointments"].find_one()
    assert appt["confirmation"]["status"] == "sent"
    assert appt["calendarSync"]["status"] == "synced"


async def test_message_is_built_from_the_database_not_the_model(db, sent, calendar):
    with outbox.held("wa:t"):
        _book()
        # Staff fix the patient's name before the message goes out.
        db["patients"].update_one({"phone": PHONE}, {"$set": {"name": "Ravi K. Sharma"}})
    await _drain()
    assert "Patient: Ravi K. Sharma" in sent[0][2]


async def test_booked_then_cancelled_in_the_same_turn_sends_nothing(db, sent, calendar):
    with outbox.held("wa:t"):
        booked = _book()
        repo.cancel_appointment(booked["appointmentId"])
    await _drain()
    assert sent == []
    assert calendar[-1][0] == "delete"


async def test_booked_then_rescheduled_sends_one_confirmation_with_new_time(db, sent, calendar):
    with outbox.held("wa:t"):
        booked = _book("09:30")
        repo.reschedule_appointment(booked["appointmentId"], MON, "11:00")
    await _drain()
    assert len(sent) == 1
    assert "Appointment confirmed" in sent[0][2] and "11:00 AM" in sent[0][2]


async def test_later_reschedule_and_cancel_each_notify(db, sent, calendar):
    booked = _book()
    await _drain()
    repo.reschedule_appointment(booked["appointmentId"], MON, "10:30")
    await _drain()
    repo.cancel_appointment(booked["appointmentId"])
    repo.cancel_appointment(booked["appointmentId"])  # repeat: no second message
    await _drain()
    assert [b.splitlines()[0] for _, _, b in sent] == [
        "Appointment confirmed",
        "Appointment rescheduled",
        "Appointment cancelled",
    ]
    assert [c[0] for c in calendar] == ["upsert", "upsert", "delete"]


async def test_stale_holds_are_released_eventually(db, sent, calendar, monkeypatch):
    with outbox.held("call:crashed"):
        _book()
    db["outbox"].update_many(
        {}, {"$set": {"status": "held", "createdAt": "2000-01-01T00:00:00+00:00"}}
    )
    await _drain()
    assert len(sent) == 1


# ---- templates ---------------------------------------------------------------------


async def test_template_used_when_configured(db, sent, calendar, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_BOOKED", "appointment_confirmation")
    booked = _book()
    await _drain()
    kind, _to, name, lang, params = sent[0]
    assert (kind, name, lang) == ("template", "appointment_confirmation", "en")
    assert params[:2] == ["Ravi Kumar", "Dr. Asha Mehta"]
    assert params[4] == booked["appointmentId"][-6:].upper()


# ---- calendar ----------------------------------------------------------------------


async def test_calendar_event_body(db, sent, calendar):
    booked = _book("09:30")
    await _drain()
    _, cal_id, event_id, body = calendar[0]
    assert cal_id == "asha@example.com"
    assert event_id == "appt" + booked["appointmentId"]
    assert body["start"] == {"dateTime": f"{MON}T09:30:00+05:30", "timeZone": "Asia/Kolkata"}
    assert body["end"]["dateTime"] == f"{MON}T10:00:00+05:30"
    assert "Ravi Kumar (34, Male)" in body["description"]


async def test_doctor_without_calendar_email_is_skipped(db, sent, calendar):
    db["doctors"].update_one({}, {"$unset": {"calendarEmail": ""}})
    _book()
    await _drain()
    assert calendar == []
    assert db["appointments"].find_one()["calendarSync"]["status"] == "skipped"
    assert _jobs(db, kind=outbox.CALENDAR_SYNC)[0]["status"] == "skipped"


async def test_calendar_permanent_error_fails_without_retry(db, sent, monkeypatch):
    async def denied(*_):
        raise google_calendar.CalendarError("not shared", permanent=True)

    monkeypatch.setattr(followups.google_calendar, "upsert_event", denied)
    _book()
    await _drain()
    job = _jobs(db, kind=outbox.CALENDAR_SYNC)[0]
    assert job["status"] == "failed" and "not shared" in job["lastError"]
    assert db["appointments"].find_one()["calendarSync"]["status"] == "error"
    assert len(sent) == 1  # the patient still gets confirmed


async def test_calendar_transient_error_is_retried_later(db, sent, monkeypatch):
    async def flaky(*_):
        raise google_calendar.CalendarError("503", permanent=False)

    monkeypatch.setattr(followups.google_calendar, "upsert_event", flaky)
    _book()
    await _drain()
    job = _jobs(db, kind=outbox.CALENDAR_SYNC)[0]
    assert job["status"] == "pending" and job["attempts"] == 1
    assert job["nextAttemptAt"] > job["createdAt"]  # backed off, not hot-looping


async def test_whatsapp_rejection_fails_permanently(db, calendar, monkeypatch):
    async def rejected(to, body):
        resp = httpx.Response(400, request=httpx.Request("POST", "https://graph.facebook.com"))
        raise httpx.HTTPStatusError("re-engagement", request=resp.request, response=resp)

    monkeypatch.setattr(followups.whatsapp, "send_text_message", rejected)
    _book()
    await _drain()
    assert _jobs(db, kind=outbox.WHATSAPP_NOTIFY)[0]["status"] == "failed"
    assert db["appointments"].find_one()["confirmation"]["status"] == "error"


async def test_disabled_features_queue_nothing(db, sent, calendar, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CALENDAR_ENABLED", False)
    monkeypatch.setattr(settings, "WHATSAPP_CONFIRMATIONS_ENABLED", False)
    booked = _book()
    assert "whatsapp_confirmation" not in booked
    assert db["outbox"].count_documents({}) == 0


# ---- Google Calendar REST client ---------------------------------------------------


async def test_upsert_creates_when_missing_then_updates(monkeypatch):
    monkeypatch.setattr(google_calendar, "_access_token_sync", lambda: "tok")
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode()))
        assert request.headers["Authorization"] == "Bearer tok"
        if request.method == "PUT" and len(seen) == 1:
            return httpx.Response(404)
        return httpx.Response(200, json={"id": "apptabc"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        google_calendar.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    await google_calendar.upsert_event("dr@x.com", "apptabc", {"summary": "s"})
    assert seen == [
        ("PUT", "/calendar/v3/calendars/dr%40x.com/events/apptabc"),
        ("POST", "/calendar/v3/calendars/dr%40x.com/events"),
    ]


async def test_not_shared_calendar_is_a_permanent_error(monkeypatch):
    monkeypatch.setattr(google_calendar, "_access_token_sync", lambda: "tok")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        google_calendar.httpx,
        "AsyncClient",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(404)), **kw
        ),
    )
    with pytest.raises(google_calendar.CalendarError) as err:
        await google_calendar.upsert_event("dr@x.com", "apptabc", {})
    assert err.value.permanent and "shared it with the service account" in str(err.value)


async def test_whatsapp_turn_releases_confirmation_only_after_the_reply(
    db, sent, calendar, monkeypatch
):
    from clinic_agent.runtime import flusher

    order: list[str] = []

    async def fake_turn(phone, text, *, bubbles=1):
        _book()  # the agent's book_appointment tool, inside the turn
        return "Booked! See you Monday."

    async def reply(to, body):
        statuses = [j["status"] for j in _jobs(db, kind=outbox.WHATSAPP_NOTIFY)]
        order.append(f"reply sent while confirmation {statuses}")

    monkeypatch.setattr(flusher, "run_turn", fake_turn)
    monkeypatch.setattr(flusher, "send_text_message", reply)
    await flusher._flush_one(PHONE, ["book me in"])
    assert order == ["reply sent while confirmation ['held']"]
    assert [j["status"] for j in _jobs(db, kind=outbox.WHATSAPP_NOTIFY)] == ["pending"]
    await _drain()
    assert sent[0][2].startswith("Appointment confirmed")

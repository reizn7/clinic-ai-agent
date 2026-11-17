"""Human handoff: staff recipients, alert queuing/dedupe, the WhatsApp alert, the tool."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from clinic_agent.config import settings
from clinic_agent.runtime import escalation, followups, outbox
from clinic_agent.tools import catalog
from clinic_agent.tools.booking_tools import CALLER_PHONE, CHANNEL, escalate_to_human

PATIENT = "919811122233"


def _staff(db, name, phone, *, alerts=True, active=True):
    db[catalog.RECORDS].insert_one(
        {
            "clinicId": settings.CLINIC_ID,
            "entityType": "staff",
            "isActive": active,
            "attributes": {"name": name, "phone": phone, "receivesEscalations": alerts},
        }
    )


@pytest.fixture
def db(_no_real_mongo, monkeypatch):
    fake = _no_real_mongo
    catalog.clear_cache()
    fake[catalog.DEFINITIONS].insert_one(
        {"clinicId": settings.CLINIC_ID, "entityType": "staff", "kind": "custom", "fields": []}
    )
    fake["clinic"].insert_one({"clinicId": settings.CLINIC_ID, "clinicName": "MediDesk Clinic"})
    fake["patients"].insert_one(
        {"clinicId": settings.CLINIC_ID, "phone": PATIENT, "name": "Ravi Kumar"}
    )
    monkeypatch.setattr(settings, "STAFF_ALERTS_ENABLED", True)
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_ESCALATION", "")
    yield fake
    catalog.clear_cache()


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple] = []

    async def text(to, body):
        calls.append(("text", to, body))
        return {"messages": [{"id": "wamid.s1"}]}

    async def template(to, name, lang, params):
        calls.append(("template", to, name, lang, params))
        return {"messages": [{"id": "wamid.s2"}]}

    monkeypatch.setattr(escalation.whatsapp, "send_text_message", text)
    monkeypatch.setattr(escalation.whatsapp, "send_template_message", template)
    return calls


def _alerts(db):
    return list(db[outbox.COLLECTION].find({"kind": outbox.STAFF_ALERT}))


def test_recipients_only_active_opted_in_with_valid_phone(db):
    _staff(db, "Front desk", "98765 43210")
    _staff(db, "Duplicate", "+91 98765-43210")  # same number → one alert
    _staff(db, "Off", "9123456789", alerts=False)
    _staff(db, "Inactive", "9123456780", active=False)
    _staff(db, "No phone", "")
    assert escalation.escalation_recipients() == [{"name": "Front desk", "phone": "919876543210"}]


def test_no_staff_page_means_no_recipients(db):
    db[catalog.DEFINITIONS].delete_many({})
    catalog.clear_cache()
    _staff(db, "Front desk", "9876543210")
    assert escalation.escalation_recipients() == []


def test_request_handoff_queues_one_job_per_staff_and_dedupes(db):
    _staff(db, "Anita", "9876543210")
    _staff(db, "Rahul", "9876543211")
    first = escalation.request_handoff(PATIENT, "voice", "wants to talk about a bill")
    assert first == {"staff_alerted": 2, "already_alerted": False}
    jobs = _alerts(db)
    assert {j["staffPhone"] for j in jobs} == {"919876543210", "919876543211"}
    assert all(j["status"] == "pending" and j["appointmentId"] is None for j in jobs)

    again = escalation.request_handoff(PATIENT, "voice", "still wants a human")
    assert again == {"staff_alerted": 0, "already_alerted": True}
    assert len(_alerts(db)) == 2


def test_request_handoff_disabled(db, monkeypatch):
    monkeypatch.setattr(settings, "STAFF_ALERTS_ENABLED", False)
    _staff(db, "Anita", "9876543210")
    assert escalation.request_handoff(PATIENT, "whatsapp", "x")["staff_alerted"] == 0
    assert _alerts(db) == []


async def test_worker_sends_text_alert(db, sent):
    _staff(db, "Anita", "9876543210")
    escalation.request_handoff(PATIENT, "voice", "billing question")
    assert await outbox.process_one(followups.HANDLERS)
    [(kind, to, body)] = sent
    assert (kind, to) == ("text", "919876543210")
    for part in ("Hi Anita", "Ravi Kumar", "+919811122233", "voice call", "billing question"):
        assert part in body
    assert "call them back" in body
    assert _alerts(db)[0]["status"] == "done"


async def test_worker_uses_template_when_configured(db, sent, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_ESCALATION", "staff_handoff")
    _staff(db, "Anita", "9876543210")
    escalation.request_handoff(PATIENT, "whatsapp", "complaint")
    await outbox.process_one(followups.HANDLERS)
    [(kind, to, name, _lang, params)] = sent
    assert (kind, to, name) == ("template", "919876543210", "staff_handoff")
    assert params == [
        "Ravi Kumar",
        "+919811122233",
        "WhatsApp chat",
        "complaint",
        "MediDesk Clinic",
    ]


async def test_rejected_alert_fails_permanently(db, monkeypatch):
    _staff(db, "Anita", "9876543210")
    escalation.request_handoff(PATIENT, "whatsapp", "x")

    async def reject(to, body):
        resp = httpx.Response(400, request=httpx.Request("POST", "https://graph"))
        raise httpx.HTTPStatusError("400", request=resp.request, response=resp)

    monkeypatch.setattr(escalation.whatsapp, "send_text_message", reject)
    await outbox.process_one(followups.HANDLERS)
    assert _alerts(db)[0]["status"] == "failed"


async def test_tool_uses_session_caller_and_channel(db):
    _staff(db, "Anita", "9876543210")
    ctx = SimpleNamespace(state={CALLER_PHONE: PATIENT, CHANNEL: "voice"})
    result = await escalate_to_human("wants a person", ctx)
    assert result["escalated"] is True
    assert "call you back" in result["message"]
    assert result["staff_alerted"] == 1
    assert _alerts(db)[0]["patientPhone"] == PATIENT
    assert _alerts(db)[0]["channel"] == "voice"


async def test_tool_never_fails_the_turn(db, monkeypatch):
    def boom(*_a):
        raise RuntimeError("db down")

    monkeypatch.setattr(escalation, "request_handoff", boom)
    ctx = SimpleNamespace(state={CALLER_PHONE: PATIENT, CHANNEL: "whatsapp"})
    result = await escalate_to_human("x", ctx)
    assert result["escalated"] is True and "error" not in result
    assert "reply to you here" in result["message"]

from fastapi.testclient import TestClient

from clinic_agent import config
from clinic_agent.app import app
from clinic_agent.whatsapp import webhook

client = TestClient(app)


def _msg_payload(text="hello"):
    return {
        "entry": [
            {"changes": [{"value": {"messages": [{"from": "919999999999", "text": {"body": text}}]}}]}
        ]
    }


def test_verify_handshake_ok(monkeypatch):
    monkeypatch.setattr(config.settings, "VERIFY_TOKEN", "tok")
    r = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "42"},
    )
    assert r.status_code == 200
    assert r.text == "42"


def test_verify_handshake_rejects_bad_token(monkeypatch):
    monkeypatch.setattr(config.settings, "VERIFY_TOKEN", "tok")
    r = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "WRONG", "hub.challenge": "42"},
    )
    assert r.status_code == 403


def test_inbound_message_triggers_agent_and_send(monkeypatch):
    sent = {}

    async def fake_run_turn(phone, text):
        return f"reply to {text}"

    async def fake_send(to, body):
        sent["to"] = to
        sent["body"] = body

    monkeypatch.setattr(webhook, "run_turn", fake_run_turn)
    monkeypatch.setattr(webhook, "send_text_message", fake_send)

    r = client.post("/webhook", json=_msg_payload("book please"))
    assert r.status_code == 200
    assert sent["to"] == "919999999999"
    assert sent["body"] == "reply to book please"


def test_status_callback_is_acked_without_agent(monkeypatch):
    called = {"n": 0}

    async def fake_run_turn(phone, text):
        called["n"] += 1
        return "x"

    monkeypatch.setattr(webhook, "run_turn", fake_run_turn)
    r = client.post(
        "/webhook",
        json={"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]},
    )
    assert r.status_code == 200
    assert called["n"] == 0

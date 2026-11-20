"""Voice routes: the call page and the token-checked call WebSocket."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from clinic_agent import app as app_module
from clinic_agent.voice import routes


def test_token_ok(monkeypatch):
    monkeypatch.setattr(routes.settings, "VOICE_WS_TOKEN", "")
    assert routes.token_ok(None)
    monkeypatch.setattr(routes.settings, "VOICE_WS_TOKEN", "s3cret")
    assert routes.token_ok("s3cret")
    assert not routes.token_ok("nope")
    assert not routes.token_ok(None)


def test_ws_token_accept_and_reject(monkeypatch):
    calls = []

    async def fake_run_call(ws):
        calls.append(ws)
        await ws.send_text("ok")
        await ws.close()

    monkeypatch.setattr(routes, "run_call", fake_run_call)
    monkeypatch.setattr(routes.settings, "VOICE_WS_TOKEN", "s3cret")
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    for bad in ("/voice/ws?token=wrong", "/voice/ws"):
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(bad) as ws:
            ws.receive_text()
    assert calls == []  # rejected before accept: no call handled

    with client.websocket_connect("/voice/ws?token=s3cret") as ws:
        assert ws.receive_text() == "ok"
    assert len(calls) == 1


def _client() -> TestClient:
    return TestClient(app_module.create_app())


def test_call_page_served():
    res = _client().get("/voice/call?token=abc")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert res.text == routes.CALL_PAGE
    assert "<title>" in res.text and "/voice/ws" in res.text


def test_only_browser_voice_routes_exist():
    # The old telephony-provider endpoint is gone: the voice router serves only
    # /voice/call and /voice/ws, and no other WebSocket under /voice/ is accepted.
    assert {r.path for r in routes.router.routes} == {"/voice/call", "/voice/ws"}
    client = _client()
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/voice/other") as ws:
        ws.receive_text()

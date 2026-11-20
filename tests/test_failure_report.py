"""Failures report: which conversations count as failed, the trace digest, the job."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from clinic_agent import observability
from clinic_agent.config import settings
from clinic_agent.integrations import email
from clinic_agent.runtime import conversations, failure_report, followups, outbox
from clinic_agent.runtime.failure_report import FailureAnalysis
from clinic_agent.runtime.outbox import PermanentError

T0 = "2026-11-18T10:00:00+00:00"
failure_signals = failure_report.failure_signals


def _tool(name: str, status: str = "success", **result) -> dict:
    return {
        "role": "agent",
        "type": "tool_call",
        "content": f"{name}()",
        "timestamp": T0,
        "tool": {
            "name": name,
            "args": {},
            "result": None if status == "error" else (result or {"ok": True}),
            "status": status,
            "error": result.get("error") if status == "error" else None,
        },
    }


def _text(role: str, content: str) -> dict:
    return {"role": role, "type": "text", "content": content, "timestamp": T0}


def _conv(messages: list[dict], **fields) -> dict:
    return {
        "externalId": fields.pop("externalId", "conv_1"),
        "clinicId": settings.CLINIC_ID,
        "channel": "whatsapp",
        "startedAt": T0,
        "endedAt": "2026-11-18T10:05:00+00:00",
        "status": "completed",
        "messages": messages,
        **fields,
    }


ANALYSIS = FailureAnalysis(
    category="no_slot_available",
    agent_at_fault=True,
    severity="high",
    summary="Wanted Dr Mehta on Monday; told no slots.",
    root_cause="check_availability was called for the wrong date.",
    evidence=["tool check_availability(date=2026-11-17)"],
    suggested_fix="Resolve 'Monday' to the next Monday in the prompt.",
    fix_area="prompt",
)


# ---- failure_signals ------------------------------------------------------------


def test_booked_conversation_is_not_a_failure():
    conv = _conv([_text("patient", "book me"), _tool("book_appointment"), _text("agent", "done")])
    assert failure_signals(conv) == []


def test_booking_attempt_without_a_booking_fails():
    conv = _conv(
        [
            _text("patient", "book me"),
            _tool("check_availability"),
            _tool("book_appointment", "error", error="slot taken"),
            _text("agent", "sorry"),
        ]
    )
    assert failure_signals(conv) == ["booking_failed", "tool_error"]


def test_crashed_turn_and_escalation_and_sentiment():
    conv = _conv(
        [
            _text("patient", "hello"),
            _text("agent", conversations.FALLBACK_REPLY),
            _tool("escalate_to_human"),
        ],
        sentiment="negative",
    )
    assert failure_signals(conv) == ["turn_crashed", "escalated", "negative_sentiment"]


def test_unresolved_needs_more_than_a_greeting():
    hi = _conv([_text("patient", "hi"), _text("agent", "Hello!")])
    assert failure_signals(hi) == []
    chat = _conv([_text("patient", "hi"), _text("agent", "Hello!"), _text("patient", "fees?")])
    assert failure_signals(chat) == ["unresolved"]


def test_info_answered_is_not_a_failure():
    conv = _conv([_text("patient", "timings?"), _tool("get_clinic_info"), _text("patient", "ok")])
    assert failure_signals(conv) == []


# ---- range ----------------------------------------------------------------------


def test_parse_range_accepts_z_and_offsets():
    start, end = failure_report.parse_range(
        {"rangeStart": "2026-11-12T18:30:00Z", "rangeEnd": "2026-11-20T00:00:00+05:30"}
    )
    assert start == datetime(2026, 11, 12, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 11, 19, 18, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "job",
    [
        {},
        {"rangeStart": "yesterday", "rangeEnd": "2026-11-20T00:00:00Z"},
        {"rangeStart": "2026-11-13T00:00:00", "rangeEnd": "2026-11-20T00:00:00"},  # naive
        {"rangeStart": "2026-11-20T00:00:00Z", "rangeEnd": "2026-11-13T00:00:00Z"},
    ],
)
def test_parse_range_rejects_bad_input(job):
    with pytest.raises(PermanentError):
        failure_report.parse_range(job)


def test_find_failed_filters_window_status_and_signals(_no_real_mongo):
    col = _no_real_mongo[conversations.COLLECTION]
    failing = [_text("patient", "book"), _tool("book_appointment", "error", error="x")]
    col.insert_many(
        [
            _conv(failing, externalId="in"),
            _conv(failing, externalId="early", startedAt="2026-11-10T10:00:00+00:00"),
            _conv(failing, externalId="live", status="active"),
            _conv(failing, externalId="other", clinicId="other-clinic"),
            _conv([_tool("book_appointment")], externalId="fine"),
        ]
    )
    found = failure_report.find_failed(
        datetime(2026, 11, 18, tzinfo=UTC), datetime(2026, 11, 19, tzinfo=UTC)
    )
    assert [(c["externalId"], s) for c, s in found] == [("in", ["booking_failed", "tool_error"])]


# ---- trace digest -----------------------------------------------------------------


def _obs(id, trace, type, name, *, root=False, **kw):
    return SimpleNamespace(
        id=id,
        trace_id=trace,
        type=type,
        name=name,
        is_root_observation=root,
        start_time=datetime(2026, 11, 18, 10, 0, tzinfo=UTC),
        level=kw.get("level", "DEFAULT"),
        status_message=kw.get("status_message"),
        input=kw.get("input"),
        output=kw.get("output"),
        model=kw.get("model"),
        latency=kw.get("latency"),
    )


def test_compact_trace_keeps_evidence_drops_prompt():
    observations = [
        _obs("1", "t1", "SPAN", "handle-whatsapp-message", root=True, input="book me", output="ok"),
        _obs(
            "2",
            "t1",
            "GENERATION",
            "call_llm",
            model="gemini-3.5-flash-lite",
            input="SYSTEM PROMPT " * 500,
            output="calling check_availability",
            level="ERROR",
            status_message="503 overloaded",
            latency=2.34,
        ),
        _obs("3", "t1", "TOOL", "check_availability", input={"date": "x"}, output={"slots": []}),
        _obs("4", "t1", "AGENT", "receptionist", input="huge", output="huge"),
    ]
    text = failure_report.compact_trace(observations)
    assert "SYSTEM PROMPT" not in text
    assert "model=gemini-3.5-flash-lite level=ERROR latency=2.3s" in text
    assert "503 overloaded" in text
    assert '{"slots": []}' in text
    assert "- AGENT receptionist" in text and "huge" not in text
    assert "input: book me" in text


def test_prompt_redacts_phone_numbers():
    conv = _conv([_text("patient", "call me on 919876543210"), _text("patient", "please")])
    prompt = failure_report.build_prompt(conv, ["unresolved"], None)
    assert "919876543210" not in prompt and "<phone>" in prompt
    assert "Ended without resolution" in prompt


# ---- the job -----------------------------------------------------------------------


@pytest.fixture
def job_env(_no_real_mongo, monkeypatch):
    monkeypatch.setattr(settings, "FAILURE_REPORT_EMAIL", "ops@example.com")
    monkeypatch.setattr(settings, "FAILURE_REPORT_MAX_CASES", 50)
    monkeypatch.setattr(settings, "CONSOLE_BASE_URL", "https://console.example.com/")
    sent: list[tuple] = []
    calls: list[str] = []
    monkeypatch.setattr(email, "send_email", lambda *a: sent.append(a))
    monkeypatch.setattr(failure_report, "_fetch_trace", lambda conv: "trace")

    def analyse(conv, signals, trace):
        calls.append(conv["externalId"])
        return ANALYSIS, "gemini-test"

    monkeypatch.setattr(failure_report, "analyse_case", analyse)
    col = _no_real_mongo[conversations.COLLECTION]
    col.insert_one(_conv([_text("patient", "book"), _tool("book_appointment", "error", error="x")]))
    return SimpleNamespace(db=_no_real_mongo, sent=sent, calls=calls, col=col)


JOB = {"rangeStart": "2026-11-18T00:00:00Z", "rangeEnd": "2026-11-19T00:00:00Z"}


async def test_job_emails_report_and_caches_analysis(job_env):
    status, fields = await failure_report.handle_job(JOB)
    assert status == "done"
    assert fields["report"] == {
        "scope": "18 Nov – 19 Nov 2026",  # noqa: RUF001  (a UTC day spans two IST dates)
        "failed": 1,
        "analysed": 1,
        "notAnalysed": 0,
        "emailedTo": "ops@example.com",
    }
    to, subject, text, html = job_env.sent[0]
    assert to == "ops@example.com"
    assert "1 failed conversation" in subject
    assert "check_availability was called for the wrong date." in text
    assert "Resolve &#x27;Monday&#x27;" in html  # escaped
    doc_id = job_env.col.find_one({"externalId": "conv_1"})["_id"]
    assert f"Conversation: https://console.example.com/conversations/{doc_id}" in text
    assert f'href="https://console.example.com/conversations/{doc_id}">Open conversation' in html

    # Second click: cached, no new model call.
    await failure_report.handle_job(JOB)
    assert job_env.calls == ["conv_1"]
    assert len(job_env.sent) == 2

    # The conversation changed since: analysed again.
    job_env.col.update_one({"externalId": "conv_1"}, {"$set": {"endedAt": "later"}})
    await failure_report.handle_job(JOB)
    assert job_env.calls == ["conv_1", "conv_1"]


async def test_job_with_no_failures_still_emails(job_env):
    _, fields = await failure_report.handle_job(
        {"rangeStart": "2026-01-01T00:00:00Z", "rangeEnd": "2026-01-02T00:00:00Z"}
    )
    assert fields["report"]["failed"] == 0
    assert "No failed conversations" in job_env.sent[0][2]


async def test_job_caps_cases(job_env, monkeypatch):
    monkeypatch.setattr(settings, "FAILURE_REPORT_MAX_CASES", 0)
    _, fields = await failure_report.handle_job(JOB)
    assert fields["report"] == {
        "scope": "18 Nov – 19 Nov 2026",  # noqa: RUF001  (a UTC day spans two IST dates)
        "failed": 1,
        "analysed": 0,
        "notAnalysed": 1,
        "emailedTo": "ops@example.com",
    }
    assert "1 more weren't analysed" in job_env.sent[0][2]


async def test_failed_analysis_is_listed_not_fatal(job_env, monkeypatch):
    monkeypatch.setattr(failure_report, "analyse_case", lambda *a: None)
    _, fields = await failure_report.handle_job(JOB)
    assert fields["report"]["analysed"] == 0
    assert "Analysis failed" in job_env.sent[0][2]


async def test_job_without_recipient_or_smtp_fails_permanently(job_env, monkeypatch):
    def not_configured(*a):
        raise email.EmailNotConfigured("SMTP_HOST is not set")

    monkeypatch.setattr(email, "send_email", not_configured)
    with pytest.raises(PermanentError, match="email not configured"):
        await failure_report.handle_job(JOB)
    monkeypatch.setattr(settings, "FAILURE_REPORT_EMAIL", "")
    with pytest.raises(PermanentError, match="FAILURE_REPORT_EMAIL"):
        await failure_report.handle_job(JOB)


def test_send_email_requires_smtp_host(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    with pytest.raises(email.EmailNotConfigured):
        email.send_email("a@example.com", "s", "t")


# ---- outbox: its own worker loop ---------------------------------------------------


async def test_report_job_runs_only_on_the_long_running_loop(job_env):
    now = datetime.now(UTC).isoformat()
    job_env.db[outbox.COLLECTION].insert_one(
        {
            "clinicId": settings.CLINIC_ID,
            "kind": outbox.FAILURE_REPORT,
            "status": "pending",
            "attempts": 0,
            "nextAttemptAt": now,
            "createdAt": now,
            **JOB,
        }
    )
    assert not await outbox.process_one(followups.HANDLERS)  # the regular worker skips it
    assert await outbox.process_one(followups.HANDLERS, long_running=True)
    job = job_env.db[outbox.COLLECTION].find_one({"kind": outbox.FAILURE_REPORT})
    assert job["status"] == "done"
    assert job["report"]["analysed"] == 1
    assert len(job_env.sent) == 1


def test_long_running_claim_gets_long_lease(_no_real_mongo):
    now = datetime.now(UTC).isoformat()
    _no_real_mongo[outbox.COLLECTION].insert_one(
        {
            "clinicId": settings.CLINIC_ID,
            "kind": outbox.FAILURE_REPORT,
            "status": "pending",
            "nextAttemptAt": now,
        }
    )
    job = outbox._claim_sync(long_running=True)
    lease = datetime.fromisoformat(job["leaseUntil"]) - datetime.now(UTC)
    assert lease.total_seconds() > 25 * 60


def test_session_helpers_are_noops_when_tracing_is_off(monkeypatch):
    monkeypatch.setattr(observability, "_client", None)
    assert observability.session_observations("conv_1") is None
    assert observability.session_url("conv_1") is None


# ---- latest-N mode -----------------------------------------------------------------


def test_parse_scope_latest_and_validation(monkeypatch):
    monkeypatch.setattr(settings, "FAILURE_REPORT_MAX_CASES", 50)
    scope = failure_report.parse_scope({"mode": "latest", "limit": "10"})
    assert scope.latest == 10 and scope.label == "latest 10 failed conversations"
    assert failure_report.parse_scope({"mode": "latest", "limit": 1}).label == (
        "latest 1 failed conversation"
    )
    for bad in (
        {"mode": "latest"},
        {"mode": "latest", "limit": 0},
        {"mode": "latest", "limit": 51},
        {"mode": "weekly"},
    ):
        with pytest.raises(PermanentError):
            failure_report.parse_scope(bad)
    # No mode = a date range (jobs queued before modes existed).
    assert failure_report.parse_scope(JOB).start == datetime(2026, 11, 18, tzinfo=UTC)


async def test_latest_mode_takes_the_newest_failures(job_env):
    failing = [_text("patient", "book"), _tool("book_appointment", "error", error="x")]
    job_env.col.insert_many(
        [
            _conv(failing, externalId="old", startedAt="2026-01-01T10:00:00+00:00"),
            _conv(failing, externalId="new", startedAt="2026-11-20T10:00:00+00:00"),
            _conv(
                [_tool("book_appointment")],
                externalId="fine-newest",
                startedAt="2026-11-21T10:00:00+00:00",
            ),
            _conv(
                failing, externalId="live", status="active", startedAt="2026-11-21T11:00:00+00:00"
            ),
        ]
    )
    _, fields = await failure_report.handle_job({"mode": "latest", "limit": 2})
    assert sorted(job_env.calls) == ["conv_1", "new"]  # not "old": only the newest 2
    assert fields["report"]["scope"] == "latest 2 failed conversations"
    assert "latest 2 failed conversations" in job_env.sent[0][1]


# ---- model fallback ----------------------------------------------------------------


class _FakeModels:
    def __init__(self, fail: dict[str, int]):
        self.fail = fail
        self.tried: list[str] = []

    def generate_content(self, *, model, contents, config):
        from google.genai import errors

        self.tried.append(model)
        if model in self.fail:
            raise errors.APIError(self.fail[model], {"error": {"message": "nope"}})
        return SimpleNamespace(text="{}", usage_metadata=None, parsed=ANALYSIS)


@pytest.fixture
def fake_gemini(monkeypatch):
    monkeypatch.setattr(settings, "FAILURE_REPORT_MODEL", "big")
    monkeypatch.setattr(settings, "MODEL", "small")
    monkeypatch.setattr(settings, "MODEL_FALLBACKS", "small, groq:x, backup")
    monkeypatch.setattr(failure_report, "_exhausted", {})

    def install(fail):
        models = _FakeModels(fail)
        monkeypatch.setattr(
            failure_report.analytics, "_get_client", lambda: SimpleNamespace(models=models)
        )
        return models

    return install


def test_report_models_order_dedupes_and_skips_groq(fake_gemini):
    assert failure_report.report_models() == ["big", "small", "backup"]


def test_quota_error_falls_back_and_cools_the_model_down(fake_gemini):
    conv = _conv([_text("patient", "hi"), _text("patient", "fees?")])
    models = fake_gemini({"big": 429, "small": 503})
    assert failure_report.analyse_case(conv, ["unresolved"], None) == (ANALYSIS, "backup")
    assert models.tried == ["big", "small", "backup"]
    # big ran out of quota: skipped next time; small was only overloaded: retried.
    models.tried.clear()
    failure_report.analyse_case(conv, ["unresolved"], None)
    assert models.tried == ["small", "backup"]


def test_other_errors_do_not_fall_back(fake_gemini):
    conv = _conv([_text("patient", "hi"), _text("patient", "fees?")])
    models = fake_gemini({"big": 400})
    assert failure_report.analyse_case(conv, ["unresolved"], None) is None
    assert models.tried == ["big"]

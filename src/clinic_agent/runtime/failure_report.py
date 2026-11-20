"""Failures report: why did the agent fail, conversation by conversation, emailed.

Triggered from the staff console (Analytics → "Send failures report"), which
inserts one outbox job::

    {"clinicId": ..., "kind": "failures.report", "status": "pending", "attempts": 0,
     "nextAttemptAt": <now>, "createdAt": <now>, "updatedAt": <now>,
     "mode": "range", "rangeStart": "<ISO 8601>", "rangeEnd": "<ISO 8601>",
     "requestedBy": "<staff id>"}

Two scopes: ``mode: "range"`` (the default) takes the Analytics page's
selected range, conversations that *started* in ``[rangeStart, rangeEnd)``;
``mode: "latest"`` with ``limit: N`` takes the N most recent failed
conversations, whenever they happened (quicker than a long range). The console
watches the job's ``status`` (``processing`` → ``done`` / ``failed``); on
``done`` the job carries ``report: {scope, failed, analysed, notAnalysed,
emailedTo}``, on ``failed`` a ``lastError``. See ``docs/failure-report-contract.md``.

How a report is made (``handle_job``):

1. **Pick the failures by rules, not by an LLM** (``failure_signals``): a
   booking the patient wanted but didn't get, a tool error, a crashed turn, an
   escalation, an unresolved chat, negative sentiment.
2. **Read each conversation's Langfuse session** (every trace of the
   conversation: model calls, tool calls, fallbacks, errors) and compact it for
   the model (``compact_trace``): no repeated system prompts.
3. **One structured Gemini call per conversation** (``analyse_case``) →
   ``FailureAnalysis``: category, whether the agent was at fault, root cause,
   evidence, suggested fix. Cached in ``failure_reports`` per conversation, so
   overlapping ranges, a second click, or a retry after an email failure reuse it.
   The category is also written to Langfuse as the ``failure_category`` session score.
   If a model is out of quota (429) or down (5xx), the next one is tried:
   ``FAILURE_REPORT_MODEL``, then ``MODEL``, then the Gemini ``MODEL_FALLBACKS``.
4. **One email** to ``FAILURE_REPORT_EMAIL``, agent-at-fault cases first, each
   linking to the conversation in the console and its Langfuse session.

Phone numbers are redacted from what the model sees, and so from the email.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import smtplib
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from clinic_agent import observability
from clinic_agent.config import settings
from clinic_agent.db import create_index_safe, get_db
from clinic_agent.integrations import email
from clinic_agent.runtime import analytics, conversations
from clinic_agent.runtime.outbox import PermanentError

log = logging.getLogger("clinic_agent.runtime.failure_report")

REPORTS = "failure_reports"
_CONCURRENCY = 4
_TRACE_CHARS = 40_000  # cap on the compacted trace handed to the model
_FIELD_CHARS = 800  # cap per input/output value in the compacted trace
_LATEST_SCAN = 2000  # "latest N" looks back at most this many finished conversations
_MODEL_COOLDOWN_SECONDS = 600  # skip a model this long after it ran out of quota

# Signals, in the order they're listed in the email.
BOOKING_FAILED = "booking_failed"
TURN_CRASHED = "turn_crashed"
TOOL_ERROR = "tool_error"
ESCALATED = "escalated"
UNRESOLVED = "unresolved"
NEGATIVE_SENTIMENT = "negative_sentiment"

SIGNAL_LABELS = {
    BOOKING_FAILED: "Wanted an appointment, didn't get one",
    TURN_CRASHED: "A turn crashed (fallback reply sent)",
    TOOL_ERROR: "A tool call errored",
    ESCALATED: "Handed to staff",
    UNRESOLVED: "Ended without resolution",
    NEGATIVE_SENTIMENT: "Patient left unhappy",
}

_BOOKING_INTENTS = {"book_appointment", "reschedule_appointment"}
_BOOKED_OUTCOMES = {"appointment_booked", "appointment_rescheduled"}


# ---- 1. which conversations failed (pure) -------------------------------------


def failure_signals(conv: dict) -> list[str]:
    """Why ``conv`` (a ``conversations`` document) counts as failed; [] if it didn't."""
    messages = conv.get("messages") or []
    calls = conversations._calls_from_messages(messages)
    outcome = conversations.derive_outcome(calls)
    signals: list[str] = []

    if conversations.derive_primary_intent(calls) in _BOOKING_INTENTS and (
        outcome not in _BOOKED_OUTCOMES
    ):
        signals.append(BOOKING_FAILED)
    if any(
        m.get("role") == "agent"
        and m.get("type", "text") == "text"
        and m.get("content") == conversations.FALLBACK_REPLY
        for m in messages
    ):
        signals.append(TURN_CRASHED)
    if any(c.get("status") == "error" for c in calls):
        signals.append(TOOL_ERROR)
    if outcome == "escalated" or conv.get("escalated"):
        signals.append(ESCALATED)
    # One message ("hi") and silence isn't a failure worth a root cause.
    patient_turns = sum(
        1 for m in messages if m.get("role") == "patient" and m.get("type", "text") == "text"
    )
    if outcome == "no_resolution" and patient_turns >= 2:
        signals.append(UNRESOLVED)
    if conv.get("sentiment") == "negative":
        signals.append(NEGATIVE_SENTIMENT)
    return signals


def parse_range(job: dict) -> tuple[datetime, datetime]:
    """``(start, end)`` in UTC from the job; PermanentError if missing or invalid."""
    try:
        start = datetime.fromisoformat(str(job["rangeStart"]))
        end = datetime.fromisoformat(str(job["rangeEnd"]))
    except (KeyError, ValueError) as exc:
        raise PermanentError("rangeStart / rangeEnd must be ISO 8601 datetimes") from exc
    if start.tzinfo is None or end.tzinfo is None:
        raise PermanentError("rangeStart / rangeEnd need a timezone (e.g. Z)")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end <= start:
        raise PermanentError("rangeEnd must be after rangeStart")
    return start, end


@dataclass(frozen=True)
class Scope:
    """Which failed conversations a report covers."""

    label: str  # for the email: "13 Nov - 19 Nov 2026" / "latest 10 failed conversations"
    start: datetime | None = None
    end: datetime | None = None
    latest: int | None = None


def parse_scope(job: dict) -> Scope:
    """The job's scope; PermanentError if it's invalid (retrying can't fix it)."""
    mode = job.get("mode") or "range"
    if mode == "latest":
        cap = max(1, settings.FAILURE_REPORT_MAX_CASES)
        try:
            n = int(job.get("limit"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise PermanentError("limit must be a whole number") from exc
        if not 1 <= n <= cap:
            raise PermanentError(f"limit must be between 1 and {cap}")
        noun = "conversation" if n == 1 else "conversations"
        return Scope(label=f"latest {n} failed {noun}", latest=n)
    if mode != "range":
        raise PermanentError(f"unknown mode {mode!r}; use 'range' or 'latest'")
    start, end = parse_range(job)
    return Scope(label=_range_label(start, end), start=start, end=end)


def find_latest_failed(limit: int) -> list[tuple[dict, list[str]]]:
    """The ``limit`` most recent finished conversations that failed, newest first."""
    cursor = (
        get_db()[conversations.COLLECTION]
        .find(
            {"clinicId": settings.CLINIC_ID, "status": {"$ne": "active"}},
            sort=[("startedAt", -1)],
        )
        .limit(_LATEST_SCAN)
    )
    out = []
    for conv in cursor:
        signals = failure_signals(conv)
        if signals:
            out.append((conv, signals))
            if len(out) >= limit:
                break
    return out


def find_failed(start: datetime, end: datetime) -> list[tuple[dict, list[str]]]:
    """Finished conversations that started in ``[start, end)`` and failed, oldest first."""
    cursor = get_db()[conversations.COLLECTION].find(
        {
            "clinicId": settings.CLINIC_ID,
            # ISO-8601 UTC strings compare chronologically as plain strings.
            "startedAt": {"$gte": start.isoformat(), "$lt": end.isoformat()},
            "status": {"$ne": "active"},  # still going: not a verdict yet
        },
        sort=[("startedAt", 1)],
    )
    out = []
    for conv in cursor:
        signals = failure_signals(conv)
        if signals:
            out.append((conv, signals))
    return out


# ---- 2. the evidence: transcript + compacted Langfuse trace (pure) -------------


def _clip(value: Any, limit: int = _FIELD_CHARS) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"…[+{len(text) - limit} chars]"


def render_transcript(messages: list[dict]) -> str:
    """The stored conversation, tool calls included, as the model reads it."""
    lines = []
    for m in messages:
        ts = str(m.get("timestamp") or "")[11:19]
        if m.get("type") == "tool_call" and m.get("tool"):
            t = m["tool"]
            result = f"ERROR {t.get('error')}" if t.get("status") == "error" else t.get("result")
            lines.append(
                f"[{ts}] tool {t.get('name')}({_clip(t.get('args'), 400)}) → {_clip(result, 400)}"
            )
        elif m.get("content"):
            lines.append(f"[{ts}] {m.get('role')}: {m.get('content')}")
    return "\n".join(lines)


def compact_trace(observations: list[Any]) -> str:
    """The Langfuse session, cut down to what explains a failure.

    Per trace (one per turn): the root's input/output; each model call's model,
    level, latency and OUTPUT (its input re-sends the system prompt and history
    every time, so it's dropped); each tool call's input and output; other spans
    only by name. Errors and warnings always keep their status message.
    """
    lines: list[str] = []
    trace_ids: list[str] = []
    for o in observations:
        if o.trace_id not in trace_ids:
            trace_ids.append(o.trace_id)
    for n, trace_id in enumerate(trace_ids, 1):
        obs = [o for o in observations if o.trace_id == trace_id]
        root = next((o for o in obs if o.is_root_observation), obs[0])
        lines.append(f"## Trace {n}: {root.name or root.type} ({_clip(root.start_time, 40)})")
        for o in obs:
            kind = (o.type or "").upper()
            head = f"- {kind} {o.name or ''}".rstrip()
            if getattr(o, "model", None):
                head += f" model={o.model}"
            if o.level and o.level not in ("DEFAULT", "DEBUG"):
                head += f" level={o.level}"
            if getattr(o, "latency", None):
                head += f" latency={float(o.latency):.1f}s"
            lines.append(head)
            if o.status_message:
                lines.append(f"    status: {_clip(o.status_message, 400)}")
            if o is root:
                lines.append(f"    input: {_clip(o.input)}")
                lines.append(f"    output: {_clip(o.output)}")
            elif kind == "GENERATION":
                lines.append(f"    output: {_clip(o.output)}")
            elif kind == "TOOL":
                lines.append(f"    input: {_clip(o.input)}")
                lines.append(f"    output: {_clip(o.output)}")
    text = "\n".join(lines)
    if len(text) > _TRACE_CHARS:
        text = text[:_TRACE_CHARS] + "\n…[trace truncated]"
    return text


def _fetch_trace(conv: dict) -> str | None:
    """The compacted Langfuse session for ``conv``; None if tracing is off / it failed."""
    try:
        since = datetime.fromisoformat(conv["startedAt"]) - timedelta(minutes=5)
        observations = observability.session_observations(conv["externalId"], since=since)
    except Exception:
        log.exception("could not read the Langfuse session for %s", conv.get("externalId"))
        return None
    if not observations:
        return None
    return compact_trace(observations)


# ---- 3. the analysis (one Gemini call per conversation) ------------------------


class FailureAnalysis(BaseModel):
    category: Literal[
        "no_slot_available",
        "tool_error",
        "wrong_tool_arguments",
        "misunderstood_patient",
        "wrong_or_made_up_info",
        "model_error",
        "unnecessary_escalation",
        "needed_a_human",
        "missing_clinic_data",
        "patient_dropped_off",
        "other",
    ]
    agent_at_fault: bool = Field(
        description="True if a better agent (prompt, tools, data, code) would have succeeded."
    )
    severity: Literal["high", "medium", "low"]
    summary: str = Field(
        description="One or two sentences: what the patient wanted, what happened."
    )
    root_cause: str = Field(
        description="The underlying reason, as specific as the evidence allows."
    )
    evidence: list[str] = Field(
        description="2-4 short quotes or trace lines that show the root cause."
    )
    suggested_fix: str = Field(description="The concrete change that would prevent this.")
    fix_area: Literal["prompt", "tool", "clinic_data", "code", "model", "none"]


_PROMPT = """\
You are reviewing a conversation where a clinic's AI receptionist (on WhatsApp or a \
voice call) may have failed the patient. Find the root cause.

The receptionist books, reschedules and cancels appointments and answers clinic \
questions using tools; it can hand the patient to staff (escalate_to_human). \
It is flagged because: {signals}.

Judge carefully:
- Not every flag is the agent's fault. No free slot, a patient who just stopped \
replying, or a patient who asked for a human are legitimate; say so \
(agent_at_fault=false).
- Escalating is right when the patient asked for a person or the request is \
outside what the tools can do; it's a failure when the agent could have handled it.
- Prefer the earliest mistake that caused the failure over its symptoms.
- Ground every claim in the transcript or trace; quote them in evidence. \
Don't invent details. If the evidence is thin, say so in root_cause.

Conversation {conv_id} · channel {channel} · outcome {outcome} · primary intent \
{intent}{escalation}

### Transcript (stored conversation, with tool calls)
{transcript}

### Langfuse trace (model calls, tool calls, errors; one trace per turn)
{trace}
"""


def build_prompt(conv: dict, signals: list[str], trace: str | None) -> str:
    calls = conversations._calls_from_messages(conv.get("messages") or [])
    reason = conversations.escalation_reason(calls)
    prompt = _PROMPT.format(
        signals="; ".join(SIGNAL_LABELS.get(s, s) for s in signals),
        conv_id=conv.get("externalId"),
        channel=conv.get("channel", conversations.CHANNEL),
        outcome=conversations.derive_outcome(calls),
        intent=conversations.derive_primary_intent(calls),
        escalation=f" · escalation reason: {reason}" if reason else "",
        transcript=render_transcript(conv.get("messages") or []) or "(empty)",
        trace=trace or "(not available: tracing was off or the session couldn't be read)",
    )
    return observability.redact(prompt)


def report_models() -> list[str]:
    """Gemini models to try, in order: the report model, the chat model, its fallbacks."""
    names = [settings.FAILURE_REPORT_MODEL, settings.MODEL, *settings.MODEL_FALLBACKS.split(",")]
    out: list[str] = []
    for name in (n.strip() for n in names):
        if name and not name.startswith("groq:") and name not in out:
            out.append(name)
    return out


# model → when it last ran out of quota (time.monotonic()); shared across reports.
_exhausted: dict[str, float] = {}


def _available_models() -> list[str]:
    now = time.monotonic()
    models = report_models()
    fresh = [m for m in models if now - _exhausted.get(m, -1e9) > _MODEL_COOLDOWN_SECONDS]
    return fresh or models  # all cooling down: try them anyway


def _is_capacity_error(exc: Exception) -> bool:
    """Out of quota / overloaded: worth trying another model."""
    code = getattr(exc, "code", None)
    return isinstance(exc, genai_errors.APIError) and (
        code == 429 or (isinstance(code, int) and code >= 500)
    )


def analyse_case(
    conv: dict, signals: list[str], trace: str | None
) -> tuple[FailureAnalysis, str] | None:
    """One structured Gemini call → ``(analysis, model used)``, or None if every model failed."""
    prompt = build_prompt(conv, signals, trace)
    for model in _available_models():
        with observability.generation(
            "analyze-failed-conversation",
            model=model,
            input=prompt,
            model_parameters={"temperature": 0.2},
            metadata={"conversation_id": conv.get("externalId"), "signals": signals},
        ) as gen:
            try:
                resp = analytics._get_client().models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        response_mime_type="application/json",
                        response_schema=FailureAnalysis,
                    ),
                )
            except Exception as exc:
                if _is_capacity_error(exc):
                    if getattr(exc, "code", None) == 429:
                        _exhausted[model] = time.monotonic()
                    log.warning("%s unavailable (%s); trying the next model", model, exc.code)  # type: ignore[attr-defined]
                    gen.update(level="WARNING", status_message=f"{model}: {exc!r}"[:300])
                    continue
                log.exception("failure analysis failed for %s", conv.get("externalId"))
                gen.update(level="ERROR", status_message=repr(exc))
                return None
            gen.update(
                output=resp.text,
                usage_details=observability.usage_details(resp.usage_metadata),
            )
            parsed = resp.parsed
            return (parsed, model) if isinstance(parsed, FailureAnalysis) else None
    log.error("failure analysis for %s: every model was unavailable", conv.get("externalId"))
    return None


# ---- the cache ----------------------------------------------------------------


def ensure_indexes() -> None:
    create_index_safe(
        get_db()[REPORTS],
        [("clinicId", 1), ("conversationId", 1)],
        unique=True,
        name="clinic_conversation",
    )


def _cached(conv: dict) -> FailureAnalysis | None:
    """The stored analysis, if the conversation hasn't changed since it was made."""
    doc = get_db()[REPORTS].find_one(
        {"clinicId": settings.CLINIC_ID, "conversationId": conv["externalId"]}
    )
    if not doc or doc.get("conversationEndedAt") != conv.get("endedAt"):
        return None
    try:
        return FailureAnalysis.model_validate(doc["analysis"])
    except Exception:
        return None


def _store(conv: dict, signals: list[str], analysis: FailureAnalysis, model: str) -> None:
    now = datetime.now(UTC).isoformat()
    get_db()[REPORTS].update_one(
        {"clinicId": settings.CLINIC_ID, "conversationId": conv["externalId"]},
        {
            "$set": {
                "conversationEndedAt": conv.get("endedAt"),
                "channel": conv.get("channel", conversations.CHANNEL),
                "signals": signals,
                "analysis": analysis.model_dump(),
                "model": model,
                "updatedAt": now,
            },
            "$setOnInsert": {"createdAt": now},
        },
        upsert=True,
    )


# ---- one case end to end ------------------------------------------------------


@dataclass
class Case:
    conv: dict
    signals: list[str]
    analysis: FailureAnalysis | None = None
    langfuse_url: str | None = None

    @property
    def console_url(self) -> str | None:
        """The conversation's page in the staff console (keyed by its Mongo ``_id``)."""
        base = settings.CONSOLE_BASE_URL.strip().rstrip("/")
        doc_id = self.conv.get("_id")
        return f"{base}/conversations/{doc_id}" if base and doc_id else None


async def _analyse(case: Case, gate: asyncio.Semaphore) -> Case:
    conv_id = case.conv["externalId"]
    async with gate:
        case.langfuse_url = await asyncio.to_thread(observability.session_url, conv_id)
        case.analysis = await asyncio.to_thread(_cached, case.conv)
        if case.analysis is not None:
            return case
        trace = await asyncio.to_thread(_fetch_trace, case.conv)
        result = await asyncio.to_thread(analyse_case, case.conv, case.signals, trace)
    if result is not None:
        case.analysis, model = result
        await asyncio.to_thread(_store, case.conv, case.signals, case.analysis, model)
        observability.score_session(
            conv_id,
            "failure_category",
            case.analysis.category,
            data_type="CATEGORICAL",
            comment=case.analysis.root_cause,
        )
    return case


# ---- 4. the email (pure) --------------------------------------------------------

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _sort_key(case: Case) -> tuple:
    a = case.analysis
    if a is None:
        return (1, 3, case.conv.get("startedAt", ""))
    return (
        0 if a.agent_at_fault else 2,
        _SEVERITY_ORDER[a.severity],
        case.conv.get("startedAt", ""),
    )


def _local(iso: str | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    if not iso:
        return "?"
    try:
        return (
            datetime.fromisoformat(iso).astimezone(ZoneInfo(settings.CLINIC_TIMEZONE)).strftime(fmt)
        )
    except ValueError:
        return iso


def _range_label(start: datetime, end: datetime) -> str:
    tz = ZoneInfo(settings.CLINIC_TIMEZONE)
    first = start.astimezone(tz)
    last = (end - timedelta(microseconds=1)).astimezone(tz)  # the range is end-exclusive
    if first.date() == last.date():
        return first.strftime("%d %b %Y")
    return f"{first.strftime('%d %b')} – {last.strftime('%d %b %Y')}"  # noqa: RUF001


def render_email(cases: list[Case], label: str, not_analysed: int) -> tuple[str, str, str]:
    """``(subject, text, html)`` for the report; ``label`` names its scope."""
    cases = sorted(cases, key=_sort_key)
    total = len(cases) + not_analysed
    at_fault = sum(1 for c in cases if c.analysis and c.analysis.agent_at_fault)
    subject = f"Medi Desk AI failures report · {label} · {total} failed conversation" + (
        "" if total == 1 else "s"
    )

    categories = Counter(c.analysis.category for c in cases if c.analysis)
    if any(c.analysis is None for c in cases):
        categories["analysis_failed"] = sum(1 for c in cases if c.analysis is None)

    # -- plain text
    text = [subject, ""]
    if not total:
        text.append("No failed conversations in this range.")
    else:
        text.append(f"{at_fault} of {len(cases)} analysed were the agent's fault.")
        if not_analysed:
            text.append(f"{not_analysed} more weren't analysed (limit {len(cases)} per report).")
        text += ["", "By category:"] + [f"  {k}: {v}" for k, v in categories.most_common()]
    for i, c in enumerate(cases, 1):
        a = c.analysis
        text += [
            "",
            "-" * 60,
            f"{i}. {c.conv['externalId']} · {c.conv.get('channel', 'whatsapp')} · {_local(c.conv.get('startedAt'))}",
        ]
        text.append("Flags: " + ", ".join(SIGNAL_LABELS.get(s, s) for s in c.signals))
        if a is None:
            text.append("Analysis failed; open the conversation to review it.")
        else:
            text += [
                f"Category: {a.category} · severity {a.severity} · agent at fault: {'yes' if a.agent_at_fault else 'no'}",
                f"What happened: {a.summary}",
                f"Root cause: {a.root_cause}",
                "Evidence:",
                *[f"  - {e}" for e in a.evidence],
                f"Suggested fix ({a.fix_area}): {a.suggested_fix}",
            ]
        if c.console_url:
            text.append(f"Conversation: {c.console_url}")
        if c.langfuse_url:
            text.append(f"Langfuse: {c.langfuse_url}")

    # -- html
    e = html.escape
    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1f2328;'
        'max-width:760px;line-height:1.5">',
        f'<h2 style="margin:0 0 4px">Failures report · {e(label)}</h2>',
    ]
    if not total:
        parts.append("<p>No failed conversations in this range.</p>")
    else:
        parts.append(
            f'<p style="margin:0 0 16px;color:#57606a">{total} failed conversation'
            f"{'' if total == 1 else 's'} · <b>{at_fault}</b> of {len(cases)} analysed were the "
            "agent's fault"
            + (f" · {not_analysed} not analysed (limit {len(cases)})" if not_analysed else "")
            + "</p>"
        )
        parts.append('<table style="border-collapse:collapse;margin-bottom:24px">')
        for k, v in categories.most_common():
            parts.append(
                f'<tr><td style="padding:2px 16px 2px 0">{e(k.replace("_", " "))}</td>'
                f'<td style="padding:2px 0;text-align:right"><b>{v}</b></td></tr>'
            )
        parts.append("</table>")
    for i, c in enumerate(cases, 1):
        a = c.analysis
        border = "#cf222e" if a and a.agent_at_fault else "#d0d7de"
        parts.append(
            f'<div style="border-left:4px solid {border};padding:8px 16px;margin:0 0 20px;'
            'background:#f6f8fa">'
        )
        title = a.category.replace("_", " ") if a else "analysis failed"
        parts.append(f'<h3 style="margin:0">{i}. {e(title)}</h3>')
        meta = [
            e(c.conv.get("channel", "whatsapp")),
            e(_local(c.conv.get("startedAt"))),
            f"<code>{e(c.conv['externalId'])}</code>",
        ]
        if a:
            meta.insert(0, f"severity {e(a.severity)}")
            meta.insert(1, "agent at fault" if a.agent_at_fault else "not the agent's fault")
        parts.append(
            f'<p style="margin:2px 0 8px;color:#57606a;font-size:13px">{" · ".join(meta)}</p>'
        )
        parts.append(
            '<p style="margin:0 0 8px;font-size:13px">Flags: '
            + ", ".join(e(SIGNAL_LABELS.get(s, s)) for s in c.signals)
            + "</p>"
        )
        if a is None:
            parts.append("<p>The analysis failed; open the conversation to review it.</p>")
        else:
            parts.append(f"<p style='margin:0 0 8px'><b>What happened.</b> {e(a.summary)}</p>")
            parts.append(f"<p style='margin:0 0 8px'><b>Root cause.</b> {e(a.root_cause)}</p>")
            if a.evidence:
                parts.append("<p style='margin:0'><b>Evidence</b></p><ul style='margin:4px 0 8px'>")
                parts += [
                    f"<li><code style='white-space:pre-wrap'>{e(x)}</code></li>" for x in a.evidence
                ]
                parts.append("</ul>")
            parts.append(
                f"<p style='margin:0 0 8px'><b>Suggested fix</b> ({e(a.fix_area.replace('_', ' '))}). "
                f"{e(a.suggested_fix)}</p>"
            )
        links = [
            f'<a href="{e(url)}">{name}</a>'
            for url, name in (
                (c.console_url, "Open conversation"),
                (c.langfuse_url, "Open in Langfuse"),
            )
            if url
        ]
        if links:
            parts.append(f'<p style="margin:0">{" · ".join(links)}</p>')
        parts.append("</div>")
    parts.append("</div>")
    return subject, "\n".join(text), "".join(parts)


# ---- the outbox handler ---------------------------------------------------------


async def handle_job(job: dict) -> tuple[str, dict]:
    """Outbox ``failures.report``: analyse the scope's failures and email the report."""
    recipient = settings.FAILURE_REPORT_EMAIL.strip()
    if not recipient:
        raise PermanentError("FAILURE_REPORT_EMAIL is not set")
    scope = parse_scope(job)

    if scope.latest is not None:
        failed = await asyncio.to_thread(find_latest_failed, scope.latest)
    else:
        failed = await asyncio.to_thread(find_failed, scope.start, scope.end)
    cap = max(0, settings.FAILURE_REPORT_MAX_CASES)
    gate = asyncio.Semaphore(_CONCURRENCY)
    cases = await asyncio.gather(
        *(_analyse(Case(conv, signals), gate) for conv, signals in failed[:cap])
    )
    not_analysed = max(0, len(failed) - cap)

    subject, text, body = render_email(list(cases), scope.label, not_analysed)
    try:
        await asyncio.to_thread(email.send_email, recipient, subject, text, body)
    except email.EmailNotConfigured as exc:
        raise PermanentError(f"email not configured: {exc}") from exc
    except smtplib.SMTPAuthenticationError as exc:
        raise PermanentError("SMTP login failed; check SMTP_USERNAME / SMTP_PASSWORD") from exc

    analysed = sum(1 for c in cases if c.analysis is not None)
    log.info(
        "failures report (%s) → %s: %d failed, %d analysed",
        scope.label,
        recipient,
        len(failed),
        analysed,
    )
    return "done", {
        "report": {
            "scope": scope.label,
            "failed": len(failed),
            "analysed": analysed,
            "notAnalysed": len(failed) - analysed,
            "emailedTo": recipient,
        }
    }

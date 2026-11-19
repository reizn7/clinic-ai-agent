"""Run the agent (WhatsApp + voice) against eval scenarios as a Langfuse experiment.

Voice scenarios (``"channel": "voice"``) run a real Gemini Live call in-process
via ``evals/voice_driver.py``; they need macOS ``say`` and take real time.

    uv run python evals/run_evals.py                    # all scenarios
    uv run python evals/run_evals.py --only emergency,clinic-info
    uv run python evals/run_evals.py --keep             # don't clean up (inspect in Mongo/console)
    uv run python evals/run_evals.py --upload           # also upsert scenarios to a Langfuse dataset

What it does, per scenario in ``evals/scenarios.json``:

1. Gives the scenario its own synthetic patient phone (``1555`` + run nonce + index;
   555 numbers are fictional), optionally books a setup appointment for it.
2. Plays the scripted patient turns through the REAL turn loop (``turn._run_turn``:
   live Gemini, real tools, the configured Mongo with its current clinic data).
3. Snapshots what landed in Mongo for that phone (appointments, escalations).
4. Scores it: deterministic checks (tools called / not called, DB outcome, no
   crashed turns) + a Gemini LLM judge (rubric, grounding in tool results, style).

Everything shows up in Langfuse under Experiments (one trace per scenario, with the
ADK agent/LLM/tool spans nested inside).

Safety when pointed at a live database:
* ``GOOGLE_CALENDAR_ENABLED`` and ``WHATSAPP_CONFIRMATIONS_ENABLED`` are forced off
  in this process, so bookings enqueue no calendar invites or WhatsApp messages.
* No WhatsApp message is ever sent (the turn loop is called directly).
* On exit, every document created for the synthetic phones is deleted (patients,
  appointments, outbox jobs, transcripts, conversations, escalations, pending
  messages). Pass ``--keep`` to skip that.
* While a run is in progress the synthetic patients/appointments/escalations are
  visible in the staff console, and they occupy real slots until cleanup.
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Must be set before clinic_agent.config is imported (env vars beat .env).
os.environ["GOOGLE_CALENDAR_ENABLED"] = "false"
os.environ["WHATSAPP_CONFIRMATIONS_ENABLED"] = "false"
os.environ.setdefault("LANGFUSE_ENVIRONMENT", "eval")

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import httpx  # noqa: E402
from google import genai  # noqa: E402
from google.genai import errors as genai_errors  # noqa: E402
from google.genai import types as genai_types  # noqa: E402
from langfuse import Evaluation  # noqa: E402

from clinic_agent import observability  # noqa: E402
from clinic_agent.config import settings  # noqa: E402
from clinic_agent.db import get_db  # noqa: E402
from clinic_agent.phone import to_e164  # noqa: E402
from clinic_agent.prompt import SYSTEM_PROMPT, VOICE_SYSTEM_PROMPT  # noqa: E402
from clinic_agent.runtime import conversations, turn  # noqa: E402
from clinic_agent.tools import repo  # noqa: E402

sys.path.insert(0, str(ROOT / "evals"))
from voice_driver import run_voice_call  # noqa: E402

SCENARIOS = ROOT / "evals" / "scenarios.json"
DATASET_NAME = "whatsapp-agent-scenarios"
JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "groq:openai/gpt-oss-120b")
# Tried in order when the judge model is overloaded (429/5xx after retries).
JUDGE_MODELS = [JUDGE_MODEL] + [
    m
    for m in os.environ.get("EVAL_JUDGE_FALLBACKS", "gemini-3.5-flash,gemma-4-26b-a4b-it,gemini-3.8-flash").split(",")
    if m and m != JUDGE_MODEL
]
JUDGE_ATTEMPTS = 3
JUDGE_ROUNDS = 4  # passes over JUDGE_MODELS before giving up
TURN_TIMEOUT_SECONDS = 120  # per WhatsApp agent turn
TURN_ATTEMPTS = 3
JUDGE_TIMEOUT_SECONDS = 90
PHONE_PREFIX = "1555"

_phones: list[str] = []  # every synthetic phone used this run (for cleanup)
_setup_appointments: dict[str, str] = {}  # phone -> appointment id booked by setup
_judge_client: genai.Client | None = None


# ---- harness ------------------------------------------------------------------


def _phone_for(nonce: str, idx: int) -> str:
    phone = f"{PHONE_PREFIX}{nonce}{idx:03d}"
    _phones.append(phone)
    return phone


def _book_setup_appointment(phone: str, doctor: str) -> dict:
    """Book a real free slot for the synthetic patient (the latest slot of the
    doctor's next open day, to stay out of the way of the earliest ones)."""
    upcoming = repo.get_upcoming_availability(doctor, 1)
    if not upcoming.get("dates"):
        raise RuntimeError(f"setup: no free slots for Dr. {doctor}")
    day = upcoming["dates"][0]
    res = repo.create_appointment(
        phone, "Eval Patient", 45, "Female", doctor, day["date"], day["slots"][-1]
    )
    if not res.get("success"):
        raise RuntimeError(f"setup: could not book Dr. {doctor}: {res}")
    _setup_appointments[phone] = res["appointmentId"]
    return {"doctor": res["doctor"], "date": res["date"], "slot": res["slot"]}


def _db_snapshot(phone: str) -> dict:
    db = get_db()
    scope = {"clinicId": settings.CLINIC_ID}
    patient = db["patients"].find_one({**scope, "phone": phone})
    appts: list[dict] = []
    if patient:
        doctors = {d["_id"]: d["name"] for d in db["doctors"].find(scope, {"name": 1})}
        for a in db["appointments"].find({**scope, "patientId": patient["_id"]}):
            appts.append(
                {
                    "id": str(a["_id"]),
                    "doctor": doctors.get(a["doctorId"], "?"),
                    "date": a["date"],
                    "slot": a["slot"],
                    "status": a["status"],
                    "setup": str(a["_id"]) == _setup_appointments.get(phone),
                }
            )
    escalations = db["escalations"].count_documents({**scope, "patientPhone": to_e164(phone)})
    return {
        "patient": {k: patient.get(k) for k in ("name", "age", "gender")} if patient else None,
        "appointments": appts,
        "escalations": escalations,
    }


def cleanup() -> None:
    """Delete everything any eval run created (every ``1555…`` phone in this clinic).

    Sweeps by prefix rather than this run's phones, so it also clears leftovers
    from an earlier run that was killed before it could clean up.
    """
    db = get_db()
    scope = {"clinicId": settings.CLINIC_ID}
    phones = {"$regex": f"^{PHONE_PREFIX}"}
    e164s = {"$regex": rf"^\+{PHONE_PREFIX}"}
    patient_ids = [p["_id"] for p in db["patients"].find({**scope, "phone": phones})]
    appt_ids = [
        a["_id"] for a in db["appointments"].find({**scope, "patientId": {"$in": patient_ids}})
    ]
    deleted = {
        "outbox": db["outbox"].delete_many({"appointmentId": {"$in": appt_ids}}).deleted_count,
        "appointments": db["appointments"].delete_many({"_id": {"$in": appt_ids}}).deleted_count,
        "patients": db["patients"].delete_many({"_id": {"$in": patient_ids}}).deleted_count,
        "transcripts": db["transcripts"].delete_many({**scope, "phone": phones}).deleted_count,
        "conversations": db[conversations.COLLECTION]
        .delete_many({**scope, "phone": e164s})
        .deleted_count,
        "escalations": db["escalations"]
        .delete_many({**scope, "patientPhone": e164s})
        .deleted_count,
        "pending_messages": db["pending_messages"].delete_many({"phone": phones}).deleted_count,
    }
    progress(f"cleanup: removed {deleted}")


# ---- task ----------------------------------------------------------------------


def make_task(nonce: str, index: dict[str, int]):
    async def task(*, item, **_: Any) -> dict:
        item_id = _get(item, "metadata", {}).get("id") or _get(item, "id")
        inp = _get(item, "input")
        phone = _phone_for(nonce, index[item_id])
        setup = None
        if (inp.get("setup") or {}).get("appointment_with"):
            setup = await asyncio.to_thread(
                _book_setup_appointment, phone, inp["setup"]["appointment_with"]
            )

        channel = inp.get("channel", "whatsapp")
        say = _progress_for(item_id)
        say(
            f"start ({channel}, {len(inp['turns'])} line(s))"
            + (f", setup {setup}" if setup else "")
        )
        t_start = time.perf_counter()
        try:
            if channel == "voice":
                out = await _voice_task(phone, inp, setup, say)
            else:
                out = await _whatsapp_task(phone, inp, setup, say)
        except Exception as exc:
            say(f"FAILED after {time.perf_counter() - t_start:.0f}s: {exc!r}")
            raise
        say(
            f"done in {time.perf_counter() - t_start:.0f}s · tools {out['tools_called']} · "
            f"db {out['db']['appointments'] or 'no appts'}, escalations {out['db']['escalations']}"
        )
        return out

    return task


def _progress_for(item_id: str):
    def say(msg: str) -> None:
        progress(f"[{item_id}] {msg}")

    return say


def progress(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


async def _whatsapp_task(phone: str, inp: dict, setup: dict | None, say) -> dict:
    """Scripted patient messages through the real WhatsApp turn loop."""
    transcript, all_tools, failed_turns = [], [], 0
    for msg in inp["turns"]:
        say(f"  patient: {msg}")
        conv_id = await conversations.resolve_conversation_id(phone)
        t0 = time.perf_counter()
        # The model call has no timeout of its own; an overloaded API can leave
        # it hanging forever, so cap each turn and retry it.
        for attempt in range(TURN_ATTEMPTS):
            try:
                reply, tool_calls, failed = await asyncio.wait_for(
                    turn._run_turn(phone, msg, conv_id), timeout=TURN_TIMEOUT_SECONDS
                )
                break
            except TimeoutError:
                say(f"  agent turn timed out (attempt {attempt + 1}/{TURN_ATTEMPTS})")
        else:
            reply, tool_calls, failed = "", [], True
        failed_turns += int(failed)
        all_tools.extend(tool_calls)
        tools = [c.get("name") for c in tool_calls]
        say(
            f"  agent ({time.perf_counter() - t0:.1f}s): {reply[:160]}"
            + (f"  [tools {tools}]" if tools else "")
        )
        transcript.append(
            {
                "patient": msg,
                "agent": reply,
                "tools": [_tool_view(c) for c in tool_calls],
                "latency_ms": round((time.perf_counter() - t0) * 1000),
            }
        )

    return {
        "channel": "whatsapp",
        "final_reply": transcript[-1]["agent"] if transcript else "",
        "transcript": transcript,
        "tools_called": [c.get("name") for c in all_tools],
        "failed_turns": failed_turns,
        "setup": setup,
        "db": await asyncio.to_thread(_db_snapshot, phone),
    }


def _tool_view(c: dict) -> dict:
    return {
        "name": c.get("name"),
        "args": c.get("args"),
        "status": c.get("status"),
        "result": c.get("result"),
    }


async def _voice_task(phone: str, inp: dict, setup: dict | None, say) -> dict:
    """One real voice call (Gemini Live audio) driven by synthesised caller speech."""
    call = await run_voice_call(
        phone, inp["turns"], voice=inp.get("tts_voice", "Aman"), progress=say
    )
    transcript = [
        {
            # What Live's input transcription heard (not the script), so ASR
            # errors are visible to the judge.
            "patient": "(call connected)" if t["user"] == conversations.CALL_STARTED else t["user"],
            "agent": t["assistant"],
            "tools": [_tool_view(c) for c in t["tools"]],
        }
        for t in call["turns"]
    ]
    crashed = call["end_reason"] == "error" or not any(t["agent"] for t in transcript)
    return {
        "channel": "voice",
        "scripted_lines": inp["turns"],
        "final_reply": transcript[-1]["agent"] if transcript else "",
        "transcript": transcript,
        "tools_called": [c.get("name") for c in call["tool_calls"]],
        "failed_turns": int(crashed),
        "timed_out_waiting_for": call["timed_out"],
        "end_reason": call["end_reason"],
        "setup": setup,
        "db": await asyncio.to_thread(_db_snapshot, phone),
    }


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Local items are dicts; Langfuse dataset items are objects."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default) or default


# ---- deterministic evaluators -----------------------------------------------------


def tools_eval(*, output, expected_output, **_):
    called = set(output["tools_called"])
    missing = [t for t in expected_output.get("tools_required", []) if t not in called]
    forbidden = [t for t in expected_output.get("tools_forbidden", []) if t in called]
    ok = not missing and not forbidden
    comment = f"called={sorted(called)}"
    if missing:
        comment += f"; missing={missing}"
    if forbidden:
        comment += f"; forbidden called={forbidden}"
    return Evaluation(name="tools_ok", value=ok, data_type="BOOLEAN", comment=comment)


def db_outcome_eval(*, output, expected_output, **_):
    db = output["db"]
    new = [a for a in db["appointments"] if not a["setup"]]
    problems = []

    want = expected_output.get("new_appointment")
    if want is False and new:
        problems.append(f"unexpected booking(s): {new}")
    elif isinstance(want, dict):
        match = [
            a
            for a in new
            if a["doctor"] == want.get("doctor", a["doctor"])
            and a["status"] == want.get("status", a["status"])
        ]
        if not match:
            problems.append(f"expected a {want} booking, found {new or 'none'}")

    if want_status := expected_output.get("setup_appointment_status"):
        setup = [a for a in db["appointments"] if a["setup"]]
        if not setup or setup[0]["status"] != want_status:
            problems.append(
                f"setup appointment status {setup[0]['status'] if setup else 'missing'}"
                f" != {want_status}"
            )

    if "escalated" in expected_output:
        got = db["escalations"] > 0
        if got != expected_output["escalated"]:
            problems.append(f"escalated={got}, expected {expected_output['escalated']}")

    return Evaluation(
        name="db_outcome",
        value=not problems,
        data_type="BOOLEAN",
        comment="; ".join(problems) or f"ok: {db}",
    )


def no_crash_eval(*, output, **_):
    n = output["failed_turns"]
    return Evaluation(
        name="no_crash",
        value=n == 0,
        data_type="BOOLEAN",
        comment=f"{n} turn(s) fell back to the error reply" if n else "ok",
    )


# ---- LLM judge ------------------------------------------------------------------

STYLE_CRITERIA = {
    "whatsapp": """\
- style_ok: follows the WhatsApp style rules (short, plain text, no markdown or
  emoji, no filler like "let me check", doesn't re-ask for facts already given).""",
    "voice": """\
- style_ok: follows the voice-call style rules (short natural spoken sentences, no
  lists or read-out formats like 2026-01-16 or 16:00, at most 2-3 options at a time,
  replies in the caller's language, spells the name back before booking, doesn't
  re-ask for facts already given). A brief "one moment" before a tool is allowed.
  The receptionist lines are speech transcriptions: ignore transcription artefacts
  (punctuation, casing, split words).""",
}

CHANNEL_LABEL = {
    "whatsapp": "WhatsApp conversation",
    "voice": "phone call (the patient lines are what speech recognition heard)",
}

JUDGE_INSTRUCTIONS = """\
You are grading one {channel_label} between a patient and a clinic's AI
receptionist. The receptionist's full system prompt is below for reference; its
HARD RULES, booking flow, safety and style sections are the standard.

Today's date: {today}

<system_prompt>
{system_prompt}
</system_prompt>

<scenario_rubric>
{rubric}
</scenario_rubric>

<conversation>
{conversation}
</conversation>

Grade three things, each true/false:
- rubric_pass: the conversation satisfies the scenario rubric.
- grounded: every doctor, date, slot, fee, clinic detail and booking/cancellation
  status the receptionist stated came from a tool result shown in the conversation
  (no invented facts; no claiming success the tools did not return).
{style_criteria}
Be strict but fair: judge only what the receptionist actually said.
"""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "rubric_pass": {"type": "boolean"},
        "rubric_reason": {"type": "string"},
        "grounded": {"type": "boolean"},
        "grounded_reason": {"type": "string"},
        "style_ok": {"type": "boolean"},
        "style_reason": {"type": "string"},
    },
    "required": [
        "rubric_reason",
        "rubric_pass",
        "grounded_reason",
        "grounded",
        "style_reason",
        "style_ok",
    ],
}


def _render_conversation(transcript: list[dict]) -> str:
    lines = []
    for t in transcript:
        lines.append(f"Patient: {t['patient']}")
        for c in t["tools"]:
            result = json.dumps(c["result"], default=str)
            if len(result) > 1500:
                result = result[:1500] + "…(truncated)"
            lines.append(f"  [tool] {c['name']}({json.dumps(c['args'], default=str)}) → {result}")
        lines.append(f"Receptionist: {t['agent']}")
    return "\n".join(lines)


def _parse_judge(text: str) -> dict:
    """JSON from the judge; tolerates a ```json fence or prose around the object."""
    start, end = text.find("{"), text.rfind("}")
    v = json.loads(text[start : end + 1] if start != -1 else text)
    missing = [k for k in JUDGE_SCHEMA["required"] if k not in v]
    if missing:
        raise ValueError(f"judge reply missing {missing}")
    return v


async def _groq_generate(model: str, prompt: str) -> str:
    """One judge call to Groq's OpenAI-compatible API with a strict JSON schema."""
    schema = {**JUDGE_SCHEMA, "additionalProperties": False}
    schema["required"] = list(JUDGE_SCHEMA["properties"])
    async with httpx.AsyncClient(timeout=JUDGE_TIMEOUT_SECONDS) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            json={
                "model": model,
                "temperature": 0,
                # Groq counts the reserved reply against the 8k tokens/min free
                # limit; the default is large enough that long transcripts never fit.
                "max_completion_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "verdict", "strict": True, "schema": schema},
                },
            },
        )
    if r.status_code != 200:
        raise _JudgeBusy(r.status_code, r.headers.get("retry-after"), r.text[:200])
    return r.json()["choices"][0]["message"]["content"]


async def _gemini_generate(model: str, prompt: str) -> str:
    assert _judge_client is not None
    config = genai_types.GenerateContentConfig(temperature=0)
    if model.startswith("gemma"):  # no JSON mode / response schema: ask in the prompt
        prompt = (
            f"{prompt}\nRespond with ONLY a JSON object (no markdown) with keys "
            f"{list(JUDGE_SCHEMA['properties'])}; *_pass/grounded/style_ok are "
            "booleans, *_reason are short strings."
        )
    else:
        config.response_mime_type = "application/json"
        config.response_json_schema = JUDGE_SCHEMA
    resp = await _judge_client.aio.models.generate_content(
        model=model, contents=prompt, config=config
    )
    return resp.text


class _JudgeBusy(Exception):
    def __init__(self, code: int, retry_after: str | None, body: str):
        super().__init__(f"{code}: {body}")
        self.code = code
        self.retry_after = float(retry_after) if retry_after else None


RETRYABLE = (429, 500, 502, 503, 504, 498, "timeout", "bad json")


async def _judge_call(prompt: str, item_id: str) -> tuple[dict, str]:
    """Ask the judge, riding out overload: retry each model with backoff on
    429/5xx, fall back through ``JUDGE_MODELS``, and repeat the whole list for
    ``JUDGE_ROUNDS`` rounds before giving up. ``groq:<model>`` uses Groq."""
    last: Exception | None = None
    for rnd in range(JUDGE_ROUNDS):
        for model in JUDGE_MODELS:
            for attempt in range(JUDGE_ATTEMPTS):
                try:
                    # Hard cap: an overloaded model can also just never answer.
                    if model.startswith("groq:"):
                        call = _groq_generate(model.removeprefix("groq:"), prompt)
                    else:
                        call = _gemini_generate(model, prompt)
                    text = await asyncio.wait_for(call, timeout=JUDGE_TIMEOUT_SECONDS)
                    return _parse_judge(text), model
                except (
                    genai_errors.APIError, _JudgeBusy, TimeoutError, httpx.HTTPError, ValueError
                ) as exc:
                    if isinstance(exc, ValueError):
                        code = "bad json"
                    elif isinstance(exc, httpx.HTTPError):
                        code = "timeout"
                    else:
                        code = getattr(exc, "code", "timeout")
                    if code not in RETRYABLE:
                        raise
                    last = exc
                    wait = getattr(exc, "retry_after", None) or 2 ** (attempt + 1)
                    wait = min(wait, 60) + random.random()
                    progress(
                        f"[{item_id}] judge {model} busy ({code}), round {rnd + 1}/"
                        f"{JUDGE_ROUNDS} attempt {attempt + 1}/{JUDGE_ATTEMPTS}, "
                        f"retrying in {wait:.0f}s"
                    )
                    await asyncio.sleep(wait)
            progress(f"[{item_id}] judge {model} unavailable, falling back")
        if rnd + 1 < JUDGE_ROUNDS:
            await asyncio.sleep(30)
    raise RuntimeError(f"all judge models unavailable: {JUDGE_MODELS}") from last


async def judge_eval(*, output, expected_output, metadata=None, **_):
    global _judge_client
    _judge_client = _judge_client or genai.Client()
    channel = output.get("channel", "whatsapp")
    prompt = JUDGE_INSTRUCTIONS.format(
        channel_label=CHANNEL_LABEL[channel],
        style_criteria=STYLE_CRITERIA[channel],
        today=datetime.now().strftime("%A, %B %d, %Y"),
        system_prompt=VOICE_SYSTEM_PROMPT if channel == "voice" else SYSTEM_PROMPT,
        rubric=expected_output.get("rubric", "(none)"),
        conversation=_render_conversation(output["transcript"]),
    )
    item_id = (metadata or {}).get("id", "?")
    progress(f"[{item_id}] judging with {JUDGE_MODELS[0]}…")
    v, model = await _judge_call(prompt, item_id)
    progress(
        f"[{item_id}] judge ({model}): rubric={v['rubric_pass']} "
        f"grounded={v['grounded']} style={v['style_ok']}"
    )
    return [
        Evaluation(
            name="judge_rubric",
            value=v["rubric_pass"],
            data_type="BOOLEAN",
            comment=v["rubric_reason"],
        ),
        Evaluation(
            name="judge_grounded",
            value=v["grounded"],
            data_type="BOOLEAN",
            comment=v["grounded_reason"],
        ),
        Evaluation(
            name="judge_style",
            value=v["style_ok"],
            data_type="BOOLEAN",
            comment=v["style_reason"],
        ),
    ]


# ---- run-level ----------------------------------------------------------------------


def pass_rates(*, item_results, **_):
    by_name: dict[str, list[bool]] = {}
    all_pass = []
    for r in item_results:
        vals = [e for e in r.evaluations if e.data_type == "BOOLEAN"]
        for e in vals:
            by_name.setdefault(e.name, []).append(bool(e.value))
        if vals:
            all_pass.append(all(bool(e.value) for e in vals))
    out = [
        Evaluation(name=f"{n}_rate", value=sum(v) / len(v), comment=f"{sum(v)}/{len(v)}")
        for n, v in sorted(by_name.items())
    ]
    if all_pass:
        out.append(
            Evaluation(
                name="scenario_pass_rate",
                value=sum(all_pass) / len(all_pass),
                comment=f"{sum(all_pass)}/{len(all_pass)} scenarios passed every check",
            )
        )
    return out


# ---- main -------------------------------------------------------------------------


def upload_dataset(lf, scenarios: list[dict]) -> None:
    lf.create_dataset(
        name=DATASET_NAME,
        description="Scripted WhatsApp conversations for the clinic receptionist agent.",
    )
    for s in scenarios:
        lf.create_dataset_item(
            dataset_name=DATASET_NAME,
            id=f"{DATASET_NAME}-{s['id']}",
            input=s["input"],
            expected_output=s["expected_output"],
            metadata={**s.get("metadata", {}), "id": s["id"]},
        )
    print(f"uploaded {len(scenarios)} items to Langfuse dataset '{DATASET_NAME}'")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--channel", choices=["whatsapp", "voice"], help="run one channel only")
    ap.add_argument("--name", default="clinic-agent", help="experiment name")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--keep", action="store_true", help="skip cleanup of synthetic data")
    ap.add_argument(
        "--cleanup", action="store_true", help="only delete leftover eval data (1555… phones)"
    )
    ap.add_argument("--upload", action="store_true", help="upsert scenarios as a Langfuse dataset")
    args = ap.parse_args()
    # `kill -USR1 <pid>` prints every thread's stack if a run ever seems stuck.
    faulthandler.register(signal.SIGUSR1)
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.cleanup:
        cleanup()
        return 0

    scenarios = json.loads(SCENARIOS.read_text())
    if args.channel:
        scenarios = [s for s in scenarios if s["input"].get("channel", "whatsapp") == args.channel]
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted]
        if missing := wanted - {s["id"] for s in scenarios}:
            print(f"unknown scenario ids: {sorted(missing)}", file=sys.stderr)
            return 2

    observability.init_tracing()
    lf = observability._client
    if lf is None:
        print("Langfuse is not configured (LANGFUSE_ENABLED + keys).", file=sys.stderr)
        return 2
    if args.upload:
        upload_dataset(lf, scenarios)

    get_db().client.admin.command("ping")
    nonce = f"{random.randint(0, 9999):04d}"
    index = {s["id"]: i for i, s in enumerate(scenarios)}
    data = [
        {
            "input": s["input"],
            "expected_output": s["expected_output"],
            "metadata": {**s.get("metadata", {}), "id": s["id"]},
        }
        for s in scenarios
    ]
    progress(
        f"running {len(data)} scenarios · agent {settings.MODEL} · judge {JUDGE_MODEL} · "
        f"db {settings.MONGO_DB_NAME}/{settings.CLINIC_ID} · phones {PHONE_PREFIX}{nonce}xxx"
    )
    try:
        result = lf.run_experiment(
            name=args.name,
            description="Scripted WhatsApp scenarios against the live clinic data",
            data=data,
            task=make_task(nonce, index),
            evaluators=[tools_eval, db_outcome_eval, no_crash_eval, judge_eval],
            run_evaluators=[pass_rates],
            max_concurrency=args.concurrency,
            metadata={"agent_model": settings.MODEL, "judge_model": JUDGE_MODEL},
        )
    finally:
        if args.keep:
            print(f"--keep: synthetic data left in place (phones {sorted(_phones)})")
        else:
            cleanup()
        observability.shutdown_tracing()

    print(result.format(include_item_results=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

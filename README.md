---
title: Medi Desk AI
emoji: 🩺
colorFrom: blue
colorTo: green
sdk: docker
app_port: 3000
pinned: false
---

# Medi Desk AI: agent

**Medi Desk AI** is a **WhatsApp and browser voice-call AI receptionist for medical clinics.** This repo is the agent; the staff console lives in `receptionist-agent-platform`. Patients
message the clinic's WhatsApp number, or open the clinic's call page in a browser,
enter their phone number, click "Call the clinic", and talk in natural language. A
Google ADK + Gemini agent answers clinic questions, checks doctor availability, and
books, reschedules, lists, or cancels appointments stored in MongoDB. Both channels
share one "brain": the same tools, data, and core prompt rules. WhatsApp runs turn
by turn on a text model; voice calls stream PCM audio over a WebSocket and are
bridged to a **Gemini Live** native-audio session. Every conversation is also recorded in a
console-ready shape (transcript, tool calls, outcome, escalation, plus an
LLM-generated summary / sentiment / language) for a staff analytics dashboard.

This is **v2** (Python, package version `2.0.0`). The original Node.js prototype
lives in [`v1/`](v1/) **for reference only**: it is not built, linted, tested, or
shipped. Only its WhatsApp transport and data model were carried forward; its
hardcoded rule-engine/state machine was replaced by the LLM agent.

> **Quick orientation:** the heart of the system is
> [`runtime/turn.py`](src/clinic_agent/runtime/turn.py) (one message in → one reply
> out), [`voice/bridge.py`](src/clinic_agent/voice/bridge.py) (one voice call:
> browser audio ⇄ Gemini Live),
> [`tools/booking_tools.py`](src/clinic_agent/tools/booking_tools.py) +
> [`tools/repo.py`](src/clinic_agent/tools/repo.py) (what the agent can do), and
> [`prompt.py`](src/clinic_agent/prompt.py) (how it behaves).

---

## Table of contents

- [Tech stack](#tech-stack)
- [Architecture](#architecture)
- [Project structure](#project-structure)
- [Module reference](#module-reference)
- [Agent tools](#agent-tools)
- [Agent behaviour (system prompt)](#agent-behaviour-system-prompt)
- [Data model (MongoDB)](#data-model-mongodb)
- [Configuration](#configuration)
- [Getting started](#getting-started)
- [Testing](#testing)
- [Deployment](#deployment)
- [Conventions & invariants](#conventions--invariants)
- [Known limitations](#known-limitations)
- [Common changes: where to edit](#common-changes-where-to-edit)

---

## Tech stack

| Concern | Choice |
|---|---|
| Language / tooling | Python **3.14** (pinned `>=3.14,<3.15`, `.python-version`), [uv](https://docs.astral.sh/uv/), hatchling build |
| Web server | FastAPI + uvicorn |
| Agent framework | Google ADK (`google-adk`): one `LlmAgent`, `InMemoryRunner` |
| LLM | Gemini via a Google AI Studio API key (`google-genai`), default model `gemini-3.5-flash-lite`; voice uses the Live API model `gemini-3.8-live` through ADK `Runner.run_live` |
| Database | MongoDB via **synchronous** `pymongo` (wrapped in `asyncio.to_thread`), TLS CA from `certifi` |
| Messaging | Meta WhatsApp Cloud API (Graph API) via `httpx` |
| Voice transport | Browser page ⇄ WebSocket (base64 PCM16 in JSON frames), bridged to Gemini Live. The page captures 16 kHz with an AudioWorklet and plays the model's native output rate, so the server never resamples |
| Config | `pydantic-settings` + `python-dotenv` |
| Dev | `pytest` + `pytest-asyncio` (auto mode) + `mongomock`, `ruff` |

No GCP project is required: Gemini runs via an API key and storage is MongoDB (Atlas free tier works).

---

## Architecture

```
WhatsApp Cloud API
   │  GET /webhook (verify handshake)    POST /webhook (inbound message)
   ▼
┌───────────────────────────────────────────────────────────────────┐
│ FastAPI app (app.py)                                              │
│  whatsapp/webhook.py ── parse sender+text, ack 200 immediately     │
│        │                                                          │
│        ▼  (DEBOUNCE_ENABLED=true)                                 │
│  runtime/debounce.py ── buffer bubbles in `pending_messages`       │
│        │                                                          │
│  runtime/flusher.py  ── background loop: claim due buffers,        │
│        │                join bubbles → ONE turn                   │
│        ▼                                                          │
│  runtime/turn.py     ── load history → contextualize → run ADK     │
│        │                agent → capture tool calls → persist       │
│        ├─► runtime/agent.py (LlmAgent = prompt + model + tools)    │
│        │       └─► tools/booking_tools.py → tools/repo.py → Mongo  │
│        ├─► runtime/history.py       (`transcripts`)               │
│        └─► runtime/conversations.py (`conversations`)             │
│        ▼                                                          │
│  whatsapp/client.py  ── send reply via Graph API                   │
│                                                                   │
│  runtime/sweeper.py  ── background loop: close idle conversations  │
│                         → runtime/analytics.py (Gemini JSON call)  │
│                                                                   │
│  voice/routes.py     ── GET /voice/call (page) · WS /voice/ws      │
│        ▼                (token check)                             │
│  voice/bridge.py     ── one call: page PCM ⇄ ADK run_live          │
│        │                (agent "voice" = same tools, voice prompt) │
│        ├─► voice/protocol.py + voice/audio.py (frames, chunking)  │
│        └─► on hangup: history (`transcripts`) +                   │
│                       conversations.finish_call (`conversations`) │
└───────────────────────────────────────────────────────────────────┘
   ▲                                   │
   │ ws(s) /voice/ws (PCM16, JSON)     ▼  MongoDB: clinic · doctors · patients ·
Browser call page                         appointments · transcripts ·
(voice/static/call.html)                  conversations · pending_messages
                                               │
                         Gemini Live API ◄─────┘ (bridge ⇄ live session)
```

### One inbound message, end to end

1. **Webhook:** Meta calls `POST /webhook`. `_extract_message()` takes
   `entry[0].changes[0].value.messages[0]`. Status callbacks (delivery/read
   receipts) have no `messages` and are just acked. The sender (`from`) and
   `text.body` are extracted and the request returns `200` immediately.
2. **Debounce** (default on): `debounce.enqueue(phone, text)` upserts one
   `pending_messages` doc per phone, pushes the text, and resets `deadline` to
   now + `DEBOUNCE_WINDOW_SECONDS` (5s). `firstAt` is set once, on insert.
   - If `DEBOUNCE_ENABLED=false`, the webhook instead schedules `run_turn` +
     `send_text_message` as a FastAPI background task, one turn per message.
3. **Flusher:** every `DEBOUNCE_TICK_SECONDS` (1s) it calls `claim_due()`, which
   atomically `find_one_and_delete`s each buffer whose `deadline` has passed **or**
   whose `firstAt` is older than `DEBOUNCE_MAX_WAIT_SECONDS` (30s). The messages are
   joined with `\n` into one turn. If the turn or the send fails, the batch is
   `requeue`d at the front of the buffer.
4. **Turn** (`run_turn(phone, message)`):
   - Loads up to the last 20 messages from `transcripts` (`MAX_HISTORY_MESSAGES`).
   - `contextualize()` builds the prompt: a `## Context` header (today's date in
     server local time, the patient's WhatsApp number), then a
     `## Prior conversation` block that includes **compact results of earlier tool
     calls**, then the new patient message.
   - Creates a **fresh ADK session (random UUID) every turn**, so ADK session state
     is never relied on. The `InMemoryRunner` is built once at import time and reused.
   - Streams events, pairs `function_call`/`function_response` by id to record each
     tool call's `name`, `args`, `result`, `status` (`error` if the result has an
     `error` key or `success: false`), and `duration_ms`. The last non-empty text
     event is the reply.
   - On exception it records the turn with `FALLBACK_REPLY` and returns that.
5. **Persist + reply:** the turn is appended to `transcripts` (prompt history) and
   `conversations` (console record), then the flusher sends the reply via
   `send_text_message`.
6. **Sweeper:** every `SWEEP_SECONDS` (60s) it closes `active` conversations idle
   longer than `IDLE_TIMEOUT_MINUTES` (30) and runs analytics on up to 10 ended
   conversations per tick.

**The agent itself is stateless.** All memory between turns lives in MongoDB and is
re-seeded into the prompt each turn.

### One voice call, end to end

The page and the server speak a small JSON protocol over `WS /voice/ws` (defined in
[`voice/protocol.py`](src/clinic_agent/voice/protocol.py)). Audio is base64 PCM16 LE
mono in `media` frames.

| Direction | Frame |
|---|---|
| page → server | `{"event":"start","call_id","caller","transcripts":true}` · `{"event":"media","payload"}` (16 kHz, ~100 ms) · `{"event":"stop"}` |
| server → page | `{"event":"ready","input_rate":16000,"output_rate":24000}` · `{"event":"media","payload"}` (at `output_rate`) · `{"event":"clear"}` · `{"event":"transcript","role","text","final"}` · `{"event":"end","reason"}` |

1. **Open the page:** the patient opens `/voice/call?token=…`, types their number,
   and clicks **Call the clinic**. Inside that click the page checks the number,
   opens the microphone (echo cancellation, noise suppression, auto gain), creates
   the AudioContext, and connects to `ws(s)://<host>/voice/ws?token=…`. If
   `VOICE_WS_TOKEN` is set and `token` doesn't match, the socket is rejected (403).
2. **`start`:** the page sends `start` with a random `call_id`, the number as typed,
   and `transcripts: true`. The bridge validates the number (`phone.is_valid_phone`).
   If it is empty or invalid, the server sends `end` (`invalid_caller`) and closes,
   and no live session or conversation is created. Otherwise it normalises the number
   (`phone.normalize_phone`: `+91 98765 43210` / `09876543210` / `9876543210` →
   `919876543210`) and opens an `active` `conversations` doc with `channel: "voice"`
   and `callId`. A socket that sends no `start` within 10 s gets `end` (`no_start`).
3. **Seed:** the bridge builds the same context as a WhatsApp turn
   (`contextualize(..., channel="voice")`: today's date, the caller's number, prior
   `transcripts` history including tool results), adds a "greet the caller" cue, and
   sends it into a **fresh** ADK live session (random session id) on the voice runner.
   The model speaks first.
4. **`ready`:** the first live event proves the session is up. The bridge sends
   `ready` with `input_rate` 16000 and the model's `output_rate`, read from the first
   audio blob's MIME type (`audio/pcm;rate=24000`). The page only starts sending mic
   audio after `ready`.
5. **Stream** (two tasks):
   - *upstream:* page `media` (16 kHz, already the model's input rate) → base64 decode
     → `LiveRequestQueue.send_realtime(audio/pcm;rate=16000)`. `stop` or a disconnect
     ends the call.
   - *downstream:* live events. Model audio is passed through at its native rate,
     re-chunked into ~100 ms `media` frames, and the remainder is flushed on
     `turn_complete`. On `interrupted` (barge-in) the bridge drops its buffer and sends
     `clear`, and the page drops all queued playback at once. Transcriptions become
     `transcript` frames if requested: partials carry the line so far and are updated
     in place, and `final: true` closes the line. A failed transcript send is logged
     and never affects audio. Finished transcriptions become user/assistant turns, and
     tool calls are captured by the same `ToolCallRecorder` as `run_turn`.
   ADK runs the tools itself (same `ALL_TOOLS`), and reconnects on the Live API's
   `goAway` using session resumption.
6. **Hang up:** if the page hangs up (`stop` or a closed socket) while a tool call is
   in flight, the bridge waits up to 5 s for it. If the live session ends or fails
   first, the page gets `end` (`session_ended` or `error`). Either way the bridge
   closes the live queue and the socket, then persists the call: its turns are
   appended to `transcripts` (so the next chat or call remembers it), and the
   conversation is finalized with `conversations.finish_call` (messages, derived
   fields, `durationSec`, final status). That makes it eligible for analytics right
   away.

---

## Project structure

```
src/clinic_agent/
  app.py                 FastAPI app, health routes, lifespan (starts sweeper + flusher), main()
  config.py              Settings singleton (env / .env), with v1-name aliases
  db.py                  Cached MongoClient + create_index_safe()
  phone.py               normalize_phone() / is_valid_phone() (typed numbers → digits) + to_e164()
  prompt.py              BASE_PROMPT + WHATSAPP_STYLE / VOICE_STYLE → SYSTEM_PROMPT / VOICE_SYSTEM_PROMPT
  whatsapp/
    webhook.py           GET verify · POST inbound (debounce or direct background reply)
    client.py            send_text_message() via the Graph API (only Graph caller)
  runtime/
    agent.py             build_agent(channel): the LlmAgent for "whatsapp" or "voice"
    turn.py              run_turn(), contextualize()
    tool_capture.py      ToolCallRecorder: pairs function calls/responses (text + voice)
    model_factory.py     get_model(): sets GOOGLE_* env for google-genai, returns MODEL
    history.py           `transcripts` collection (prompt history)
    conversations.py     `conversations` collection (console record, sessionised)
    analytics.py         summary/sentiment/language via structured Gemini output
    sweeper.py           idle finalize + analytics background loop
    debounce.py          Mongo-backed inbound buffer (`pending_messages`)
    flusher.py           debounce flusher background loop
  tools/
    booking_tools.py     the 9 async ADK tools (ALL_TOOLS)
    repo.py              sync data access: clinic/doctors/patients/appointments + indexes
    slots.py             slot generation from working hours
  voice/
    routes.py            GET /voice/call (page) + WS /voice/ws (token check), mounted if VOICE_ENABLED
    bridge.py            run_call(): one call, page ⇄ ADK live session, persistence on hangup
    protocol.py          call-page protocol: parse start/media, build ready/media/clear/transcript/end
    audio.py             base64, MIME rate parsing, ~100 ms outbound chunker
    static/call.html     the browser call page (vanilla JS, AudioWorklets, no build step)
scripts/
  seed.py                idempotent: 1 clinic + 2 doctors + all indexes
  seed_hospital.py       insert-only: hospital-scale data (about, 14 more doctors, Staff, Services & charges, Subsidies & schemes)
  smoke_live.py          live end-to-end check (real Mongo + Gemini, no WhatsApp send)
  voice_sim.py           local call-page simulator: streams WAV / `say` audio, saves replies to out.wav
tests/                   unit tests (pytest + mongomock, no live services)
Dockerfile               uv + Python 3.14 image
PLAN.md                  original design plan & decisions (partly historical, see note below)
v1/                      original Node.js/Express/Mongoose prototype (reference only)
```

> **About PLAN.md:** it is the design document and also the `readme` declared in
> `pyproject.toml` (the Docker build copies it for that reason). Some sections are
> historical: it mentions an `agent_sessions` collection / `MongoSessionService`
> and lists 7 tools. The **code is authoritative**. There is no `agent_sessions`
> collection, and there are 9 tools.

---

## Module reference

### `app.py`
- `create_app()` sets up logging and exposes `GET /` (`{"status": "Medi Desk AI running"}`),
  `GET /healthz` (`{"status": "ok"}`), and mounts the webhook router at `/webhook`.
- `lifespan` starts `sweeper.run_forever()` and `flusher.run_forever()` as asyncio
  tasks and cancels them on shutdown.
- When `VOICE_ENABLED` is true, it also mounts the voice router: the call page
  (`GET /voice/call`) and its WebSocket (`WS /voice/ws`).
- `main()` is the `medi-desk-ai` console script (`clinic-agent` still works as an alias). It runs uvicorn on `0.0.0.0:$PORT`.

### `phone.py`
- `normalize_phone(raw, default_cc="91")` turns a number typed on the call page into
  digits with the country code and no `+`, the form `patients.phone` and `transcripts`
  use. It handles `+91…`, a trunk `0` plus 10 digits, bare 10-digit national numbers
  (India assumed), and a `00` prefix. Digits that already carry a country code pass
  through unchanged. Only the voice path calls it; WhatsApp `from` is already canonical.
- `is_valid_phone(raw)` accepts only phone punctuation (digits, spaces, `+ - ( ) .`,
  with `+` only at the start). After normalising, the number must be 8–15 digits with
  a non-zero country code, and an Indian (`91`) number must have exactly 10 national
  digits. The bridge rejects a call whose `caller` fails this check.
- `to_e164(phone)` adds the `+` used by `conversations.phone`, without re-interpreting
  the number.

### `config.py`
A `Settings` (pydantic-settings) singleton, `settings`, imported everywhere. Env
names are case-sensitive; unknown keys are ignored. Aliases: `WHATSAPP_TOKEN` →
`ACCESS_TOKEN`, `MONGODB_URI` → `MONGO_URI` (v1 compatibility). See
[Configuration](#configuration).

### `db.py`
- `get_db()` is an `lru_cache`d `MongoClient(MONGO_URI, tlsCAFile=certifi.where())[MONGO_DB_NAME]`.
  certifi is needed because uv's standalone Python has no system CA bundle.
- `create_index_safe()` creates an index but tolerates "already exists with different
  options/name" errors (codes 85/86).

### `runtime/agent.py` and `runtime/model_factory.py`
`build_agent(channel="whatsapp")` returns `LlmAgent(name="clinic_receptionist",
model=get_model(), instruction=SYSTEM_PROMPT, tools=ALL_TOOLS)`. With
`channel="voice"` it uses `get_voice_model()` (`settings.VOICE_MODEL`) and
`VOICE_SYSTEM_PROMPT`, with the same `ALL_TOOLS`. Both model getters set
`GOOGLE_GENAI_USE_VERTEXAI` / `GOOGLE_API_KEY` env vars (via `setdefault`). There is
no multi-agent tree, handoff, or Vertex fallback.

### `runtime/turn.py` and `runtime/tool_capture.py`
`run_turn(phone, message) -> str` (see the flow above). `contextualize()` is pure and
unit-tested. Its `channel="voice"` variant changes only the identity line ("phoning the
clinic from number") and drops the `Patient message:` label; the WhatsApp output is
unchanged. `tool_capture.ToolCallRecorder` pairs `function_call` / `function_response`
events by id into `{name, args, result, status, duration_ms}`; both harnesses use it.
`looks_like_error()` defines the tool error convention (`turn._looks_like_error` is
kept as an alias).

### `runtime/history.py` (`transcripts`)
One doc per phone: `{phone, messages: [{role: "user"|"assistant", content, ts, tools?}], createdAt, updatedAt}`.
Assistant messages carry `tools: [{name, result}]` so later turns can recall ids and slots.
Used **only** for prompt seeding; the console does not read it.

### `runtime/conversations.py` (`conversations`)
One doc **per conversation** (not per phone), following a data contract shared with
the staff console. Each doc has a `channel`: `whatsapp` or `voice`.

- **WhatsApp:** `record_turn()` appends a turn to the phone's `active` WhatsApp
  conversation, or starts a new one if none exists or the last message is more than 30
  minutes old. When it rotates, it finalizes the stale conversation first. It never
  picks up an active `voice` conversation.
- **Voice:** each call is exactly **one** conversation, with no rotation.
  `start_call()` inserts it as `active` (with `callId`, the page's `call_id`) once the
  caller's number has been accepted.
  `finish_call()` writes all turns at hangup, sets `durationSec`, derived fields, and
  the final status, and clears any `analyticsStatus`, so analytics runs on the full
  call. The opening greeting turn has no patient message.

Derived fields (`_derived_fields`) are recomputed from **all** tool calls in the
conversation, with no extra LLM call, using the same logic for both channels:

| Field | Derivation |
|---|---|
| `outcome` | `appointment_rescheduled` (reschedule succeeded, or book + cancel) › `appointment_booked` › `appointment_cancelled` › `escalated` › `info_provided` (clinic info/doctors/my-appointments) › `no_resolution` |
| `intents` / `primaryIntent` | Mapped from tool names in order of first use: `book_appointment`, `cancel_appointment`, `reschedule_appointment`, `doctor_availability` (check/find), `clinic_info` (clinic info / doctors), `other` |
| `doctorName` | `doctor_name` arg of the last `book_appointment`/`check_availability` call |
| `appointmentAt` | `date`+`slot` of the last successful booking as a **naive local** ISO datetime |
| `escalated` / `escalationReason` | Successful `escalate_to_human` call and its `reason` arg |
| `status` | `active` while chatting (even after escalation). When closed: `escalated`, `abandoned` (outcome `no_resolution`), or `completed` |

It also holds the sweeper helpers: `close_idle_active()`, `claim_analytics_batch()`
(atomically flips `analyticsStatus` to `processing` and reclaims docs stuck past
`ANALYTICS_STUCK_MINUTES`), and `apply_analytics()` (sets `complete`, or `failed`).

### `runtime/analytics.py`
`generate_analytics(transcript)` makes one `generate_content` call with
`response_schema=ConversationAnalytics` (`summary: str`,
`sentiment: positive|neutral|negative`, `language: en|hi`), temperature 0.2, and
minimal thinking. Gemini ≥3 uses `thinking_level="LOW"`; 2.x uses `thinking_budget=0`.
Passing the wrong knob returns a 400. It returns `None` on empty input or failure.

### `runtime/debounce.py` and `runtime/flusher.py`
See steps 2–3 above. `pending_messages` has unique `phone` and `deadline` indexes.
Timestamps are stored as ISO strings.

### `runtime/sweeper.py`
`run_once()` closes idle conversations, then (if `ANALYTICS_ENABLED`) claims a batch
of 10 and applies analytics. `run_forever()` survives tick failures.

### `runtime/outbox.py`, `runtime/followups.py`, `integrations/google_calendar.py`
**Appointment follow-ups.** Every successful book / reschedule / cancel queues jobs
in the `outbox` collection; a worker started in the app lifespan delivers them, so a
slow or failing Google/Meta call never blocks the conversation.
- `calendar.sync` makes the doctor's Google Calendar match the appointment: create,
  move, or delete the event. Event ids are deterministic per appointment, so retries
  are idempotent. Doctors without a `calendarEmail` are skipped.
- `whatsapp.notify` sends the patient a fixed-text confirmation (or a Meta template,
  if configured) built from the **stored** appointment, never from model output. It
  is skipped if the booking was cancelled before it went out.
- Confirmations are **held** until the agent's WhatsApp reply has been sent, or until
  the voice call ends (`outbox.held(token)` around the turn/call), then released.
  Holds nobody released (a crash) are released after 60 min.
- Several unsent changes to one appointment collapse into one accurate message:
  booked + rescheduled sends a single "confirmed" with the new time; booked +
  cancelled sends nothing.
- Retries: transient errors back off 30 s → 8 min, up to 6 attempts. Permanent errors
  (calendar not shared, WhatsApp rejection) fail at once with a readable `lastError`.
- Outcomes are written onto the appointment (`calendarSync`, `confirmation`) for the
  console, and each job is a Langfuse trace (`sync-doctor-calendar`,
  `send-appointment-confirmation`, `send-staff-reply`).
- `staff.reply` (not tied to an appointment) is a staff member's WhatsApp reply,
  inserted by the console; see below.

### `runtime/escalation.py` (human handoff)
The contract is `docs/staff-handoff-contract.md` in the console repo
(`receptionist-agent-platform`). No alerts go to staff phones; staff work the
console's **Escalations** page.
- `escalate_to_human` inserts one `escalations` doc (`status: "open"`, patient name
  and E.164 phone, channel, reason, `conversationId`). At most one unresolved
  escalation per conversation: asking again returns it with `already_requested: true`.
- **WhatsApp:** the turn that escalates sets the conversation's
  `handledBy: "staff"` (plus `escalationId`, `escalatedAt`). From then on the AI
  doesn't reply. `capture_for_staff` (called by the flusher and the non-debounced
  webhook path) appends the patient's messages to the conversation and to prompt
  history instead. A staff-handled chat is never closed or rotated for being idle.
- Staff reply from the console, which pushes a `role: "staff"` message
  (`delivery.status: "pending"`) and a `staff.reply` outbox job. `send_staff_reply`
  sends it to the conversation's phone and sets `delivery` to `sent` (with
  `waMessageId`) or `failed` (WhatsApp 4xx such as a closed 24 h window fails at
  once; other errors retry, and the message is marked failed when retries run out).
  The reply is also added to prompt history as `Clinic staff: …`, so the AI knows
  what was said when the chat comes back.
- Resolving the escalation in the console sets `handledBy: "agent"`, and the AI
  answers the next message.
- **Voice:** the escalation record carries the number to call back; the call's
  conversation gets `escalationId` / `escalatedAt` on hangup.

### `tools/repo.py`
Synchronous Mongo data access, ported from v1's Mongoose services. Key behaviours:
- **Every query is scoped to `CLINIC_ID`** and every insert stamps it: several clinics
  can share one database (the console writes their data, see below).
- Doctors are matched by **case-insensitive exact name** among `isActive: true` doctors,
  with a leading "Dr."/"Doctor" stripped and the name regex-escaped.
- `get_clinic()` / `list_active_doctors()` add a `details` object with the clinic's
  configured, agent-visible extra fields (via `tools/catalog.py`).
- `get_available_slots()` rejects past dates and days not in `availableDays`, tiles
  `workingHours` by `consultationDuration`, and removes slots booked with an active status.
- `get_upcoming_availability()` returns the next 3 dates (starting tomorrow, up to 60
  days ahead) with free slots, at most 6 slots each.
- `create_appointment()` re-validates the slot, upserts the patient by phone, and
  inserts with status `Pending`. A `DuplicateKeyError` returns `{success: false, conflict: true}`.
- `reschedule_appointment()` keeps the doctor and status and only changes date/slot.
- `cancel_appointment()` sets status `Cancelled` + `cancelledAt`.
- `ensure_indexes()` creates the double-booking guard, the unique `(clinicId, phone)`
  patient index and the clinic/doctor/catalog indexes.

### `tools/catalog.py`
The agent (reader) side of the console's **clinic data contract**
([`receptionist-agent-platform/docs/clinic-data-contract.md`](../receptionist-agent-platform/docs/clinic-data-contract.md)).
Each clinic defines its own pages and fields in the console (`entity_definitions`):
extra fields on the system pages (`clinic`, `doctor`, `patient`, stored in each
document's `attributes`) and custom pages such as lab tests or services (stored in
`records`).
- Definitions are cached for 60 s, so console edits reach patients within a minute.
- The agent sees a field only if it is `agentVisible`, not `pii` and not `archived`.
  Values are rendered as `{label: value}`; a value that doesn't match its field type
  is dropped and logged, never allowed to fail a turn. Patient attributes are never read.
- `search_catalog()` text-searches one `agentEnabled` custom page (active rows, max 10).
- `prompt_section()` lists those catalogs for the system prompt.

### `tools/slots.py`
`generate_slots({"start":"09:00","end":"13:00"}, 30)` returns `["09:00","09:30",…,"12:30"]`.
A slot only counts if it fits fully before `end`. `available_slots(all, booked)` filters
out the booked ones.

### `whatsapp/client.py`
`send_text_message(to, body)` POSTs to
`https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages` with a
Bearer token and a 15s timeout, and raises on HTTP ≥400. Only plain text is sent.

### `voice/routes.py`
`GET /voice/call` returns the call page (`static/call.html`, read once at import).
`@router.websocket("/voice/ws")` rejects the socket (close 1008, which is a 403 before
accept) when `VOICE_WS_TOKEN` is set and `?token=` doesn't match (constant-time
compare), then accepts and hands off to `bridge.run_call`.

### `voice/bridge.py`
`run_call(ws, runner=, run_config=)` handles one call and **never raises** (see "One
voice call" above). It waits up to `START_TIMEOUT_SECONDS` (10) for `start` and
rejects invalid callers before building anything. `CallBridge` holds the per-call
state: live queue, recorder, transcript, partial transcription text, and the
chunker, which is created at `ready` for the model's output rate. `CallTranscript`
groups finished transcriptions and tool calls into turns: caller speech after the
agent has spoken starts a new turn. `get_runner()` lazily builds the module-level
`InMemoryRunner(build_agent("voice"))`. `build_run_config()` sets BIDI streaming,
`AUDIO` response modality, input and output transcription, an optional prebuilt voice
(`VOICE_NAME`), sliding-window context compression (to get past the ~15 min
audio-session cap), and session resumption (so ADK reconnects on `goAway`).

### `voice/protocol.py`
`parse_message` decodes a frame. `parse_start` → `CallStart{call_id, caller,
transcripts}`: a missing or malformed `call_id` (anything but 1–64 of
`[A-Za-z0-9_-]`) is replaced by a server-generated one, a non-string `caller` becomes
`""`, and `transcripts` is on only when it is literally `true`. `media_payload` reads
a `media` frame. Builders: `ready_event`, `media_event`, `clear_event`,
`transcript_event`, `end_event`, plus the `END_*` reason constants (`invalid_caller`,
`no_start`, `session_ended`, `error`).

### `voice/audio.py`
`b64decode`/`b64encode`, `rate_from_mime`, `whole_samples` (drops a trailing odd
byte), `chunk_size_for(rate)` (~100 ms of whole samples), and `OutboundChunker`. The
chunker emits fixed-size chunks, holds the remainder, flushes it as-is on turn end,
and `clear()`s on barge-in. There is no resampling: the page sends 16 kHz, which is
the model's input rate, and plays the model's output rate itself.

### `voice/static/call.html`
One self-contained page (vanilla JS, no external scripts, light/dark, phone-sized).
It reads `token` from its own URL and derives `ws:`/`wss:` from `location.protocol`.
To host the page elsewhere (e.g. Vercel), set `DEFAULT_SERVER` in the script or open it
with `?server=<backend host>&token=…`; it then connects to `wss://<backend>/voice/ws`.
Two inline AudioWorklets (loaded from a Blob URL) handle the audio:
- `capture` downmixes the mic to mono, downsamples from the context rate to
  `input_rate` with a box filter, and posts 100 ms PCM16 frames (3,200 bytes).
- `player` keeps a queue of chunks at `output_rate` and plays them gaplessly at the
  context rate with linear interpolation. `clear` empties the queue at once.

The transcript panel updates partial lines in place. Hanging up sends `stop`, closes
the socket, stops the mic tracks, and closes the AudioContext.

---

## Agent tools

Defined in [`tools/booking_tools.py`](src/clinic_agent/tools/booking_tools.py) as
async functions registered in `ALL_TOOLS`. **ADK builds each tool's schema from the
function signature and docstring**, so the docstrings are part of the model's
interface. Each tool runs the corresponding sync `repo` function via `asyncio.to_thread`.

| Tool | Args | Returns (shape) |
|---|---|---|
| `get_clinic_info` | – | `{clinicName, timings, address, phone, details?}` |
| `list_doctors` | – | `{doctors: [{id, name, specialization, details?}]}` (`details` = the clinic's agent-visible extra fields, e.g. degrees, experience) |
| `check_availability` | `doctor_name, date` | `{doctor, date, weekday, slots[], note?}` or `{error}` |
| `find_next_available` | `doctor_name` | `{doctor, dates: [{date, weekday, slots[]}]}` or `{error}` |
| `book_appointment` | `patient_name, age, gender, doctor_name, date, slot, phone` | `{success: true, appointmentId, doctor, date, weekday, slot, status}` or `{success: false, error, conflict?, available_slots?}` |
| `list_my_appointments` | `phone` | `{appointments: [{appointmentId, doctor, date, slot, status}]}` |
| `reschedule_appointment` | `appointment_id, new_date, new_slot` | like `book_appointment` |
| `cancel_appointment` | `appointment_id` | `{success, appointmentId, status}` or `{success: false, error}` |
| `search_catalog` | `catalog, query?` | `{catalog, label, query, total_matches, results: [{name, <field label>: value…}]}` or `{error, available_catalogs}` |
| `escalate_to_human` | `reason` | `{escalated: true, message, escalation_id, already_requested}`: opens an escalation on the console; on WhatsApp, staff take over the chat |

Formats: `date` = `YYYY-MM-DD`, `slot` = `HH:mm`, `doctor_name` without the "Dr." prefix.
**Error convention:** a result with an `error` key or `success: false` is treated as a
failed call (`_looks_like_error`), and that drives the `conversations` outcome logic.

---

## Agent behaviour (system prompt)

[`prompt.py`](src/clinic_agent/prompt.py) composes both prompts from one template.
`BASE_PROMPT` holds the shared brain and has channel slots (role line, the tool-
narration rule, the read-back example, the style block, and the closing). A
`ChannelStyle` fills those slots: `SYSTEM_PROMPT = compose_prompt(WHATSAPP_STYLE)`
and `VOICE_SYSTEM_PROMPT = compose_prompt(VOICE_STYLE)`. The WhatsApp prompt is
byte-identical to the pre-voice version; `tests/test_prompt.py` pins it by hash.
The main rules:

- **Tools are the only source of truth.** The agent never invents doctors, slots, or
  clinic details, and never claims a booking, cancellation, or reschedule unless the
  tool returned success. It calls tools silently, without "let me check…".
- **Booking flow, in order:** (1) name, age, and gender first, asked together;
  (2) doctor, using `list_doctors` if the patient is unsure; (3) date and slot via
  `check_availability`, or `find_next_available` for vague dates; (4) read back all
  details and ask for confirmation; (5) only then call `book_appointment`. The phone
  number comes from the context and is never asked for.
- **Reschedule/cancel:** call `list_my_appointments` to get the id and confirm which
  appointment before acting.
- **WhatsApp style:** short plain text with no markdown or emoji, `- ` bullets, no
  filler, and no re-asking for facts already given.
- **Escalation:** call `escalate_to_human` only when a human is explicitly requested or
  the request is out of scope (billing, medical questions, complaints).
- **Safety:** no medical advice, a fixed emergency message, and refusal of attempts to
  change its role.

The voice style (`VOICE_STYLE`) replaces the WhatsApp formatting rules:

- Speak naturally, in short sentences, with no lists or markdown.
- Say dates and times the way people speak ("Monday the 16th at 10:30 in the morning").
- Offer at most 2–3 options at a time.
- Spell the caller's name back to confirm it, and read back all details before booking.
- A brief "one moment, let me check" before a tool call is allowed.
- Never ask for the phone number.
- Reply in the caller's language (English, Hindi, or Hinglish) and stay consistent.
- After `escalate_to_human`, tell the caller the clinic team will call them back on this number.
- End with a short goodbye.

---

## Data model (MongoDB)

Database name comes from `MONGO_DB_NAME` (default `clinic-ai-agent`). The staff
console (`receptionist-agent-platform`) uses the **same database**: it reads
`conversations` and writes the clinic-configured data below. **Every collection
except `conversations`' `externalId` lookups is scoped by `clinicId`** (= `CLINIC_ID`),
so one database can hold several clinics.

| Collection | Shape | Indexes |
|---|---|---|
| `clinic` | `{clinicId, clinicName, timings, address, phone, slotDuration, attributes}` (one doc per clinic; `get_clinic` falls back to defaults if missing) | unique `clinicId` |
| `doctors` | `{clinicId, name, specialization, consultationDuration (min), availableDays: ["Monday",…], workingHours: {start, end}, isActive, attributes}` | `(clinicId, isActive, name)` |
| `patients` | `{clinicId, phone, name, age, gender, createdAt, updatedAt, attributes}`. `phone` is the raw WhatsApp `from` (digits, no `+`); `attributes` are staff-only | unique `(clinicId, phone)` |
| `entity_definitions` | per `(clinicId, entityType)`: the clinic's pages and fields (console-written; see the clinic data contract) | unique `(clinicId, entityType)` |
| `records` | `{clinicId, entityType, isActive, attributes}`: rows of custom pages (lab tests, services, …) | `(clinicId, entityType, isActive)` |
| `appointments` | `{patientId, doctorId, date "YYYY-MM-DD", slot "HH:mm", status, createdAt, updatedAt, cancelledAt?}` | **unique partial `(doctorId, date, slot)`** where `status ∈ {Pending, Confirmed, Arrived, Completed}` |
| `transcripts` | per phone, see `history.py` | – |
| `conversations` | per conversation, see `conversations.py`. `channel` is `whatsapp` or `voice`; voice docs add `callId` and `durationSec`. Handoff fields: `handledBy` (`agent`/`staff`), `escalationId`, `escalatedAt`, `lastPatientMessageAt`; staff messages have `role: "staff"` and `delivery`. `phone` is E.164 (`+` prefixed), timestamps are ISO-8601 UTC strings | unique `externalId`; `(clinicId, startedAt)`, `(clinicId, outcome, startedAt)`, `(clinicId, escalated, startedAt)`, `(phone, startedAt)` |
| `escalations` | one per request for a human (see `runtime/escalation.py`); the agent inserts `open`, the console claims and resolves | `(clinicId, status, createdAt)`, `(clinicId, conversationId, status)` |
| `pending_messages` | `{clinicId, phone, messages[], deadline, firstAt}` (ISO strings) | unique `(clinicId, phone)`; `deadline` |

Appointment statuses: `Pending` (all agent bookings), `Confirmed`, `Arrived`,
`Completed`, `Cancelled`. Only `Cancelled` frees a slot.

Indexes are created at app startup (best effort: a failure is logged, not fatal) and
by the seed / migration scripts.

**Upgrading an existing database** to clinic scoping: run
`uv run python scripts/migrate_clinic_scope.py --dry-run`, then without `--dry-run`.
It backfills `clinicId` on old documents, creates the scoped indexes and drops the
legacy phone-only unique indexes. Until it runs, documents without a `clinicId` are
invisible to the agent.

### Setting up appointment follow-ups

**Google Calendar** (`GOOGLE_CALENDAR_ENABLED=true`):
1. In Google Cloud: create a project, enable the **Google Calendar API**, and create
   a **service account** with a JSON key. There's no need to grant it any IAM role.
2. Set `GOOGLE_SERVICE_ACCOUNT_FILE=/path/key.json` (or put the JSON in
   `GOOGLE_SERVICE_ACCOUNT_JSON`) and `CLINIC_TIMEZONE`.
3. Each doctor opens Google Calendar → *Settings and sharing* → *Share with specific
   people*, adds the service account's email (`…@….iam.gserviceaccount.com`) with
   **Make changes to events**, and staff enter that calendar's email as the doctor's
   *Google Calendar email* in the console.

**WhatsApp confirmations** (`WHATSAPP_CONFIRMATIONS_ENABLED=true`, the default):
WhatsApp chats work with plain text. To also reach **voice callers** (outside the 24-h
window), create these *Utility* templates in Meta Business Manager and set their
names in `WHATSAPP_TEMPLATE_*`:

| Setting | Suggested template body |
|---|---|
| `WHATSAPP_TEMPLATE_BOOKED` | `Hi {{1}}, your appointment with {{2}} on {{3}} at {{4}} is confirmed. Your booking reference is {{5}}. Reply to this message if you need to reschedule or cancel.` |
| `WHATSAPP_TEMPLATE_RESCHEDULED` | `Hi {{1}}, your appointment with {{2}} at {{4}} has been moved to {{3}}. Your booking reference is {{5}}. Reply to this message if you need to make another change.` |
| `WHATSAPP_TEMPLATE_CANCELLED` | `Hi {{1}}, your appointment with {{2}} on {{3}} at {{4}} has been cancelled. Reply to this message whenever you would like to book again.` |

Create them with **Number** variables (not Name), **no buttons**, and the language
whose code matches `WHATSAPP_TEMPLATE_LANGUAGE` (`en` = "English").

Check a doctor's calendar sharing without booking anything:
`uv run python scripts/check_calendar.py <calendar email>`.

---

## Configuration

Copy `.env.example` to `.env`. All settings live in
[`config.py`](src/clinic_agent/config.py).

| Variable | Default | Purpose |
|---|---|---|
| `VERIFY_TOKEN` | `""` | Must match the verify token set in the Meta webhook UI |
| `ACCESS_TOKEN` (alias `WHATSAPP_TOKEN`) | `""` | WhatsApp Cloud API token (use a permanent System-User token in prod) |
| `PHONE_NUMBER_ID` | `""` | From WhatsApp → API Setup |
| `GRAPH_API_VERSION` | `v21.0` | Graph API version |
| `GOOGLE_API_KEY` | `""` | Gemini API key (AI Studio) |
| `GOOGLE_GENAI_USE_VERTEXAI` | `false` | Keep `false` (API-key mode) |
| `MODEL` | `gemini-3.5-flash-lite` | Gemini model for both the agent and analytics |
| `MONGO_URI` (alias `MONGODB_URI`) | `mongodb://localhost:27017` | Mongo connection string |
| `MONGO_DB_NAME` | `clinic-ai-agent` | Database name |
| `CLINIC_ID` | `main-clinic` | Stamped on every `conversations` doc |
| `ANALYTICS_ENABLED` | `true` | Run summary/sentiment/language generation |
| `IDLE_TIMEOUT_MINUTES` | `30` | Sweeper closes conversations idle this long |
| `SWEEP_SECONDS` | `60` | Sweeper interval |
| `ANALYTICS_STUCK_MINUTES` | `10` | Reclaim analytics stuck in `processing` |
| `DEBOUNCE_ENABLED` | `true` | Coalesce rapid messages |
| `DEBOUNCE_WINDOW_SECONDS` | `5.0` | Quiet gap before flushing |
| `DEBOUNCE_MAX_WAIT_SECONDS` | `30.0` | Hard cap even while the user keeps typing |
| `DEBOUNCE_TICK_SECONDS` | `1.0` | Flusher poll interval |
| `VOICE_ENABLED` | `true` | Serve browser voice calls: the page at `/voice/call` and its WebSocket at `/voice/ws` |
| `VOICE_MODEL` | `gemini-3.8-live` | Gemini Live native-audio model for voice calls (must support function calling) |
| `VOICE_NAME` | `""` | Optional prebuilt voice (e.g. `Kore`, `Puck`); empty means the model default |
| `VOICE_WS_TOKEN` | `""` | Optional shared secret; if set, open the page as `/voice/call?token=<value>` (the page passes it to the WebSocket) |
| `LOG_LEVEL` | `INFO` | Logging level |
| `PORT` | `3000` | HTTP port |

---

## Getting started

### Prerequisites
- **uv** ≥ 0.11 and **Python 3.14** (`uv python install 3.14`)
- A **MongoDB** connection string (Atlas free M0 works)
- A **Gemini API key** from [Google AI Studio](https://aistudio.google.com/apikey)
- A **Meta WhatsApp** app (Access Token, Phone Number ID)

### Install, configure, seed, run

```bash
uv sync
cp .env.example .env                  # fill in the values above
uv run python scripts/seed.py         # idempotent: clinic + 2 doctors + indexes
uv run python scripts/seed_hospital.py  # optional, insert-only: realistic hospital data
uv run medi-desk-ai                   # serves on :$PORT (default 3000)
```

### Seed data
- `seed.py`: "ABC Clinic" (Mon–Sat 9–5); **Dr. Asha Mehta** (General Physician,
  Mon–Fri 09:00–13:00, 30 min) and **Dr. Rohit Sharma** (Dentist, Mon/Wed/Fri/Sat
  14:00–17:00, 20 min). It upserts by natural key and never touches patients or
  appointments. It also adds example clinic-configured data (doctor degrees /
  experience / languages, and a "Lab tests" catalog with 4 tests), inserted only if
  missing so console edits are never overwritten.
- `seed_hospital.py`: turns the clinic into a realistic 150-bed multi-speciality
  hospital. It fills the clinic's `about` and adds clinic fields (emergency, ambulance
  helpline, visiting hours, beds, specialities, facilities, insurance/TPAs, payment
  modes, follow-up policy). It adds a doctor page (degrees, experience, languages,
  fee, about, OPD room) and 14 more doctors. It also adds three custom pages: **Staff**
  (7 people, no phone numbers, alerts off), **Services & charges** (86 priced items
  across consultations, lab, imaging, cardiac, packages, wards/rooms, ICU, ambulance,
  emergency, surgery packages, dental, physiotherapy) and **Subsidies & schemes** (10).
  It only inserts what's missing, so console edits survive a re-run.

### Connect WhatsApp (local dev)

```bash
cloudflared tunnel --url http://localhost:3000      # or: ngrok http 3000
```

In the Meta dashboard → **WhatsApp → Configuration → Webhook**: set the callback URL
to `https://<tunnel>/webhook` and the verify token to your `VERIFY_TOKEN`, then
subscribe to the **`messages`** field. In dev mode, add your own number as a test
recipient under **API Setup**, then text the business number.

### Voice calls in the browser (local dev)

```bash
uv run medi-desk-ai
cloudflared tunnel --url http://localhost:3000      # or: ngrok http 3000
```

Open `https://<tunnel-host>/voice/call?token=<VOICE_WS_TOKEN>` (leave out `token` if
`VOICE_WS_TOKEN` is empty), enter a phone number, and click **Call the clinic**. On
the same machine, `http://localhost:3000/voice/call` works without HTTPS. Browsers
only allow the microphone on HTTPS or localhost. The agent greets you first. **Use
headphones**, so the agent doesn't hear itself through your speakers and interrupt
itself.

Browser support: desktop Chrome/Edge and Android Chrome are the targets. iOS Safari
(14.5+, which has AudioWorklet) should work but is best-effort. See the known
limitations.

### Test calls without a browser: `scripts/voice_sim.py`

The simulator behaves like the call page against a local server. It sends `start`
(fake caller, default `+919876500001`), waits for `ready`, streams caller audio as
real-time 100 ms 16 kHz `media` frames (silence between utterances), waits for each
reply to finish, prints the final `transcript` lines, writes everything the agent
said to `out.wav` at the `output_rate` from `ready`, and sends `stop`.

```bash
uv run python scripts/voice_sim.py                       # greeting only → out.wav
uv run python scripts/voice_sim.py --wav hello.wav       # 16-bit mono WAV(s), any rate
uv run python scripts/voice_sim.py \
  --say "Hi, I'd like to book an appointment with Doctor Asha Mehta" \
  --say "My name is Ravi Kumar, I'm 34, male" \
  --say "Next Monday, the earliest slot please" \
  --say "Yes, please book it"                            # macOS `say` synthesises each turn
```

Options: `--url` (default `ws://localhost:3000/voice/ws`), `--token`, `--caller`,
`--out`, `--reply-gap`, `--max-reply`, `--no-transcripts`. `--say` needs macOS
`say`; elsewhere, use `--wav`. Afterwards, check the `conversations` doc
(`channel: "voice"`, `callId` from the simulator's log) for the `book_appointment`
tool call.

---

## Testing

```bash
uv run pytest            # unit tests (mongomock, no live services needed)
uv run ruff check .      # lint (line length 100; v1/ excluded)
```

| Test file | Covers |
|---|---|
| `test_slots.py` | slot tiling and filtering |
| `test_repo.py` | availability, booking, double-booking, reschedule/cancel |
| `test_contextualize.py` | prompt seeding (date, phone, history, tool results) |
| `test_turn_capture.py` | tool error detection |
| `test_history.py` | `transcripts` append/read |
| `test_conversations.py` | sessionisation and derived fields |
| `test_sweeper.py` | idle close and analytics claim/apply |
| `test_debounce.py` | enqueue/claim/requeue windows and cap |
| `test_webhook.py` | verify handshake and inbound parsing |
| `test_phone.py` | phone normalisation, `is_valid_phone`, and E.164 |
| `test_prompt.py` | WhatsApp prompt pinned by hash; voice prompt composition; `build_agent(channel)` |
| `test_tool_capture.py` | function call/response pairing, status, duration |
| `test_voice_audio.py` | chunk sizes, remainder, flush, clear |
| `test_voice_protocol.py` | `start` parsing (incl. missing/invalid fields), every outbound builder |
| `test_voice_conversations.py` | one conversation per call (`callId`), derived fields, analytics eligibility, WhatsApp isolation |
| `test_voice_bridge.py` | full call against a fake live runner (`ready`, native-rate audio, barge-in `clear`, transcript frames only when requested, tool call, persistence), `end` on session end/error, invalid caller, start timeout |
| `test_voice_routes.py` | token accept/reject on `/voice/ws`, `/voice/call` served only when `VOICE_ENABLED`, no other voice routes |

Tests patch `get_db` with `mongomock`. pytest runs with `asyncio_mode = "auto"` and
`--import-mode=importlib`.

**Live smoke test** (real Mongo + Gemini; sends no WhatsApp message; cleans up after
itself; seed first):

```bash
uv run python scripts/smoke_live.py
```

It exercises debounce coalescing → a live agent turn (tools + conversation record) →
analytics.

---

## Deployment

A single container ([`Dockerfile`](Dockerfile), `ghcr.io/astral-sh/uv:python3.14-bookworm-slim`,
`uv sync --frozen --no-dev`, `CMD /app/.venv/bin/medi-desk-ai`):

```bash
docker build -t medi-desk-ai .
docker run --env-file .env -p 3000:3000 medi-desk-ai
```

Host it anywhere with HTTPS (Render, Railway, Fly.io) and point the Meta webhook at
`https://<host>/webhook`. Use an **always-on single instance**. See the limitations below.

### Hugging Face Spaces (free, no card)

The YAML block at the top of this README is the Space config (`sdk: docker`,
`app_port: 3000`); Hugging Face reads it, GitHub ignores it.

1. Create a Space: SDK **Docker**, hardware **CPU basic**, visibility **Public**
   (Meta's webhook can't authenticate to a private Space).
2. **Settings → Variables and secrets:** add every value from `.env.example` you use
   as a secret, plus `TZ=Asia/Kolkata` (the container clock is UTC otherwise).
   Use `GOOGLE_SERVICE_ACCOUNT_JSON`, not the file variant.
3. Push: `git remote add space https://huggingface.co/spaces/<user>/<space>` then
   `git push space main` (password = an HF access token with write scope).
4. Seed Atlas from your machine (`scripts/seed.py`), set the Meta webhook to
   `https://<user>-<space>.hf.space/webhook`, and open the call page at
   `https://<user>-<space>.hf.space/voice/call?token=<VOICE_WS_TOKEN>`.

Free Spaces sleep after ~48 h without traffic; open the URL before a demo to wake it.

---

## Conventions & invariants

Keep these true when changing the code:

- **Async boundary:** FastAPI handlers and tools are async, while pymongo and
  google-genai calls are sync. Always wrap them with `asyncio.to_thread`. Do not call
  `get_db()` directly inside async code.
- **Tool contract:** tools return plain JSON-serialisable dicts. Signal failure with
  `error` or `success: False`, because outcome derivation and `status` depend on it.
  Keep docstrings accurate; they are the tool schema the model sees.
- **New tools** must be added to `ALL_TOOLS`, to `_TOOL_INTENT` (and possibly
  `derive_outcome` / `_INFO_TOOLS`) in `conversations.py`, and to the tool list in
  `prompt.py`. Both channels get them automatically.
- **Prompt edits:** shared rules go in `BASE_PROMPT`, and channel wording goes in
  `WHATSAPP_STYLE` / `VOICE_STYLE`. Any change to the WhatsApp prompt means updating
  the hash in `tests/test_prompt.py` on purpose.
- **Dates and times** in clinic data are plain strings (`YYYY-MM-DD`, `HH:mm`) to avoid
  timezone drift. Runtime timestamps in `conversations` and `pending_messages` are
  ISO-8601 UTC strings, compared lexicographically.
- **Phones:** clinic data and `transcripts` use digits with the country code and no
  `+` (the raw WhatsApp `from`; voice callers go through `phone.normalize_phone`).
  `conversations.phone` is E.164 with `+` (`phone.to_e164`).
- **Stateless turns:** never depend on ADK session state across turns. Anything the
  agent must remember has to come through `transcripts` and `contextualize()`.
- **Background loops** must never die on a tick failure (catch, log, continue;
  re-raise only `CancelledError`).
- **Style:** `from __future__ import annotations`, ruff rules
  `E,F,W,I,B,UP,SIM,RUF,ASYNC`, and module docstrings explaining the design.
- **Don't edit `v1/`.** It is reference-only and excluded from lint and Docker.

---

## Known limitations

- **Single instance assumed:** the sweeper and flusher run in-process. A sleeping free
  tier pauses them, and multiple replicas would double-process work. Scaling out needs
  a shared worker/lock. A hard crash mid-flush can drop one debounced batch.
- **No webhook signature verification** (Meta `X-Hub-Signature-256`). Add it before
  public exposure.
- **Text only:** non-text messages (media, location, buttons) reach the agent as empty
  text. Delivery-status callbacks are ignored.
- **No ownership check on cancel/reschedule:** these tools act on any
  `appointment_id`. The agent obtains ids via `list_my_appointments(phone)`, but the
  repo does not verify that the appointment belongs to the caller.
- **Staff replies are free text only.** WhatsApp delivers them only within 24 h of
  the patient's last message; the console disables the reply box after that. A
  staff-handled chat stays silent until someone resolves its escalation (there is no
  automatic timeout).
- **Timezone:** "today", past-date checks, and `appointmentAt` use the **server's
  local time**, with no clinic timezone configured. Deploy with the clinic's TZ or add
  one.
- **Two idle timeouts:** conversation rotation in `record_turn` uses a hardcoded
  30-minute `IDLE_TIMEOUT` in `conversations.py`, while the sweeper uses
  `IDLE_TIMEOUT_MINUTES`. Keep them aligned.
- **Doctor names are regex-matched without escaping** (`^{name}$`, case-insensitive).
  Fine for plain names.
- **Indexes are created only by the seed scripts** (see the data model section).
- **Single clinic / single agent.** `CLINIC_ID` is stamped on conversations, but there
  is no multi-tenancy.
- **Voice: the caller's number is self-entered and unverified.** Anyone with the page
  link can type any number and then see and manage that number's appointments. A real
  deployment needs OTP (or another proof of ownership) before the call starts.
- **Voice: no call transfer.** `escalate_to_human` alerts staff and the agent says
  the team will call back; the call isn't transferred or ended by the server.
- **Voice: Live session limits.** Audio-only Live sessions are capped at about 15
  minutes unless context compression is on (it is). Connections are recycled about
  every 10 minutes, and ADK reconnects with session resumption. If the live session
  still ends, the page gets `end` (`session_ended`/`error`) and the call is persisted,
  but the caller hears no goodbye.
- **Voice: plain WebSocket audio.** PCM over TCP has no jitter buffer or packet-loss
  concealment beyond what the page queues, so audio can stutter or lag on poor mobile
  networks. Each direction is about 256 kbit/s of base64 PCM.
- **Voice: echo.** The page asks for browser echo cancellation, but Chrome doesn't
  reliably cancel audio played through Web Audio, so on laptop speakers the agent can
  hear itself and barge in on itself. Use headphones.
- **Voice: iOS Safari is best-effort.** It needs iOS 14.5+ (AudioWorklet). While the
  mic is open, iOS may route playback to the earpiece or play it quietly. Locking the
  screen or switching apps suspends the AudioContext and the call goes silent. Neither
  is handled.
- **Voice: WS secured only by an optional shared token** in the page URL
  (`VOICE_WS_TOKEN`). It is not per-user and not signed, and the page itself is public.
- **Voice: function-calling mode is model-dependent.** `gemini-3.8-live` may default
  to *non-blocking* tool calls, so the model could keep talking while a tool runs. The
  prompt forbids describing results before they arrive, but verify on real calls.
- **Voice: no call recording.** If the caller hangs up while a tool is running, the
  bridge waits up to 5 s to record its result.
- **Voice transcript grouping is heuristic.** Turns are built from finished
  transcriptions, and Gemini 3.x sends one final input transcription per utterance,
  sometimes after the reply has started. So in rare cases a caller line can land one
  turn late in the stored record.
- **No telephony.** Calls come only from the browser page. A phone provider could be
  added later by writing a new protocol adapter in front of the same `CallBridge`.

---

## Common changes: where to edit

| I want to… | Edit |
|---|---|
| Change the agent's tone, flow, or rules | `src/clinic_agent/prompt.py` (`BASE_PROMPT` for both channels, `WHATSAPP_STYLE` / `VOICE_STYLE` per channel) |
| Change the voice model or voice | `VOICE_MODEL` / `VOICE_NAME` env vars; Live session options in `voice/bridge.py::build_run_config` |
| Change the call page (UI, mic capture, playback) | `voice/static/call.html` (restart the server; it is read once at startup) |
| Change the call wire protocol | `voice/protocol.py` + `voice/bridge.py` + `call.html` + `scripts/voice_sim.py` |
| Change call audio handling (chunking, barge-in) | `voice/audio.py`, `voice/bridge.py::CallBridge._on_event` |
| Change how a call is recorded | `voice/bridge.py::CallBridge._persist`, `runtime/conversations.py::finish_call` |
| Add or change a capability | `tools/repo.py` (logic) + `tools/booking_tools.py` (tool + docstring) + `ALL_TOOLS` + `conversations._TOOL_INTENT` + `prompt.py` tool list |
| Change the model | `MODEL` env var (analytics picks the right thinking config automatically) |
| Change what the model sees each turn | `runtime/turn.py::contextualize` and `history.MAX_HISTORY_MESSAGES` |
| Change console fields / outcome logic | `runtime/conversations.py` (it follows the console data contract, so coordinate with the console team) |
| Change analytics fields | `runtime/analytics.py::ConversationAnalytics` + `_PROMPT` + `conversations.apply_analytics` |
| Tune debounce or idle behaviour | env vars (`DEBOUNCE_*`, `IDLE_TIMEOUT_MINUTES`, `SWEEP_SECONDS`) |
| Change slot math | `tools/slots.py` |
| Change how replies are sent | `whatsapp/client.py` |
| Add an HTTP route | `app.py::create_app` |

---

## License

Proprietary — internal project.

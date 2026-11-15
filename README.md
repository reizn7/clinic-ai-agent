# clinic-ai-agent

A **WhatsApp and phone AI receptionist for a medical clinic.** Patients message the
clinic's WhatsApp number, or phone the clinic's Exotel number, and talk in natural
language. A Google ADK + Gemini agent answers clinic questions, checks doctor
availability, and books, reschedules, lists, or cancels appointments stored in
MongoDB. Both channels share one "brain": the same tools, data, and core prompt
rules. WhatsApp runs turn by turn on a text model; calls are bridged to a
**Gemini Live** native-audio session. Every conversation is also recorded in a
console-ready shape (transcript, tool calls, outcome, escalation, plus an
LLM-generated summary / sentiment / language) for a staff analytics dashboard.

This is **v2** (Python, package version `2.0.0`). The original Node.js prototype
lives in [`v1/`](v1/) **for reference only**: it is not built, linted, tested, or
shipped. Only its WhatsApp transport and data model were carried forward; its
hardcoded rule-engine/state machine was replaced by the LLM agent.

> **Quick orientation:** the heart of the system is
> [`runtime/turn.py`](src/clinic_agent/runtime/turn.py) (one message in → one reply
> out), [`voice/bridge.py`](src/clinic_agent/voice/bridge.py) (one phone call:
> Exotel audio ⇄ Gemini Live),
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
| Telephony | Exotel Voicebot applet (bidirectional WebSocket, base64 16-bit PCM) |
| Audio | `numpy` + `soxr` streaming resampler (Python 3.14 has no `audioop`) |
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
│  voice/exotel.py     ── WS /voice/exotel (token check, rate)       │
│        ▼                                                          │
│  voice/bridge.py     ── one call: Exotel PCM ⇄ ADK run_live        │
│        │                (agent "voice" = same tools, voice prompt) │
│        ├─► voice/audio.py (resample 16k⇄24k, 320-byte chunks)     │
│        └─► on hangup: history (`transcripts`) +                   │
│                       conversations.finish_call (`conversations`) │
└───────────────────────────────────────────────────────────────────┘
   ▲                                   │
   │ wss (Exotel Voicebot applet)      ▼  MongoDB: clinic · doctors · patients ·
Phone caller ──► Exotel                   appointments · transcripts ·
                                          conversations · pending_messages
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

### One phone call, end to end

1. **Connect:** Exotel's Voicebot applet opens `wss://<host>/voice/exotel?sample-rate=16000&token=…`.
   If `VOICE_WS_TOKEN` is set and `token` doesn't match, the socket is rejected (403).
2. **`start`:** the bridge waits for Exotel's `start` event and reads `start.stream_sid`,
   `start.call_sid` and `start.from` (the caller). It normalises the caller number
   (`phone.normalize_phone`: `09876543210` / `+91 98765 43210` / `9876543210` →
   `919876543210`) and opens an `active` `conversations` doc with `channel: "voice"`.
3. **Seed:** the bridge builds the same context as a WhatsApp turn
   (`contextualize(..., channel="voice")`: today's date, the caller's number, prior
   `transcripts` history including tool results), adds a "greet the caller" cue, and
   sends it into a **fresh** ADK live session (random session id) on the voice runner.
   The model speaks first.
4. **Stream** (two tasks):
   - *upstream:* Exotel `media` → base64 decode → resample to 16 kHz (if Exotel isn't
     already at 16 kHz) → `LiveRequestQueue.send_realtime(audio/pcm;rate=16000)`.
     `stop` or a disconnect ends the call. `dtmf`/`mark` are logged at DEBUG and ignored.
   - *downstream:* live events. Audio (24 kHz) is resampled to the Exotel rate,
     re-chunked into ~100 ms payloads that are multiples of 320 bytes (min 3,200), and
     sent as Exotel `media`. The remainder is flushed, zero-padded, on `turn_complete`.
     On `interrupted` (barge-in) the bridge drops its buffer and sends Exotel `clear`.
     Finished input/output transcriptions become user/assistant turns, and tool calls
     are captured by the same `ToolCallRecorder` as `run_turn`.
   ADK runs the tools itself (same `ALL_TOOLS`), and reconnects on the Live API's
   `goAway` using session resumption.
5. **Hang up:** if a tool call is still in flight, the bridge waits up to 5 s for it,
   then closes the live queue and the socket. Whatever happened (including errors), it
   persists the call: its turns are appended to `transcripts` (so the next chat or call
   remembers it), and the conversation is finalized with `conversations.finish_call`
   (messages, derived fields, `durationSec`, final status). That makes it eligible
   for analytics right away.

---

## Project structure

```
src/clinic_agent/
  app.py                 FastAPI app, health routes, lifespan (starts sweeper + flusher), main()
  config.py              Settings singleton (env / .env), with v1-name aliases
  db.py                  Cached MongoClient + create_index_safe()
  phone.py               normalize_phone() (Exotel formats → digits) + to_e164()
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
    exotel.py            WS /voice/exotel router (token check, ?sample-rate=), mounted if VOICE_ENABLED
    bridge.py            run_call(): one call, Exotel ⇄ ADK live session, persistence on hangup
    protocol.py          Exotel event parsing (start/media/stop) + outbound media/clear/mark
    audio.py             base64, soxr streaming resampler, Exotel 320-byte chunker
scripts/
  seed.py                idempotent: 1 clinic + 2 doctors + all indexes
  seed_demo.py           DESTRUCTIVE: wipes data, seeds 7 doctors, sample patients + appointments
  smoke_live.py          live end-to-end check (real Mongo + Gemini, no WhatsApp send)
  voice_sim.py           local Exotel simulator: streams WAV / `say` audio, saves replies to out.wav
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
- `create_app()` sets up logging and exposes `GET /` (`{"status": "Clinic AI Agent v2 running"}`),
  `GET /healthz` (`{"status": "ok"}`), and mounts the webhook router at `/webhook`.
- `lifespan` starts `sweeper.run_forever()` and `flusher.run_forever()` as asyncio
  tasks and cancels them on shutdown.
- When `VOICE_ENABLED` is true, it also mounts the Exotel WebSocket router (`/voice/exotel`).
- `main()` is the `clinic-agent` console script. It runs uvicorn on `0.0.0.0:$PORT`.

### `phone.py`
- `normalize_phone(raw, default_cc="91")` returns digits with the country code and no
  `+`, the form `patients.phone` and `transcripts` use. It handles `+91…`, a trunk `0`
  plus 10 digits, bare 10-digit national numbers (India assumed), and a `00` prefix.
  Digits that already carry a country code pass through unchanged. Only the voice path
  calls it; WhatsApp `from` is already canonical.
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
  `start_call()` inserts it as `active` (with `callSid`) when the call connects.
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

### `tools/repo.py`
Synchronous Mongo data access, ported from v1's Mongoose services. Key behaviours:
- Doctors are matched by **case-insensitive exact name** among `isActive: true` doctors.
- `get_available_slots()` rejects past dates and days not in `availableDays`, tiles
  `workingHours` by `consultationDuration`, and removes slots booked with an active status.
- `get_upcoming_availability()` returns the next 3 dates (starting tomorrow, up to 60
  days ahead) with free slots, at most 6 slots each.
- `create_appointment()` re-validates the slot, upserts the patient by phone, and
  inserts with status `Pending`. A `DuplicateKeyError` returns `{success: false, conflict: true}`.
- `reschedule_appointment()` keeps the doctor and status and only changes date/slot.
- `cancel_appointment()` sets status `Cancelled` + `cancelledAt`.
- `ensure_indexes()` creates the double-booking guard and the unique patient phone index.

### `tools/slots.py`
`generate_slots({"start":"09:00","end":"13:00"}, 30)` returns `["09:00","09:30",…,"12:30"]`.
A slot only counts if it fits fully before `end`. `available_slots(all, booked)` filters
out the booked ones.

### `whatsapp/client.py`
`send_text_message(to, body)` POSTs to
`https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages` with a
Bearer token and a 15s timeout, and raises on HTTP ≥400. Only plain text is sent.

### `voice/exotel.py`
`@router.websocket("/voice/exotel")`. It rejects the socket (close 1008, which is a
403 before accept) when `VOICE_WS_TOKEN` is set and `?token=` doesn't match
(constant-time compare). It reads `?sample-rate=` (8000/16000/24000, falling back to
`VOICE_SAMPLE_RATE`), accepts, and hands off to `bridge.run_call`.

### `voice/bridge.py`
`run_call(ws, sample_rate=, runner=, run_config=)` handles one call and **never
raises** (see "One phone call" above). `CallBridge` holds the per-call state: live
queue, recorder, transcript, chunker, and resamplers. `CallTranscript` groups finished
transcriptions and tool calls into turns: caller speech after the agent has spoken
starts a new turn. `get_runner()` lazily builds the module-level
`InMemoryRunner(build_agent("voice"))`. `build_run_config()` sets BIDI streaming,
`AUDIO` response modality, input and output transcription, an optional prebuilt voice
(`VOICE_NAME`), sliding-window context compression (to get past the ~15 min
audio-session cap), and session resumption (so ADK reconnects on `goAway`).

### `voice/protocol.py`
Parses Exotel frames (`parse_message`, `parse_start` → `CallInfo{stream_sid, call_sid,
caller, callee, account_sid, sample_rate, custom_parameters}`, `media_payload`). It
accepts both the documented snake_case and the camelCase seen in Exotel's sample repo.
It also builds outbound `media` / `clear` / `mark` events.

### `voice/audio.py`
`b64decode`/`b64encode`, `rate_from_mime`, `StreamResampler` (stateful soxr, carries
odd bytes, `reset()` on barge-in), and `OutboundChunker`. The chunker emits exact
`chunk_size_for(rate)` chunks (~100 ms, a multiple of 320, clamped to Exotel's
3,200–100,000 bytes), holds the remainder, flushes zero-padded, and `clear()`s on
barge-in.

---

## Agent tools

Defined in [`tools/booking_tools.py`](src/clinic_agent/tools/booking_tools.py) as
async functions registered in `ALL_TOOLS`. **ADK builds each tool's schema from the
function signature and docstring**, so the docstrings are part of the model's
interface. Each tool runs the corresponding sync `repo` function via `asyncio.to_thread`.

| Tool | Args | Returns (shape) |
|---|---|---|
| `get_clinic_info` | – | `{clinicName, timings, address, phone}` |
| `list_doctors` | – | `{doctors: [{id, name, specialization}]}` |
| `check_availability` | `doctor_name, date` | `{doctor, date, weekday, slots[], note?}` or `{error}` |
| `find_next_available` | `doctor_name` | `{doctor, dates: [{date, weekday, slots[]}]}` or `{error}` |
| `book_appointment` | `patient_name, age, gender, doctor_name, date, slot, phone` | `{success: true, appointmentId, doctor, date, weekday, slot, status}` or `{success: false, error, conflict?, available_slots?}` |
| `list_my_appointments` | `phone` | `{appointments: [{appointmentId, doctor, date, slot, status}]}` |
| `reschedule_appointment` | `appointment_id, new_date, new_slot` | like `book_appointment` |
| `cancel_appointment` | `appointment_id` | `{success, appointmentId, status}` or `{success: false, error}` |
| `escalate_to_human` | `reason` | `{escalated: true, message}` (logs it; does **not** notify anyone) |

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
- After `escalate_to_human`, tell the caller the clinic will call them back.
- End with a short goodbye.

---

## Data model (MongoDB)

Database name comes from `MONGO_DB_NAME` (default `clinic-ai-agent`).

| Collection | Shape | Indexes |
|---|---|---|
| `clinic` | `{clinicName, timings, address, phone, slotDuration}` (single doc; `get_clinic` falls back to defaults if missing) | – |
| `doctors` | `{name, specialization, consultationDuration (min), availableDays: ["Monday",…], workingHours: {start, end}, isActive}` | – |
| `patients` | `{phone, name, age, gender, createdAt, updatedAt}`. `phone` is the raw WhatsApp `from` (digits, no `+`) | unique `phone` |
| `appointments` | `{patientId, doctorId, date "YYYY-MM-DD", slot "HH:mm", status, createdAt, updatedAt, cancelledAt?}` | **unique partial `(doctorId, date, slot)`** where `status ∈ {Pending, Confirmed, Arrived, Completed}` |
| `transcripts` | per phone, see `history.py` | – |
| `conversations` | per conversation, see `conversations.py`. `channel` is `whatsapp` or `voice`; voice docs add `callSid` and `durationSec`. `phone` is E.164 (`+` prefixed), timestamps are ISO-8601 UTC strings | unique `externalId`; `(clinicId, startedAt)`, `(clinicId, outcome, startedAt)`, `(clinicId, escalated, startedAt)`, `(phone, startedAt)` |
| `pending_messages` | `{phone, messages[], deadline, firstAt}` (ISO strings) | unique `phone`; `deadline` |

Appointment statuses: `Pending` (all agent bookings), `Confirmed`, `Arrived`,
`Completed`, `Cancelled`. Only `Cancelled` frees a slot.

**Indexes are created only by the seed scripts**, not at app startup. Run
`scripts/seed.py` against any new database, or the double-booking guard won't exist.

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
| `VOICE_ENABLED` | `true` | Mount the Exotel WebSocket at `/voice/exotel` |
| `VOICE_MODEL` | `gemini-3.8-live` | Gemini Live native-audio model for calls (must support function calling) |
| `VOICE_NAME` | `""` | Optional prebuilt voice (e.g. `Kore`, `Puck`); empty means the model default |
| `VOICE_SAMPLE_RATE` | `16000` | PCM rate requested from Exotel (`8000`/`16000`/`24000`); must match `?sample-rate=` in the applet URL |
| `VOICE_WS_TOKEN` | `""` | Optional shared secret; if set, the WS URL must carry `?token=<value>` |
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
# or, for a richer demo dataset (WIPES patients/appointments/conversations/transcripts/pending_messages/clinic/doctors):
# uv run python scripts/seed_demo.py
uv run clinic-agent                   # serves on :$PORT (default 3000)
```

### Seed data
- `seed.py`: "ABC Clinic" (Mon–Sat 9–5); **Dr. Asha Mehta** (General Physician,
  Mon–Fri 09:00–13:00, 30 min) and **Dr. Rohit Sharma** (Dentist, Mon/Wed/Fri/Sat
  14:00–17:00, 20 min). It upserts by natural key and never touches patients or
  appointments.
- `seed_demo.py`: 7 doctors across specialties (General Physician, Dentist,
  Pediatrician, Cardiologist, Dermatologist, Orthopedic Surgeon, Gynecologist), a few
  sample patients, and existing `Confirmed` appointments on upcoming working days.

### Connect WhatsApp (local dev)

```bash
cloudflared tunnel --url http://localhost:3000      # or: ngrok http 3000
```

In the Meta dashboard → **WhatsApp → Configuration → Webhook**: set the callback URL
to `https://<tunnel>/webhook` and the verify token to your `VERIFY_TOKEN`, then
subscribe to the **`messages`** field. In dev mode, add your own number as a test
recipient under **API Setup**, then text the business number.

### Connect Exotel (local dev)

```bash
uv run clinic-agent
cloudflared tunnel --url http://localhost:3000      # or: ngrok http 3000
```

In the Exotel dashboard, build a call flow (App Bazaar) for your ExoPhone with a
**Voicebot** applet, and set its URL to:

```
wss://<tunnel-host>/voice/exotel?sample-rate=16000&token=<VOICE_WS_TOKEN>
```

`sample-rate` must match `VOICE_SAMPLE_RATE`. Leave out `token` only if
`VOICE_WS_TOKEN` is empty. Call the ExoPhone; the agent greets you first. Run with
`LOG_LEVEL=DEBUG` to log the raw `start` payload (to confirm field names) and the
ignored `dtmf`/`mark` events.

### Test calls without a phone: `scripts/voice_sim.py`

The simulator acts like Exotel against a local server. It sends `connected` and
`start` (fake caller, default `+919876500001`), streams caller audio as real-time
100 ms `media` frames (silence between utterances), waits for each reply to finish,
writes everything the agent said to `out.wav`, and sends `stop`.

```bash
uv run python scripts/voice_sim.py                       # greeting only → out.wav
uv run python scripts/voice_sim.py --wav hello.wav       # 16-bit mono WAV(s), any rate
uv run python scripts/voice_sim.py \
  --say "Hi, I'd like to book an appointment with Doctor Asha Mehta" \
  --say "My name is Ravi Kumar, I'm 34, male" \
  --say "Next Monday, the earliest slot please" \
  --say "Yes, please book it"                            # macOS `say` synthesises each turn
```

Options: `--url` (default `ws://localhost:3000/voice/exotel`), `--rate`, `--token`,
`--caller`, `--out`, `--reply-gap`, `--max-reply`. Afterwards, check the
`conversations` doc (`channel: "voice"`, `callSid` from the simulator's log) for the
`book_appointment` tool call.

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
| `test_phone.py` | phone normalisation (Exotel formats) and E.164 |
| `test_prompt.py` | WhatsApp prompt pinned by hash; voice prompt composition; `build_agent(channel)` |
| `test_tool_capture.py` | function call/response pairing, status, duration |
| `test_voice_audio.py` | chunk sizes, remainder/flush padding, resampled lengths |
| `test_voice_protocol.py` | Exotel `start`/`media` parsing, outbound events |
| `test_voice_conversations.py` | one conversation per call, derived fields, analytics eligibility, WhatsApp isolation |
| `test_voice_bridge.py` | full call against a fake live runner (audio, barge-in `clear`, transcription, tool call, persistence), failure handling, token/rate routing |

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
`uv sync --frozen --no-dev`, `CMD uv run --no-dev clinic-agent`):

```bash
docker build -t clinic-ai-agent .
docker run --env-file .env -p 3000:3000 clinic-ai-agent
```

Host it anywhere with HTTPS (Render, Railway, Fly.io) and point the Meta webhook at
`https://<host>/webhook`. Use an **always-on single instance**. See the limitations below.

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
- **`escalate_to_human` only flags.** It logs and sets `escalated` on the conversation
  for the console queue; no one is actively notified.
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
- **Voice: no call transfer yet.** `escalate_to_human` only flags the conversation;
  the agent tells the caller the clinic will call back. Nobody is notified.
- **Voice: Live session limits.** Audio-only Live sessions are capped at about 15
  minutes unless context compression is on (it is). Connections are recycled about
  every 10 minutes, and ADK reconnects with session resumption. Exotel caps a stream at
  60 minutes. If the live session still ends, the call ends; it is persisted, but the
  caller hears no goodbye.
- **Voice: WS secured only by an optional token** in the URL (`VOICE_WS_TOKEN`). The
  URL is not signed, and Exotel's Basic-auth / IP-allowlist options are not used.
- **Voice: function-calling mode is model-dependent.** `gemini-3.8-live` may default
  to *non-blocking* tool calls, so the model could keep talking while a tool runs. The
  prompt forbids describing results before they arrive, but verify on real calls.
- **Voice: no call recording, DTMF, or outbound calls.** If the caller hangs up while
  a tool is running, the bridge waits up to 5 s to record its result.
- **Voice transcript grouping is heuristic.** Turns are built from finished
  transcriptions, and Gemini 3.x sends one final input transcription per utterance,
  sometimes after the reply has started. So in rare cases a caller line can land one
  turn late in the stored record.

---

## Common changes: where to edit

| I want to… | Edit |
|---|---|
| Change the agent's tone, flow, or rules | `src/clinic_agent/prompt.py` (`BASE_PROMPT` for both channels, `WHATSAPP_STYLE` / `VOICE_STYLE` per channel) |
| Change the voice model or voice | `VOICE_MODEL` / `VOICE_NAME` env vars; Live session options in `voice/bridge.py::build_run_config` |
| Change call audio handling (rates, chunking, barge-in) | `voice/audio.py`, `voice/bridge.py::CallBridge._on_event` |
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

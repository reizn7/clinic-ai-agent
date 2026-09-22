# Clinic AI Agent — v2 Plan

A distilled, **text-only WhatsApp AI agent** for a single clinic, written in Python.
It keeps only the WhatsApp transport from v1 and distills the agent runtime from the
mature `nextdim-ai` platform. Voice, multi-tenant manifests, and analytics are
explicitly out of scope for now (clean seams left to add them later).

---

## 1. Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Language / runtime | **Python 3.14** | Per requirement; `google-adk` supports 3.10–3.14. |
| Agent framework | **Google ADK** (single `LlmAgent`) | Reuse nextdim's proven runtime; no multi-agent tree. |
| LLM | **Gemini via API key** (`GOOGLE_GENAI_USE_VERTEXAI=false`) | No GCP project/billing/ADC — just an AI Studio key. |
| WhatsApp | **Meta WhatsApp Cloud API** (Graph API) | Direct port of v1's proven integration. |
| Storage | **MongoDB** | GCP-free; reuse nextdim's `MongoSessionService` near-verbatim. |
| Tools | **Local toolset over Mongo** | Self-contained clinic booking; no mcp-server, no EHR gateway. |
| Shape | **Single FastAPI service** | Collapses nextdim's 3-service split (channel + brain + tools). |
| Build tool | **uv** | Same toolchain as `nextdim-ai` (`pyproject.toml` + `uv.lock`, `uv run` / `uv sync`). |

**GCP required? No.** The entire v2 is GCP-free. External accounts needed: a Meta
app (WhatsApp), a Google account (for the AI Studio Gemini key — *not* a GCP
project), and a MongoDB Atlas cluster (free M0). No `gcloud`, no ADC, no service
account, no Vertex.

---

## 2. What carries forward

**From v1 `clinic-ai-agent` (Node) — WhatsApp transport ONLY:**
- `routes/webhook.js` → FastAPI `GET /webhook` (verify `hub.verify_token`) +
  `POST /webhook` (parse `entry[0].changes[0].value.messages[0]`, ack 200 fast).
- `services/whatsappService.js` → `send_text_message(to, body)` via
  `graph.facebook.com/{VER}/{PHONE_NUMBER_ID}/messages`, Bearer `ACCESS_TOKEN`.

Everything else in v1 (the rule-engine state machine, its Express controllers/
routes) is **dropped** — the LLM replaces the hardcoded slot-filling flow.

**From `nextdim-ai/unified-text-agent` — the agent brain, distilled:**
- `domains/runtime/model_factory.py` → `build_model()` for Gemini (API-key path).
  Drop: Claude/Vertex fallback chain, Priority-PayGo, thinking knobs.
- `domains/runtime/mongo_session_service.py` → `MongoSessionService` (durable ADK
  sessions in Mongo), reused near-verbatim, **keyed by WhatsApp phone number**.
- `server/main.py::_default_run_turn_impl` → the ADK `Runner.run_async` turn loop
  + `contextualize()` seeding. Drop: `prior_state`/voice handoff, outbound
  prefetch, `information_collected` export, multi-agent author chain.
- `behaviors/{base,agent_class,specialist}.py` → collapsed into one `build_agent()`
  (render prompt + attach tools + `LlmAgent(...)`). Keep the per-turn
  `current_datetime` injection.
- `plugins/human_escalation.py` → kept (cheap, self-contained, real WhatsApp value).

**Explicitly dropped from nextdim:** `domains/handoff/` (multi-agent tree),
`domains/outbound/` (campaigns), multi-tenant manifest machinery + `PersonaCache`,
the configurator (Pub/Sub, BigQuery, SSE, Twilio), and the entire voice stack.

---

## 3. Architecture

```
WhatsApp Cloud API
   │  GET /webhook (verify)   POST /webhook (inbound message)
   ▼
┌──────────────────────────────────────────────────────────┐
│  whatsapp/   (ported from v1 → Python)                     │
│    webhook.py  — verify + parse inbound                    │
│    client.py   — send_text_message() via Graph API (httpx) │
├──────────────────────────────────────────────────────────┤
│  runtime/    (distilled from nextdim unified-text-agent)   │
│    session_service.py — MongoSessionService (key = phone)  │
│    model_factory.py   — build_model() → ADK Gemini         │
│    agent.py           — build_agent(): one LlmAgent        │
│    turn.py            — run_turn(): ADK Runner loop → reply │
├──────────────────────────────────────────────────────────┤
│  tools/      (local booking over Mongo)                    │
│    booking_tools.py, slots.py, db.py                       │
└──────────────────────────────────────────────────────────┘
   │
   ▼  MongoDB  (ADK sessions + clinic/doctor/patient/appointment)
```

### Message flow (one inbound WhatsApp text)
1. `POST /webhook` extracts `from` (phone) + `text.body`; returns **200 immediately**
   (non-message callbacks — delivery/read receipts — are acked and ignored).
2. `run_turn(phone, text)`: `MongoSessionService` resumes the durable session for
   that phone (server-side; no transcript replay), gets/builds the single ADK agent.
3. Agent (Gemini) responds conversationally, calling booking tools as needed; tools
   read/write the clinic Mongo collections.
4. Final reply text → `whatsapp.client.send_text_message(phone, reply)`.
5. Any error → a fixed fallback reply so the patient is never left hanging.

---

## 4. Session & state design

**Stateless agent + we own the transcript** (nextdim's *configurator* pattern, not a
custom ADK session service). Each turn runs on a fresh in-memory ADK session; prior
turns are stored per phone in Mongo (`transcripts` collection, `runtime/history.py`)
and re-seeded into the message via `contextualize()` (`runtime/turn.py`). This is
simpler and more robust than implementing an ADK `BaseSessionService`, and keeps
storage a thin swappable repo.

*(Deviation from the original plan, which proposed reusing nextdim's durable
`MongoSessionService`. The transcript-ownership design was chosen during
implementation for simplicity — same durability, less ADK-internal surface.)*

The phone number is injected into the turn context via `contextualize()`, so the
agent always knows the sender and never asks for it. History is capped
(`MAX_HISTORY_MESSAGES`) to bound prompt tokens.

---

## 5. Local booking toolset (ADK function tools over Mongo)

The LLM drives the conversation (gathering name/age/gender/date/slot naturally) and
calls these tools; there is no hardcoded state machine.

| Tool | Kind | Behavior |
|---|---|---|
| `get_clinic_info()` | read | Clinic name, timings, address, phone. |
| `list_doctors()` | read | Active doctors + specialization. |
| `check_availability(doctor, date)` | read | Tile working hours into slots minus booked (ported slot math). |
| `book_appointment(name, age, gender, doctor, date, slot, phone)` | write | Upsert patient by phone; create appointment; **unique partial index on `(doctorId, date, slot)`** guards double-booking; surface the conflict back to the LLM. |
| `list_my_appointments(phone)` | read | Upcoming appointments for the caller. |
| `cancel_appointment(appointment_id)` | write | Cancel (status → Cancelled). |
| `escalate_to_human(reason)` | signal | Flag that the patient wants a human; agent hands off gracefully. |

---

## 6. Data model (Mongo collections)

Ported from v1's schemas (the data model was sound; only the conversation engine
is discarded):
- **clinic** — `clinicName`, `timings`, `address`, `phone`, `slotDuration`.
- **doctor** — `name`, `specialization`, `consultationDuration`, `availableDays[]`,
  `workingHours{start,end}`, `isActive`.
- **patient** — `phone` (unique), `name`, `age`, `gender`.
- **appointment** — `patientId`, `doctorId`, `date` (`YYYY-MM-DD`), `slot` (`HH:mm`),
  `status`; unique partial index `(doctorId, date, slot)` excluding cancelled.
- **agent_sessions** — ADK session docs (owned by `MongoSessionService`).

Dates/times stay as plain strings (`YYYY-MM-DD` / `HH:mm`) to avoid timezone drift.

---

## 7. Environment (`.env`) — all GCP-free

```
# WhatsApp (Meta Cloud API)
VERIFY_TOKEN=
ACCESS_TOKEN=
PHONE_NUMBER_ID=
GRAPH_API_VERSION=v21.0

# LLM (Gemini via AI Studio key — no GCP)
GOOGLE_API_KEY=
GOOGLE_GENAI_USE_VERTEXAI=false
MODEL=gemini-2.5-flash

# Storage
MONGO_URI=
MONGO_DB_NAME=clinic-ai-agent

# App
LOG_LEVEL=INFO
PORT=3000
```

---

## 8. Toolchain, dependencies & Python 3.14

**Built with `uv`, same as `nextdim-ai`.** The project is a `uv`-managed package:
a single `pyproject.toml` + committed `uv.lock`, with `uv sync` to install and
`uv run` to execute (`uv run uvicorn ...`, `uv run python -m ...`, `uv run pytest`).
`requires-python = ">=3.14,<3.15"`; `uv python pin 3.14` writes `.python-version`.

Core deps: `google-adk`, `google-genai`, `fastapi`, `uvicorn[standard]`, `httpx`,
`pymongo`, `pydantic`, `pydantic-settings`, `python-dotenv`. Dev group: `ruff`,
`pytest`, `pytest-asyncio` (mirrors nextdim's dev tooling).

**3.14 install note:** ADK publishes per-version constraints files so transitive
deps resolve to versions with 3.14 wheels. With uv, apply them via a constraints
dependency (or `uv pip install -c`):

```bash
uv add google-adk -c https://raw.githubusercontent.com/google/adk-python/main/constraints/py3.14.txt
```

(A couple of native deps — `pydantic-core`, `grpcio` — are the usual laggards on a
new Python; Milestone 0 confirms a clean `uv sync` before any feature work.)

---

## 9. Milestones

0. **Bootstrap** — project scaffold, `pyproject.toml` (3.14 + constraints), clean
   `uv sync` / install smoke test.
1. **WhatsApp round-trip** — FastAPI `/webhook` verify + echo bot; `send_text_message`;
   ngrok; confirm a real WhatsApp echo works (no AI yet).
2. **ADK brain** — `model_factory` + `MongoSessionService` + single `LlmAgent`
   receptionist prompt; Gemini replies, durable session per phone (echo replaced).
3. **Tools + Mongo** — port data model + a `seed.py` (1 clinic, 2 doctors); wire the
   booking tools; full "book an appointment" conversation works end-to-end.
4. **Polish** — human-escalation, inbound **debounce** (buffer rapid multi-bubble
   texts ~2–3s per phone), fallback reply, `Dockerfile`, a few unit tests.

---

## 10. Risks & notes
- **Message debounce:** WhatsApp users send several short bubbles in a row. Without
  a per-phone debounce the agent replies mid-thought. nextdim solved this in the
  configurator; we add a small in-process debounce in Milestone 4.
- **3.14 wheels:** verify native-dep wheels at Milestone 0 (constraints file
  mitigates this).
- **No signature verification** on the webhook yet (Meta `X-Hub-Signature-256`) —
  fine for dev; add before any real deployment.
- **Single clinic / single agent** assumed throughout — multi-tenant is a future seam.

---

## 11. Future seams (deferred, not designed away)
- Voice (LiveKit) — parallel runtime, same tools.
- Multi-tenant manifests + `PersonaCache` — swap the single config for per-tenant.
- Outbound reminders/recalls — add a scheduler + outbound send.
- mcp-server / real EHR tools — swap the local toolset for `tools_client` over HTTP.
- Webhook signature verification, delivery-status handling, media messages.

# clinic-ai-agent

A **WhatsApp AI receptionist for a medical clinic.** Patients message the clinic's
WhatsApp number in natural language; a Google ADK + Gemini agent chats with them and
books, looks up, or cancels appointments in MongoDB. Every conversation is recorded
in a console-ready shape (transcripts, tool calls, outcomes, escalations, and an
LLM-generated summary/sentiment/language) for a staff analytics dashboard.

This is **v2** (Python). The original Node.js prototype lives in [`v1/`](v1/) for
reference — only its WhatsApp transport was carried forward; the hardcoded
rule-engine was replaced by the LLM agent.

---

## Highlights

- **Conversational booking** — no rigid menus; the agent gathers details naturally
  and calls tools to check availability and book.
- **Manifest-free, single agent** — one Gemini `LlmAgent` with a production-grade
  receptionist prompt (distilled from the nextdim-ai prompt library).
- **Debounce** — rapid WhatsApp bubbles are coalesced into one turn.
- **Durable conversations + analytics** — a background sweeper finalizes idle
  conversations and generates summary / sentiment / language.
- **No GCP required** — Gemini via an AI Studio API key, storage on MongoDB Atlas.

## Tech stack

Python 3.14 · [uv](https://docs.astral.sh/uv/) · FastAPI · Google ADK (Gemini) ·
MongoDB · Meta WhatsApp Cloud API.

---

## Architecture

```
WhatsApp Cloud API
   │  GET /webhook (verify)   POST /webhook (inbound)
   ▼
┌──────────────────────────────────────────────────────────────┐
│  whatsapp/   webhook (verify + parse) · client (Graph API send)│
├──────────────────────────────────────────────────────────────┤
│  debounce  → buffer bubbles → flusher coalesces into one turn   │
│  runtime/  → agent (Gemini LlmAgent) · turn loop · history      │
│  tools/    → booking ops over Mongo (doctors, slots, appts)     │
│  sweeper   → finalize idle conversations + generate analytics    │
└──────────────────────────────────────────────────────────────┘
   │
   ▼  MongoDB  (transcripts · conversations · clinic/doctors/patients/appointments)
```

**One inbound message, end to end:**

1. `POST /webhook` parses the sender + text, acks `200` immediately.
2. The message is **buffered** (`debounce`); the **flusher** waits for a quiet gap
   (~5s) then joins the batch into a single turn.
3. `run_turn` seeds a fresh ADK session with the caller, today's date, and prior
   turns (including earlier **tool results**, so the agent can recall e.g. an
   appointment id), then runs the Gemini agent.
4. The agent calls booking **tools** against Mongo as needed and produces a reply.
5. The reply is sent back over the WhatsApp Cloud API; the turn is written to
   `transcripts` (prompt history) and `conversations` (console record).
6. A background **sweeper** later closes the conversation once it's idle and
   generates its summary / sentiment / language.

State is **not** kept in memory between turns: prior turns come from Mongo and are
re-seeded into each turn, so the service is effectively stateless per request.

---

## Project structure

```
src/clinic_agent/
  app.py                 FastAPI app + lifespan (starts sweeper & flusher)
  config.py              Settings (env / .env), with v1-name aliases
  db.py                  Shared MongoClient (TLS via certifi)
  prompt.py              Receptionist system prompt
  whatsapp/
    webhook.py           GET verify · POST inbound (enqueues to debounce)
    client.py            send_text_message() via the Graph API
  runtime/
    agent.py             builds the single Gemini LlmAgent
    turn.py              run_turn(): ADK runner loop, tool capture, contextualize
    model_factory.py     Gemini (API-key) model selection
    history.py           transcripts collection (prompt history)
    conversations.py     console `conversations` collection (sessionised)
    analytics.py         summary/sentiment/language via structured Gemini output
    sweeper.py           idle finalize + analytics background loop
    debounce.py          Mongo-backed inbound buffer
    flusher.py           debounce flusher background loop
  tools/
    booking_tools.py     the 7 ADK tools the agent can call
    repo.py              clinic data access (clinic/doctors/patients/appointments)
    slots.py             slot generation from working hours
scripts/
  seed.py                seed a clinic + doctors + indexes
  smoke_live.py          live end-to-end check (real Mongo + Gemini)
tests/                   unit tests (pytest + mongomock)
Dockerfile               uv + Python 3.14 image
PLAN.md                  design decisions & build log
v1/                      original Node.js prototype (reference only)
```

### Agent tools

`get_clinic_info` · `list_doctors` · `check_availability` · `book_appointment` ·
`list_my_appointments` · `cancel_appointment` · `escalate_to_human`.

---

## Getting started

### Prerequisites

- **uv** ≥ 0.11 and **Python 3.14** (`uv python install 3.14`)
- A **MongoDB** connection string (Atlas free M0 works)
- A **Gemini API key** from [Google AI Studio](https://aistudio.google.com/apikey)
- A **Meta WhatsApp** app (Access Token, Phone Number ID) — see below

### Install

```bash
uv sync
```

### Configure

```bash
cp .env.example .env
# then fill in the values (see the table below)
```

| Variable | Purpose |
|---|---|
| `VERIFY_TOKEN` | Any string; must match the token you set in the Meta webhook UI |
| `ACCESS_TOKEN` | WhatsApp Cloud API token (a permanent System-User token for production) |
| `PHONE_NUMBER_ID` | From WhatsApp → API Setup |
| `GRAPH_API_VERSION` | e.g. `v21.0` |
| `GOOGLE_API_KEY` | Gemini API key (AI Studio) |
| `GOOGLE_GENAI_USE_VERTEXAI` | `false` (use the API key, not Vertex) |
| `MODEL` | e.g. `gemini-2.5-flash` |
| `MONGO_URI` / `MONGO_DB_NAME` | MongoDB connection + database name |

Optional tuning (analytics sweeper, debounce, `CLINIC_ID`) is documented in
[`.env.example`](.env.example) with defaults.

### Seed the database

```bash
uv run python scripts/seed.py
```

Creates one clinic, two doctors, and the required indexes. Idempotent.

### Run

```bash
uv run clinic-agent          # serves on :$PORT (default 3000)
```

Expose it so Meta can reach the webhook (during local dev):

```bash
cloudflared tunnel --url http://localhost:3000      # or: ngrok http 3000
```

Then in the Meta dashboard → **WhatsApp → Configuration → Webhook**: set the
callback URL to `https://<tunnel>/webhook`, the verify token to your
`VERIFY_TOKEN`, save, and subscribe to the **`messages`** field. In dev mode, add
your own number as a test recipient under **API Setup**, then text the number.

---

## How it works

### Debounce
WhatsApp users send several short bubbles in a row. Inbound messages are buffered
per phone in the `pending_messages` collection; a flusher loop (tick ~1s) waits for
a quiet window (5s, hard cap 30s), joins the bubbles with newlines, and runs **one**
agent turn. Tunables: `DEBOUNCE_*` in `.env`.

### Sessions & analytics
Each turn appends to a per-phone `transcripts` doc (prompt history) and a sessionised
`conversations` doc (console record). A background **sweeper** (`SWEEP_SECONDS`)
closes conversations idle beyond `IDLE_TIMEOUT_MINUTES` (default 30) and then makes a
single structured Gemini call to fill `summary` / `sentiment` / `language`. This runs
off the reply path and is idempotent (`analyticsStatus`).

> The `conversations` collection follows a data contract shared with the staff
> console. See [`PLAN.md`](PLAN.md) for the field shapes and open judgment calls
> (e.g. `appointmentAt` timezone, intent mapping).

### Collections
- `transcripts` — per-phone prompt history (role/content/ts, + compact tool results)
- `conversations` — per-conversation console record (messages, tool calls, outcome,
  escalation, summary/sentiment/language)
- `clinic`, `doctors`, `patients`, `appointments` — clinic data
  (unique partial index guards against double-booking a slot)
- `pending_messages` — the debounce buffer

---

## Testing

```bash
uv run pytest            # unit tests (mongomock — no live services needed)
uv run ruff check .      # lint
```

For a real end-to-end check against your live Mongo + Gemini (run on a machine with
network access to your cluster):

```bash
uv run python scripts/smoke_live.py
```

It exercises debounce → a live agent turn (tools + conversation record) → analytics,
then cleans up its own test data.

---

## Deployment

The service is a single container ([`Dockerfile`](Dockerfile), uv + Python 3.14):

```bash
docker build -t clinic-ai-agent .
docker run --env-file .env -p 3000:3000 clinic-ai-agent
```

Host it anywhere that serves HTTPS (Render, Railway, Fly.io) and point the Meta
webhook at `https://<host>/webhook`.

**Notes:**
- The sweeper and flusher run **in-process**, so a single always-on instance is
  assumed. A free tier that sleeps on idle will pause the background loops; use an
  always-on instance, or scale-out would need these moved to a shared worker.
- Webhook signature verification (Meta `X-Hub-Signature-256`) is not yet enabled —
  add it before exposing the webhook publicly.

---

## License

Proprietary — internal project.

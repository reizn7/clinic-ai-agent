"""Runtime configuration, loaded from the environment / .env.

A single ``settings`` singleton is imported everywhere. Field aliases accept the
v1-style names too (``WHATSAPP_TOKEN``/``MONGODB_URI``) so an existing .env keeps
working without edits.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=True, extra="ignore"
    )

    # --- WhatsApp (Meta Cloud API) ---
    ACCESS_TOKEN: str = Field(
        default="", validation_alias=AliasChoices("ACCESS_TOKEN", "WHATSAPP_TOKEN")
    )
    PHONE_NUMBER_ID: str = ""
    VERIFY_TOKEN: str = ""
    GRAPH_API_VERSION: str = "v21.0"

    # --- LLM (Gemini via AI Studio key; no GCP) ---
    GOOGLE_API_KEY: str = ""
    GOOGLE_GENAI_USE_VERTEXAI: bool = False
    MODEL: str = "gemini-3.5-flash-lite"

    # --- Storage (MongoDB) ---
    MONGO_URI: str = Field(
        default="mongodb://localhost:27017",
        validation_alias=AliasChoices("MONGO_URI", "MONGODB_URI"),
    )
    MONGO_DB_NAME: str = "clinic-ai-agent"

    # Stamped on every conversations doc so a second clinic is a later change,
    # not a migration (see the console data contract).
    CLINIC_ID: str = "main-clinic"

    # --- Conversation finalize + analytics (idle sweeper) ---
    ANALYTICS_ENABLED: bool = True
    IDLE_TIMEOUT_MINUTES: int = 30  # silence after which a conversation is closed
    SWEEP_SECONDS: int = 60  # how often the background sweeper runs
    ANALYTICS_STUCK_MINUTES: int = 10  # reclaim analytics stuck in "processing"

    # --- Inbound debounce (coalesce rapid WhatsApp bubbles) ---
    DEBOUNCE_ENABLED: bool = True
    DEBOUNCE_WINDOW_SECONDS: float = 5.0  # quiet gap before flushing a batch
    DEBOUNCE_MAX_WAIT_SECONDS: float = 30.0  # hard cap even if still typing
    DEBOUNCE_TICK_SECONDS: float = 1.0  # how often the flusher polls

    # --- Voice channel (browser call page → Gemini Live) ---
    VOICE_ENABLED: bool = True  # serve GET /voice/call (page) + WS /voice/ws (call audio)
    VOICE_MODEL: str = "gemini-3.8-live"  # Live API native-audio model (function calling)
    VOICE_NAME: str = ""  # optional prebuilt voice, e.g. "Kore"; empty = model default
    VOICE_WS_TOKEN: str = ""  # optional shared secret; if set, open /voice/call?token=<it>

    # --- Appointment follow-ups (outbox worker) ---
    # The clinic's local timezone: appointment date+slot are local times.
    CLINIC_TIMEZONE: str = "Asia/Kolkata"
    # Google Calendar: a service account each doctor shares their calendar with
    # ("Make changes to events"). Doctors without a calendarEmail are skipped.
    GOOGLE_CALENDAR_ENABLED: bool = False
    GOOGLE_SERVICE_ACCOUNT_FILE: str = ""  # path to the service account JSON key
    GOOGLE_SERVICE_ACCOUNT_JSON: str = ""  # or the key JSON itself (e.g. a secret env var)
    # WhatsApp confirmations to the patient after book / reschedule / cancel.
    WHATSAPP_CONFIRMATIONS_ENABLED: bool = True
    # Approved Meta template names (needed outside the 24h window, e.g. after a
    # voice call). Empty = send plain text instead.
    WHATSAPP_TEMPLATE_BOOKED: str = ""
    WHATSAPP_TEMPLATE_RESCHEDULED: str = ""
    WHATSAPP_TEMPLATE_CANCELLED: str = ""
    WHATSAPP_TEMPLATE_LANGUAGE: str = "en"
    OUTBOX_TICK_SECONDS: float = 2.0

    # --- Human handoff (escalate_to_human) ---
    # WhatsApp-alert the staff marked "Receives handoff alerts" on the console's
    # Staff page when a patient asks for a person.
    STAFF_ALERTS_ENABLED: bool = True
    # Approved template for alerts outside the 24h window; empty = plain text.
    # Body params: {{1}} patient name, {{2}} patient phone, {{3}} channel,
    # {{4}} reason, {{5}} clinic name.
    WHATSAPP_TEMPLATE_ESCALATION: str = ""

    # --- Tracing (Langfuse, OpenTelemetry-based) ---
    # Off unless enabled AND both keys are set; the app never needs Langfuse to run.
    LANGFUSE_ENABLED: bool = False
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_BASE_URL: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("LANGFUSE_BASE_URL", "LANGFUSE_HOST"),
    )
    LANGFUSE_ENVIRONMENT: str = "development"  # e.g. development / staging / production
    # Redact phone numbers from trace payloads and hash them for user/session ids.
    LANGFUSE_MASK_PHONES: bool = True

    # --- App ---
    LOG_LEVEL: str = "INFO"
    PORT: int = 3000


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

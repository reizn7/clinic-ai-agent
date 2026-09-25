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
    MODEL: str = "gemini-2.5-flash"

    # --- Storage (MongoDB) ---
    MONGO_URI: str = Field(
        default="mongodb://localhost:27017",
        validation_alias=AliasChoices("MONGO_URI", "MONGODB_URI"),
    )
    MONGO_DB_NAME: str = "clinic-ai-agent"

    # Stamped on every conversations doc so a second clinic is a later change,
    # not a migration (see the console data contract).
    CLINIC_ID: str = "main-clinic"

    # --- App ---
    LOG_LEVEL: str = "INFO"
    PORT: int = 3000


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

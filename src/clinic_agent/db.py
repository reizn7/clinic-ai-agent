"""Shared MongoDB access.

A single lazily-created ``MongoClient`` reused across the app. Handlers are async,
so callers wrap the (synchronous) pymongo calls in ``asyncio.to_thread``.
"""

from __future__ import annotations

from functools import lru_cache

import certifi
from pymongo import MongoClient
from pymongo.database import Database

from clinic_agent.config import settings


@lru_cache
def get_db() -> Database:
    # uv's standalone Python ships no system CA bundle, so TLS to Atlas
    # (mongodb+srv) fails cert verification without an explicit CA file. certifi
    # provides one; harmlessly ignored for a non-TLS local mongodb:// URI.
    client: MongoClient = MongoClient(settings.MONGO_URI, tlsCAFile=certifi.where())
    return client[settings.MONGO_DB_NAME]

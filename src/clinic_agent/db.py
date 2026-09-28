"""Shared MongoDB access.

A single lazily-created ``MongoClient`` reused across the app. Handlers are async,
so callers wrap the (synchronous) pymongo calls in ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import certifi
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import OperationFailure

from clinic_agent.config import settings

log = logging.getLogger("clinic_agent.db")


@lru_cache
def get_db() -> Database:
    # uv's standalone Python ships no system CA bundle, so TLS to Atlas
    # (mongodb+srv) fails cert verification without an explicit CA file. certifi
    # provides one; harmlessly ignored for a non-TLS local mongodb:// URI.
    client: MongoClient = MongoClient(settings.MONGO_URI, tlsCAFile=certifi.where())
    return client[settings.MONGO_DB_NAME]


def create_index_safe(collection: Collection, keys: Any, **opts: Any) -> None:
    """Create an index, tolerating one that already exists with different options
    or a different name (common on a shared cluster). The query uses the keys,
    not the index name, so skipping a duplicate is harmless."""
    try:
        collection.create_index(keys, **opts)
    except OperationFailure as exc:
        if exc.code in (85, 86):  # IndexOptionsConflict / IndexKeySpecsConflict
            log.warning("index already exists (skipping): %s %s", keys, opts.get("name", ""))
            return
        raise

"""Shared fixtures."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from clinic_agent import observability

_DB_MODULES = [
    "clinic_agent.db",
    "clinic_agent.runtime.conversations",
    "clinic_agent.runtime.debounce",
    "clinic_agent.runtime.followups",
    "clinic_agent.runtime.history",
    "clinic_agent.runtime.outbox",
    "clinic_agent.tools.access_code",
    "clinic_agent.tools.catalog",
    "clinic_agent.tools.repo",
]


@pytest.fixture(autouse=True)
def _no_real_mongo(monkeypatch):
    """Safety net: every module's ``get_db`` points at an in-memory mongomock DB.

    The real database is shared with the live agent and the console, so no test
    may ever reach it. Tests that need specific data still patch ``get_db`` with
    their own fake; anything they don't patch lands here instead of in Atlas.
    """
    import importlib

    import mongomock

    fake = mongomock.MongoClient()["safety-net"]
    for name in _DB_MODULES:
        monkeypatch.setattr(importlib.import_module(name), "get_db", lambda: fake)
    return fake


@pytest.fixture(scope="session")
def _langfuse_memory():
    """ONE real Langfuse client for the whole run, exporting to memory.

    Langfuse installs the global OpenTelemetry tracer provider; a second client
    or a shutdown would leave later tests exporting nothing, so it is shared and
    never shut down. Phone masking is on, as in production.
    """
    from langfuse import Langfuse

    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        base_url="http://localhost:1",
        span_exporter=exporter,
        mask_otel_spans=observability._mask_otel_spans,
    )
    return client, exporter


@pytest.fixture
def langfuse_spans(_langfuse_memory, monkeypatch):
    """Enable tracing for one test; call the fixture value to get its finished spans."""
    client, exporter = _langfuse_memory
    exporter.clear()
    monkeypatch.setattr(observability, "_client", client)

    def spans():
        client.flush()
        return list(exporter.get_finished_spans())

    return spans

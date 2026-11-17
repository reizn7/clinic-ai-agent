"""Shared fixtures."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from clinic_agent import observability


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

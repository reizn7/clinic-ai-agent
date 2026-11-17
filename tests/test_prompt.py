from __future__ import annotations

import hashlib

from clinic_agent.prompt import (
    BASE_PROMPT,
    SYSTEM_PROMPT,
    VOICE_STYLE,
    VOICE_SYSTEM_PROMPT,
    WHATSAPP_STYLE,
    compose_prompt,
)

# sha256 of the shipped WhatsApp SYSTEM_PROMPT. Pinned so voice-prompt work can't
# change it by accident; update both values only for a deliberate WhatsApp change.
# Last deliberate change: workingDays rule, privacy/verify_caller section, tools
# no longer take a phone argument; handoff: already_requested, and on WhatsApp the
# AI stops after escalating (staff take over the chat in the console).
_PRE_SPLIT_SHA256 = "bbd811e58f674eff1486f337046f1c8c55f7f47e061e77f203e125661b83a38a"
_PRE_SPLIT_LEN = 7372


def test_whatsapp_prompt_unchanged() -> None:
    assert len(SYSTEM_PROMPT) == _PRE_SPLIT_LEN
    assert hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest() == _PRE_SPLIT_SHA256


def test_composition() -> None:
    assert compose_prompt(WHATSAPP_STYLE) == SYSTEM_PROMPT
    assert compose_prompt(VOICE_STYLE) == VOICE_SYSTEM_PROMPT
    assert "{" not in VOICE_SYSTEM_PROMPT  # every slot filled


def test_shared_core_rules_in_both() -> None:
    for core in [
        "TOOLS ARE THE ONLY SOURCE OF TRUTH",
        "# BOOKING FLOW",
        "Step 5 — BOOK",
        "Reschedule and cancel: use list_my_appointments",
        "# Escalation — escalate_to_human",
        "Never give medical advice",
    ]:
        assert core in BASE_PROMPT
        assert core in SYSTEM_PROMPT
        assert core in VOICE_SYSTEM_PROMPT


def test_voice_prompt_style() -> None:
    p = VOICE_SYSTEM_PROMPT
    assert "live voice call" in p
    assert "one moment, let me check" in p
    assert "spelling it back" in p
    assert "Hinglish" in p
    assert "call them back" in p
    assert "goodbye" in p
    # WhatsApp-only rules must not leak into voice
    assert "WhatsApp" not in p
    assert "Call tools silently" not in p
    assert '"- " bullets' not in p


async def test_build_agent_channels(monkeypatch) -> None:
    from clinic_agent.config import settings
    from clinic_agent.runtime.agent import build_agent
    from clinic_agent.tools import catalog

    # No catalogs configured → each channel's prompt is exactly its static prompt.
    monkeypatch.setattr(catalog, "prompt_section", lambda: "")
    wa = build_agent()
    assert wa.model == settings.MODEL
    assert await wa.instruction(None) == SYSTEM_PROMPT
    voice = build_agent("voice")
    assert voice.model == settings.VOICE_MODEL
    assert await voice.instruction(None) == VOICE_SYSTEM_PROMPT

    # A clinic's catalogs are appended at run time, after the pinned prompt.
    monkeypatch.setattr(catalog, "prompt_section", lambda: "# Clinic catalogs\n- lab_test")
    assert await wa.instruction(None) == SYSTEM_PROMPT + "\n\n# Clinic catalogs\n- lab_test"

    # A DB failure falls back to the plain prompt instead of failing the turn.
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(catalog, "prompt_section", boom)
    assert await voice.instruction(None) == VOICE_SYSTEM_PROMPT
    assert [t.__name__ for t in wa.tools] == [t.__name__ for t in voice.tools]

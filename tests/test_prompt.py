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

# sha256 of SYSTEM_PROMPT as shipped before the BASE/WHATSAPP/VOICE split. The
# WhatsApp agent's effective prompt must stay byte-for-byte identical.
_PRE_SPLIT_SHA256 = "dfa912ccfc3e3c7fc4dcc8ac1230ed0aef07d39967eaa4b2945c87fad0dc8b6a"
_PRE_SPLIT_LEN = 5848


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


def test_build_agent_channels() -> None:
    from clinic_agent.config import settings
    from clinic_agent.runtime.agent import build_agent

    wa = build_agent()
    assert wa.model == settings.MODEL
    assert wa.instruction == SYSTEM_PROMPT
    voice = build_agent("voice")
    assert voice.model == settings.VOICE_MODEL
    assert voice.instruction == VOICE_SYSTEM_PROMPT
    assert [t.__name__ for t in wa.tools] == [t.__name__ for t in voice.tools]

from clinic_agent.runtime.turn import contextualize


def test_no_history_includes_identity_and_date():
    out = contextualize("Hi", [], "919999999999")
    assert "919999999999" in out
    assert "Today's date is" in out
    assert out.strip().endswith("Hi")


def test_history_is_labelled_and_included():
    history = [
        {"role": "user", "content": "I want to book"},
        {"role": "assistant", "content": "Sure, with which doctor?"},
    ]
    out = contextualize("Dr. Asha", history, "911234567890")
    assert "Patient: I want to book" in out
    assert "Assistant: Sure, with which doctor?" in out
    assert "Dr. Asha" in out
    assert "911234567890" in out


def test_no_phone_omits_phone_line_but_keeps_date():
    out = contextualize("Hello", [], "")
    assert "WhatsApp number" not in out
    assert "Today's date is" in out
    assert out.strip().endswith("Hello")

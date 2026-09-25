from clinic_agent.runtime.turn import _looks_like_error


def test_looks_like_error_on_error_key():
    assert _looks_like_error({"error": "boom"}) is True


def test_looks_like_error_on_false_success():
    assert _looks_like_error({"success": False, "conflict": True}) is True


def test_looks_like_error_false_on_success():
    assert _looks_like_error({"success": True, "appointmentId": "x"}) is False
    assert _looks_like_error({"doctors": []}) is False
    assert _looks_like_error("not a dict") is False

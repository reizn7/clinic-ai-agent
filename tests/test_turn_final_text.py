from types import SimpleNamespace

from google.genai import types

from clinic_agent.runtime.turn import _final_text


def test_final_text_drops_reasoning_parts() -> None:
    event = SimpleNamespace(
        content=types.Content(
            role="model",
            parts=[
                types.Part(text="We need to call list_doctors.", thought=True),
                types.Part(text="Here are our doctors."),
            ],
        )
    )
    assert _final_text(event) == "Here are our doctors."

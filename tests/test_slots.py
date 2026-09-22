from clinic_agent.tools.slots import available_slots, generate_slots


def test_generate_slots_tiles_window():
    assert generate_slots({"start": "09:00", "end": "11:00"}, 30) == [
        "09:00",
        "09:30",
        "10:00",
        "10:30",
    ]


def test_generate_slots_partial_trailing_dropped():
    # 09:00-10:00 at 45m fits only one full slot (09:00), 09:45 would overrun.
    assert generate_slots({"start": "09:00", "end": "10:00"}, 45) == ["09:00"]


def test_generate_slots_empty_window():
    assert generate_slots({}, 30) == []
    assert generate_slots({"start": "09:00"}, 30) == []


def test_available_slots_removes_booked():
    allslots = ["09:00", "09:30", "10:00"]
    assert available_slots(allslots, ["09:30"]) == ["09:00", "10:00"]
    assert available_slots(allslots, []) == allslots

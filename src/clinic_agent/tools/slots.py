"""Slot generation from a doctor's working hours (ported from v1's slotGenerator).

A slot is an ``"HH:mm"`` start time; the working window is tiled into fixed-length
slots of ``duration_minutes``.
"""

from __future__ import annotations


def _to_minutes(hhmm: str) -> int:
    h, m = (int(x) for x in str(hhmm).split(":"))
    return h * 60 + m


def _to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def generate_slots(working_hours: dict, duration_minutes: int) -> list[str]:
    """All slot start-times for a window, e.g. {"start":"09:00","end":"17:00"}."""
    if not working_hours or not working_hours.get("start") or not working_hours.get("end"):
        return []
    duration = int(duration_minutes) or 30
    start = _to_minutes(working_hours["start"])
    end = _to_minutes(working_hours["end"])

    slots = []
    t = start
    while t + duration <= end:
        slots.append(_to_hhmm(t))
        t += duration
    return slots


def available_slots(all_slots: list[str], booked: list[str]) -> list[str]:
    taken = set(booked)
    return [s for s in all_slots if s not in taken]

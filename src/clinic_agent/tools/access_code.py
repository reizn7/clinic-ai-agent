"""Per-patient security codes: proof that a voice caller owns the number they typed.

A voice caller's number is self-entered on the call page, so on its own it proves
nothing. Each patient (one per phone per clinic) gets a 6-digit code that is only
ever delivered in the WhatsApp booking/reschedule confirmation sent to that
number. Quoting it back on a call proves the caller can read that WhatsApp chat.

The code is created the first time a confirmation is composed and then reused, so
every confirmation the patient has ever received carries the same, current code.
It is stored in plain text on the patient document (``accessCode``): a 6-digit
secret has too little entropy for a hash to protect it, and staff can read it out
to a patient they have identified some other way.

Brute force is bounded by a lockout: ``MAX_FAILURES`` wrong codes for a number
lock it for ``LOCK_MINUTES``, across calls (the per-call cap lives in the tool).
"""

from __future__ import annotations

import hmac
import secrets
from datetime import UTC, datetime, timedelta

from bson import ObjectId

from clinic_agent.config import settings
from clinic_agent.db import get_db

CODE_LENGTH = 6
MAX_FAILURES = 5
LOCK_MINUTES = 60


def _new_code() -> str:
    return f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}"


def ensure_access_code(patient_id: ObjectId) -> str | None:
    """The patient's code, creating one if they have none yet. ``None`` if no patient."""
    col = get_db()["patients"]
    scope = {"clinicId": settings.CLINIC_ID, "_id": patient_id}
    doc = col.find_one(scope, {"accessCode": 1})
    if not doc:
        return None
    if doc.get("accessCode"):
        return doc["accessCode"]
    # Set only if still missing, so two concurrent confirmations agree on one code.
    col.update_one(
        {**scope, "accessCode": {"$exists": False}},
        {"$set": {"accessCode": _new_code(), "accessCodeCreatedAt": datetime.now(UTC)}},
    )
    return (col.find_one(scope, {"accessCode": 1}) or {}).get("accessCode")


def _clean(code: str) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())


def verify(phone: str, code: str) -> dict:
    """Check ``code`` against the patient on ``phone``.

    Returns ``{"verified": True}`` or ``{"verified": False, "reason": ...}`` where
    reason is ``invalid`` or ``locked``. An unknown number, a patient without a
    code, and a wrong code all read as ``invalid``, so the answer does not reveal
    whether a number belongs to a patient.
    """
    col = get_db()["patients"]
    patient = col.find_one({"clinicId": settings.CLINIC_ID, "phone": phone})
    if not patient:
        return {"verified": False, "reason": "invalid"}

    now = datetime.now(UTC)
    locked_until = patient.get("accessCodeLockedUntil")
    if locked_until is not None:
        if locked_until.tzinfo is None:  # pymongo returns naive UTC by default
            locked_until = locked_until.replace(tzinfo=UTC)
        if locked_until > now:
            return {"verified": False, "reason": "locked"}

    stored = patient.get("accessCode") or ""
    if stored and hmac.compare_digest(stored, _clean(code)):
        col.update_one(
            {"_id": patient["_id"]},
            {
                "$set": {"accessCodeFailures": 0},
                "$unset": {"accessCodeLockedUntil": ""},
            },
        )
        return {"verified": True}

    failures = int(patient.get("accessCodeFailures") or 0) + 1
    update: dict = {"accessCodeFailures": failures}
    if failures >= MAX_FAILURES:
        update = {
            "accessCodeFailures": 0,
            "accessCodeLockedUntil": now + timedelta(minutes=LOCK_MINUTES),
        }
    col.update_one({"_id": patient["_id"]}, {"$set": update})
    return {"verified": False, "reason": "locked" if failures >= MAX_FAILURES else "invalid"}

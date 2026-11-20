"""Outbox: follow-up jobs (calendar sync, WhatsApp confirmations, staff replies,
failure reports).

A booking tool writes the appointment and, in the same breath, queues jobs here.
The worker (``run_forever``, started in the app lifespan) delivers them, so a slow
or failing Google / Meta call never blocks or breaks the patient's conversation,
and nothing is lost on a crash.

Job lifecycle (``status``)::

    held ──release()──► pending ──claim──► processing ──► done | skipped
                          ▲                     │
                          └──── retry (backoff) ┘──► failed   (permanent error / out of retries)
    (unsent notify jobs replaced by a newer change for the same appointment → superseded)

**Holding.** A WhatsApp confirmation must arrive *after* the agent's own reply
(WhatsApp turn) or after the call (voice). The turn/call wraps its work in
``held(token)``; jobs created inside are ``held`` and become ``pending`` when the
block exits — i.e. after the reply was sent / the call ended. Held jobs nobody
released (a crash) are released after ``HOLD_MAX``. Calendar jobs are never held.

Handlers live in ``followups``; they re-read the appointment from the database
when they run, so what gets sent always reflects what is actually stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId

from clinic_agent import observability
from clinic_agent.config import settings
from clinic_agent.db import create_index_safe, get_db

log = logging.getLogger("clinic_agent.runtime.outbox")

COLLECTION = "outbox"
MAX_ATTEMPTS = 6
LEASE = timedelta(minutes=2)
HOLD_MAX = timedelta(minutes=60)
_BATCH = 20

CALENDAR_SYNC = "calendar.sync"
WHATSAPP_NOTIFY = "whatsapp.notify"
# Inserted by the staff console: a staff member's WhatsApp reply (runtime.escalation).
STAFF_REPLY = "staff.reply"
# Inserted by the staff console (Analytics → "Send failures report"): analyse the
# failed conversations in a date range and email the report (runtime.failure_report).
FAILURE_REPORT = "failures.report"

# Jobs that take minutes run on their own worker loop with a long lease, so they
# never hold up patient confirmations and a lease can't expire mid-run (which
# would let the job be claimed again and the email sent twice).
LONG_RUNNING = frozenset({FAILURE_REPORT})
LONG_LEASE = timedelta(minutes=30)

_UNSENT = ["held", "pending"]
# Langfuse trace names (stable, verb-first: dashboards filter on them).
_TRACE_NAMES = {
    CALENDAR_SYNC: "sync-doctor-calendar",
    WHATSAPP_NOTIFY: "send-appointment-confirmation",
    STAFF_REPLY: "send-staff-reply",
    FAILURE_REPORT: "send-failure-report",
}

_hold: contextvars.ContextVar[str | None] = contextvars.ContextVar("outbox_hold", default=None)


class PermanentError(Exception):
    """A job failure retrying can't fix (bad config, rejected by the API)."""


# ---- holding ------------------------------------------------------------------


@contextlib.contextmanager
def held(token: str) -> Iterator[str]:
    """Hold notification jobs created inside this block until it exits."""
    reset = _hold.set(token)
    try:
        yield token
    finally:
        _hold.reset(reset)
        try:
            release(token)
        except Exception:
            log.exception("could not release held jobs for %s (released after %s)", token, HOLD_MAX)


def release(token: str) -> int:
    now = _now()
    res = get_db()[COLLECTION].update_many(
        {"clinicId": settings.CLINIC_ID, "status": "held", "hold": token},
        {"$set": {"status": "pending", "nextAttemptAt": now, "updatedAt": now}},
    )
    return res.modified_count


# ---- enqueue (called from the booking repo, sync) --------------------------------


def ensure_indexes() -> None:
    col = get_db()[COLLECTION]
    create_index_safe(col, [("clinicId", 1), ("status", 1), ("nextAttemptAt", 1)], name="due")
    create_index_safe(col, [("appointmentId", 1), ("kind", 1), ("status", 1)], name="by_appt")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _job(kind: str, appointment_id: ObjectId | None, *, status: str, **extra: Any) -> dict:
    now = _now()
    return {
        "clinicId": settings.CLINIC_ID,
        "kind": kind,
        "appointmentId": appointment_id,
        "status": status,
        "attempts": 0,
        "nextAttemptAt": now,
        "createdAt": now,
        "updatedAt": now,
        **extra,
    }


def enqueue_appointment_change(appointment_id: ObjectId, event: str) -> None:
    """Queue the follow-ups for one appointment change.

    ``event`` is ``booked`` | ``rescheduled`` | ``cancelled``. Never raises: the
    appointment is already saved, and a queueing problem must not turn a
    successful booking into an error for the patient.
    """
    try:
        _enqueue(appointment_id, event)
    except Exception:
        log.exception("could not queue follow-ups for appointment %s (%s)", appointment_id, event)


def _enqueue(appointment_id: ObjectId, event: str) -> None:
    col = get_db()[COLLECTION]
    if settings.GOOGLE_CALENDAR_ENABLED:
        # One idempotent "make the calendar match the appointment" job is enough;
        # skip if one is already waiting for this appointment.
        waiting = col.find_one(
            {"appointmentId": appointment_id, "kind": CALENDAR_SYNC, "status": "pending"}
        )
        if not waiting:
            col.insert_one(_job(CALENDAR_SYNC, appointment_id, status="pending"))

    if not settings.WHATSAPP_CONFIRMATIONS_ENABLED:
        return
    # Collapse unsent messages about the same appointment into one accurate one:
    # booked+rescheduled before sending → a single "booked" (with the new time);
    # booked+cancelled → nothing at all.
    unsent = list(
        col.find(
            {"appointmentId": appointment_id, "kind": WHATSAPP_NOTIFY, "status": {"$in": _UNSENT}}
        )
    )
    if unsent:
        col.update_many(
            {"_id": {"$in": [j["_id"] for j in unsent]}},
            {"$set": {"status": "superseded", "updatedAt": _now()}},
        )
        if any(j.get("event") == "booked" for j in unsent):
            if event == "cancelled":
                return
            event = "booked"
    hold = _hold.get()
    col.insert_one(
        _job(
            WHATSAPP_NOTIFY,
            appointment_id,
            status="held" if hold else "pending",
            event=event,
            **({"hold": hold} if hold else {}),
        )
    )


def _subject(job: dict) -> str:
    """What a job is about, for logs and traces."""
    if job.get("appointmentId") is not None:
        return str(job["appointmentId"])
    return str(job.get("conversationId") or job["_id"])


# ---- worker -----------------------------------------------------------------------


def _claim_sync(long_running: bool = False) -> dict | None:
    """Claim the next due job: a long-running one, or (default) any other kind."""
    now = datetime.now(UTC)
    kinds = sorted(LONG_RUNNING)
    lease = LONG_LEASE if long_running else LEASE
    return get_db()[COLLECTION].find_one_and_update(
        {
            "clinicId": settings.CLINIC_ID,
            "kind": {"$in": kinds} if long_running else {"$nin": kinds},
            "$or": [
                {"status": "pending", "nextAttemptAt": {"$lte": now.isoformat()}},
                {"status": "processing", "leaseUntil": {"$lt": now.isoformat()}},  # crashed
            ],
        },
        {
            "$set": {
                "status": "processing",
                "leaseUntil": (now + lease).isoformat(),
                "updatedAt": now.isoformat(),
            },
            "$inc": {"attempts": 1},
        },
        sort=[("nextAttemptAt", 1)],
        return_document=True,
    )


def _finish_sync(job: dict, status: str, **fields: Any) -> None:
    get_db()[COLLECTION].update_one(
        {"_id": job["_id"]},
        {"$set": {"status": status, "updatedAt": _now(), **fields}, "$unset": {"leaseUntil": ""}},
    )


def _retry_sync(job: dict, error: str) -> str:
    attempts = job.get("attempts", 1)
    if attempts >= MAX_ATTEMPTS:
        _finish_sync(job, "failed", lastError=error)
        return "failed"
    delay = timedelta(seconds=30 * 2 ** (attempts - 1))  # 30s, 1m, 2m, 4m, 8m
    _finish_sync(
        job, "pending", lastError=error, nextAttemptAt=(datetime.now(UTC) + delay).isoformat()
    )
    return "retry"


def _release_stale_holds_sync() -> int:
    cutoff = (datetime.now(UTC) - HOLD_MAX).isoformat()
    res = get_db()[COLLECTION].update_many(
        {"clinicId": settings.CLINIC_ID, "status": "held", "createdAt": {"$lt": cutoff}},
        {"$set": {"status": "pending", "nextAttemptAt": _now(), "updatedAt": _now()}},
    )
    return res.modified_count


Handler = Callable[[dict], Awaitable[tuple[str, dict]]]


async def process_one(handlers: dict[str, Handler], *, long_running: bool = False) -> bool:
    """Claim and run one due job. Returns False when nothing was due."""
    job = await asyncio.to_thread(_claim_sync, long_running)
    if job is None:
        return False
    handler = handlers.get(job["kind"])
    if handler is None:
        await asyncio.to_thread(_finish_sync, job, "failed", lastError="no handler")
        return True
    with observability.trace_root(
        _TRACE_NAMES.get(job["kind"], job["kind"]),
        channel="followup",
        input={"subject": _subject(job), "event": job.get("event")},
        metadata={"attempt": job.get("attempts"), "job_id": str(job["_id"])},
    ) as obs:
        try:
            status, fields = await handler(job)
            await asyncio.to_thread(_finish_sync, job, status, **fields)
            obs.update(output={"status": status, **{k: str(v) for k, v in fields.items()}})
            log.info("outbox %s %s → %s", job["kind"], _subject(job), status)
        except PermanentError as exc:
            await asyncio.to_thread(_finish_sync, job, "failed", lastError=str(exc))
            obs.update(level="ERROR", status_message=str(exc)[:300])
            log.error("outbox %s %s failed: %s", job["kind"], _subject(job), exc)
        except Exception as exc:
            outcome = await asyncio.to_thread(_retry_sync, job, repr(exc))
            obs.update(level="WARNING", status_message=f"{outcome}: {exc!r}"[:300])
            log.warning("outbox %s %s %s: %r", job["kind"], _subject(job), outcome, exc)
    return True


async def run_once(handlers: dict[str, Handler], *, long_running: bool = False) -> int:
    if not long_running:
        await asyncio.to_thread(_release_stale_holds_sync)
    done = 0
    while done < _BATCH and await process_one(handlers, long_running=long_running):
        done += 1
    return done


async def run_forever(*, long_running: bool = False) -> None:
    """Loop until cancelled; one tick failing never kills the loop.

    The app runs two: the regular worker, and one for ``LONG_RUNNING`` kinds.
    """
    from clinic_agent.runtime import followups

    log.info(
        "outbox worker%s started (every %ss)",
        " (long-running jobs)" if long_running else "",
        settings.OUTBOX_TICK_SECONDS,
    )
    while True:
        try:
            await run_once(followups.HANDLERS, long_running=long_running)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("outbox tick failed")
        await asyncio.sleep(settings.OUTBOX_TICK_SECONDS)

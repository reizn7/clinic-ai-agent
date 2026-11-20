# Failures report: console ↔ agent contract

The Analytics page's **Send failures report** button asks the agent to analyse
every failed conversation in the selected date range and email a root-cause
report. The console never calls the agent over HTTP: it inserts an outbox job
and watches it, the same way staff replies (`staff.reply`) work.

## 1. On click: insert one job

Collection `outbox`:

```json
{
  "clinicId": "main-clinic",
  "kind": "failures.report",
  "status": "pending",
  "attempts": 0,
  "nextAttemptAt": "2026-11-20T09:00:00.000+00:00",
  "createdAt": "2026-11-20T09:00:00.000+00:00",
  "updatedAt": "2026-11-20T09:00:00.000+00:00",
  "rangeStart": "2026-11-12T18:30:00.000Z",
  "rangeEnd": "2026-11-19T18:30:00.000Z",
  "requestedBy": "<staff user id>"
}
```

- `rangeStart` / `rangeEnd`: the page's selected range as ISO 8601 **with a
  timezone** (`Z` or an offset). The end is **exclusive**: for "13–19 Nov" in
  IST send `2026-11-12T18:30Z` → `2026-11-19T18:30Z`. Conversations that
  *started* in the range are included; ones still active are skipped.
- `nextAttemptAt`, `createdAt` and `updatedAt` are ISO strings, like the rest of
  the outbox.
- Use the same `clinicId` the agent runs with (`CLINIC_ID`).
- **One at a time:** before inserting, check for a `failures.report` job for the
  clinic with `status` in `pending` / `processing`. If there is one, show
  "A report is already being prepared" instead of queueing another.

## 2. While it runs: show progress

Poll the job by `_id` (every ~3 s is plenty):

| `status` | Show |
|---|---|
| `pending` / `processing` | "Preparing report…" (button disabled). Can take a few minutes. |
| `done` | "Report sent to {report.emailedTo}" plus counts from `report` |
| `failed` | "Couldn't send the report", with `lastError` for admins |

When `done`, the job has:

```json
"report": { "failed": 12, "analysed": 12, "notAnalysed": 0, "emailedTo": "…" }
```

`failed` = failed conversations in the range. `notAnalysed` > 0 means some went
past the per-report limit (`FAILURE_REPORT_MAX_CASES`, default 50) or their
analysis errored; those are still listed in the email.

Retries are automatic. If the email server can't be reached, the job goes back to
`pending` with a backoff, up to 6 attempts. Analyses already made are reused, so
retrying costs nothing extra.

## 3. What counts as failed

A finished conversation is included if any of these hold:

- the patient tried to book or reschedule and didn't end up with an appointment
- a turn crashed and the patient got the fallback reply
- a tool call errored
- it was handed to staff
- it ended with no resolution after more than one patient message
- analytics rated the patient's sentiment negative

The model then decides whether the agent was at fault. "No free slot" or "the
patient stopped replying" are reported as not the agent's fault.

## 4. Stored analyses (optional for the console)

Each analysis is kept in `failure_reports`, one per conversation:
`{clinicId, conversationId, channel, signals[], analysis{category, agent_at_fault,
severity, summary, root_cause, evidence[], suggested_fix, fix_area}, model,
conversationEndedAt, createdAt, updatedAt}`. The console could show these on the
conversation page later. The email doesn't depend on that.

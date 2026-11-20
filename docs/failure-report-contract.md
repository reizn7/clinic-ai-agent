# Failures report: console ↔ agent contract

The Analytics page's **Send failures report** button (admins only) asks the
agent to analyse failed conversations and email a root-cause report. There are
two scopes: every failed conversation in the selected date range, or only the
latest N failed conversations from any date, which is quicker. The console never calls the agent over HTTP: it inserts an outbox job
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
  "mode": "range",
  "rangeStart": "2026-11-12T18:30:00.000Z",
  "rangeEnd": "2026-11-19T18:30:00.000Z",
  "requestedBy": "<staff user id>"
}
```

or, for the latest N failed conversations:

```json
{ "...same fields...": "", "mode": "latest", "limit": 10 }
```

- `mode`: `"range"` (the default when it's missing) or `"latest"`.
- `limit` (latest only): 1 to `FAILURE_REPORT_MAX_CASES` (default 50). The
  agent looks back through at most the last 2,000 finished conversations.
- `rangeStart` / `rangeEnd` (range only): the page's selected range as ISO 8601 **with a
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
"report": { "scope": "13 Nov – 19 Nov 2026", "failed": 12, "analysed": 12,
            "notAnalysed": 0, "emailedTo": "…" }
```

`scope` is the human-readable scope, e.g. `latest 10 failed conversations`.

`failed` = failed conversations in the range. `notAnalysed` > 0 means some went
past the per-report limit (`FAILURE_REPORT_MAX_CASES`, default 50) or their
analysis errored; those are still listed in the email.

Each case in the email links to the conversation in the console
(`CONSOLE_BASE_URL/conversations/<_id>`) and to its Langfuse session.

**Speed.** Each case is one model call. If a model runs out of quota (429) or is
down (5xx), the agent falls back to the next Gemini model (`FAILURE_REPORT_MODEL`,
then `MODEL`, then `MODEL_FALLBACKS`) and skips the exhausted one for 10 minutes.
On a free-tier key, the per-minute limits are what make a long range slow.

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

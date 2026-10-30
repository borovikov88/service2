# Event wakeup for the durable CRM inbox — not activated

After a verified live incoming event commits, an opt-in callback starts a detached,
bounded OS process. The callback waits at most 100 ms for the short dispatch handoff and performs no delivery, AI, provider requests or
Git network checks. The process invokes the existing guarded host intake wrapper;
its exact-main, clean checkout, deploy/intake locks, explicit organization 1,
activation/cutoff, source access and lease/idempotency checks remain authoritative.
Only private opaque wake generations are written under the existing host `tmp`.
No communication payload, phone, account ID or credential is stored there.

`CRM_INTAKE_EVENT_WAKEUP_ENABLED` defaults to false. This package does not change
production configuration, scheduler, cohort or credentials. No migration.

A wake is not delivery. Process startup failure, a dispatch mutex stalled beyond its 100 ms budget,
deploy/release blocking, a crash, future retry or an exhausted bounded batch can
leave SQL rows pending. The existing independent periodic reconciliation worker
must remain and a reliable same-host periodic invocation must be demonstrated
before activation. GitHub cron has no minute-level SLA. No existing host intake
schedule has been verified; deployment does not install one. Hosting-panel cron
is a possible existing operational route, but registration and observed successful
ticks still require authorization and evidence. Do not invent a Passenger thread
that runs business processing or claim a new persistent access path exists.

Worker generation coalescing handles events arriving during processing. Lock
handoff is serialized with generation comparison. A process is limited by an
120-second outer processing budget and remaining-budget subprocess timeouts
plus bounded process-group cleanup; backlog is preserved for recovery, never mass requeued.
The existing inbox lease expires after a crash. Existing retry timing is retained.

The UI's timestamp is now labelled “Последняя попытка обработки”: the command
writes it even on failure. It is not source freshness or successful-delivery proof.
For incident diagnosis request only sanitized schedule timestamps/result codes,
activation last error, pending/retry/expired-lease counts and oldest due time,
and bounded latest source decision codes. Do not request client payloads.

This does not solve semantic deduplication between distinct phone calls. Calls
remain keyed by call identity; messages retain the existing verified conversation
link. Never attach to the latest deal merely because a phone matches. No live
backfill, requeue, source import or historical guard override is part of activation.

Rollback: disable the opt-in setting; keep inbox rows and existing periodic worker.
Do not delete pending data, reset cutoffs or reverse any historical migration.

## Host fallback: verified capability versus missing runtime evidence

Public Beget documentation confirms panel CronTab supports arbitrary/Bash commands
and minute schedules: https://beget.com/ru/kb/manual/crontab . Repository deployment
notes record `crontab_unavailable` on this host and the panel route for Avito.
That is evidence of a supported setup route, not evidence of a registered CRM job.
GitHub explicitly permits scheduled-run delays/drops under load:
https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule .
The existing CRM workflow requests every five minutes; it is not a five-minute SLA.

Minimal proposed panel setup, NOT executed: one job under the existing deployment
Unix user, once per minute, invoking the existing guarded intake wrapper with the
canonical app path and installed HEAD. Generate the fully shell-quoted command
from verified runtime paths; do not paste guessed paths into the panel. Set private
umask and the existing HOME/PATH required by git/SSH, absolute Bash/Python paths,
and bounded runtime. Keep exact origin/main, clean checkout, intake/deploy locks
and organization 1 guards unchanged. Do not append an unbounded `&` launcher.
Use private same-host bounded receipts, no email containing source data, no CI
artifact exports. A successful `busy` exit is NOT a successful intake pass.

Before authorizing activation, collect these bounded read-only facts through an
already authorized host/panel route (currently unavailable to this executor):

1. Actual panel entry: enabled state, schedule, Unix identity, canonical executable
   and app paths; redact secrets and unrelated jobs. Verify no duplicate CRM entry.
2. Installed SHA and branch cleanliness; executable virtualenv, Bash, flock,
   timeout, Git; existing same-host temp-directory ownership/permissions.
3. Last several scheduled start/end UTC timestamps and sanitized exit/result
   codes: complete/error/busy/deploying/release_pending/timeout. Include a sequence
   of at least three expected ticks; an old log file alone is insufficient.
4. Activation flags/cutoff and `last_run_at`/`last_error`, no changes. Aggregated
   due/retry/expired-lease presence and oldest due age, scoped to organization 1,
   observed at two timestamps. This is privileged operations evidence, not a new
   team-visible UI count. Use existing org/state/due index and capped samples;
   no source bodies/phones/account identifiers or full-table dump.
5. For a naturally occurring eligible event, compare persisted receipt, inbox
   enqueue, claim/decision and CRM linkage times under existing privacy. Do not
   create a test production call, replay historical events or force requeue.

Only after explicit setup permission: register exactly one panel job, read back
its configuration, observe real ticks and bounded queue progress, then separately
approve publication/activation of event wakeup. A minute fallback plus after-commit
wake avoids relying on GitHub schedule latency, but is not a guaranteed delivery
SLA during deployment, overload, provider failures or process termination.

## UI status limits (no schema changes)

The existing activation row stores only the latest attempt timestamp and error.
When that attempt succeeded, its time is a known successful bounded pass. After a
failure, the earlier successful timestamp is unavailable; do not invent it.
A successful pass can leave a backlog because limits/retries are intentional.
The UI returns no inbox counts/source metadata and adds no queue scan. Unlinked
inbox rows do not have a reusable permission-filtered team list; exposing global
organization counts would leak private/inaccessible activity. A future exact
backlog view needs a reviewed source authorization query and query-plan evidence.
Persisting last-success history would require a separately reviewed additive
schema change; this minimal local package does not add one.

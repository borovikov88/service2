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

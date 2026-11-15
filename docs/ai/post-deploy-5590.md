# Addressed post-deploy preparation for 5590e16a

This is a local preparation package, not authorization to execute it. The
application candidate 21b2a7fc and the separate b803f9cb control package remain
unchanged. No application, migration, normalization, worker or advertisement
publication code changes here.

## Publication without moving installed HEAD or main

Do **not merge this branch into main**. A merge changes main and intentionally
makes this package and the existing outbox setup refuse to run. The intended
future transport is the existing `ci-deploy.yml` workflow, dispatched on the
single reviewed `codex/service2-post-deploy-5590` branch. That workflow already
exists on the default branch with workflow_dispatch. GitHub supports selecting
a different branch for a manual run:
https://docs.github.com/actions/managing-workflow-runs-and-deployments/managing-workflow-runs/manually-running-a-workflow

Branch publication alone does not match the main-only push trigger. A PR can
run tests but cannot deploy. Manual `release_mode=post-deploy` skips the scope
job and therefore both ordinary Service2 and calculator deployment jobs.
The addressed job independently requires the exact control branch, reviewed
control SHA, fixed installed/main SHA, and a control-only whole-tree diff.
No mixed application diff exemption exists. Existing production environment
protection/allowed-branch policy still applies. If it denies this branch, stop;
do not change protection, impersonate main, or dispatch an ordinary deploy.

After explicit execution approval and review of the final SHA: publish just
that control branch (no force push/merge); use Actions > Test and deploy main >
Run workflow > select that branch. Supply `release_mode=post-deploy`,
`confirm_main_sha=5590e16a00134a2cf20e0fe47f90e08b0b0c2115`, exact reviewed
`confirm_control_sha`, and **one** post_operation. Confirm the actual run SHA
before any environment approval. This path has not been exercised remotely.

## Operations and receipts

* `status`: existing `monitor_avito_statuses --status`, six bounded aggregate
  counters; no provider request, scan, replay or setup. It observes runtime
  configuration; it does not prove successful worker processing.
* `mcp`: unchanged activation helper, Passenger restart only if configured,
  HTTPS live/ready checks, exact dedicated issuer/resource/scope metadata, then
  unchanged finalization helper. An already-current marker still requires live
  metadata. Wrong metadata does not erase the marker or bypass the helper.
  A smoke failure leaves the pending state and reports completed steps; no
  automatic rollback or retry. Passenger may need time to restart; inspect the
  receipt before a separately approved retry.
* `avito-outbox`: unchanged scoped manager `setup`, preserving account binding,
  operator opt-out/cutoff and current-main guards. May install a minute cron
  and allow the existing worker to process previously authorized outgoing
  replies. This entrypoint never runs a tick or creates/sends a test reply.
  `installed`/`present` is schedule configuration; `panel_setup_required` and
  `disabled` are explicit outcomes, not worker success.
* `avito-services`: unchanged `configure_avito_services_workplace`, which can
  enable scoped reports, register the webhook using its durable attempt/readback
  guard, run the signed storage probe and refresh existing setup evidence.
  It does not purchase advertising or publish listings. Partial webhook/storage
  or known additional failure is a blocked receipt, even if setup=applied.
  Missing/unknown evidence instead produces explicit `status=partial`, exit 3,
  `reason=private_review_required`: core setup/webhook/storage were confirmed,
  but not all supplemental observations. This intentionally does not make an
  overall readiness claim. Already-checked/attempted envelopes are interpreted
  using nested saved results; the attempt alone is not confirmation. A partial
  run requires private evidence review, not an automatic setup retry.
  No raw component evidence is published.
  No automatic retry of an ambiguous provider write.

The host owns a 450-second deadline and TERM/HUP cleanup, independently of the
540-second SSH capture. It acquires existing AI/deploy shared locks in that order and a separate
exclusive addressed-writer lock. Existing helpers retain their own narrow
locks. The outbox manager's child process supervision is adapted in memory to
the bounded capture (both modules' bounded calls and the direct crontab run);
its business guards and installed source files are unchanged. Lock files are
checked for symlinks, ownership and replacement. Source,
installed SHA and both remote refs are checked before each top-level operation
and after successful completion. These are cooperative locks and repeated observations,
not an atomic transaction with GitHub refs or an adversarial filesystem proof.
An unrelated actor can change remote main during a helper; the final guard then
fails with already completed operations reported. It cannot undo provider work.

Raw child stdout/stderr is bounded in memory, never echoed or uploaded. Public
receipts contain only exact source/run bindings, fixed phase/category/result
enums and allowlisted counters/booleans. SSH exit 255 means transport failure;
it does **not** establish timeout vs authentication. Other invalid receipts are
unclassified, not assumed transport or application failures. Without a valid final
remote receipt, execution is uncertain: stop, do not retry automatically.
A validated remote receipt distinguishes a guard from an application boundary.
The earlier CRM diagnostic run's generic unavailable error remains unexplained;
this package does not reinterpret or rerun it.

## UI alternatives and subsequent verification

The authenticated connection-4 page's “Исходящие Авито” section exposes worker
history and, for the authorized owner, the exact current hosting cron command.
In Beget Cron/Планировщик, inspect existing jobs first. Only if that scoped job
is absent and configuration is available, add the displayed command once at
the displayed minute schedule. This resumes processing of authorized replies;
do not create test messages. Observe two natural periodic completions in the
private Service2 UI, distinguishing an empty queue from an accepted reply.

There is no existing UI for the three MCP environment flags or the full
services setup helper. Public metadata is read-only. `/avito/` and the private
connection page can verify saved state but cannot substitute for missing setup.
Do not run a full deploy to reach these post-deploy steps.

Recorded production facts supplied independently: health run 43690421957 on
5590e16a passed live/ready; diagnostic run 43690453039 failed unavailable with
cause unknown; public MCP metadata still showed the root issuer. These are
not execution evidence for this new package. A configured schedule, a launched
process and successful processing require separate evidence.

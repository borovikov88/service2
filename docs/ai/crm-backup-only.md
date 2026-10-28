# Backup-only prerequisite for CRM migration0159

Local proposal. Publication, merge and production execution require separate
approval. This branch starts at main5590943 and contains no0159 or need-deal code.
PR451 stays unchanged. No new credential, account, grant or scheduler is needed.

## Existing route and minimal addition

The existing `crm_pilot_db_backup_remote.sh` and `crm_pilot_db_backup.py` already
perform a bounded private backup without deployment or migration. However, the
only existing Actions entry point is the marker-triggered *post-deploy* step.
It cannot protect a migration in that same release. An already authorized hosting
operator could run the existing runner on the exact installed/current main; this
agent has no supported authenticated host-console route and does not bypass SSH
or environment guards to do so.

The proposed manual `crm-db-backup.yml` reuses the same production Environment,
existing deploy identity, pinned known host, remote runner and sanitized receipt
validator. It checks dispatch/main/repository, exact confirmed/current SHA and
explicit full-database scope before configuring SSH. The remote runner checks
installed HEAD/current origin main again; it never updates checkout. No deploy,
restart, migrate, restore or artifact upload appears in this workflow.

Publishing this workflow is a separate prerequisite release without0159, through
normal CI/review. The repository's existing push workflow may deploy that
prerequisite main commit; the backup-only dispatch itself never deploys. Once
that prerequisite is installed, use its exact main SHA for the dispatch. Do not
publish/merge451 first to make the runner available. Rebase/synchronize451 after
the prerequisite changes main and repeat its exact-head CI/review.

## Explicit scope and data handling

Confirmation is `all-organizations-default-mysql`: the entire default MySQL
database visible to the existing app account, across **all organizations**. This
is not an organization-filtered export. Every visible base table must be InnoDB;
visible views, triggers, routines or events reject the operation. The helper
verifies exact visible CREATE TABLE coverage and schema fingerprints before and
after dumping. Hidden privileged objects, other databases, MySQL users/grants,
uploaded media, settings/secrets, code, external1C/provider data and off-host
disaster recovery are not covered. No table names, rows or SQL are sent to CI.

The existing canonical exclusive `service2-deploy.lock` excludes guarded deploy
and intake execution; the intake worker holds the same lock shared. Contention
fails closed. Ordinary application DML can continue under native InnoDB consistent
snapshot; metadata locks retain existing tables. Arrange no out-of-band DDL.
There is no new lock order or scheduler change.

The native client options file is0600 inside a0700 temporary host directory and
is removed on exit. Archives/receipts remain0600 in the existing private host
directory `../tmp/service2-crm-backups` (0700), outside the checkout/web root.
Only aggregate allowlisted proof returns to CI. SSH stdout/stderr are captured
privately and deleted; failure messages pass the existing allowlist. No GitHub
artifact, Library upload or transfer of customer data to a new server is used.

## Procedure after separate authorization

1. Verify reviewed prerequisite/main SHA is installed and clean; all required CI
   passed. Confirm the intended database scope is all organizations, not one org.
2. Dispatch **Private CRM database backup only** on main with `confirm_main_sha`
   equal to that exact SHA and `confirm_scope=all-organizations-default-mysql`.
   Keep normal production Environment approvals and guards. Do not trigger the
   deployment recovery workflow as a substitute.
3. Require terminal success and a new allowlisted receipt from this run, with
   expected SHA matching the installed/current main, UTC start/completion inside
   this run, positive bounded counts/sizes, native successful completion,
   stable schema, gzip readback and SHA256, `integrity_verified=true`. An old
   file or old success log alone is insufficient. Retain the private host copy.
4. Schedule this as the immediate prerequisite to451, with no unrelated release
   or schema change between backup and migration. No arbitrary age/SLA is
   invented: record actual completion time and intervening changes. Reconfirm
   coverage/freshness and rerun the same guarded backup if scope/schema changes
   or the release is postponed; a fresh run is not an automatic background job.
5. This is integrity/coverage proof, **not a production restore test**:
   `restore_verified=false` is honest and not by itself a failure of this additive
   release gate. Existing synthetic MySQL CI proves native restore on disposable
   synthetic data, not restoration of this production copy. No safe existing
   isolated production-data restore target has been confirmed. Do not create a
   database/user/grants or copy data elsewhere to manufacture restore proof.
6. Owner decides whether the stated integrity/coverage evidence and lack of a
   production restore rehearsal are sufficient. If actual restore proof is
   required, obtain a separately authorized existing isolated target and plan;
   never restore over production or claim the integrity check proves recovery.

Failures (SHA mismatch, busy lock, missing native tool, unsupported schema,
timeout/caps, dump exit, schema drift, corrupt gzip or invalid receipt) block451.
Use only sanitized diagnostics; do not weaken guards or print raw captures.

## Roll-forward / application rollback

Prefer a reviewed forward fix. After conversions exist, retain0159 tables and
their migration history, commands, deals, tasks, bindings and permanent claims.
Any application revert must be schema-compatible and pass preservation tests;
never reverse0159, truncate tables or restore an older backup over later work
without a separate recovery decision. This workflow offers no restore action.

## Validation and next authorization

Offline tests cover dispatch/scope/SHA rejection, secret-safe guard output,
workflow boundaries and the existing private backup/receipt/locking behavior.
Existing CI actionlint now includes this workflow. Local absence of native MySQL
is reported as a limit, not a restore PASS. No production operation has run.

Minimal next request: approve publication/normal prerequisite release of this
backup-only package (without0159), then one explicit full-default-database private
backup run using existing credentials; accept or reject the documented
integrity-only production evidence. PR451 remains gated on that result and its
refreshed checks after the prerequisite main change.

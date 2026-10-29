# Private backup before migrations

`update.sh` runs `scripts/crm_pre_migrate_backup.py` after checkout of the exact
tested main SHA, dependency installation and deployment checks, but before the
first migration. This includes releases with no pending migrations. A nonzero
result stops migrations, collectstatic and Passenger restart. Preflight stays
read-only and does not make a backup.

The gate borrows deployment's existing fd8 call-analysis and fd9 deployment
exclusive locks. It verifies canonical paths, inode/owner/mode/link count and
Linux fdinfo `FLOCK ADVISORY WRITE` for each inherited open description. It does
not open a second lock, upgrade a shared lock, unlock or close the parent's
locks. Deploy and intake remain mutually exclusive under the existing protocol.
Missing/unsupported fdinfo fails closed; there is no skip-lock option.

The existing native helper copies the entire configured default MySQL schema,
without organization filters, to `../tmp/service2-crm-backups` on the same host.
It uses only existing application credentials. Coverage is every table exposed
by that account: all must be InnoDB base tables; visible views/triggers/routines/
events and unsupported schemas fail closed. The helper compares schema before,
during and after, holds metadata locks, verifies native dump completion and
per-table coverage, then gzip and SHA256. External files/uploads, credentials,
other databases and invisible objects are not included. This is no new ledger.

Before starting, the actual private destination filesystem must have at least
512MiB (existing compressed cap) plus64MiB reserve free. A concurrent space loss
still fails closed. Existing limits remain: 1GiB raw SQL,512MiB gzip,300s internal
deadline; the deploy call adds a TERM330s/KILL+10s watchdog. Native children are
terminated/reaped on helper failure. The single-threaded Linux helper also sets
PR_SET_PDEATHSIG(SIGKILL) before native exec and checks the parent PID race, so
native dump stops even after helper SIGKILL. Setup failure, a threaded caller or a setuid/setgid/file-capability executable
fails closed; unreadable capability metadata is not treated as absence. Normal cleanup remains; an orphan zombie may await host init reaping.
A synthetic test kills the helper and verifies that its separate-session native
child no longer executes. No limits are silently raised.

The gate revalidates the persisted receipt and archive: current invocation,
exact SHA, timestamps, private regular files with one link, receipt equality,
size, gzip and SHA. No old receipt, missing/malformed receipt or exit0 alone is
accepted. It checks locks and checkout again before success. Only allowlisted
aggregate receipt fields and fixed failure tokens reach CI logs; SQL, credentials
and raw exception text do not. Files remain in a0700 directory with0600 modes.
No GitHub artifacts, Library upload, retention deletion or remote copies exist.

## Release and failure handling

Run the ordinary reviewed PR/CI/merge/deploy path. The streamed new update.sh
checks out its tested SHA before importing the new gate, including the first
installation of this mechanism. CI exercises both the existing backup-only CLI
and this gate against a disposable MySQL database with synthetic rows, including
a real restore there. Production receipt deliberately keeps
`restore_verified=false`: no production restore rehearsal is claimed or required.

A verified production receipt emitted before migrations is the release evidence;
CI's synthetic restore is not a production backup. Confirm installed SHA, migration
result and health after deployment. Existing post-deploy backup-only workflow is
unchanged and remains available through its explicit authorization guards.

If the gate fails, do not rerun blindly or migrate manually. Inspect the fixed
failure phase/code and private receipt/file through an authorized host route;
a published backup may remain if final validation failed. Do not delete old copies.
The checkout/dependencies can already have changed while Passenger still serves
the prior code. Record disk SHA and running SHA separately; this is not atomic
application rollback. Use the normal reviewed release/recovery path.

Rollback of application code must preserve0159/0160 tables and consumption/role
history once data exists. Never reverse those migrations or restore an older DB
over new writes without a separate decision. Locks do not freeze every HTTP or
background write, so a backup cannot promise lossless automatic DB rollback.

The deploy job's existing15-minute limit includes this bounded backup; it was not
increased. Insufficient remaining time stops release rather than bypassing backup.
The reviewed SQLite LiveServer isolation fix is reused only for CI reliability;
no Contacts features are included. The new gate and tests introduce no credentials, grants, cohort/cutoff or scheduler
changes and no production migration of their own.

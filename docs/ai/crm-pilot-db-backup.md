# CRM pilot: private backup prerequisite

This separate prerequisite release contains no CRM migrations. Merge it through
normal PR checks, independent review and the existing protected deployment path.
The backup must succeed **before merging the later CRM migration release**.
The final push-to-main head commit message must explicitly contain
`[crm-pilot-db-backup-20261026]`. PRs, dispatch and unrelated commits cannot run it.
The conditional step follows normal deployment and health; it does not replace them.

The existing production Environment, SSH identity, pinned known host and exact-SHA
checks remain. The remote runner verifies current `origin/main` and local HEAD.
The helper takes the existing exclusive `service2-deploy.lock`, rechecks HEAD and
tracked application code, and holds that mutex through receipt publication.
Untracked application code also blocks. Generated `public_static` is not app code.
It imports deployed Django settings through `../venv/bin/python`, without app
startup hooks. Only the configured default MySQL database is eligible.

A native `mysqldump` or `mariadb-dump` is mandatory. There is no API fallback.
Credentials are supplied only in a temporary 0600 client options file within a
0700 directory and removed on exit. No credential values enter argv, child
environment or logs. Inherited MySQL configuration environment is discarded;
the alternate login file is `/dev/null`. Unsupported Django connection options
fail closed rather than silently connecting with different TLS/socket settings.

All visible base tables must use InnoDB. Visible views, triggers, routines and
events fail closed:
this deliberately narrow helper does not claim to back up those object types.
The dump uses one transaction, quick streaming and no table/data locks.
A separate read transaction keeps metadata locks on existing tables while the
native dump runs, allowing DML but delaying ALTER/DROP. A schema inventory and
canonical CREATE TABLE fingerprints are checked before/after dumping. The only
ignored schema field is the changing table AUTO_INCREMENT counter.
External object creation is not globally locked. Pre/post scope comparisons cannot
prove the absence of a transient external DDL change that was reverted; arrange
no out-of-band schema work during the backup. Normal deployments share the mutex.

Files stay on the host in `../tmp/service2-crm-backups` (0700), outside checkout.
Symlink components and writable-by-others ancestors are rejected. Basenames use
only expected SHA, UTC timestamp and random nonce. Gzip and receipt files are
0600; incomplete files use `.partial` and publish by rename only after verification.
No SQL, table names, personal data, credentials or database contents leave the host.
Only the aggregate allowlisted receipt is returned over SSH; raw output/errors are
captured privately and never echoed. Errors return fixed sanitized messages.

Limits: 300 seconds total, 1 GiB raw SQL, 512 MiB compressed, 8 MiB per SQL line
or CREATE TABLE statement, 16 MiB total schema, 2,000 tables. Missing tools/privileges, unsupported
scope, dirty app files, contention, schema mismatch, timeout, size cap, nonzero
dump exit, absent native completion footer or invalid gzip all fail closed.
The child process group is terminated/reaped on failure. Settings, SQL, streaming,
gzip readback, SHA256 and publication are all within the operation deadline.

The private JSON receipt records expected SHA, table count, sizes, gzip SHA256,
schema fingerprint, timestamps, `integrity_verified=true`, `restore_verified=false`.
Verification proves successful native completion, exact CREATE TABLE coverage,
stable visible schema scope and readable gzip bytes. Object visibility is limited
to the deployed app account; hidden privileged objects/whole-server DR are not certified. It does **not** prove a successful
restore, row-by-row equivalence, an off-host disaster-recovery copy or atomic
rollback of migrations. Keep the private archive; no automatic retention/deletion
of successful backups is performed. A hard host kill may leave private partials;
those are not successful backups and never have a verified receipt.

The current pilot is additive and performs no legacy backfill. Full legacy cutover
still requires a separate restore test and explicit roll-forward/recovery plan;
do not treat the pilot's integrity receipt as that restore evidence. Any failed
backup blocks the CRM migration merge; investigate using a narrowly scoped fix.

The existing MySQL 8.4 compatibility CI runs a native synthetic integration: a
separate database/user with only SELECT/SHOW VIEW, real dump/schema/footer and
private-file checks, followed by restore into another disposable CI database and
binary/unicode row comparison. This proves that client/server CI combination;
it does not assert a restore of production or support every hosting tool version.

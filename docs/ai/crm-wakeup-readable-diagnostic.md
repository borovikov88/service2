# Readable wakeup diagnostic snapshot (local proposal)

This is diagnostic instrumentation, not a correction to the launcher or proof of
successful immediate processing. No migration, endpoint, credentials, cron or
activation changes. Existing launcher real-UID ownership checks remain unchanged.

The web callback writes only its last failure to
`BASE_DIR.parent/tmp/crm-intake-wakeup-diagnostic.json`, independent of the
`crm-intake-wakeup` directory. Existing hosting `tmp` must be a non-symlink directory
owned by the effective application user and not group/world writable. No permissions
are changed. The newly created file is mode 0600; symlinks, hardlinks, foreign-owned,
non-regular and publicly readable files are rejected. Effective UID here checks the
owner of this *new diagnostic sink*, not eligibility to launch the worker.

Only UTC time, schema version, allowlisted code/errno and UID-equality boolean are
stored; no numeric UID, payload, exception text, paths or CRM identifiers. Maximum
512 bytes, replace in place under nonblocking flock. One snapshot, no archives or
unbounded history: older errors are overwritten. A crash/short write can invalidate
the snapshot; reads return unavailable. Lock contention/storage failure drops the
sample and must never break webhook ACK. Filesystem calls are local best effort,
not a guarantee against a stalled kernel filesystem. No fsync/network/DB calls.

## Operator access prerequisite before release

Through the existing authorised Beget file manager, confirm that the already known
application-sibling `tmp` directory can be listed/read under the hosting account.
This is the same parent in which the operator previously inspected the wakeup
subdirectory, but readable contents of a newly web-created 0600 file must still be
verified after the first natural failure. Do not chmod/chown or guess shared logs.
If web EUID differs from the directory owner, snapshot is deliberately unavailable;
report that limitation instead of weakening security. No new access is requested.

After separately authorised publication/deployment, confirm installed SHA and leave
cron and event flag untouched. Wait for a natural qualifying event; do not call the
launcher, create CRM test records, backfill or requeue. Read only this snapshot using
that file manager, or the existing authorised host command route from the actual
application checkout:

    ../venv/bin/python manage.py crm_wakeup_diagnostic

The command does not query the DB or consume work and prints a validated allowlisted
`CRM_WAKEUP_DIAGNOSTIC_V1` result: absent, unavailable, or last_failure. Invalid JSON,
extra fields, unrecognised codes/timestamp and oversize files are never echoed.
`absent` is not proof of a working launcher; an old timestamp is not a current failure.
Correlate only timestamps with existing safe queue/cron observations. Full stderr
and client messages are unnecessary. The snapshot is NOT per-organisation history:
it is a deployment diagnostic for the existing org-1-only callback.

Rollback application normally; the small private snapshot may remain inert. No
automatic deletion, retention sweep or permission changes. Root-cause fixes require
actual new evidence and their own reviewed release scope.

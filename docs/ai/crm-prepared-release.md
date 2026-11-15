# CRM release integrated with 5590e16a

On 2026-11-15 the owner explicitly requested production publication of the
prepared CRM work, after merging current main `5590e16a00134a2cf20e0fe47f90e08b0b0c2115`
and reconciling migration history. This supersedes the earlier publication hold.

The release contains the Service2 CRM design, shared contact/company creation,
lead completion with either/both contact and company and with/without a deal,
verified 1C order linkage and progress, responsible assignment/notifications,
and failed stages with a bottom drag target. Main's non-destructive 1C client
review, selected company/contact identity protections and call diagnostics remain.

## Database history

Neither existing `0182` is renamed or replaced. `0186_merge_crm_release` joins
`0182_merge_onec_review_crm_contact` and `0185_crm_failure_stage` with no operations.
Production already at the main `0182` applies only the CRM `0182`, `0183`, `0184`,
`0185` and the merge. A checkout already at CRM `0185` receives only the missing
1C review branch and merge. Both forward paths are tested with existing contacts,
company/person pairs, completed leads, historical losses and review proposals.

`0187_enable_aqualine_crm_close` enables the previously gated negative-close
feature for organization 1 only, provided its existing CRM read/write scope is
already enabled. It does not grant capabilities or change roles. The transaction
locks the organization, records a `crm_release_activation` audit marker with
approval `crm-prepared-release-20261115`, and prints a flag-only readback for the
deployment log. An existing flag is also recorded, and a later revocation is
never overridden by replay. Reverse is a no-op: a schema rollback does not revoke
the explicitly requested feature. Other organizations retain their settings.

## Release checks

The merged integration selection passed 131 tests (two MySQL-only tests skipped
locally). New migration/activation tests and browser acceptance cover both
histories, target-only activation, idempotence, contact creation, completion,
1C progress, assignment notifications and failure drops. The release browser
selection has a separate CI step. MySQL CI includes the new CRM regressions and
the reviewed 1C integration/concurrency tests. Main's local Python-only 1C review
browser harness is unchanged; its server integration is included.

The existing combined Chromium gate reached its five-minute limit in the first
release run. Shared-workspace scenarios and commercial CRM scenarios now run in
separate bounded steps with per-test output. Every original test module remains
enabled, and each step retains the five-minute limit.

The broader browser run also identified two outdated test interactions: closing
a lead now explicitly selects "Завершить без создания", and a new click after
Escape waits for the existing 250ms accidental-click protection to expire.
Both corrected scenarios pass against the real local UI; cancellation still
asserts zero writes. No product behavior or expected persistence was relaxed.

Disposable SQLite LiveServer databases use `BEGIN IMMEDIATE` so concurrent
background lookups and a contact-creation POST do not compete to upgrade a
deferred transaction. The configuration contract, 320 concurrent HTTP reads,
both customer-completion browser cases and safe diagnostic capture pass locally.
MySQL and normal application settings retain their existing transaction behavior.

Publication must use the existing PR checks and protected Passenger deployment.
The unchanged deployment path validates exact SHA and preflight, verifies a
fresh private database backup before migrations, collects static assets and
checks application health. Confirm `CRM_RELEASE_ACTIVATION: org=1 close_enabled=true`
in the successful deployment log before declaring the failure feature live.

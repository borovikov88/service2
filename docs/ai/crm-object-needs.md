# Object needs: stable source identity and explicit classification

Source/review package. WaterReading → existing supply ServiceTask/CrmItem remains the
operational identity. No commercial deal/lead, contract, delivery, stock issue,
monthly calculation, finance or provider integration is created by the review.
No grants, cohorts, activation/cutoff flags or Avito code are changed.

## Source protocol and history

Migration 0158 adds three empty tables, without RunPython/backfill. A new source
write atomically retains one CrmObjectNeed anchor per Reading, task and CRM item,
plus immutable source revisions. Content is normalized by strip and SHA-256;
saving the same normalized content adds no revision. Edits and clearing add a
revision, with the explicit current HTTP/admin/import actor (or unknown for a
trusted low-level edit with no actor), independently of Reading.added_by.

WaterReading.save acquires the source locks **before SQL INSERT/UPDATE**, then
re-reads the persisted row to honor partial update_fields. The post_save signal
only retains existing limits notification behavior. Queryset composition/pool
update and bulk source creation/update are refused; historical models/raw SQL
remain trusted bypasses, not supported application writers. Soft-delete/restore
saves with explicit update_fields omitting composition/pool bypass source locks.
No new source writer or permission is exposed.

All supply candidates are considered, including done/cancelled/waiting/archive.
Exactly one consistent candidate can be anchored. Multiple candidates, changed
canonical links, cross-org graphs and incomplete revision history fail closed;
there is no first/latest/active selection, merge, delete or backfill.

For an editable auto-generated task/item, only fields still equal to their prior
generated value are refreshed, with a structural ServiceTaskChange. Manual text
is retained. Task waiting/done/cancelled/archive/completed, item waiting supplier,
waiting stock/sent/done/archive, and noneditable tasks retain their entire task
and item snapshots. Their changed/cleared source revision is visible read-only.
The source path never calls lifecycle sync, changes assignments, reopens work,
or marks delivery/cancellation/completion. Existing unrelated task/CRM sync
continues through its existing authorized flows.

## Queue and review

The internal `/crm/workspace/needs/?org_id=…` screen requires current
can_view_team_deals **and individual CrmAccess.view_team_deals**, active account,
membership/HR, paid/trial access and crm_v2_read. D06 or a source task participant
alone does not grant CRM access. Current direct-task SQL authority (no preview),
source privacy and consistent organization/client/Pool/Reading/item graph are
applied before search/count/pagination. Call-backed/malformed call-source tasks
are excluded entirely. GET never creates an anchor, including legacy reads.

50 rows/page; search uses 300ms debounce, AbortController and URL state. Filters
cover mine/available, review state and position mode. History is capped at 20
source and 20 review revisions; the detail text is capped at 20000 characters
with an explicit truncation marker and source-task link. Oversized source text
is read-only in this screen. No free source/provider JSON is accepted.

Review requires current explicit edit_own_deals, individual CRM read grant,
crm_v2_read/write, the existing commercial account/member/HR/role guard, current
primary_responsible == actor, existing task edit authority and direct read,
current object/source graph and editable state. Primary alone gives no right;
owner/admin never bypass own. Null/other/inactive primary leaves a read-only
screen linking the existing task assignment flow; no claim/reassignment exists.

One review has 1–50 typed positions: stable unique line_token (64 ASCII chars),
description (1000), mode and mandatory reason (500). Whitelist modes are
included, chemistry_monthly, approval_required, warranty, error, cancelled and
already_supplied. These are explicit manager assertions, not evidence of a
contract, stock issue, delivery, invoice or settlement. Review changes neither
task/item state nor assignment. A changed source never inherits old positions;
old reviews are displayed only as history naming their own source revision.

The command binds actor/org/task, source sequence+hash, review sequence, task
version and positions to one strict operation_key/SHA-256. Current authority,
source/object graph, source revision and task version are rechecked on replay.
Same key/payload returns the same structural IDs; different payload/stale input
fails without partial rows. Source/review/command changes roll back together.
The browser freezes its exact payload/key after an uncertain response and
retries that same request. Explicit validation/conflict/permission errors retain
the entered text and require a refresh before a new operation.

## Lock/writer map

Source instance.save: Organization mutex → all candidate/shared task rows by PK
→ CRM items by PK → Pool → Client → Reading → anchor/source revisions. Review:
Organization → task/CRM graph → current User → OrgAccess → Employee → CrmAccess
(and task participant authority) → Pool/Client/Reading → anchor/history/command.
Task precedes actor because existing feedback is task→CRM→actor. Commercial
actor→task commands share the same org mutex and cannot overlap this order.
The task graph is locked in one sorted query, not seed-task then its siblings.
No blanket deadlock retry or general lifecycle lock rewrite is introduced.

Current writers are HTTP create/edit, ordinary WaterReading admin and the
existing ImportExportModelAdmin resource; historical migrations are separate.
The existing Reading resource now forces one transaction and rollback on
validation errors for its batch, including dry-run. Before per-row saves it
locks all relevant orgs in PK order, the whole existing task/CRM graph, all
Pools/Clients and existing Readings. Scope/graph changes fail closed instead of
acquiring a new unsorted org/task lock after actor FK locks. This necessary
behavior change is confined to this resource; financial import is unchanged.
Preflight and the row loader use the same field/widget identity decoder,
including accepted numeric aliases. Successful preflight is required before
every row and save; an existing Reading outside that prelocked set rejects the
batch. Retained source identities reject a missing Pool organization before
Reading SQL; source-less unscoped legacy readings keep their existing behavior.

Existing source assignment/limits in-app notifications remain in the source
transaction. Their existing Web Push moves to on_commit so failed source writes
and rolled-back import previews cannot send. Deferred callbacks capture only
typed source/recipient/notification IDs, reload source text and lock/recheck
current active staff/member/HR, direct task authority, source/privacy/Pool graph,
current limits/preferences and task state before delivery. Other notification
callers keep default defer_push=False. There is no new provider, outbox, retry
queue or delivery guarantee.

For either source callback kind the order is the full task/CRM graph → Reading
→ fresh graph identity check → current User shared lock → OrgAccess → Employee.
On MySQL the User query uses LOCK IN SHARE MODE, compatible with Notification
recipient FK locks held by a multi-reading restore. The exclusive staff-user
helper is never called in this path, avoiding a shared-to-exclusive upgrade.
Callbacks acquire no Org/Pool/Client locks. On MySQL they require an actual
READ COMMITTED session and re-read parent metadata after recipient waits;
metadata remains a bounded delivery-decision snapshot, without serializing
legacy reparenting through the external push send. Graph growth fails closed
before recipient locks instead of acquiring additional task locks.

## Required release checks

Targeted source/business/security/queue/import/migration tests and check +
makemigrations --check. `test_crm_object_need_concurrency` uses separate
connections and real held row locks, selected in isolated MySQL CI; SQLite
skips cannot prove races. `test_crm_object_need_browser` plus the separate
crm_object_need.browser.cjs uses actual Chromium and pinned Bootstrap 5.2.3
served from the CI npm package, blocks all other nonlocal requests, and checks
mixed classification, a committed/lost response replay with identical payload,
search/URL, reload and mobile overflow with zero page errors. The dedicated
browser env flag must be enabled in the mandatory CI browser step. Full exact
head CI and independent review remain root's publication gates.

## Selected positions to an own preparatory deal (draft, release on hold)

Migration 0159 adds three empty additive tables: stable need-line identities,
immutable review-line bindings and permanent deal consumption. There is no
backfill, grant change or operational lifecycle write. Only explicitly selected
`approval_required` positions can be reconciled and converted. One selected
group creates one own preparatory deal through the existing commercial command
and one real `crm_followup`; the supply request remains unchanged. Chemistry,
included work, delivery, realization and payment remain separate.

Reconciliation explicitly chooses an existing stable line ID or a new independent
need. Known tokens cannot be reminted; consumed identities stay consumed after
deal cancellation/archive and later source reviews. A new token explicitly
declared new is a new audited identity, not semantic deduplication. Source/task
changes require manual history inspection before a new operation. Exact replay
after a newer review of the same unchanged source retains the original result,
but current source/task versions, rights, ownership and graph guards still apply.

Unknown HTTP outcomes freeze the exact request/key. HTTP 409 warns that an
earlier operation may already have succeeded and requires history inspection.
Team deal visibility does not grant source/provenance visibility. No private
source description is automatically copied into the commercial title.

Tests cover additive migration preservation, SQL uniqueness, rollback, current
authority and replay. The MySQL workflow selects actual held-lock races; SQLite
skips do not prove them. The existing mandatory object-needs Chromium scenario
also converts a mixed review through Django, loses a committed response, retries
the exact bytes and checks permanent history, one deal/next task, unchanged
source, reload and mobile layout. Full exact-head CI and independent review are
required; this draft does not authorize production migration, merge or deploy.

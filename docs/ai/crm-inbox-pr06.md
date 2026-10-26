# CRM source inbox (PR06)

Historical scope of PR06 below. The next working release installs the SQL lead
consumer and a bounded recurring worker; see `crm-lead-intake.md`. Capture remains
independent of the processing flag. Activation has its own live cutoff and does
not turn the already captured backlog into today's lead queue.

The inbox is a durable handoff for saved incoming sources. It does not create a
lead, response deadline, callback task or CRM notification. The production CRM
consumer is absent; successful technical validation leaves `awaiting_consumer`,
with its original delivery available to a future consumer. `crm_intake_process`
(default OFF) gates claims/effects, not source capture. There is no scheduler.

## Covered writers and limits

| Source writer | Capture |
| --- | --- |
| `communication_services.receive_message` | Inbound website chat/request and Avito messages; duplicates repair missing inbox atomically |
| Avito webhook / bounded recent sync | Same saved message identity; provider `created` retained when valid; self/outgoing excluded |
| MegaFon `cmd=history` | External inbound telephony PhoneCall; this normal final-call command does not itself imply historical import |
| MegaFon `cmd=event` | Outside durable scope: only existing last-event connection metadata; optional empty callid cannot identify a durable source |
| Internal calls / uploaded audio / outgoing | Excluded from new inbound capture |

Early-stage MegaFon receipts and transfer semantics require PR07 before anyone
claims reliable early-stage lead intake. Provider history pagination, outgoing and
media completeness remain PR08. Reconciliation reads local sources only.

## Identity and time

A SHA256 source key encodes organization, source type/provider, connection PK,
external account snapshot, message conversation namespace, external source ID.
An absent message external ID uses the stable local source PK; it cannot dedupe
independent deliveries. A second account with the same external IDs is separate.
Deleting and recreating the same logical provider source is not a remap contract:
its durable key survives deletion and ingestion fails closed, preventing a second
effect. Explicit remap/reconciliation support would be a separate operation.
Changing an account on an existing source fails closed; source, inbox and live
connection scopes must match. Inbox locators do not grant source viewing rights.

Source scope, first receipt, initial historical flag and cutover timestamp are
captured once. The additive migration creates only a cutover control row: no
source backfill, grants or flags. If that control row is absent (for example after
a disposable test DB flush), the next source transaction recreates a conservative
new boundary. Existing sources retain their saved boundary and classification.
Provider occurrence stays nullable; receipt time and the PhoneCall.started_at
compatibility fallback never manufacture provider time. Missing initial call
start can later acquire a genuine provider occurrence in a new revision.

Known provider time before the saved cutover, explicit history/import, and unknown
time recovery sync are historical. Unknown-time fresh live webhook may be current.
Initial classification is retained. A separate persisted call historical flag can
only become more conservative: once any revision proves pre-cutover occurrence,
subsequent replay/reconciliation remains historical even if later provider facts
change. Historical message capture saves evidence without creating a new client,
reopening a dialogue, advancing activity, starting enrichment or notifying users.
Duplicate replay preserves PR05 guards/evidence/reuse with `create=False`.
Current out-of-order provider timestamps cannot move dialogue activity backwards.

Message revision is 1. A call gets an ordered local revision when provider facts
change: result, direction, duration, genuine occurrence, provider user/extension
and historical classification. Exact latest replay does not add a revision.
Recording download/URL, AI/transcript and manual client bindings do not create
revisions. Counters express local acceptance order, not provider chronology.
Older revisions are explicitly superseded if the mutable source no longer
represents them. No body, phone, name, transcript, payload, recording URL or secret
is copied into inbox or status/error diagnostics.

## Transactions, leases and adapter boundary

Capture occurs in the source transaction, with connection -> organization ->
conversation/call -> inbox locking. Metadata/settings writers must not acquire
organization/inbox before waiting for a source connection in the reverse order;
this package does not retrofit every legacy ORM writer. Failure of mandatory capture rolls back the
source and prevents a false success acknowledgement. `on_commit` is not needed
for durability. PR05 manual clients and pending/resolved identity decisions remain
authoritative. The inbox never calls client capture during delivery/reconciliation.

Claims hold inbox locks only. Delivery then obtains normal connection -> org ->
source -> inbox locks and rechecks lease token/deadline, actual snapshots and the
process flag. Finite leases survive worker death. A stale owner cannot acknowledge
a newer claim. No consumer means `awaiting_consumer` with no failed-attempt spend.
A SQL-only internal Python callable is the test/future adapter seam; no arbitrary
CLI/config import is available. Its SQL effect and done acknowledgement commit
in one transaction; exceptions roll both back. It receives a stable source-key /
revision idempotency key. External side effects cannot satisfy this transaction
contract and need their own durable outbox. The future business consumer must
honor historical classification and existing permissions/ownership policies.

Exceptions retain only `consumer_failed`, with bounded exponential backoff (five
failed attempts, up to one-hour delay). Exhaustion retains `failed` evidence.
Scoped explicit retry resets failures. Blocked/unverified scope is not retried as
current identity. Supported inbox ORM writes require instance.save; scope fields
are immutable and bulk/update/delete are rejected. Raw SQL, historical migration
models and explicit private ORM escape hatches are trusted administrative paths,
not a security boundary against a database administrator. Legacy source models
remain mutable; every delivery compares immutable inbox snapshot, source metadata
and live scope to prevent metadata rewrites from bypassing account checks.

## Bounded local operations

Examples are syntax only, not authorization to run against production:

```sh
python manage.py crm_inbox status --organization ORG --source-type message --connection CHANNEL_PK
python manage.py crm_inbox work --organization ORG --source-type call --connection TELEPHONY_PK --limit 100 --seconds 10
python manage.py crm_inbox reconcile --organization ORG --source-type message --connection CHANNEL_PK --limit 100 --seconds 10 --dry-run
python manage.py crm_inbox retry --organization ORG --source-type message --connection CHANNEL_PK --limit 20
```

Local tests use `--settings=service_site.test_settings`. The operator command is
not a new HTTP permission or source-content API. Status exposes scoped state
counts, due/stale claims, oldest receipt age, last technical delivery and cursor
progress. It does not label the channel/AI healthy or imply an installed consumer.

Default reconciliation includes only sources with new capture provenance. Each
cycle snapshots a finite upper PK and scans ascending PK with a durable cursor;
row capture and cursor advancement commit together. Failed rows retain the gap.
Completed cycles return to the beginning, so same-PK call changes are eventually
revisited. This is O(N) cyclic reconciliation, not an indexed update feed. A
source mutated behind an active cursor is seen in the next cycle. Limits bound
rows and elapsed-time checks between rows; a single DB lock wait/transaction is
subject to the database's lock timeout. No production recurring execution is
installed. Dry-run never changes cursors or source/inbox rows.

Legacy reconciliation requires explicit `--include-legacy --after-id N
--through-id M`. It produces historical, unverified, blocked locators without
claiming today's account as historical provenance. No automatic whole-history
scan or provider requests occur. A capture below the current cursor is recovered
on the next cycle. Missing inbox can be rebuilt from source metadata; deleting all
revision rows loses the deleted revision audit sequence and is a trusted corruption
scenario, not a supported operation.

Rollback must preserve accepted source/inbox evidence. Reversing migration 0154
removes the new journal and is safe only on disposable data or before any capture;
it is not an operational rollback plan after accepted events.

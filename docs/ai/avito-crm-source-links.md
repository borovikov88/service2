# Avito Services source provenance

Pilot: connection 4, account 446312402. This adapter links existing communication
sources to existing CrmItem records. It is not another lead, client, order,
finance ledger, task scheduler or provider integration.

## Manager workflow

Open /avito/4/attribution/ from the Services analytics CRM block, Communications,
or the selected Services conversation. Ordinary managers use existing conversation
view/take/assignment permissions; channel-management capability is not required.
The endpoint checks the exact organization and current source rights again on
every read, update and replay.

A manager selects an existing CRM card, optional quality/reason and an existing
next-step ServiceTask. Search is debounced and organization-scoped. No message
creates a request and no task is created. Existing CRM stage is displayed as its
legacy workflow result, never as proof of an order, payment or revenue.

Conversation and call OneToOne keys plus the exactly-one-source check guarantee
one current source link. Corrections update that link with a revision/operation
key. A retry with the same operation and payload returns the original result;
a different payload or stale revision is a conflict. DataAuditLog stores IDs,
codes and reason/order-reference hashes, not transcripts, message bodies, reasons
or financial amounts. Original source rows are protected from deletion while
referenced; normal privacy changes remain independent and are rechecked.

Phone calls belong to the telephony organization, not automatically to Avito.
Quality-only work leaves their channel unknown. A separate explicit manager
acknowledgement records Avito origin. Unconfirmed call candidates have separate
counts and never inflate the Avito denominator. An ad ID is either checked stored
provider context, an explicitly selected currently owned ad, or unknown.
Provider JSON is never overwritten.

## Rights and parallel CRM

The single canonical services/crm_permissions.py file is adopted byte-identically
from reviewed PR402 head79a6a12f633efbe33d73d871d1d455a0dbb7f8ce. No other PR402
or PR405 files or ancestry are adopted. Their broad legacy CRM/privacy changes
are not declared deployed. Selected-organization CRM directions and active
employee lifecycle are used by this adapter only.

Source reads use existing conversation capabilities or own/all-call policy and
call privacy. Task references intersect existing task visibility with same-tenant,
parent-source and current private-call checks. Raw source content is not copied.
The new scoped page renders a minimal existing CRM card, avoiding legacy routes
that select a user's first organization. Later commercial CRM migration must
retarget this provenance adapter through the agreed legacy mapping, not create
duplicate leads or source associations.

## Optional observed order reference

Only existing finance-overview actors can select or view order metadata. Candidate
identity requires the selected CRM card's exact imported ClientCRMProfile.onec_ref,
source=onec, sync evidence and no merge. Rows must belong to the organization's
current active planning snapshot, direction=receipt and order_schedule/order_due,
with exact nonzero order and counterparty UUIDs. Multiple schedule rows dedupe;
inconsistent party/number/date/state or excessive rows are rejected. Financial
POST locks and revalidates the active snapshot. Ordinary managers cannot clear
the reference or change its CRM target indirectly.

The label is: «Наблюдаемый документ заказа 1С; связь подтверждена вручную».
A planning-source observation is not a verified order registry, financial
settlement, causal advertising attribution or revenue. Changed/missing source
evidence remains unknown with historical reference retained. Payments and revenue
stay unknown. Counts use distinct source/card/document identities and report
multi-ad or unknown-ad ambiguity, never sums of client matches.

## Release and validation

0150_crm_source_link is additive after0149. Register crm_source_models in the
existing app. No new cron, notifications, messages, XML publication or 1C writes.
The protected deployment may call crm_source_links.setup_evidence() for closed
read-only table/scope/count metadata. It never creates business associations.
Actual customer links require a manager's explicit selection; production proof
must not fabricate a source-to-request or financial association for testing.

Explicit ad and observed-order confirmations keep their own actor/time. Quality
edits preserve those acknowledgements. Safe audit hashes distinguish same-ID
reconfirmation against changed source evidence; financial-only history remains
hidden from non-finance users. MySQL-only two-connection concurrency tests check
one link and one audit entry for a repeated operation; SQLite skips that test.

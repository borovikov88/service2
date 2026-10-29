# Contact phones, explicit user links and informative lead sources (local)

Base bbd6ef4. Reuses the entire original lead-details9f31733 through cherry-picks;
no other unpublished feature package is included. Not published or deployed.

Lead metadata now carries source-specific caller/callee, factual phone, current
readable contact URL, provider and timestamps. Call employee is distinct from lead
assignee. Unknown facts stay unknown; no phone-based identity guesses. Queries
select only needed source columns, never message bodies or recording references.
Private/current source guards and listen checks remain. Existing saved audio,
transcript and explicit suggestions are reused; GET does not start AI.

ClientContact adds phone_type work/personal/other (default other for unknown old
rows). Personal is a label, never a privacy toggle. Owner/admin can add or update
one explicit number, select primary, retaining other numbers and imported source
metadata. The whole phone snapshot is revision-checked under organization/client
locks; aliases within a person are reconciled, ambiguous duplicates rejected.
A hidden contacts_revision avoids the existing generic phone-mask name selector.
Legacy primary edit/import and client merge preserve these numbers/types; conflicting
non-other types require explicit reconciliation, not silent overwrite.

OrganizationPersonLink explicitly connects an existing organization user and
existing private Client, optionally the existing Employee. It never creates users,
people, grants or changes Client.user. One person/user per organization; replay
is a no-op, reassignment is rejected pending a separate reconciliation operation.
Client merge involving such a link is blocked. Mapping is manual; old accounts are
not bulk matched, nor is directory linkage telephony identity proof.

Migration0161 is additive: one phone label column and one relation table. No
classification/backfill or finance/1C changes. After link data exists, application
rollback must preserve0161 and history; do not reverse/drop it automatically.

Local tests use synthetic data only. Native MySQL is unavailable here (no mysql
client or existing MySQL Docker image); races are explicit skipped local gates,
not claimed PASS. The new regression, migration and race modules are added to the
existing native MySQL workflow for future authorized CI. Chromium uses installed154
locally; pinned CI remains required before release. No publication authorization
for this package is assumed.

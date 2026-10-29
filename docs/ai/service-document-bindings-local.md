# Manual service document bindings — local backend slice

Depends on contact/lead/privacy through 0161 and the document adapter/allocation
preview commits. Migration 0162 adds four tables only: contract/deal/object link,
bound document, immutable allocation versions and operation/source events.
No legacy matching/data conversion, accounting entries, payment or deal closure.

The caller explicitly confirms the contract from the document and the existing
concrete object already selected by the CRM deal command. Current own-deal edit,
finance visibility, organization access, configured source and confirmed customer
identity are required. One contract is reserved for one deal/object. Object and
history references use PROTECT. No matching by names, no Client ID replacement.

The deal card links to a CSRF-protected document/period workspace. Users explicitly
enter source document/org/customer/contract references and select periods/quantity/
amount from the returned document lines. No names are auto-matched. Concrete object
selection remains in the existing deal editor. Only the existing server-side
GET-only adapter supplies financial source contents. Signed browser tokens contain
actor/org/deal/reference/hash/revision/operation identifiers, not document amounts.
Every save re-reads the source and rechecks current access. Changed source returns
409 for explicit review; negative corrections have no save form. Existing history
and source events are displayed with bounded counts.

The existing Diagnostic MCP user allowlist and target-org policy are retained in
addition to CRM edit/finance/customer/object guards. If unavailable, the UI is
explicitly blocked without network access or permission changes. No live 1C request
was performed during development. A production configuration/allowlist/network
check is still needed before claiming this works with the live source. HTTP and
browser tests use synthetic snapshots/transports. No browser JSON can become a
trusted DocumentSnapshot; source HTTP errors expose only a safe generic message.

Each allocation version is a complete explicit replacement for that document's
previous allocation set; history is retained. Fractions such as 0.5 and quantity2
are supported without inferring periods/completion. Duplicate line/month and
source totals are checked exactly. Whole-document reservation prevents consuming
renumbered lines through a second deal. Changes in content, contract/customer or
object invalidate the active allocation until explicit reconciliation. Invalid
scope stores only an invalidation marker, not another customer's new payload.
Corrections with negative values, ambiguous equal lines, future/unposted/deleted
documents stay blocked. No automatic netting of corrections is invented.

A 15-minute technical snapshot freshness bound and monotonic server observation
watermark prevent stale saves/replay rollback; they do not prove upstream state
has not changed after reading. observe_document is the internal invalidation hook;
the explicit preview/save flow invokes it. There is no background observer and
no claim of detection between explicit refreshes. Two bounded source reads are not a 1C transaction.
Replay returns the original version and whether it is still active. Organization
locking serializes supported commands; native MySQL races remain a release gate.

Pending product decision: negative corrective documents currently require manual
source review and cannot alter allocated totals. Safe default is retaining all
versions and withholding completion credit until an explicit correction policy.
No payment/automatic completion model is introduced.

Rollback: application only after data exists; keep 0161/0162 tables, versions and
source events. Empty-table migration reverse is tested solely in synthetic tests.
Publication/deployment/live binding are not authorized for this package.

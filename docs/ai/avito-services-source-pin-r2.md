# Future r2: normal publication of the unchanged approved source

Prepared separately from read-only PR384. Do not ship or execute until the real
r1 photo source-read failure is diagnosed and fixed, current r1 photo upload /
preview / editor save / exact restore is confirmed, and the combined revision
passes independent review, CI, normal deployment and health checks.

The command is run_avito_services_source_pin with fixed generation
services-source-pin-r2-20261024. It requires the existing protected operator
environment, exact git HEAD, and BOTH markers in the actual merge commit:

- [avito-services-release-v15-20261024]
- [avito-services-source-pin-r2-20261024]

The separate result frame is AVITO_SERVICES_SOURCE_PIN_RECEIPT. The r1 command
and its strict published15 scope remain unchanged. R2's completed ledger is
reconciled before attempting any r1 context: publishing may advance the draft
revision after the photo restore, so the current published revision need no
longer be15. Its historical photo_before_publication result is never described
as a fresh browser check or verification of an Avito card.

## Preconditions and the normal shared publication flow

The existing publish view and r2 call one shared publish_services_snapshot
implementation. Permission checks, account inventory, media availability,
10-photo publication limit, history, public snapshot and publication binding
are not duplicated in a parallel writer.

For this r2 operation only, published15 and the current draft must match as
raw JSON content and exact canonical XML bytes. Existing Id/AvitoId pairs stay
unchanged. Every original ad must be confirmed active; no row may be excluded
to make the unchanged-source check pass. Pending owner edits cause a block and
are preserved. An existing valid binding for published15 needs no publication.

Before mutation, the current actor and ordinary editor permission, account,
organization, channel, credential stamp, public key, draft revision, published
revision and both snapshot digests are rechecked under connection→feed locks.
The strict inventory/byte check is repeated inside the mutation transaction.
No external network calls occur while those locks are held.

The normal version event, immutable publication binding and r2 generation
result/version ID are committed atomically. A result callback failure rolls
them all back. The prior published15 snapshot is captured as legacy history
NOW if absent; its original publication time remains unknown. No legacy
binding is fabricated, and no event timestamp is backdated.

A retry with a committed r2 result verifies that exact current binding and
snapshot, then performs a read-only HTTP parity check. It never republishes
identical bytes. A busy uncommitted lease blocks before photo or inventory
actions. An interrupted uncommitted operation can retry only the same pinned
source after all current checks.

## Independent results

published/already_published mean a known local Service2 file commit. The
publication sub-result separately reports actual HTTP parity and provider
upload/source/item/live-card facts. HTTP failure does not erase the known local
commit or justify another publication.

The exact immutable URL remains in the authorized Service2 page. Public
receipts contain only its page path, revision/history IDs, timestamps, counts
and fixed flags. No capability URL, source URL, hash, token, actor ID, raw
exception, or listing identifier is exported.

The operation never calls Avito mutation APIs, forces a bulk upload, creates
copies, reactivates ads, buys services, changes prices/text/photos for testing,
or touches the goods account. It does not change Avito's configured feed URL.
The owner still needs a separately agreed source choice in Avito, followed by
a later completed report. Immutable URLs pin the whole snapshot; a later
Service2 publication does not update a previously chosen immutable source.

A provider copy returning401 still has no verified download-auth contract.
A normal same-content publication does not remove that provider limitation.
Legacy same-byte history remains a competing input candidate; old reports do
not confirm the new local event. Text and photo order in a live Avito card
remain unverified by this API contour.

## Producer/consumer regression and conditional activation

Photo source metadata accepts the original ten fields or one additional
https_upgrade_followed boolean/null. Missing legacy evidence is normalized to
null, not false. A true value is retained through r1, the durable receipt and
r2's historical photo result. It comes from the same source GET; no diagnostic
re-fetch is presented as that proof.

The release integration regression executes real media_release_source, image
decode, token signing, signed HTTP endpoint, upload, private preview and two
ordinary editor saves with exact restoration. Only socket/network and external
account reads are substituted. The narrow HTTP→HTTPS redirect is validated
before the first source decode; unsupported Location or non-image content
blocks before any token/POST. A historical failed/no-operation-id ledger can
resume after this positive preflight. Once a durable pending operation exists,
unknown delivery only reconciles the exact receipt; it never mints another
token or replays POST. These are tests, not production verification.

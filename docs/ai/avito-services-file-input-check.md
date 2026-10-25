# Separate stored-file input verification

Scope is Services connection4/account446312402 only. This verification is independent
of external-link import and of the historical r1/source-pin generation. It never
publishes a file or calls an Avito write API.

## Read-only inventory first

The command
python manage.py run_avito_services_filecheck --connection-id 4 --account-id 446312402
returns closed counters and flags only.
It uses database records; it never opens source files, resolves DNS or fetches a
remote image. Candidate availability and the generation state are distinct:
a pending/unknown local generation is blocked even if suitable assets exist. A candidate must belong to the current organization/account, have
published_in_feed_at, and be referenced by the same Id/AvitoId row in BOTH current
draft and published snapshots. At least two distinct approved row photos are
required to exercise a real cover/order change. An unattached upload is not
automatically treated as approved.

The image model retains normalized JPEG, not the uploaded raw original.
The inventory cannot infer real-versus-synthetic provenance from uploaded_by.
The synthetic storage probe creates no image model row and deletes its file.
No eligible agreed asset yields file_check_needs_owner_original. No synthetic
fixture, screenshot or external download is substituted.

## Explicit file operation

The --perform path additionally requires the protected SERVICE2_AVITO_FILECHECK
environment, exact deployed current-main SHA, clean checkout, and the actual
merge marker [avito-services-filecheck-20261025]. Preparing a capability records
the exact operation hash before its single HTTP request. A pending/unknown
generation is never taken over or posted again.

The fixed first-party signed endpoint runs in Passenger and reuses the normal
FILES upload, private preview and ordinary editor handlers. It checks the file
digest/path/size/format, exercises changed cover/order states, reopens the
ordinary editor after each save, and restores the exact original draft.
Three append-only edit versions remain, including an explicit restore event even
when the second step has already returned the original order. Pending text edits
are preserved and reported separately from published content. Failure rolls all
draft writes back.
The public snapshot, its revision/limits/key and the external r1 ledger must
remain unchanged. A deduplicated upload is explicitly reported as asset_reused;
it is not claimed as a new storage write.

No browser click or raw-original input is claimed by a server handler roundtrip.
No external import, external source recovery, or Avito card content is verified.

## Subsequent publication is still blocked independently

This local check advances the draft revision by3 even after exact restoration.
The old failed r1 ledger is retained verbatim. A later reviewed external-source
generation must bind the new current revision. Existing r1 pending/unknown or
reserved operations block this check. This mode never completes old r1, clears
HTTP429, supplies the r1 proof required by PR388, or changes a source-pin ledger.

The five publication facts remain separate: local file publication, subsequent
provider upload, confirmed version source, processed rows, and actual live card
content. Immutable version URLs pin the whole snapshot; future Service2 edits
do not update a previously selected immutable URL.

If the inventory has no eligible stored asset, this prepared mode cannot infer
approval from a new unattached upload. A real owner-supplied original and an
explicit scoped approval/input channel are then required; simply uploading an
unattached file does not automatically satisfy this generation's source guard.

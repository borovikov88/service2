# CRM + leads: bounded cursor loading — LOCAL, not published

Public base2cc4e2e; dependency PR477 UI headf9d805b9 preserved separately.
Both boards show initial50 per column, then50 per explicit request. No200-card
ceiling and no need to open a table for card201. Deal table and closed-lead history
retain existing page pagination. Initial board work is proportional to configured
column count; source history/media is not fetched without its existing detail action.
No global CI workflow changes are included in this LOCAL delta.

## Protocol / consistency boundary

Migration0169 adds one `CrmBoardEpoch` table, organization-keyed. No business backfill,
no grants, activation or real records. CommercialModel validated save increments the
monotonic epoch for lead/deal/stage/pipeline/access under its existing org mutex;
tracked model/queryset deletion also locks org before invalidation. Queryset deletes
freeze candidate PKs before locking and recheck survivors under locks; newly matching
rows from other scopes cannot enter the delete. Rollback rolls
back epoch with the row. Bulk update/create remain forbidden as before. Raw SQL and
historical migration models are NOT a supported live write API.

Existing data routes accept signed cursors bound to actor/org/board/column/normalized
filters/epoch and last sort key. CRM temporal filters preserve a signed as_of
instant across portions, including local midnight and due-time crossings; refreshing
recomputes them. Table temporal filtering remains current-time. Deal order is descending PK; lead order preserves
received_at/PK tuple. TTL1h. Each request rechecks current authority and reads max51
matching rows before serialization. No growing list of IDs is sent by the browser.
Org lock acquired before board read and epoch validation prevents supported stage
writes from interleaving column membership reads. GET never creates epoch rows.

Epoch mismatch, invalid signature, expired cursor or changed cursor scope gives409
`reset_required`, with no portion. UI clears ALL rows/cursors, aborts superseded loads,
then reads current initial portions and shows an explanation. After own successful
move the board starts fresh. DOM merge uses stable IDs, suppressing repeated chunks.
A single request generation rejects late responses after filter/pipeline/history
changes; search invalidates an outstanding request immediately, before debounce.

This is a LIVE view, not a retained database snapshot. Client names, task details,
source/privacy and HR changes do not all bump this epoch. Each new portion uses the
current server rights/source filters; access rejection clears loaded results. Already
delivered content is not remotely revoked from browser memory. Refresh revalidates
all displayed rows and source previews. A visible note asks for refresh after related
data changes; we do not claim instant synchronization of all tabs/dependencies.
Counts/sums cover the complete current authorized filter, not just loaded cards,
and are not a frozen financial snapshot. No financial facts are created.

## Acceptance / scope

- >200 in a column and >50 in another, both boards; exhaust cursors with no duplicate
  or missing IDs in unchanged data; identical retry does not advance cursor.
- Signed actor/org/filter/column scope, expiry/tampering; insertion, move, archive,
  sorting edit and deletion invalidate tracked streams. Revoked access denies new read.
- Native MySQL concurrent reader/writer: org mutex serializes portion and move;
  old continuation is rejected. Additive migration preserves business rows.
- Browser desktop/mobile/light/dark, empty-column lower drop, cancel/repeat,
  own and external change reset, delayed response/search, back/forward, table pages.
- Both JS resource versions updated; previous compact controls/drop surfaces retained.

No production requests/writes, notifications, cron or instrumentation. No claim of
hosting latency; live-epoch reads briefly hold the existing org mutex. Representative
hosting performance remains a release/acceptance consideration. Expanded DOM size
grows only on user requests; virtualization is a later optimization if needed.

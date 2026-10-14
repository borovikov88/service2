# Active Avito inventory candidates

The explicit full-audit POST and protected hosting operator audit enumerate all
active listings in the complete Item scan, then join unique Avito IDs in the
complete last_successful report to unique XML IDs in the shop's existing feed.
They read the existing fixed stock-only plugin route; no XML generator/provider
write, subscription or notification is invoked.

Absent report/feed rows, duplicate bindings, invalid stock rows and denied
sources stay unknown. Preorder is displayed and never treated as a removal
instruction. A nonpositive shop quantity combined with XML in-stock availability
is a discrepancy candidate, not a verified physical/1C warehouse balance.

The normalized list is limited to 500 active listings; exceeding it returns an
explicit error, not a truncated complete audit. Counts are recomputed from
enumerated rows by the receipt schema. No raw titles, descriptions, contact
fields or provider bodies enter hosting output. The UI retains existing owner/
admin, CSRF, org and refresh-lease guards; GETs read cached data only.

The account's configured feed URL, atomicity of account/site snapshots, physical
stock and historical expiry cause remain unverified. The same ID in the report
and shop XML is a candidate binding. No deletion, ID recreation, price/stock edit
or paid action follows from this report.


Current configured source is read with GET /autoload/v2/profile (feeds_data[].feed_url). Report feed_urls can point to a processed copy and never establish the current account configuration. The operator reads current configuration twice under the existing account/credential binding checks, compares private source fingerprints in-process, and projects only enabled status, a strict fixed source kind and structural enum/boolean diagnostics. URLs, feed names, report email and fingerprints are not logged or stored. Unsupported, mixed, signed, unavailable or changing configuration remains unverified. No profile settings or provider state are written.


Repeated report identity pairs (XML ID and Avito ID, including a missing Avito ID) invalidate completeness even when page metadata and the upload alias agree. The reader raises a fixed duplicate-row error before counters or inventory are derived; it does not silently deduplicate and claim missing rows were read. Existing cached duplicate reports are hidden on dashboard GET without a database write and cannot be exported as a complete CSV. Different Avito IDs remain separate records for duplicate-listing analysis. A failed refresh retains the previous snapshot and marks it stale; the next scheduled/manual read may retry normally.

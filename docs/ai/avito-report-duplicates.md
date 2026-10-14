# Partial diagnostics for repeated Avito report rows

The standalone read-only audit can receive duplicate `(ad_id, avito_id)` pairs
even when page sizes, pagination metadata and the upload alias before/after match.
This does not prove duplicate listings: distinguish provider report rows from ads.

The completeness guard remains in place. No deduplication, report counters,
stock reconciliation, saved successful snapshot, CSV export, listing action or
automatic source correction is allowed from such a report.

Only after every page and the before/after upload summaries pass validation,
the operator publishes `autoload_report_duplicate_rows` with a strictly projected
partial diagnostic: upload ID/status/start, rows read, unique identity pairs,
extra rows, within-page/cross-page repeats, equal/different normalized payloads,
and at most five numeric row/page positions. IDs, feed names/URLs, message bodies,
raw rows and credentials are excluded. Repeated rows with changed messages remain
blocked. Alias drift, malformed pagination, limits and unrelated errors do not
gain a partial duplicate receipt.

Read-only workflow success confirms safe execution, not a complete business
audit. Preserve the previous complete report as stale; never show partial
counters as fresh zero or diagnose inventory from them. A fresh standalone scan
may independently succeed or fail, so retain its own completeness and timestamp.

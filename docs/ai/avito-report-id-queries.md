# Reading a shop report without overlapping provider pages

A live report returned 208 rows but only 204 unique identity pairs. Four rows
overlapped page boundaries; one repeated payload differed. Dropping duplicates
would leave possible omissions and cannot establish completeness.

For a completed `last_successful` report, the reader can use bounded `query`
requests by XML ID only with an enabled HTTPS shop aggregate source proven
through the current v2 profile. Upload URLs may be processed Avito copies.
The current profile and its private source fingerprint must match before/after;
an unknown, mixed, disabled or denied current profile does not enable queries.
Explicitly foreign historical feed metadata also retains guarded pagination.
The unfiltered first page supplies validated total/pagination metadata.
The fixed existing source client reads the current public XML; IDs must be
unique canonical numeric shop IDs, and their count must equal the provider total.

Each query requests at most 50 IDs on page1 with perPage50. Its total must equal
the requested count, every requested ID must appear, no foreign ID is accepted,
and identity pairs remain unique. All returned rows together must exactly cover
the XML IDs and declared total. The unfiltered probe rows must match these rows.
The XML byte digest and complete upload summary must remain unchanged across
the read. Existing response-size, time, row, message and uniqueness limits apply.
Source/profile reads receive the shared monotonic report deadline, capped by
their existing smaller limits. A late last query cannot start another XML read;
transport timeout uses the remaining budget, preserving the collector's reserved
status-scan window.

No rows are deduplicated or copied from the unfiltered probe. No fallback may
turn a failed query, altered source/upload or missing ID into success.
Other sources, current/processing uploads and an unavailable or nonmatching XML
continue through the existing guarded provider pagination. A failure preserves
a stale earlier snapshot and remains incomplete.

The receipt exposes only the fixed strategy `shop_xml_id_queries` or
`provider_pages`, alongside existing sanitized results. Query strings, source
URLs and extra identities are not logged. No provider/source/listing write,
notification, credential, permission or deployment guard is changed.

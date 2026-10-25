# Services: preserving an unchanged published photograph reference

This change is scoped to Services connection4/account446312402. The existing XML format remains `Images/Image url`; no Image-ID preservation API, authentication bypass, Avito caching guarantee or provider-side format exception is introduced.

The normal publisher first verifies the complete owned active inventory using the supported account/items methods. Only its fresh observations can authorize reuse. Media validation requires the current enabled account/organization-bound published DB snapshot, exact Id/AvitoId, exact photo position and exact original URL. New URLs, changed query strings, reordered links and cross-listing copies still require the ordinary bounded pinned HTTP/decode validation. Other connections retain the ordinary policy.

An unchanged reference is syntax checked and its hostname must resolve entirely to public addresses within the ordinary deadline. It is not fetched again. This keeps the previously published reference without pretending that an external403/429 proves or disproves Avito access to its own upload process. The result is explicitly `published_reference_retained`, with listing, photo number and URL digest, not fresh availability. The publisher records this boundary in the immutable publication reason and rechecks the exact baseline under its locks.

First-party assets are always classified before retention. An uploaded draft file is checked against its immutable scope/path/digest, opened and decoded through the existing private storage. Public HTTP404 before publication is expected; authenticated preview is the supported preview route. Neither foreign assets nor forged/encoded Service2 paths can be grandfathered. The public mark, XML snapshot, version history, binding and optional operation ledger callback commit in the same transaction.

The publisher returns a location-specific safe error (listing ID, one-based photo number, Barnaul check time and distinct HTTP401/403/429/redirect/decode reason). External URLs, bodies and response headers are absent from this diagnostic. Exact URLs already remain visible in the authorized editor.

A separate scoped diagnostic probes at most the first unchanged external image of each of two selected ads, and at most one request per hostname. Other URLs on that host are labelled unprobed, never assigned the first URL's status. Scoped negative URL-digest caches hold403/429 for15minutes; attempts during that period reuse the safe cause without another request. This diagnostic does not publish, upload or change draft/public snapshots.

Retention is a Service2 preflight policy based on the owner's existing published snapshot. It does not prove the contents of a live Avito card. Provider ingestion, per-row processing and visible photograph comparison remain separate evidence.

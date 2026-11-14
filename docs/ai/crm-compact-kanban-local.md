# Compact CRM board — local UI slice

Base: 2cc4e2e26b61ee169abd2c1ce56370a7d6abc3de (includes recurring contracts PR475 and GET hotfix PR476). Original UI commit 4bd22330 is preserved. This delta adds no migrations or backend permission changes.

The CRM pipeline selector moves into the heading between CRM and New deal on desktop; on narrow screens it occupies its own horizontally scrollable heading row. Search, owner, view switching and existing next-action controls remain. Outcome/stage/object/specialization/date controls are removed. Existing filtered URLs/API behavior stays compatible, with an explicit linked-filter notice and a reset that preserves the remaining context.

Lead columns used align-items:start; mobile CRM used separate block columns. They now stretch to the longest loaded column. The whole column remains the existing drop surface, including its lower empty area. Column gaps are not drop targets. Desktop CRM already stretched: this slice explicitly retains and tests that geometry. Transition reasons, final-stage confirmation, conversion, role and organization rules are unchanged. Mobile/keyboard movement inside the card is retained.

Pagination is NOT replaced in this slice. CRM currently returns 50 deals across a selected pipeline ordered by descending ID; totals aggregate the full authorized filtered set. A stage can have a nonzero total but no cards on the current page. Leads use independent 50-card pages per column. Scrolling does not fetch more. The CRM pager is hidden only when both previous and next pages are absent.

Recommended separate bounded pagination slice: per-column Load more, a signed cursor bound to organization, actor, stage and filters, stable descending ID ordering with an initial high-watermark; fixed response limit, ID deduplication and revision-aware replacement. Reset cursors on filter/navigation changes. Recheck current permissions on every request. Concurrent transitions can change membership, so refresh must reconcile it; do not promise snapshot consistency without server snapshot semantics. Keep table pagination. Never simulate loading by returning all authorized rows in HTML.

The supplied Library screenshot could not be materialized (supported helper returned download failed). Its pixels were not inspected. Synthetic browser screenshots are local QA only and excluded from the transferable archive.

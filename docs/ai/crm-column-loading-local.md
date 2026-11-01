# Bounded per-stage CRM board loading — LOCAL draft

Base main2cc4e2e; local dependency PR477 headf9d805b9 (compact controls/drop surfaces),
kept unchanged. No migration, command/right/source/financial changes, CI workflow changes,
publication or production activity. Leads are deliberately unchanged in this first slice.

Acceptance: each stage independently shows first50 authorized matching deals;
expand in50 increments; totals cover complete matching set, preserve currencies/unknown;
no append-offset gaps after inserts/moves/archive; table page50 unchanged; filter/view
changes abort stale requests; current command/revision/reason gates retained.

Implementation: `layout=board` in existing authenticated data route. Strict
`column_limits` only for selected pipeline stages,50/100/150/200, total budget2000;
one SQL window query selects limit+1 per stage before serialization. Card membership
across stages comes from one statement, avoiding a double read during a move. Fresh prefixes replace all displayed
rows; IDs are not accumulated from stale pages. Existing per-row next-task privacy
projection reused. Initial HTML uses board for kanban and page for table.

A deliberate cap200 per stage is displayed explicitly; remaining cards are reachable
with search/table. This is not infinite scrolling or a cross-query database snapshot. A concurrent
change after a read is visible on next refresh; current write revision guards still
reject stale commands. Refresh preserves expanded limits within same scope; search,
owner, stage filters, pipeline and view reset limits. A browser reload starts50 again;
expansion depth is not URL state. No count/row consistency across simultaneous database
writes is promised, as in the existing list; no offset-based permanent skip on expansion.

Performance bound is not a measured hosting SLA. Multiple stages may serialize more
than the old global50. Before production release, evaluate representative data volume;
large (>200) columns use explicit filter/table fallback. A later keyset/snapshot design
can remove this first-slice cap without unbounded responses.

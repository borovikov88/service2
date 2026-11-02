# Materials operational journal — LOCAL, inactive

Source: CRM_Aqualine_TZ_v0_4.md version3 §7 lines293–319, D09–D12,
AC50/52/61–70. Base99d96caf (deployed PR477). Separate from cursor package.
Existing PoolReading.required_materials and crm_object_needs store requests and
versioned need text; neither is an issued/delivered quantity ledger. Reuse Pool,
Client, User, CrmDeal and existing object/deal read policies. Do not generate another
warehouse, nomenclature directory, WaterReading, ServiceTask, contract or deal.

## This vertical slice

One issue record = one explicitly named position/unit/packaging, quantity, assigned
existing Pool, optional matching existing deal, actual date, source, holder/author.
No visit/lead/quote/contract required. Holder is the authenticated employee; taking
on behalf of someone else is intentionally unavailable. Price, realization and
payment are NOT CHECKED (not zero, not absent). No financial or provider writes.

Append-only actual deliveries and office returns, each with date/source/author;
partial quantities cannot exceed the remaining custody amount. Delivery may target
another readable Pool/client with explicit reason; changing deal attribution also
requires a reason. Original issue remains unchanged. Identical command retry reuses
the record; changed payload/revision conflicts. Organization mutex serializes races.
A reviewer may reverse a mistaken movement exactly, preserving its source and history.
Mistaken issuance can be voided only without effective deliveries/returns; void is
not a physical return. No editing/deleting history or silently rewriting quantities.
Customer returns after delivery and financial-document corrections are unavailable.

Dates of occurrence and recording differ. No future facts. Correction cannot precede
the corrected movement. All-period issue list retains unresolved older issues;
this is not yet the monthly billing register. Same names/units are not grouping keys:
no conversion or cross-record aggregation without verified identities/packaging.

History rechecks org/client/object consistency. Changed recipient client masks old
source/reason and blocks further movements for reconciliation. Current object rights
apply to every read/write. No copied communication/private task data.

## Default-deny activation

Existing organization.crm_materials must be true, together with crm_v2_read and
crm_v2_write for writes. New CrmMaterialAccess grants default false for EVERY user,
including administrators/superusers. Current active account, one active Employee,
organization staff membership and existing object access are prerequisites.
record_own: create own issues and own delivery/office returns. review_team: inspect
team issues and reconcile movements/void mistakes; does not imply record_own.
No permission UI, command or real grants/flag changes are included. Protected entry:
/crm/workspace/materials/?org_id=<authorized-org>. Menu activation remains pending.

Question for owner: which specific staff should receive record_own, and which
reviewers should receive review_team? No defaults inferred from names or admin role.
Stock deduction/negative warehouse balances and UOM conversion are not implemented;
they remain blocked until their source/policy is verified, not guessed.

## Schema / integration

0170_crm_material_journal adds three empty tables (access, issue, movement), depends
on public0168. No backfill or defaults enabling users. Cursor0169 is separately
unpublished. Number0170 avoids filename collision but both branches fork at0168:
before a combined release, choose order and validate an explicit merge/dependency
migration. Neither branch is silently imported into the other. Application rollback
must retain tables/history; schema reversal is not a production recovery plan.

## Acceptance boundary

Covered locally: AC61, narrowAC52/62/63; explicit dates/source and unknown finance
parts ofAC69/70. Tests assert zero new deals/tasks/readings, retry/conflict, overdraw,
corrections, org/current-grant boundaries, CSRF/XSS, MySQL races and preservation.
Still incomplete: AC50 monthly register, AC64 aggregation, AC65 canonical resolution,
AC66/67 document allocation and multiple actual contracts, AC68 independent debt,
AC69 real paper migration, late finalized-period reconciliation inAC70. Do not claim
that this operational slice satisfies all AC50/52/61–70 or real live acceptance.

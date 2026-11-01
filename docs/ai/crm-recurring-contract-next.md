# Regular service contract: next local slice

Base: e0ead14e838876d0946fddd41cc1e0f5cbbc1575 (PR473). LOCAL ONLY.
Source: CRM_Aqualine_TZ_v0_4.md Library version 3, §6 service (lines 260–276),
§8 amount meaning (429), §10 (452–458), §11 (468), §13 (504–508),
§15 (545), AC18/23/24/28/53/54/56/57/58/60.
Later decisions override the initial multi-object proposal: one service contract
belongs to one deal/object. Quantity 2 or 0.5 never determines calendar periods.
Manual final CRM stage with warning is allowed; it is not accounting completion.

## Existing code, not a new implementation

- `ServiceContractBinding` (0162) has a unique source/org/contract locator and one
  deal/object. It is created only during confirmed document allocation; it is not
  a registry of all signed contracts and has no contractual term/price versions.
- `ServiceAllocationVersion` stores explicit line/month/Decimal allocations and
  history. `crm_service_bindings` rechecks current rights/source snapshots,
  prevents duplicate consumption, invalidates changed sources. This does not
  prove complete coverage of a contract whose expected periods are not stored.
- `Pool.service_monthly_price`, `service_frequency`, `service_interval_days` and
  status are current operational fields. `ServiceVisitPlan` is unique by pool/week.
- `ManagementMoneyPlan.service_*` describes expectations in the money forecast;
  it must not become evidence of delivery, realization or payment.
- Quote lifecycle (0163) and service handoff (0164) are available, but handoff to
  an existing request/tasks is not a regular-service contract lifecycle.

## Implemented in this local slice

`crm_recurring_terms.preview_terms` is a pure, immutable draft validator, with no
ORM, endpoint, permissions changes or writes. Inputs explicitly specify term,
months, quantity, unit, agreed amount (including unknown), completeness and renewal
review date. Seasonal gaps and partial-month quantities remain explicit. It rejects
repeated/out-of-term months, malformed values and sub-kopeck sums. It neither
prorates partial months nor invents future months or prices. Amounts are agreed RUB
inputs; no currency conversion or historic currency rewriting occurs.

The listed-period subtotal is distinct from the whole-contract total. A total is
available only for a finite term with an explicitly complete list and all amounts
known. Decimal sums/hash do not depend on caller precision or ordering. This is
only a proposed contractual amount, never recognized revenue or payment.
`can_activate` is always false. No allocation or source status is used as authority.
Technical bound: 1200 explicitly supplied rows; not a rule limiting contract duration.

The follow-on local slice adds 0166 (two additive tables): one immutable draft
container per existing deal and append-only revisions. `save_draft` supports
prepare, same-term revise and explicitly confirmed same-contract renew; all
versions remain inactive drafts. A renewed term must follow the previous finite
term without reusing its calendar months. Unknown amounts remain unknown. Current
organization/own-deal/payer/object rights and revisions are checked under locks;
replay rechecks current rights and cannot create a second container/version.
History reads are bounded to 20 versions and deliberately use the same own-deal
write scope; broader team/read-only UI is not introduced by this internal service.

No UI, activation, operational update or auto task is implemented. No 1C locator
is created or reassigned. The draft contract basis is explicit text, not verified
external contract identity: duplicates across other deals must be reconciled at
future activation, never auto-matched by text. This is not a finished active
contract feature. On application rollback keep 0166 evidence tables if populated;
no production migration/reverse/restore has been performed.

## Minimal next vertical package and acceptance

1. Append-only contractual versions for an existing service deal and its single
   object, with payer/party references, signed basis, explicit period obligations,
   work/inclusions/exclusions/seasonality/calculation rules, responsible and review
   date. Draft→explicit confirmation, author/time/revision/idempotency and org lock.
   Do not require an imported realization merely to register a signed contract.
2. Separate authoritative terms from the existing 1C locator; reuse the locator,
   never create a duplicate financial ledger. Verify party/object/contract identity
   at any connection to 0162. Do not infer a real mapping from name/date/quantity.
3. One source of effective price/frequency must be defined before activation.
   Existing Pool editing and calendar/forecast readers must either derive the
   effective value or reject conflicting edits; no silently divergent second field.
4. Period table shows agreed obligation separately from current valid realization
   allocations. Missing/stale/foreign/future/changed evidence remains unknown or
   reconciliation-required. Payment remains unverified until separately supported
   by existing finance source completeness/explicit purpose. No closing/payment fact.
5. Renewal review date is explicit (no invented lead time); no auto-renewal/new lead
   or 12 duplicate tasks. Once renewal semantics are approved, stable operation keys
   and lineage preserve old periods and prevent overlapping double obligations.
6. Tests: org/role/private source denial, manager read vs own-write, inactive actor,
   concurrent version updates/retries, period duplicates/gaps, unknown/zero amounts,
   2 units and 0.5 quantity without inferred months, immutable old versions, changed
   1C source, unrelated chemical debts. Native MySQL migration/races for DB changes;
   browser mobile/dark/cancel/retry/stale version for the future UI.

## Rights and source boundaries

A future UI reuses current CRM organization read rights; writes require current
own-deal capability, ownership, company access and revision. No role grants.
Financial document inspection keeps existing additional finance/1C rights;
ordinary CRM read must not reveal restricted document payloads. Pure DTO validation
is not authorization. No production data, source calls, bindings, sends or cron.

## Owner decisions needed before activation/renewal

- RESOLVED by the owner's reply “1”: renewal of the same contract appends a dated
  revision in the same deal. A genuinely new contract is not silently merged.
  Draft renewal is implemented; this does not confirm activation or external
  source identity. Existing 0162 locator ownership remains unchanged.
- Can confirmed effective contract terms become the single source for Pool price
  and frequency, with changes through a new dated version and conflicting direct
  Pool edits blocked? Until decided, draft validation does not alter active Pool
  fields/calendar/forecast. No automatic proration for partial months is proposed:
  each agreed amount must be entered explicitly.

Actual payment reconciliation, correction policy, multiple visits in one week,
materials ledger and real customer document mapping remain separate work.

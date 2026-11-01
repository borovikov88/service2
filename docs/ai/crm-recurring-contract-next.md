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

## Local implementation and accepted policy

Same-contract renewal appends a dated revision in the existing deal. After explicit
confirmation, effective price/frequency come from that revision, never from a
conflicting Pool edit. Unconfirmed objects retain their existing behavior. No
existing contract is automatically confirmed or imported.

- 0166: immutable deal/object/payer/basis container and append-only draft revisions.
- 0167: separate immutable confirmation with actor/time/effective date. No backfill.
- Own-service-deal UI: prepare/revise/renew, explicit monthly Decimal quantity/unit
  and agreed RUB amount, unknown amount distinct from zero, explicit visit anchor,
  known/unknown frequency, renewal review date, confirmation and paginated history.
- Pure validator bounds 1200 explicit periods, rejects duplicates/out-of-term dates
  and sub-kopeck amounts. A finite complete known-price list can have a total;
  indefinite contracts are never annualized. Its `can_activate=False` means a pure
  preview is not authority: a separate locked confirmation command is required.
- Commands recheck active user, organization, own-deal capability, current payer and
  object scope. Org/deal/Pool locks serialize revision/confirmation and conflicting
  Pool writes. Replay does not create another revision/confirmation.
- Effective date cannot be retroactive. Same-term revisions preserve past rows;
  partial-month amount changes need explicit allocation, so are rejected here.
  Renewal keeps earlier versions, real documents and payments untouched.
- Object/client/list read projections and calendar derive dated terms in batches.
  Unknown/missing months never fall back to stale legacy prices. Existing visit
  plans and actual readings remain; reading evidence cannot cross a revision.
- Model save/clean, default/base queryset update, bulk update and upsert guards
  cover ORM API/admin/import paths. Independent fields remain editable. Existing
  forms disable protected fields and preserve stored legacy values. Raw SQL is not
  an application API and this package does not claim database-trigger protection.
  Expression/bulk changes of protected fields fail closed, even if a caller intends
  a no-op; constant unchanged values are allowed.

## Explicit limits and next slices

One authoritative contract lineage per Pool: a different contract/deal on an
already confirmed Pool is blocked pending explicit transfer/reconciliation. No
matching by textual contract number. ClientOwnedObject drafts can be prepared,
confirmation currently requires Pool. This is deliberately narrower than a full
external contract registry. History UI currently uses own-deal editing rights and
requires an open service deal; no broader team access is granted.

The existing calendar supports one visit per object/week. Two dates in a revision
transition week are rejected, not collapsed. Explicit first visit is required for
a known frequency; quantity never supplies that date. Cross-revision manual moves
require reconciliation. Calendar does not rewrite stored visit plans or readings.
Legacy broad-period missed-visit notifications remain unchanged until the first
terms effective date, then are suppressed for that object because they cannot
prove contractual lateness. The dated calendar remains available; replacement
contract-aware reminder delivery is a separate slice and is stated in the UI.

History shows contractual obligations separately from unverified realizations and
payments. This package neither wires in 0162 allocation coverage nor proves payment,
creates money plans, closes contracts, sends messages or generates tasks. Full
scope/inclusions/exclusions, automatic renewal reminders and completeness-aware
realization/payment comparison remain subsequent work. Review date is stored, not
an automatic renewal action. Payer selector is bounded to the first 100 authorized
clients plus the current payer; a general searchable picker is not included.

On application rollback preserve 0166/0167 tables and evidence. Do not reverse
populated migrations. Older application code cannot enforce these new ORM guards:
a rollback after real confirmations requires holding conflicting object edits
until compatible code is restored. No production migration or rollback was run.

## Acceptance evidence

Synthetic tests cover dated unknown/zero behavior, same-lineage renewal, source
history preservation, canceled/unsubmitted forms, repeat commands, rights denial,
Pool ORM guards, additive migrations, MySQL locks/retries and calendar boundary
cases. Browser scenario covers desktop/light and mobile/dark, keyboard submit,
reload/cancel/replay. Local system Chromium differs from the CI-pinned binary;
final local receipt records the actual executable and test results. No live UI or
real contract/data acceptance is implied. Publication requires a separate grant.

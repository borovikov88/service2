# 1C direct order costs in Service2 gross-profit reporting

## Goal

Include direct supplier/service expenses that 1C posts to a specific customer order in Service2 customer/order cost and gross profit.

Current Service2 gross-profit import reads `AccumulationRegister_Продажи_RecordType`. This captures revenue and standard realization cost, but it does not capture direct order expenses posted separately by 1C in `AccumulationRegister_ДоходыИРасходы_RecordType`.

The result today is that a customer/order can have the correct standard realization cost but omit contractor, transport, and similar direct expenses from the Service2 customer cost.

## Confirmed live example / acceptance control

Customer order:
- `Document_ЗаказПокупателя` № `НФНФ-000114`, Ref_Key `b1fd48d6-915c-11f1-8f70-fa163e1420a5`.

Sale:
- `Document_РасходнаяНакладная` № `НФНФ-000335`, 2026-09-17.
- Revenue: `94 494.00`.
- Current standard realization cost is about `29 696.64`.

Direct supplier expense:
- `Document_ПриходнаяНакладная` № `НФНФ-000310`, 2026-09-01, total `30 000.00`.
- Its movements in `AccumulationRegister_ДоходыИРасходы_RecordType` contain two active order-linked expense rows for customer order `b1fd48d6-915c-11f1-8f70-fa163e1420a5`: `25 000.00` and `5 000.00`.
- 1C labels these movements `СодержаниеПроводки = Прочие расходы` and they later appear in 1C financial result for the same order.

Expected Service2 managerial result for this order after refresh/confirm:
- revenue: `94 494.00`;
- total cost includes standard realization cost plus the direct `30 000.00`;
- gross profit is reduced by exactly `30 000.00` versus the current Service2 result;
- the direct `30 000.00` is attributed to the customer from order №114, not to supplier Мороков.

Do not hardcode the example GUID, customer, supplier, amount, or document numbers in production code. The example is only an acceptance control.

## Accounting rule

A direct-order expense is eligible only when all of the following are true:

1. It is an active movement from `AccumulationRegister_ДоходыИРасходы_RecordType`.
2. `Организация_Key` is one of the configured/allowed 1C organizations.
3. `ЗаказПокупателя_Key` is present and is not the zero GUID.
4. The movement belongs to a supplier receipt: normalize `Recorder_Type` and require `Document_ПриходнаяНакладная` for this release.
5. `СуммаРасходов` is non-zero. Preserve the sign so corrections/returns reduce cost instead of being converted to positive expense.
6. The movement period is inside the requested OData import period.

Do NOT import normal realization expense movements a second time. In particular, sales-document movements such as `Document_РасходнаяНакладная` / `СодержаниеПроводки = Отражение расходов` remain represented by `AccumulationRegister_Продажи_RecordType` and must not be duplicated.

The first release intentionally limits direct-order expense recorder type to `Document_ПриходнаяНакладная`. Do not broaden this to bank payments (`РасходСоСчета`) or generic cashflow: payment is DDS, not a second cost event.

## Attribution

For every eligible direct expense movement:

- resolve `ЗаказПокупателя_Key` through `Document_ЗаказПокупателя`;
- take the customer (`Контрагент_Key`) from that customer order;
- take the responsible/manager (`Ответственный_Key`) from that customer order when available;
- resolve readable customer and manager names through the existing catalog-resolution path;
- use the expense movement `Period` as the accounting month;
- query the receipt document by `Recorder` only for readable/auditable document metadata (number/date), not to decide the customer;
- never attribute the expense to the supplier from the incoming receipt.

If the required order/customer reference cannot be resolved, fail the OData draft safely rather than silently assigning the cost to an empty/wrong customer.

## Representation in OneCMonthlyProfit

Integrate these expenses into the existing immutable OData profit draft -> confirm -> active-version flow. Do not read live 1C from the dashboard/page request.

Preferred implementation: append validated cost-only rows to the same OData profit snapshot that already stores sales rows.

For a direct expense row:
- `revenue = 0`;
- `cost = СуммаРасходов`;
- `gross_profit = -cost`;
- use a readable generic nomenclature label such as `Прямые расходы по заказу` unless the source can be resolved safely without an unbounded extra read;
- preserve explicit source metadata identifying the row as a direct order expense and containing normalized recorder type, recorder GUID, line number, order GUID, and original `СодержаниеПроводки`/account/operation keys when available;
- document name should identify the originating incoming receipt when its number/date are resolved;
- source identity must be deterministic and collision-safe versus normal sales rows. Include a direct-expense namespace in the identity if needed.

Do not introduce a second managerial-profit formula in the template. Once imported, existing `profit_dashboard` aggregation should include the cost-only rows in totals and `customer_breakdown`.

## Draft/confirmation semantics

Direct order expenses must be part of the preview snapshot before confirmation:

- preview totals must show the added cost and reduced gross profit;
- active confirmed data must remain unchanged until confirm;
- confirming the exact snapshot must not re-read live 1C and must create the same cost rows shown in preview;
- automatic refresh/unified sync must use the same code path;
- preserve compatibility with already confirmed historical batches and, where practical, pending older OData draft snapshots.

Do not bypass the existing active-period/versioning model.

## OData safety

Reuse the existing OData transport/configuration and its controls:

- GET only;
- configured HTTPS base URL and server credentials only;
- no caller supplied raw URL/filter/select;
- same-origin pagination only;
- configured organization scope;
- bounded rows/pages/batches;
- validate required metadata fields/types before trusting values;
- revalidate organization, activity, date range, recorder type, order key, and numeric values on returned rows.

The live register fields needed for this release are expected to include:
`Active`, `LineNumber`, `Period`, `Recorder`, `Recorder_Type`, `ЗаказПокупателя_Key`, `Организация_Key`, `СодержаниеПроводки`, `СуммаРасходов`, plus optional source metadata fields already published by 1C.

Live metadata currently publishes numeric `СуммаРасходов` as `Edm.Double`; implementation must match actual metadata conventions used by the existing OData reader rather than assuming Decimal-only fields.

## Customer report behavior

After refresh + confirm, the Service2 customer report must treat direct-order cost rows exactly as cost for that customer/order:

- customer cost increases by the direct expense amount;
- customer gross profit decreases by the same amount;
- revenue is unchanged;
- manager filtering follows the customer order responsible when resolved;
- the detail view remains auditable and distinguishes the direct expense from a sale line;
- no duplicate document-collapse logic may hide the cost or cause it to be counted twice.

## Required tests

Add focused regression tests at the import/draft/dashboard levels.

At minimum prove:

1. A normal sale plus two eligible receipt expense movements (25,000 + 5,000) produces preview/confirmed/dashboard totals where cost increases by 30,000 and gross profit decreases by 30,000.
2. The expense rows resolve to the customer from `Document_ЗаказПокупателя`, not the receipt supplier.
3. `ЗаказПокупателя_Key = zero` is ignored.
4. `Active = false` is ignored/fails closed per existing reader policy and cannot affect an exact total.
5. Another organization cannot leak into the target organization import.
6. A sales recorder / `Отражение расходов` is not imported as direct expense and therefore standard realization cost is not doubled.
7. A negative direct expense movement reduces cost correctly.
8. Unresolved customer order/customer references fail the draft safely rather than creating an unattributed row.
9. Preview snapshot and confirm remain immutable: confirm does not fetch a different live expense set.
10. Existing OData sales, retail grouping, XLSX imports, and profit dashboard tests remain green.

Run targeted OData profit draft/preview/unified-sync/dashboard tests, Django check, migration dry-run, and the full Django test suite.

## Delivery / review gate

This changes a financial calculation/data path. Do not merge or deploy from the implementation task.

Before merge, require:
- green normal PR CI on the exact GitHub head;
- independent substantive Codex review of the exact final diff/head;
- formal independent `APPROVED` according to the repository review gate.

No migration should be added unless the implementation can demonstrate why the existing `OneCMonthlyProfit` snapshot/model cannot safely represent direct cost-only rows.

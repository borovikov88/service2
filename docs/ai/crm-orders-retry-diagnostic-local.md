# CRM order retry diagnosis (local, not deployed)

Read-only public Actions evidence on 18 November 2026:
- finance worker run 44268004645, job 134677236623 (08:33 UTC);
- run 44212393857 (01:00 UTC);
- run 44145632100 and 44050347790 on 17 November.

All four completed SSH worker commands report `steps:0`,
`finance_position_state:completed`, `money_forecast_state:completed`,
`crm_orders_state:retryable_error`. GitHub success means the scheduled command
returned successfully, not that order evidence refreshed successfully.

`worker_tick` counts only unified-report cursor steps. For a completed financial
run it skips that loop, then separately calls the CRM order finalizer. Thus zero
steps neither proves a hang nor means no order-read attempt occurred. Synthetic
characterization uses the real worker/finalizer with a failing mocked order
wrapper; no live import or production action was performed.

The old catch collapses configuration, source-read and snapshot-validation
failures into the same generic message. The historical logs do not establish
which occurred. Permission denial follows a separate `failed` branch, not this
retryable branch. These receipts do not contain employee/payroll scope, months,
rows or mapping, so they cannot prove salary completeness or its cause.

The local diagnostic-only change attaches allowlisted `config/read/apply/unknown`
stages to already-retryable CRM exceptions, persists the stage and exports only
that enum. No raw exception, source URL, credential or financial value is printed.
Permission checks, import/apply behavior and retry semantics remain unchanged.
Success/disabled/permission failure do not retain a misleading prior stage.
This change does NOT fix or establish the underlying production order failure.

After an authorized reviewed release, inspect the next normal receipt to locate
the failing stage, then investigate that exact branch with authorized private
metadata. Do not trigger an import merely to obtain a diagnostic, bypass a 403,
or weaken source/organization/financial validation to force success.

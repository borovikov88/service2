# CRM acceptance checkpoint — 2026-11-01, LOCAL source review

Source: `CRM_Aqualine_TZ_v0_4.md`, Library `libfile_83c4d48b69c08191b25419bb1e8a2dba`, version3,
§18 lines608–683 (AC01–AC70). Fresh fetched main: `2cc4e2e26b61ee169abd2c1ce56370a7d6abc3de`.
The old audit in spec§1–3 is not a description of today's code.
This is a bounded source/test inventory, not a new execution of all70 acceptance
scenarios or production acceptance. Deployed does not imply verified live write UX.

## Implemented building blocks, not to implement again

| Criteria | Current source and test evidence | Remaining acceptance boundary |
|---|---|---|
| AC01,02,08,16,33,48 | `services/crm_inbox.py`, `crm_lead_intake.py`, `crm_lead_commands.py`; `tests/test_crm_lead_intake.py`, `test_crm_lead_commands.py` | Idempotent capture/claim, verified conversation context, cutoff and employee identity exist. Instant production wakeup still unproven; minute fallback is not proof of post-commit launch. |
| AC03,04,09,17,34 | `crm_lead_intake._context_key`, lead continuation command; `test_explicit_continuation_routes_context_preserves_sources_and_pages` | Only explicit confirmed context continues; new call IDs are not automatically same need. Fully automatic classification of new work vs existing contract is NOT complete. |
| AC06,21–23,39,46 | `services/crm_lead_access.py`, `crm_commercial_access.py`, `crm_permissions.py`, protected source metadata; org/privacy tests | Current per-source/org rights exist. No claim every export/notification of every future feature was accepted live. Owner-private applies only verified owner+peer; not every personal-type phone. |
| AC10,11,19,20,27,28 | commercial command model/revision guards, object selection, own next-action; `tests/test_crm_live_views.py`, `test_crm_commercial_card.py`, `test_crm_live_pilot_commands.py` | Planned object is not active Pool; task edits do not establish deal finance facts. Manual final CRM stage now explicitly allowed with warning by later owner decision. |
| AC12,13 and limited14 | `services/crm_quotes.py`, `tests/test_crm_quotes.py` | Immutable accepted version/variant, explicit sent/accepted/rejected/replaced exist. Printing is not sent. Actual channel delivery of quotes is not implemented by this lifecycle; unknown manual send is not delivery proof. |
| AC18,49 | `services/crm_handoffs.py`, `crm_construction.py`; respective tests/browser modules | Existing service request/tasks, personal acceptance; existing construction project with explicit launch authority. No duplicate execution container or advance gate. Real project launch is not performed by this task. |
| AC24,53,54,57,60 | `crm_service_bindings.py`, `crm_service_binding_reader.py`, `test_crm_service_bindings.py` | Document identity/version, explicit months and partial quantities, reconciliation guards exist. Universal bindings are not actual verified allocation of any real customer's documents. Negative corrections and full financial closure remain incomplete. |
| AC30,32 | `crm_live_views._workspace_data`, workspace JS and browser tests | Existing filter totals cover full authorized query. General page hides stages with older deals; local cursor-loading draft for both boards addresses that narrow gap. No hosting p95 claim. |
| AC37,38,39,51 | `services/crm_object_needs.py`, corrections/need-deal services and tests | Versioned source need and reviewed per-position modes exist. This is NOT a quantitative warehouse issue/delivery/return journal. |
| AC53,56,58,60 (partial) | `crm_recurring_terms.py`, `crm_recurring_drafts.py`, `crm_recurring_effective.py`, `crm_recurring_reminders.py` and tests | Explicit dated contractual obligations, same-deal renewal, protected effective terms and retained reminder policy exist. They do not prove realization coverage or full payment. |

## Open or not fully re-accepted

- AC05: contact source/roles exist, but complete acceptance of third-party phone extraction without customer replacement not established by this review.
- AC07: next action exists; dedicated missed-call attempts/result/SLA end-to-end acceptance remains open.
- AC14–15: existing Avito outbox/external source tests exist; no new real provider delivery/history verification here. Quote delivery integration is still separate.
- AC25,36,55–58,68: no completed unified contract-specific payment/return/completeness reconciliation and financial success gate proven. `no_current_receivable` is never sufficient payment evidence.
- AC26: migration foundation and unknown legacy outcome exist; actual complete migration inventory and owner acceptance of ambiguous rows remain open, no mass migration authorized.
- AC29: manual CRM and intake status exist; instant production event gate remains open. This task does not add host instrumentation or operate cron.
- AC31: existing material/task generation and correction tests exist; complete legacy operational acceptance not rerun here.
- AC35,56 original unconditional final-stage blocking was superseded by explicit owner authorization of manual CRM completion with warning. Financial completion remains a distinct unresolved acceptance, not fabricated by stage change.
- AC40–45,47,59: manual tender pipeline exists; Avindex auto-selection/parser/lot identity/deadline lifecycle remains deferred. Owner has not supplied selection criteria; do not invent them.
- AC50,52,61–70: complete quantitative materials issue→delivery/return/reassignment→monthly aggregation→partial document allocation is still missing as a coherent vertical slice. Textual required-materials need and service realization allocation do not satisfy it. Preserve package/UOM, separate one-off contracts and late delivery dates when implementing.
- §16: full CRM funnel/source/reaction/conversion analytics not established; finance reports are not a substitute.
- §17: production migration and per-role live acceptance remain operational tasks, not inferred from synthetic tests.

## Next order

1. Review local signed cursor loading for both boards (AC30/32 and owner's Nov1 UI request), separately from PR477; migration0169 adds one invalidation table, not business data.
2. Quantitative materials issue journal (AC61/52/69) with no invented delivery/financial facts; then partial delivery/return and reassignment (AC62/63/70), then monthly aggregates/contracts (AC64–68).
3. Contract financial completeness/reconciliation and real mapping after explicit verified evidence; quote actual delivery separately.
4. Intake natural-event proof remains separate ops work; tender auto-selection waits for owner criteria. None blocks independent LOCAL development.

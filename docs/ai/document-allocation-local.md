# Explicit service document allocation preview (local only)

Builds on the existing bounded realization-document adapter, not profit-register
movements. Allocation inputs select an exact current snapshot/line fingerprint,
calendar month, decimal quantity and amount. Duplicate line/month selections,
changed/moved lines, non-finite values and over-allocation are rejected. Equivalent
reordered proposals have the same digest. This digest is NOT durable idempotency.

Quantity 2 or 0.5 never establishes two months or a completed half-month. Explicit
allocation remains a proposal. Future/unposted/deleted documents and future periods
are flagged; realized amount, paid amount and confirmed months remain unavailable.

There is no API, new model, DB migration, import, source write, financial ledger or
production binding. The pure validator is not an authorization boundary. Before a
writer can be added, require current authenticated CRM/finance rights, a verified
one-contract/one-deal/object association, confirmed service line/unit scope, fresh
source reread, durable unique consumption and explicit reconcile history. Unknown
correction and line-renumbering rules remain blocking for credit, not silently
interpreted as new service. Existing customer identity alone does not prove the
contract association. No production source or client data was used in fixtures.

Next implementation gate: establish the authoritative contract association and
approved correction/reconciliation contract, then add additive bindings and
transactional conflict tests. Never infer payment from realization, balance or
absence of receivables. No live import/transport was executed for this package.

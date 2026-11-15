# Private single-call CRM diagnosis (local change)

Base: `667d33cd6b6b172445566e891d36c0fde117713e`. No production inspection or deployment.

An already authorized private operator may use the following only in a separately
authorized installed environment; this command does not create an access path:

```sh
python manage.py crm_call_diagnostics --organization <id> --actor <authorized-user-id> --call-id <id>
```

The actor is the operator's corresponding account, not an arbitrary privileged
account to impersonate. Use private local stdout; do not put results in public
Actions, shared logs or tickets. There is no endpoint, workflow, sync or replay.
Local synthetic tests use `--settings=service_site.test_settings`.

Existing CRM runtime-administration policy (owner/admin/superuser plus active
membership, HR and view_leads), current communication call visibility, source
binding and owner+peer privacy all apply. Being Django staff grants nothing.
The same `call_diagnostics_unavailable` with no stdout covers absent, foreign,
denied, private, corrupt and unreadable-linked sources. A work call by another
employee to an owner's marked peer is not globally blacklisted.

For an authorized source, output is limited to booleans, bounded state/category
enums and up to 20 latest revisions. More revisions means more source history,
not duplicate leads. `lead_linked` requires current access to the related card,
including its other sources. Unknown stored error strings become `other`.
An unexpected inbox state fails closed with `call_diagnostics_unavailable`.
No numbers, names, content, recordings, provider identifiers, keys or raw errors
are emitted. There are no organization queue totals.

`source_gate` / per-revision `activation_gate` report only current eligibility:
disabled, activation_missing, not_incoming, unverified, historical, eligible.
`eligible` does not prove a worker ran or will run. `capture_recorded=false`
means no saved inbox revision for this authorized call, not an instruction to
repair it. Wakeup is configuration only, not scheduler health. Reads are a
bounded observation with a final authorization recheck, not an atomic snapshot
or a claim about provider history. No processing, mutation, filesystem locks/writes, last-run
updates, wakeup, provider or AI requests occur.

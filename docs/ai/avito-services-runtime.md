# Scoped runtime evidence and one-time services setup

Release scope: Service2 connection4 / Avito446312402 only for configuration,
webhook registration and owner message quiet policy. No ad/payment mutations.
Other connections retain their account settings.

The global heartbeat keeps an additive checks JSON list. The real native
supervisor begins a run and clears previous check evidence; worker writes use
that exact active run_id. Each row captures check/outcome/fixed code/current
account, organization and channel. UI/receipt filter current binding and
completed run timestamps. Legacy heartbeat with no rows means unknown cause;
cached old errors are shown separately and never attributed to a new partial.
Django/DB evidence failure cannot create a false successful check.

The protected post-deploy command configure_avito_services_workplace requires
the exact deployed SHA, its deployment-only environment guard and the known
HTTPS AVITO_SERVICES_PUBLIC_ORIGIN resolved by public_origin(), with matching
CLI origin and ALLOWED_HOSTS. The unrelated generic SITE_URL is not an authority
for this scoped setup.
It targets only connection4/account446312402 and derives the actor from an
already valid advance-monitor subscription. Missing/revoked actor or changed
keys/account fail closed. Auto-report configuration is checkpointed once:
future deploys never re-enable an owner's later opt-out.

Webhook registration reuses the existing Avito client and callback URL rules.
Only a missing callback is registered, then subscriptions are reread. An
ambiguous POST is never blindly repeated: a durable attempted checkpoint and
current hash allow later readback; unresolved registration requires the owner's
existing explicit reconnect UI. No stale callbacks are deleted. Registration
and incoming delivery are distinct. Delivery requires a newly authenticated,
account-validated ingested message under the current registration window,
credential fingerprint and token hash. Legacy last_received_at is insufficient.

Detailed reports are fetched by the existing native worker and keep their
normal hourly due/backoff behavior. Enabling is not successful background
execution: per-run success after enabled_at must be observed. Report warnings
do not cause worker failure merely by their count. Message categories use
provider text, never a guessed numeric-code mapping. Public receipts expose
fixed semantic reasons and allowlisted parameter names, not raw titles/values.

Publication correlation runs only after a valid complete scoped report.
Its exception rolls back its savepoint, preserves the successful report,
and is recorded as a separate failed publication_correlation check.

New webhook owner notifications for the opted-in services account use quiet
hours21:00–08:00 Asia/Barnaul, including retries. This leaves a delivery buffer
for the20:00 account slot. Messages are ingested normally. Only the authorised
setup owner's immediate notification is deferred; other recipients/accounts
are unchanged. A durable message-ID range produces one in-app morning digest
through the native worker. Disabled preferences/errors leave the range pending.
No outgoing client response is created; no private text/recipient data is logged.
The complete four-slot account schedule and healthy/monday reports remain
separate original-TZ work and are not asserted by this increment.

Tariff date remains explicitly unconfigured until entered from the cabinet.
Neither money nor listing finish_time is substituted for the subscription date.

Validation: targeted runtime/scope/setup/ambiguous-registration/delivery-window/
quiet-hour/preferences/message-semantics regressions; full Django and MySQL CI,
independent final-head review, protected deploy/health, then native worker and
read-only provider evidence. Local execution unavailable in this chat.

Storage proof uses a signed120-second POST to the fixed Passenger probe route,
not filesystem writes by the SSH operator. Setup scope/generation commits before
HTTP; no connection lock spans the self-request. The view handles a synthetic
JPEG under the real Passenger UID, returns fixed booleans and removes it.
Redirects are denied, timeout10s, responsebounded2KiB; no token/path/bodylogged.
Only success is checkpointed; failure remains an independent functional result.
Incoming-message and legacy-event writes compare the original authenticated
account/channel/org/token/active binding before replacing it under a row lock.
Quiet digest flushing also rechecks current conversation-view permission.

Origin follow-up: the previous services_setup_origin_mismatch guard compared the
CLI origin with generic SITE_URL, despite the dedicated Avito origin already
being configured. Setup, media and publication now share the dedicated authority.
The constructed callback is checked for canonical HTTPS host, exact connection
path/token and no query/fragment before token persistence or provider POST.
Legacy-origin subscriptions and cached legacy-origin proof cannot confirm the
new canonical webhook. Origin diagnostics export booleans/source only, never
URL values, callback paths or tokens. Global SITE_URL and account settings are
not rewritten by this fix.

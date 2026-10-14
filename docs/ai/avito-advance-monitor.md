# Avito advance balance and personal alerts

The wallet (real/bonus money) and the current CPA advance balance are separate.
The advance reader uses only `POST https://api.avito.ru/cpa/v3/balanceInfo`,
`X-Source: Service2`, an empty JSON body and a conservative shared60-second
rate budget. This is the provider's read-only balance method, not a payment or
promotion. No deprecated v2 advance-issued amount or wallet fallback is used.

Only a bounded signed integer balance in kopeks is accepted. Missing, boolean,
float/string, error or denied responses are unknown, never zero. Scoped cached
data shows rubles, original success time and stale/error state on failure.
Dashboard GET does not contact Avito.

Owner/admin channel managers self-subscribe through CSRF-protected POST on the
Avito page. The server uses request.user, never a submitted recipient. Another
admin cannot change this person's subscription. Existing scheduler-readiness
activation and user notification preferences remain. Keys, account, organization,
channel, recipient access and active state are checked before/after worker reads;
account self is checked before/after the balance request. A revoked binding
disables the subscription and does not send a financial alert.

The existing native15-minute worker checks due advance subscriptions independently
before Autoload reports. Its full status-scan time reservation stays in place.
Successful manual advance refreshes also evaluate alerts inside the connection
transaction. Shared leases and row locks prevent concurrent duplicate delivery.
Only the configured recipient receives in-app communication notifications; no
email, messenger or push delivery is added.

Zones: healthy above100RUB, low greater than0 through100RUB inclusive, empty at0
or negative. Send one alert on entering low and one on entering empty. Repeated
polls in the same zone do not repeat alerts. A jump directly to empty sends only
the empty alert; never invent an observed100RUB balance. Topups rearm the relevant
transition. API failures do not change zones. Notifications and zone/cache changes
commit atomically, with existing per-user notification preferences respected.

The standalone read-only operator may test endpoint availability on saved keys.
Its strict receipt publishes only available/source/error metadata, never current
financial amounts or raw responses. All real values stay in the scoped Service2
cache/UI and requested personal notification. Unit-test values are synthetic.

Release: exact-head CI, independent financial/security review, protected normal
deploy and health; then verify endpoint availability separately from subscription
activation and delivery. A manual self-subscription is needed to bind the recipient
to their authenticated Service2 user. Do not choose a production user by guessing.

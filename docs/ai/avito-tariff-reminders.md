# Avito renewal-date reminders and notification evidence

CPA advance, wallet balance and a tariff's calendar validity are different facts.
Advance warnings at 100 RUB / zero do not observe the paid plan's expiry date.
The current goods/CPA integration has no validated API reader for that date.
Do not infer expiry from balance, an API error, last payment or a presumed month.
Transport and real-estate tariff endpoints are not evidence for a goods account.

The separate renewal reminder uses a date explicitly entered by the authenticated
channel manager from their Avito cabinet. Its source is always "manual". Neither
UI nor notifications claim the actual plan became inactive. After renewal, the
user must enter the new date. Missing configuration remains visibly unmonitored;
the operator must not guess the date or the production recipient.

In the configured communication timezone, send the current most urgent stage:
within three days, tomorrow, and when the entered date has arrived for legacy subscriptions. First enabling
late emits only that stage; no burst of missed historical stages. Repeated checks
do not repeat a stage. Changing the date creates a new generation; enabling an
unchanged valid date retains the generation. The existing native 15-minute worker
checks calendar reminders before provider calls, with a bounded local scan.

CSRF-protected self-subscription is bound to actor, organization, channel and
account. Another admin cannot change the person's reminder. Key/token rotations
and missing Avito credentials do not cancel a manual calendar reminder. Revoked
membership/capability, account or organization changes disable it without delivery.
Fresh supervised worker evidence is required for activation.

Notification creation, deterministic generation/stage dedupe and state updates
share a connection row lock and transaction. In-app opt-out does not consume the
stage; it shows a visible error and can recover after opt-in. Delivery is in-app
only. No push, email, SMS, payment, renewal or promotion is performed.

The read-only operator now exposes configuration/evidence flags for both advance
and date reminders. It publishes no amount, manual date, recipient ID/name, keys
or notification message. Notification-existence evidence is limited to the exact
advance recipient, organization, connection and generation. Missing evidence does
not prove a failed delivery or a historical threshold crossing.

Release: exact-head CI and independent review, normal protected deploy and health;
then read the sanitized live flags. Live activation and delivery are separate from
code readiness. An authenticated user must enter their real date and self-subscribe.


## Services connection4 / account446312402

The Services reminder adds a seven-day stage to the existing three-day,
one-day and due-date stages. Other connections keep their existing schedule.
All Services dates and quiet-hour boundaries use Asia/Barnaul, including when
the generic communication timezone differs. Only the current most urgent stage
is emitted on a late first activation; repeats use the same generation/stage
dedupe. A skipped night scan or retry does not consume a delivery stage.

Owner reminders are deferred during21:00–08:00 to the next08:00Barnaul slot.
This affects only the calendar reminder, never the manager's explicit outgoing
reply. No device push, email, renewal, activation or payment is performed.

An explicit authenticated date save records source=manual,
confirmation_source=avito_cabinet_manual, server confirmed_at and confirmed_by.
Saving the same date refreshes that provenance without resetting the generation
or resending warnings. Disable/retry are not new date confirmations. Historical
manual dates without these fields remain visibly time-unconfirmed; no timestamp
is inferred from a balance, payment, last scan or an API failure. An unset date
stays unset until the owner enters the date shown in their Avito cabinet.

A displayed successful external old→active status/new-term transition is not
proof of a free entitlement or a Service2 action. Supported same-ID activation
and a pre-action zero incremental price are still unverified for this account.
The UI gives an exact existing-card manual sequence and keeps automatic return
blocked. Tariff/contact/promotion charges are separate from the incremental
price of returning that card. Do not create a copy, deactivate an active card
for a test or buy a paid option merely to check this feature.

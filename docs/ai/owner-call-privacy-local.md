# Owner-only call privacy: local package

Base bbd6ef4. No publication, provider changes, activation or production operations.

Uses the existing CallPrivateNumber(org, owner, peer) and its owner-only audited
settings service. A contact-card action explicitly marks the current phone snapshot;
new imported numbers are not silently added. Managers never receive this state.
Phone type personal is unrelated; shared Avito/website messages are unchanged.

The authenticated MegaFon history transaction checks exclusion before PhoneCall,
recording_ref, inbox and recording download. Existing rows are retained unchanged
on a private replay. No raw payload/tombstone is persisted for excluded events;
replays re-evaluate the current policy. Removal is not historical data erasure.

Early exclusion requires one current manually confirmed TelephonyEmployeeIdentity,
matching connection/org and every supplied user/extension, active employee/user,
owner membership, no reassignment flag and confirmation before event time. Names,
assignee, contact-user links and a phone match alone are not identity proof.
Missing/ambiguous/time-inconsistent mapping is not guessed: existing capture and
conservative saved-call protections remain; early non-storage is NOT guaranteed
until identity readiness is established. No existing privacy protection is relaxed.
Saved-call media/AI/intake/SQL visibility guards remain unchanged and are regression tested.

Read-only setup diagnosis (authorized local/test DB or separately authorized host):
`python manage.py preview_owner_call_privacy --organization <id> --owner <id>`.
Use test_settings for local tests. Output contains counts/reasons only, no phones,
labels, credentials or provider requests. No production invocation performed.

Internal calls already have a separate provider-history importer and are excluded
from CRM intake/source links. This package does not infer internal from one staff
participant or turn an external transfer into a whole internal call. Existing rows
belonging to another participant cannot be privately reclassified by the final leg.
The provider's call/transfer segmentation still needs explicit schema evidence for
mixed-party recordings; the webhook lacks reliable complete participant history.

Official public docs checked read-only: https://www.megapbx.ru/record describes
excluding an employee entirely and transfer limitations. That is NOT owner+peer
filtering and would suppress the owner's business calls. Management API overview:
https://vats.megafon.ru/management-api . Linked https://api.megapbx.ru was inaccessible
through the supported web reader. Pair-specific recording control is unverified;
no provider setting or credentials were changed, no alternative route attempted.

Rollback is application-only. Existing calls, recordings, transcripts and privacy
settings are not deleted by this package. No schema migration is required.

Local validation: 61 selected Django regressions PASS with 5 existing conditional
skips; 8 focused cases PASS after the stored-attribution fix; owner/staff mobile
browser scenario PASS on installed Chromium 154. check and migration drift checks
run locally. Independent runtime review found no remaining blockers after the
profile/provider-only transfer regression was fixed. Native MySQL and pinned CI
Chromium remain release gates; local SQLite skips do not replace them.

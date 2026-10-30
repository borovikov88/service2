# Owner-only serving process status (LOCAL)

Use the existing authenticated `crm/workspace/leads/data/?org_id=1` route.
No new endpoint, credentials, source diagnostics, process ID, paths, environment
listing or changes to eligibility/permissions. Existing CRM read access and an
active owner membership are required; manager/superuser alone is insufficient.
Response remains no-store. No file/DB writes, launcher calls or queue reads added.

Before publishing, operator must demonstrate that the existing logged-in owner
browser can GET this existing route (200, not login HTML/401/403). Beget shell
access is not this prerequisite. Do not copy cookies or bypass network denials.
Read only HTTP status and `intake.serving_runtime`; never export the lead list.
After an authorised release, existing browser's same-origin fetch can select only:

    const r = await fetch('/crm/workspace/leads/data/?org_id=1', {cache:'no-store'});
    const data = r.ok ? await r.json() : null;
    console.log({status:r.status, runtime:data?.intake?.serving_runtime ?? null});

The literal `crm-wakeup-serving-v1` proves this version of the diagnostic function
was loaded, not the whole Git checkout SHA. `module_loaded_at` is module import
time, not OS process start time. `event_wakeup_enabled` comes from Django settings
in the responding process. `configuration_gate` distinguishes disabled web flag,
nonpilot org and enabled configuration. It does NOT simulate an event:
`event_gate=not_observed`. No source timestamps or private-call counts are exposed.

This is the best minimal existing-route check for the current gap, but has limits:
- CLI output, disk SHA and .env contents do not prove web settings.
- Missing field can mean old web code or absent owner role; verify authorization.
- Owner GET may hit a different worker from the webhook. Repeated reads do not
  prove coverage of all workers; do not claim fleet-wide or webhook code identity.
- No event observations/counters are added because they could leak private traffic
  and still would not reliably cross process boundaries.
- Snapshot `absent` still proves neither successful wakeup nor an executed callback.

If a responding owner process reports disabled flag, investigate its existing
configuration/restart provenance before changes. If enabled but actual webhook
remains unobservable, report that remaining process-attribution gap; do not ship
another speculative diagnostic sink or weaken guards. The earlier file snapshot
can still supply evidence when a current callback actually fails.

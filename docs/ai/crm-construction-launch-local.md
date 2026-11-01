# Existing construction project: local launch package

Base: verified service handoff 2d6baf60 on deployed main 095bf71d. Original
read-only preview 10085cc remains unchanged on its own branch. This is a separate,
unpublished package; no production authority, data, configuration or deployment.

Scope: §10/D08 explicit first launch into an existing CrmItem(project) with an
existing Pool and an accepted immutable calculator quote. The actor needs current
commercial/client/project/source read access AND explicit organization/user launch
authority. The actor need not be the deal manager. Name/admin/superuser/task or
telephony identity does not grant launch. No default authority rows, admin UI,
provisioning command or automatic backfill exists. Actual personal account and
activation procedure require a separate owner decision before any real grant.

0165 adds two tables: CrmConstructionAuthority (disabled by default) and immutable
CrmConstructionLaunch, unique per deal/project and org operation key. No data
migration. Keep evidence tables on application rollback, never reverse after use.

The command locks organization, task graph, actors/roles, deal/client/Pool, quote
and version, and authority; revalidates signed preview/revisions/current sources.
A confirmed first launch moves the existing project to prj_build and explicitly
sets its responsible. Planned dates/documents remain frozen in launch evidence;
task dates/responsibles/statuses are unchanged. No project/task creation, finance,
advance gate, actual messages or automatic completion. Retry retains current
privacy/authority checks and never resets later project state.

Only idea/design can first-launch. Hold/build/commission/done need separate
history reconciliation, not automatic restart. Legacy history cannot establish
never-launched from stage alone: the authorized person must explicitly attest
first launch after checking history. Existing launch evidence always blocks a new
launch, even if stage is manually moved back. No automated legacy reconciliation.

UI: quote-page link, paginated existing-project selection, optional 0–50 existing
tasks, existing authorized participant, explicit dates and document note, preview,
confirmation, cancel, retained history. Source permission withdrawal hides packet
details. Changed tasks/responsible show historical discrepancy; source/quote change
requires reconciliation. This is not recipient acceptance or completed-work proof.

Limitations: no new/planned object or project creation, attachments repository,
revised launch/supersession, restart of legacy/held work, operational-only recipient
access or final acceptance. No grant issuance workflow. More than 100 eligible
recipient candidates is not a supported selection scope in this first slice.
Personal activation remains pending owner verification of exact user/org, not a
blocker to synthetic local implementation/tests.

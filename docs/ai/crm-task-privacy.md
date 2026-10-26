# CRM task/source read privacy (PR-02)

Integration baseline: main `24cf7c8613177ef1ef1f4906c6f1d164474386be` plus
accepted PR-01 `79a6a12f633efbe33d73d871d1d455a0dbb7f8ce`, local integration
commit `153d98412bae35ae442f5facdd5f8a7a12999c8c`, tree
`cf502bc06d2bea90efe361e10e795ea2671331df`. Published integration commit
`65acba9a250e271e990d9b39c9f2ab64c324ffef` has the identical tree and the two
upstream parents. All reproduction data is synthetic.

## Existing read matrix before this change

All rows are subject to their existing organization/page capability checks.
An explicitly private source call is unavailable even to its owner/admin.
Public task visibility does **not** grant direct-task access. A primary assignee
without M2M membership does **not** acquire direct/edit permission.

| Existing reader | Existing actor selection | Existing source-private guard | PR-02 |
| --- | --- | --- | --- |
| `task_edit` GET, including earlier unnamed `guarded_task_edit` URL | Author, M2M participant, owner/admin, technical superuser; no public/primary-only alternative | `task_source_is_private`, final source preview recheck under organization lock | Require current active account and current org/client affiliation; preserve actor alternatives |
| `task_feedback` GET/history | Membership plus direct-task policy; followup tasks only | `task_source_is_private` | Inherits current direct read check; write locking stays separate |
| `crm_view.related_tasks` | Every same-org linked task, regardless of actor | Missing | Direct read actor policy plus batch source privacy before context/template |
| `crm_tasks` list/search | Owner/admin all; otherwise author, primary, M2M | Batch `private_source_task_ids` | Keep existing selection, recheck current account/org and source privacy |
| `client_detail` task preview/count | Owner/admin all; otherwise public, author, primary, M2M; manager does not bypass this filter | Batch source privacy | Preserve public/primary preview, current org/account and source check; same-org FK scope |
| `readings_all` calendar/search index | Existing role selection; otherwise M2M participant | Batch source privacy | Keep selection, apply current org/account guard with batch source check |
| `archive_list` task rows/counts | Every task in selected org | Missing | Direct read actor policy and batch source check before counts/template |
| `notifications_list` and unread badge | Stored recipient only, stale task title/message rendered | Missing historical read recheck | Resolve existing task paths (relative/absolute), batch recheck task authority/source; deleted tasks and malformed URLs hidden |
| Notification push-open/token links | Stored recipient only | Missing link recheck | Recheck current task authority/source before redirect |

Existing customer supply tasks also permit their active creator/M2M participant
through current `Client.user`/`ClientAccess` on the task's client or pool client,
with matching organization. This retains the existing customer supply-creator
test without opening other organization tasks. Revoking that client affiliation
closes the next request. No new personal/no-org task route is introduced.

This preserves the deliberate difference between existing preview and direct
read/edit contracts. It does not publish new task business fields or Q06 rights.
An unrelated private task on a manager's client card was already hidden: the
new negative regression passed before production-code changes.

## Existing endpoint/data-path inventory

- `crm_view` template formerly rendered task title, primary-responsible and
  task URL. Regression failed on the integration baseline with private task ID
  present in `related_tasks`; an allowed neighbouring task remains visible.
- `sync_crm_item_for_task` also copied title/description via existing CRM bulk
  reassignment when a call-backed task already had a CRM link. That existing
  POST failed a synthetic red regression. Copying is now blocked for all
  call-backed payloads (including public calls, malformed IDs, cross-org IDs or
  `source=call_analysis` without an ID). Existing WaterReading/supply business
  synchronization stays unchanged. Present call-task creation paths never assign
  `crm_item`; historical real copies were not inspected and their remediation
  is not claimed. No provenance heuristics or production cleanup are performed.
- `task_edit` contains task text, history and optional source preview. Its source
  preview separately checks existing communication capabilities; reading a task
  does not authorize a call recording/transcript. Task feedback history remains
  protected by its task gate. `task_feedback_permissions.lock_feedback_actor`
  retains its current locking account/role/participant reads unchanged.
- There is no task-photo/task-file model or separate task download endpoint.
  CRM photos belong to `CrmItem`/`CrmItemPhoto`, and existing
  `crm_issue_photo_download`/`crm_issue_primary_photo_download` authorize that
  item; linking a task does not authorize a different CRM photo.
- Existing call recording, analysis status/transcript and transcript export
  routes independently enforce call privacy/communication permissions.
  Existing private-call audio/export tests remain in the security suite.
  Message attachments use the existing conversation capability/org gate;
  no task-to-message/source publication path is added.
- Operations MCP task readers, change feed and queue/retry delivery already
  invoke call privacy. `operations_push_queue` and assignment/employee retry
  paths recheck current task/source and locked recipient authority. Call-control
  push obtains the organization lock and current recipients before delivery.
  Its primary, M2M, owner and admin recipient branches now also recheck Employee
  lifecycle under batched User -> OrgAccess -> Employee locks; independent
  review found that this separate scheduler initially lacked the delivery guard.
  Delivery also rechecks locked current Employee inactive/dismissed status in
  `_locked_task_recipient`, retaining user -> org access -> employee ordering.
  Identity binding already uses org access -> employee. Existing payroll
  dismissal locks the employee and updates its lifecycle. No general staff
  writer or feedback lock is replaced. SQLite tests do not prove MySQL race or
  deadlock behavior for every HR update; this lock-order detail is explicitly
  supplied to independent review.
- In-app task notifications have no task FK: the existing local `task_edit` or
  `task_feedback` action URL path is resolved, including absolute URLs and the
  earlier unnamed waiting guard. A hostname grants no authority and is never
  contacted. Malformed stored URLs suppress the notification safely, including
  its message and unread badge, without breaking page rendering. Rechecks are batched and also apply to stored messages from before
  participant/role/source-privacy revocation. Previously delivered external
  notifications cannot be recalled; no new channel or cache is introduced.
- No task-specific export endpoint, persistent task search index or task AI
  summary reader exists here. Calendar/search candidates and CRM task search
  are filtered before their existing in-memory indexes/haystacks are generated.

## Verification and boundaries

Synthetic regressions cover source privacy changes after an allowed read,
CRM/archive/notification removal and counters, participant/membership revocation,
owner demotion, cached superuser/deactivation, org-specific inactive/dismissed
staff (while preserving separately valid client affiliation), existing public/primary client
preview versus direct denial, and fixed query counts (2 without sources, 5 for
12 same-source private tasks, 3 for 12 task notifications).

PR-01 A/B org-isolation, CRM workflow/archive, supply generation, feedback locks,
Operations MCP/retry/actor and call-privacy suites are run locally. Full standard
CI and independent exact-head security review are separate gates. No models,
migrations, lifecycle rewrite, finance/1C/Avito/calculator changes, publication
whitelist, merge or deployment are included.

Independent review also observed that private-source `archive_restore_task`
may still redirect after an existing write action without returning content.
That lifecycle/write inconsistency is outside this read-privacy change; it is
not claimed fixed, and needs a separately scoped A01/write-policy follow-up.

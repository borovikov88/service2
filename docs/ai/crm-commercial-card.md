# Bounded commercial deal card

This change extends the existing commercial workspace and central command service.
It does not change lead intake, qualification, cohorts, source cutoff/history,
commercial grants, bootstrap flags or the legacy CrmItem writer.

## Card and schema

Migration `0157_crm_commercial_card` adds blank work subject (2000 characters),
object kind, optional PROTECT links to Pool / ClientOwnedObject, and bounded
planned name/address (255 / 500). A physical CHECK permits exactly one
representation. Model validation checks the current object/client/organization
relationship. Existing cards and minimal qualified deals default to no object
and an empty work subject. No data import, historical backfill or operational
Pool creation is performed. A planned installation remains a description on
this deal. Existing object deletion reports a handled refusal when a deal
protects it. Shared object editing stays in its existing editor.

`deal.edit` accepts title and `CARD_FIELDS`. `create_commercial_deal` accepts an
optional `card_fields` dictionary; omitting it preserves the original create
payload hash, including replay of existing minimal commands. Unknown amount
remains NULL and is displayed as unknown; explicit 0 is retained. Inputs are
Decimal/string only, nonnegative, finite, below 10^16 and at most two decimal
places. Known amounts require uppercase three-letter currency and indicative
or agreed basis. Agreed is a manager's commercial statement, not financial
approval, a contract, payment evidence or a transfer trigger. Currency totals
and conversion are not introduced.

Expected decision uses the existing aware datetime. The drawer displays and
submits the communication timezone (Asia/Barnaul by default), like the current
lead inputs. It is independent of the next task's deadline and introduces no
SLA or working-calendar assumption. Specialization remains free text up to 40
characters; unresolved Q02 process variants are not invented. Source bodies,
lead need/location and private task text are never automatically published into
these team-visible business fields.

## Current scope and history

All writes retain current active user, organization membership/HR, read/write
flags, exact capability, ownership, revision and idempotency checks. Current
and selected client/object links are checked before effect and before replay.
Merged client identities cannot be written as active cards. Pool selection
requires its existing current read policy and excludes deleted rows;
ClientOwnedObject uses the existing client-card role policy. Organization match
alone is insufficient. Lookup requires own create or own open-deal edit and
returns at most 20 of each kind with id/type/name/address only. It does not grant
access to financial fields, service parameters, tasks or communications.

Legacy object reparenting/deletion remains possible outside the commercial
writer. Detail and list serializers redact an unavailable relation, and object
search uses the same current permitted graph before joining names/addresses.
Such cards retain history but writes fail closed until the shared graph is
repaired through its existing authorized process. Immutable events accept only
typed commercial/structural values; subject, specialization and planned text
use boolean change markers, never arbitrary JSON/source content.

## Explicit negative outcome

`deal.close_negative` requires all existing command gates plus **crm_close=True**,
`transition_own_deals`, `manage_own_next_action` and own open nonarchived scope.
The existing crm_close default stays OFF; no activation or permission grant is
included. The frontend hides this action when unavailable; closed history
remains readable regardless of that write flag.

Only lost/cancelled from the current first three preparatory stages are
supported. A fresh nonempty reason up to 500 characters and the current task
version (or explicit NULL when no pointer exists) are required. Organization,
pointer, current/replay tasks, current authority and the deal graph are checked
under the existing lock order. The active own reminder is cancelled and
archived with `crm_deal_negative_close`, retaining its immutable link and
pointer. It is never marked performed/completed. Terminal reminders remain
unchanged. Private/inaccessible tasks, inconsistent links, stale versions and
immutable or waiting reminders fail atomically; the drawer's existing task
link provides the repair route. Independent tasks, leads and operational work
are not closed. Exact replay rechecks current flags and privacy before returning
its structural result and creates no second event/cancellation. Won, reopening,
archive actions, contracts, execution and payment logic remain unavailable.

## Workspace and gates

Table and kanban retain the same persistent transition command, including drag
and backward reason. Search and filters are live, cancellable and URL-backed:
owner, pipeline/stage, outcome (open default), object kind, specialization and
expected-decision date range. Date bounds are inclusive Barnaul calendar days.
Stable descending IDs, a default page size of 50 and previous/next controls
provide bounded server pagination; filter changes reset the page. The page is
bounded to 10000; use narrower filters beyond that. Next-task attention is applied
before SQL pagination: selecting overdue/missing first checks at most 1000 open,
nonarchived business-visible deals plus one sentinel. If that scope exceeds 1000,
the API explicitly asks for narrower ordinary filters before any task/privacy or
timing classification; it never returns a partial or false-empty filtered set.
For an accepted scope, current pointer/org/client consistency, readable_tasks,
source privacy and waiting_control are reused in one bounded batch. Hidden or
inconsistent reminders count as neither missing nor overdue. Closed deals are
outside both attention categories. Date-only tasks become late after their
Barnaul calendar day. Exact matching IDs then pass through normal SQL pagination.
The ordinary all-attention workspace retains its 50+1 query and does not classify
1000 cards for counters. Selector numbers are omitted; the result shows current
page size and, for a selected attention filter, its exact bounded matching total.
URL/back navigation and page reset follow the selected filter; a refused filter
returns the selector to its last fetched scope with an explicit error instead of
showing stale rows as a partial result. Reloading a saved invalid or too-wide
filter opens a recoverable HTML shell with an initial notice and canonical
fallback filters; access-denied and unavailable-pipeline responses stay intact.
Existing immutable operation queue/unknown-result retries are retained. Drawer fields and
filters use the existing responsive/dark component styles.

Targeted tests cover decimal/null, scopes, current access and replay, negative
closure/task privacy/atomicity, physical migration shape/preservation and
pagination. MySQL's selected suite adds the new modules and a close-vs-next-task
race (SQLite skips row-lock races). Node tests retain queue regressions and add
full commercial conflict drafts and frozen negative-outcome retries.

The ordinary full suite skips the real-browser test. The dedicated normal-CI
Chromium step sets `CRM_CARD_BROWSER_TEST=1` and runs it as a required test, using
exact `playwright@1.62.1` in RUNNER_TEMP, npm install with lifecycle scripts
disabled and the official Chromium installer. A synthetic local Django static
live server/session verifies create, commercial edit, planned/existing object,
reload, NULL versus zero, persisted mouse drag, and mobile 390px/dark field
reachability. It blocks external browser requests and uses no provider or
production account. Workflow reuse guards and all existing suites stay intact;
workflow changes invalidate reuse under the existing policy. Local syntax and
unit tests are not claimed as browser execution. Full CI, actual Chromium,
MySQL and independent exact-head review remain publication gates. Keep additive
schema on operational rollback and use fix-forward; no production migration or
deployment was performed by this implementation.

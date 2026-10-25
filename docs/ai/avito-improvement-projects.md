# Services improvement projects: one unapplied owner cycle

Scope: Services connection 4 / Avito account 446312402. Goods connection 1 is not changed. This release reuses AvitoServicesFeedVersion and existing feed/account/organization permissions. It adds no lead, order, feed, scheduler, API service, model or migration.

## What is saved

Up to three owner-selected candidates: 8346503274 (water treatment, Barnaul), 8343962135 (pools/hammams, Barnaul), 8457366990 (pools, Biysk). The IDs are not a performance ranking.

A project is a typed append-only verify event with kind avito_improvement_proposal_v1. Its private payload contains local published source version/digest, exact Title before/proposed values, actor/time, fixed scope/binding, structural issue, saved analytics provenance/coverage, unavailable Wordstat status, and a planned evaluation. The public/protected diagnostic receipt contains only IDs, event IDs, safe states/codes and revision IDs. Actual ad text and financial values never enter Git or public logs.

The event snapshot is the unchanged current draft. Feed draft, published snapshot, revisions, updated_at, provider observations and existing publication evidence are not changed. The UI explicitly calls it “Проект, не применён”; it does not show the proposal as an actual edit or a successful verification. Existing typed photo/promotion evidence selectors retain their separate meaning.

## Eligibility

The current enabled published local snapshot must match its publication history and digest under the existing binding. Both Id and AvitoId are retained. Current draft Title must still equal the source Title.

An active provider identity must be established by saved item-detail evidence or a completed, matching full-account autoload report. Status source time and receipt time must be aware, not future, and no more than four hours old. Re-fetching an old report does not refresh its source time. A newer inactive observation, known expired term, critical row or owner-withdrawn intent blocks a project.

The shared decision_evidence API must confirm current published membership/revision and a received source-period snapshot within 24 hours. Incomplete rows or unknown metrics may support only a labelled structural hypothesis; they never establish a performance winner. Missing metadata blocks preparation rather than becoming zero.

Service words in the approved Title/Description and an unambiguous exact address-city token must match the owner’s candidate. A conflicting explicit City blocks the candidate. A project changes only Title and only for a concrete missing-geography or joined-punctuation issue. Proposed text is derived privately from the approved Title: existing service words are retained, punctuation/whitespace may be normalized, and an absent confirmed city may be appended. No old or proposed working ad text is hardcoded in Git. The existing editor Title limit applies. Identical/already-clear titles and unsupported service/city claims are skipped. Description, photos, prices and identities remain unchanged.

## Idempotence and access

Preparation uses the existing current actor/organization/channel-management authorization, then connection and feed row locks. Binding and source revision are checked again before append. A key over current scope, binding, source publication and Title before/proposed values prevents duplicate projects under concurrent requests. Stale form revisions/publication IDs return conflict.

The route is /avito/4/services-feed/improvements/. GET is DB-only and never_cache. POST requires existing management capability, organization scope and CSRF. There is no publish/apply/provider-send endpoint.

The existing protected exact-main setup may call setup_owner_cycle_once once. A scoped connection4 metadata marker records a prepared or blocked attempt and safe results. Later deploys do not start a new automatic cycle, including after a new publication. Changed account/credentials do not adopt old results. Explicit authorized preparation remains available in the UI and is idempotent; no automatic publication follows.

## Evaluation and limits

The planned primary metric is the share of target inquiries from confirmed quality classifications in the existing CRM. Evaluation has not started. Its planned window is 14 source dates after separate owner publication and confirmation of calendar boundaries. Ten classified inquiries is an organizational minimum review guard, not a statistical significance claim.

Unknown attribution/classification, insufficient volume, missing dates/rows, unknown calendar alignment, changed ad cohorts or simultaneous price/content/promotion changes make the result inconclusive. Neither a source-file publication nor an active status proves the new title appeared in the provider card. No experiment is silently launched.

## Wordstat

No authorized Wordstat tool is callable in the current session; plugin discovery returned no matching plugin. This does not establish whether the owner has an authorized browser elsewhere. Bounded repository searches found no Wordstat module/config; the unrelated YANDEX_SUGGEST_API_KEY is not used.

Official Yandex guidance now directs Wordstat API to Yandex Search API:
https://yandex.ru/support2/wordstat/ru/content/api-wordstat
https://aistudio.yandex.ru/en/docs/search-api/concepts/wordstat
https://aistudio.yandex.ru/en/docs/search-api/operations/wordstat-gettop
https://aistudio.yandex.ru/en/docs/search-api/operations/wordstat-getregiontree
https://aistudio.yandex.ru/ru/docs/search-api/pricing

Those methods require the documented service-account role and API key or IAM authorization; statistical methods are billed. No new Cloud service, key, billing or request is activated here. A current export or authorized official channel is still needed for query frequencies with exact phrase/operators, region, period, devices and receipt time. Region IDs are not guessed from administrative codes. Overlapping broad queries are not summed; Yandex demand is not presented as Avito search phrases. Frequencies remain null, not zero.

## Checks

Targeted tests cover unchanged snapshots/proofs, idempotence, partial/missing analytics, status freshness and source-vs-fetch time, city/service limits, stale sources, authority changes, current/foreign scope, CSRF, explicit project labels, typed-history selection, one-time setup and concurrent row-lock serialization. The concurrency test runs on a backend supporting SELECT FOR UPDATE, including the existing MySQL CI; SQLite does not claim that lock proof.

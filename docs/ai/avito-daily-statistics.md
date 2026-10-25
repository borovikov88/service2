# Services daily-statistics foundation

Scope is connection4/account446312402. This continues the existing stats-v2 reader and existing native monitor. It performs official read calls only. No promotion purchase, source setting, listing content, customer message, finance calculation or 1C data is changed.

Two additive tables retain collection provenance and daily item facts. Each invocation reads at most one API page, with the existing account/method throttle and a scoped DB lease. Partial pages are resumed without sleeping. The complete page count must be confirmed by dataTotalCount; missing total, changing totals, duplicate item IDs or bounds do not become a complete day. Only a completed binding-verified run replaces current daily facts. Missing provider rows/metrics are unknown, including when the provider returns an empty report.

The available date-only contract does not establish the provider's day timezone or an hourly/offset option. provider_timezone=unknown and requested_calendar=Asia/Barnaul are deliberately separate. The collector uses a visible two-day lag, not yesterday disguised as another date. True yesterday/previous-day,7/prior7,30 full Barnaul-day comparisons remain blocked. Separate aggregates are explicitly sums of provider-date counters, not unique people or exact local days. Changing date labels cannot repair this boundary.

Source counters retain contacts, phone, messenger and their intersection independently. None is not zero. Percentages are calculated only from available matching counters with positive denominators. Cost per contact uses reported cash spending in RUB, converted once from the existing documented kopeks metrics. allSpending, cash, presence/target actions, promotion, other spending and commission remain separate; they must not be added as disjoint costs. Unconfirmed bonus units and raw provider average/conversion fields are excluded.

Service labels are exact ServiceType, otherwise the published announcement Title. They are labels, not keyword-based attribution. City uses explicit City only. Current published dimensions are stamped at collection; historical geography/category is not invented for a backfill. Missing city stays unknown. Existing append-only feed history supplies the change journal. Observed promo spending does not prove a promotion purchase or its causal effect. Promotion application history, Wordstat, experiments and recommendations remain later work.

The existing CRM remains authoritative. Provider ad IDs are preserved for a future confirmed bridge. Conversations/calls, unique clients, leads, orders, payments and revenue must remain separate. No phone/title similarity join and no new CRM lead/order model are introduced.

Background setup is an explicit authenticated channel-admin action in the workspace, or a protected exact-main command:
python manage.py collect_avito_daily_statistics --connection-id 4 --account-id 446312402 --action enable --expected-sha <reviewed current main>
with SERVICE2_AVITO_DAILY_STATISTICS_SETUP=1. Status/enable/collection are separate facts. Actual API access, provider-day alignment and natural background execution require production evidence; a schema or passing CI alone does not prove them.

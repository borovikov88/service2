# CRM object needs — interrupted fix-cycle handoff

Status: **changes required; not ready for merge or deployment**.

Production main `c59c5993c2eff73e540ad86cf098d5ce00a04e5c` is successfully deployed: run [40361152332](https://github.com/borovikov88/service2/actions/runs/40361152332), deploy job121706853555, exact SHA and health success. PR441 commercial card and PR444 browser hardening are live. Existing Avito changes are preserved.

## Durable source and restoration

Original24file package was restored by the same developer. Exact tree equality `8079d8ce3e0d972ee52a1494089a9bf5805873de` was proven. Its durable source checkpoint is commit `a95e5728f2389561047db830e4b9c19e4d58375d`, before this report-only commit. This source still contains independent F1/F2/F2b/L1 blockers. It does not include later uncommitted fixes.

## Fix-cycle interrupted by unavailable execution environment

Same developer reported F1/F2/F2b edits and partial L1 callback changes, plus corrections tests. **No fixed commit/tree or clean freeze exists.** F1/F2 six-test subset passed in0.241s. Expanded eight-test delta had two pool-decoder errors; source was edited, but the delta_v2 result did not return. These are partial historical checks, not final passing validation. Full suite, actual MySQL/Chromium and PR CI were not run for this correction cycle.

L1 remains incomplete: callbacks still use recipient User FOR UPDATE. Independent reviewer conditionally accepts a source-callback-only current shared User lock to avoid the bulk restore A/B Notification-FK cycle. Do not call _locked_staff_user after shared lock: it upgrades to User X. Read transport does not write/lock User X. Preserve active/member/role/HR checks; full Task/CRM graph→Reading→fresh identity guard precedes recipient locks, parents remain fresh bounded snapshots. Actual final SQL/helper review and real MySQL bulk A/B interleaving are mandatory. Do not alter archive/global notifications or isolation globally.

F2b was independently reproduced on immutable old8079: decimal ID alias updates an existing Reading while missing upfront lock IDs (one failing test,0.097s). The canonical emptygraph import hypothesis was withdrawn because import already prelocks all canonical existing Reading IDs. Fix uses the same actual field/widget decoding and existing PK-in-prelocked-set guard.

## Incoming worker

[Process CRM incoming leads](https://github.com/borovikov88/service2/actions/workflows/crm-intake.yml) UI explicitly showed **0 workflow runs**. Production lead page under Alexander still showed waiting for first run. Browser was signed out of GitHub; first dispatch connector action was unavailable. No sign-in request was repeated, no schedule/guard/token workaround used. First actual worker execution and real event-to-lead flow remain unconfirmed.

## Resume

Check actual environment/worktree and same writer before changing anything. If uncommitted fixes survive, preserve them. If absent, recover original source from a95 and the same developer's edit history, without inventing completion or starting a second implementation. Complete L1, freeze and save exact corrected source, integrate current remote main preserving Avito/PR444, then independent exact-head review, MySQL races, real Chromium and full CI. Only then normal guarded merge/deployment/health. Package2, grants/cohort/flags/cutoff, destructive migration and finance import remain out of scope.

Detailed machine-readable status accompanies this report. Durable review archive version21 contains the original independent review/probes and F2b evidence. Later interrupted edits have not been saved there.

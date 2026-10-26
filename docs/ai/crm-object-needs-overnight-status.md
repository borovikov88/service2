# CRM object needs — resumed correction cycle

Status: **changes required; no merge/deployment approval**.

Execution environment recovered and all four modified source files plus the new corrections suite survived. The previous writer was closed (followup rejected by the collaboration tool). One replacement writer was actually launched as GPT-6.1 Sol High: /root/crm_object_needs_fix_resume. No duplicate writer is running.

Before further editing, inherited changes were frozen locally as12b8b0922df2bfe6f044f3b8a42399bfd8683929. Exact source tree019d32828c44f0b4fee55dc0618d3dbbd28de6d6 was reproduced through GitHub and saved in source commit bc9e074bb60f55f6b7ea2d6eb009462e2ded3eae. This source checkpoint contains F1/F2/F2b fixes and partial L1; recipient still uses User FOR UPDATE. It is not final verified code. Original8079 source remains in a95e5728f2389561047db830e4b9c19e4d58375d.

No tests were run merely to freeze inherited edits. Previously6F1/F2 tests passed; expanded8tests had2decoder errors, source was then edited, and delta_v2 result never returned. Final corrected checks, actual MySQL/Chromium/fullCI and independent final review remain pending.

Next: callback-only current User SHARED lock without _locked_staff_user/UserX upgrade, preserving active/member/role/HR, full Task/CRM graph→Reading→identity guard→UserS→OrgAccess/Employee. Parents remain bounded fresh snapshots. Actual MySQL bulk restore A/B interleaving and no-upgrade check are mandatory; do not rewrite archive/global policy. Finish targeted tests/check/makemigrations, clean freeze, integrate latest main preserving Avito/PR444, independent exact-head review and actualCI, then guarded publication.

Production mainc59c5993c2eff73e540ad86cf098d5ce00a04e5c remains successfully deployed with commercial card PR441 and browser hardening PR444. Intake last verified0workflowruns and awaiting first launch, no firstdispatch tool and GitHub signed out; do not repeat declined login. No production data/cohort/flags/cutoff changes, fake cards, package2, finance import or legacy cutover.

Machine-readable status accompanies this report. Durable archive version21 contains original review and F2b evidence; latest source is now separately protected by the source checkpoint above.

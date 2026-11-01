# Monthly material projection — LOCAL, read only

Source CRM_Aqualine_TZ_v0_4 version3 §7 lines293–320, AC50/52/64/65/70.
Extends the existing journal route with GET month=YYYY-MM; POST to this mode405.
Same flags/access/current object scope. No grants, activation or new tables.
25 issue groups/page,20 events/group with detail link; totals query all current
eligible month events for that ONE issue and destination/kind. Original unit and
packaging preserved; never sum distinct issue identities from matching names.
Delivery uses occurred_on, not issue date or entered-at. Corrections in later months
remove the reversed event from current eligible quantities; original and reversal
remain in history. Explicitly a live projection, not frozen accounting close/snapshot.
Late recorded events marked for period reconciliation. Closed-period detection is
unavailable without verified financial/document policy; no automatic realization.
Restricted historical client/object relation suppresses quantities and event sources.
All-period unresolved issues remain available on existing journal screen.

Partial scope: delivery/return monthly view and safe identity separation. No prices,
allocation/remainders to invoice, contracts/payment/debt or canonical aggregation.
AC64 full same-item aggregation remains pending confirmed canonical identity.
Do not treat this screen as the complete financial manager register.

Dependencies / future staged release:
1. Existing deployed base99d96caf.
2. Cursor packagebd951ba2, migration0169 adds one table, dependency0168.
3. Materials36a5ef52 plus this monthly delta, migration0170 adds three tables,
   original dependency0168; merge both preserved histories and add empty0171
   with dependencies0169+0170. On a phase2 host already at0169, migrate applies
   only0170 and0171. No backfill, no runtime activation, no destructive rebase.
Original package refs and private archives are retained. A separate local combined
candidate will carry the explicit merge, not rewriting either original package.

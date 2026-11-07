# Plan — outcome-first controller

## Facts from the API (not the memos)

| workflow | primary / guardrail | auto-apply | approvals | mid-flight |
|---|---|---|---|---|
| nw_renewal | engagement / pickup −2pp | schedule, caller_id (TV ≤ 0.5) | 10–19, 3/24h, TTL 24h, apply ≤ 2h | allowed |
| nw_kyc | **pickup** / engagement −2pp | **none** | 10–18, 2/24h, latency up to 8h | next_campaign_only |
| ks_cancel | engagement / pickup −2pp | schedule, caller_id, welcome, voice (TV ≤ **0.2**) — **not prompt** | 10–19, 4/24h, TTL 12h | allowed |

- History (14 days) was run on the default config: W1/V1/P1 with D1/D2. So history tells us about **hours × cohort**,
  **D1 vs D2** and the **P1 hallucination baseline**. It tells us nothing about other welcome messages, voices or prompts.
- Call events have no features. Cohort membership comes from joining on `lead` events.
- An approved change lapses after 2h, so we have to poll at least hourly during approver hours.

## Memo claims that are wrong against policy
- Sales: "Kestrel has auto-apply for everything". In fact prompt changes are excluded and each change is capped at TV 0.2.
- Sales: "Northwind is fine with auto-apply on KYC". In fact `nw_kyc` has auto-apply disabled.
- Sales: a single "blended score". This breaks a non-negotiable constraint (Product agrees).
- Sales: "roll out persuasive (P4) wherever available". Prompt changes need validation evidence, and P4 could raise hallucinations.
- Kestrel supervisor: "2 requests/day, not before 11:00". Policy allows 4/day from 10:00. **Accept it as a stricter voluntary limit.**
- Product: per-person call-time as the "headline". Most people in a campaign are new, and returning people have
  only a handful of past dials. That data is too sparse to support a per-person claim. Do it at cohort level.

## Scope
**Build:** C1 ingestion · C2 shared cohorts · C3 bootstrap from history · C4 call-time (cohort level) · C5 caller-ID health ·
C6 welcome × voice experiments · C7 promotion + decay rollback · C8 hallucination response · C9 governance client ·
C10 impact/affected/dry run · C12 audit store · C13 crash recovery.
**Partial:** C11 supervisor view (one generated status page per workflow) · C14 loop metrics in the store plus a status file.
**Cut:** C15 cross-tenant fairness (2 tenants, credits aren't the binding constraint) · per-person call-time.

## Architecture
Single Python process, SQLite in WAL mode (an embedded file, so no new infrastructure; it maps 1:1 onto Postgres tables).

```
sim /events/chunks ──► Ingestor ──► aggregates (wf, day, hour, cohort, dim, value → dials/conn/eng/halluc)
                         │           + leads (lead_id → features, cohort)        [one txn per chunk + cursor]
                         ▼
                     Detectors (thresholds on populations) ──► findings
                         ▼
                     Planner: experiments, promotions, rollbacks, DID rotation, incident fixes
                         ▼
                     Governance client: batch → proposal → poll → apply / re-propose   [outbox, idempotent ids]
                         ▼
                     Audit store: decisions(why, evidence, proposal, approval, outcome)
```

Loop: advance 60 min (15 min during incidents) → ingest → reconcile approvals → decide → act.
Restart: read the cursor, ingest any missed chunks, reconcile `/approvals` against our outbox, carry on.
Proposal ids are deterministic (`wf:kind:day:seq`), so a retry hits `409 duplicate` and never double-proposes.

## Learning loop
- **Cohorts:** one fixed taxonomy (lang × age_band, coarse), merged until each cohort has at least ~400 connects/day.
  Detection, experiments and the status page all use the same IDs.
- **Experiments:** the simulator samples dimensions independently, so welcome and voice run as one factorial
  experiment. The incumbent keeps about 70% of traffic and challengers share 30%, which limits the harm a
  bad arm can do. Analysis: main effects with two-proportion z-tests, Holm correction, and stopping at
  pre-registered sample sizes (≈1,300 connects per arm to detect a +10% relative change at power 0.8). Arms
  that are clearly losing get dropped early.
- **Promotion:** winner at 90%, incumbent kept as a 10% holdout. **Decay:** rolling 2-day comparison against the
  holdout; a significant negative difference triggers a rollback proposal.
- **Guardrail:** every promotion also has to pass a non-inferiority test on the guardrail KPI.
- **Caller ID:** per-DID pickup compared with the other DIDs over the last 4h and 24h. If a DID is significantly
  below 0.75×, rotate away within `max_shift` (auto-apply where allowed). Unused DIDs get a small exploration share.
- **Call time:** cohort × hour pickup lift from history becomes `hour_weights`. Every in-window hour keeps weight
  > 0, so no capacity is lost. Auto-applied where allowed; through approval on KYC, where it is the primary lever.
- **Hallucination:** the rate per prompt version is compared with P1's history baseline using a binomial test
  (min ~150 connects). Response: first validate the last known-good prompt (cheap, and its engagement is
  known). Only if that fails, `suggest-fixes` plus validation of the candidates. Then propose with evidence and
  verify after applying. P5-style "strict script" fixes get flagged (Kestrel memo).

## Governance
One batched proposal per workflow per decision window, sent at the start of approver hours (11:00 for Kestrel).
Expired or stale proposals are rebuilt from fresh data and the current config. Supervisor edits are never
overwritten: our diff is re-applied on top of their config, and their dimensions are left alone unless an
incident requires changing them. Customer-facing dimensions on Northwind always go through approval; on Kestrel
we use auto-apply but record each change for the "what changed today" page.

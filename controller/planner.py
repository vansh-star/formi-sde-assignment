"""What the controller *wants* each workflow's config to be, and why.

Planners own learned state in the `state` table and log every transition to `decisions`. They never talk to
the simulator's governance endpoints; governance.py turns their targets into proposals or auto-applies.

Each planner exposes:
  update(now)          → refresh state from data, logging decisions
  desired(cfg)         → {component: value} plus whether it is protective (urgent)
"""
from __future__ import annotations

import math

from . import analysis as A
from .config_ops import normalise

DAY = 1440


def cur_hour(now):
    return (now % DAY) // 60


class WFContext:
    def __init__(self, store, wf: str, spec: dict):
        self.s, self.wf, self.spec = store, wf, spec
        self.kpi = spec["objective"]["primary"]
        g = spec["objective"].get("guardrail") or {}
        self.guard_kpi = A.other_kpi(self.kpi)
        self.guard_drop = next(iter(g.values()), 0.02) if g else 0.02
        self.window = spec["dialling_window"]

    def owned_by_supervisor(self, dim: str, now: int) -> bool:
        """A dim a supervisor edited in the last 24h is theirs: we don't touch it unless a safety incident
        requires it, and then only through an approval that says so."""
        own = self.s.state(self.wf, "supervisor_dims", {})
        return dim in own and now - own[dim] < DAY


# ============================================================================ experiments (C6, C7)
class Experiment:
    """Factorial welcome × voice experiment on the workflow's whole population.

    Incumbent keeps 1−EXPLORE; challengers split EXPLORE. Analysis is stratified (day, hour, cohort,
    prompt, other dim), one-sided z on the decision KPI, Holm across challengers at each daily look.
      - drop a challenger early if it is worse with p < ALPHA_DROP (we prefer losing a winner to hurting a cohort)
      - promote the best challenger if Holm-adjusted p < ALPHA_WIN, n ≥ MIN_N per arm, the guardrail is
        non-inferior, and no cohort is significantly hurt; the incumbent stays as a HOLDOUT
      - after promotion, watch winner vs holdout on a rolling window; roll back if the holdout is better
    """
    EXPLORE = 0.3     # total challenger share
    PER_ARM = 0.15    # cap per challenger: a 2-arm dim explores 15%, not 30% (bounds harm from one bad arm)
    HOLDOUT = 0.1
    MIN_N = 400
    MAX_N = 3000
    ALPHA_WIN = 0.01
    ALPHA_DROP = 0.01
    ALPHA_DECAY = 0.02
    DECAY_WINDOW = 2 * DAY      # recent-harm look while exploring
    WATCH_WINDOW = 36 * 60      # winner-vs-holdout look after promotion
    MIN_REL = 0.03   # don't promote a lift smaller than this, even if significant

    def __init__(self, ctx: WFContext, dim: str):
        self.c, self.dim = ctx, dim
        self.key = f"exp:{dim}"
        # welcome/voice act after pickup, so they are judged on engagement whatever the primary KPI is;
        # the guardrail is then pickup. On a pickup-primary workflow this means: never hurt pickup.
        self.kpi = "engagement_rate"
        self.guard = "pickup_rate"

    def st(self):
        return self.c.s.state(self.c.wf, self.key)

    def save(self, st):
        self.c.s.set_state(self.c.wf, self.key, st)

    def init(self, cfg, now):
        opts = self.c.spec["options"][self.dim]
        cur = cfg["allocations"]["default"][self.dim]
        inc = max(cur, key=cur.get)
        st = {"status": "running", "incumbent": inc, "active": [o for o in opts if o != inc], "dropped": {},
              "winner": None, "holdout": None, "started": now, "promoted_at": None, "overrides": {},
              "urgent": False, "looks": 0}
        if not st["active"]:
            st["status"] = "concluded"
        self.save(st)
        self.c.s.decide(now, self.c.wf, "experiment_start",
                        f"{self.dim}: test {', '.join(st['active']) or 'nothing'} against {inc} "
                        f"({int(self.EXPLORE * 100)}% exploration)", {"dim": self.dim, **st})
        return st

    # -- targets
    def target(self, st) -> dict:
        if st["status"] == "supervisor":
            return st["sup_alloc"]
        if st["status"] == "running" and st["active"]:
            share = min(self.PER_ARM, self.EXPLORE / len(st["active"]))
            return normalise({st["incumbent"]: 1 - share * len(st["active"]), **{a: share for a in st["active"]}})
        if st["status"] == "promoted":
            return normalise({st["winner"]: 1 - self.HOLDOUT, st["holdout"]: self.HOLDOUT})
        return {st["incumbent"]: 1.0}

    # -- learning
    def update(self, now, daily_look: bool, cfg: dict | None = None):
        st = self.st()
        if st is None:
            return
        own = self.c.s.state(self.c.wf, "supervisor_dims", {})
        if cfg and self.dim in own and own[self.dim] > st.get("sup_seen", -10 ** 9):
            self._adopt_supervisor(st, cfg, now, own[self.dim])
        if st["status"] == "supervisor":
            if daily_look:
                self._challenge_supervisor(st, now)
            self.save(st)
            return
        if st["status"] == "concluded":
            self.save(st)
            return
        strata = A.arm_strata(self.c.s, self.c.wf, self.dim, self.kpi, st["started"])
        if st["status"] == "running":
            self._running(st, strata, now, daily_look)
        elif st["status"] == "promoted":
            self._promoted(st, now)
        self.save(st)

    def _running(self, st, strata, now, daily_look):
        inc = st["incumbent"]
        cmp = {a: A.compare(strata, a, inc) for a in st["active"]}
        recent = A.arm_strata(self.c.s, self.c.wf, self.dim, self.kpi, max(st["started"], now - self.DECAY_WINDOW))
        # early harm stop — checked every loop, acted on as protective. Two looks: since start (steady harm)
        # and the last 48h (an arm that started fine and went bad, i.e. decay during exploration)
        for a in list(st["active"]):
            c, r = cmp.get(a), A.compare(recent, a, inc)
            hit = None
            if c and c.n_a >= 150 and c.rel < -0.05 and c.p_less < self.ALPHA_DROP:
                hit = ("since start", c)
            elif r and r.n_a >= 150 and r.rel < -0.08 and r.p_less < self.ALPHA_DROP / 2:
                hit = ("last 48h", r)
            if hit:
                why, c = hit
                st["active"].remove(a)
                cmp.pop(a, None)
                st["dropped"][a] = {"why": f"worse than incumbent ({why})", **c.as_dict()}
                st["urgent"] = True
                self.c.s.decide(now, self.c.wf, "arm_dropped",
                                f"{self.dim}={a} dropped: engagement {c.rel:+.1%} vs {inc} {why} (p={c.p_less:.4f}, n={c.n_a})",
                                {"dim": self.dim, "arm": a, "vs": inc, **c.as_dict()})
        if not daily_look:
            return
        st["looks"] += 1
        live = [(a, cmp[a]) for a in st["active"] if cmp.get(a) and cmp[a].n_a >= self.MIN_N]
        if live:
            adj = A_holm([c.p_greater for _, c in live])
            ranked = sorted(zip(live, adj), key=lambda t: -t[0][1].diff)
            for (a, c), p in ranked:
                if p < self.ALPHA_WIN and c.rel >= self.MIN_REL:
                    r = A.compare(recent, a, inc)
                    if r and r.n_a >= 100 and r.diff < 0:
                        # won on the cumulative record but is behind over the last 48h: likely already decaying
                        self.c.s.decide(now, self.c.wf, "promotion_blocked",
                                        f"{self.dim}={a} wins overall ({c.rel:+.1%}) but trails {inc} over the last 48h "
                                        f"({r.rel:+.1%}, n={r.n_a}); not promoting a variant that may be fading",
                                        {"overall": c.as_dict(), "recent": r.as_dict()})
                        continue
                    ok, gdetail = self._guardrail_ok(a, inc, st["started"])
                    hurt = self._cohort_harm(a, inc, st["started"])
                    if not ok:
                        self.c.s.decide(now, self.c.wf, "promotion_blocked",
                                        f"{self.dim}={a} wins on engagement but fails pickup guardrail", gdetail)
                        continue
                    st.update(status="promoted", winner=a, holdout=inc, promoted_at=now, urgent=False,
                              overrides={h: {inc: 1.0} for h in hurt})
                    self.c.s.decide(now, self.c.wf, "promote",
                                    f"{self.dim}: promote {a} over {inc}: engagement {c.rel:+.1%} "
                                    f"(Holm p={p:.4f}, n={c.n_a}/{c.n_b}); {inc} kept as {int(self.HOLDOUT*100)}% holdout"
                                    + (f"; cohorts kept on {inc} because {a} hurts them: {sorted(hurt)}" if hurt else ""),
                                    {"dim": self.dim, "winner": a, "incumbent": inc, "p_holm": p, **c.as_dict(),
                                     "guardrail": gdetail, "cohort_harm": hurt, "looks": st["looks"]})
                    return
        # futility: every remaining arm has enough data and nobody won
        if st["active"] and all(cmp.get(a) and cmp[a].n_a >= self.MAX_N for a in st["active"]):
            for a in list(st["active"]):
                st["dropped"][a] = {"why": "no significant lift at max sample", **cmp[a].as_dict()}
            st["active"] = []
        if not st["active"]:
            st["status"] = "concluded"
            self.c.s.decide(now, self.c.wf, "experiment_concluded",
                            f"{self.dim}: no challenger beat {inc}; keeping {inc} at 100%", {"dropped": st["dropped"]})

    def _adopt_supervisor(self, st, cfg, now, at):
        """A supervisor set this dim by hand. Their choice becomes the incumbent; exploration stops. We keep
        learning from what was served and only propose overriding them with evidence (and say so)."""
        alloc = normalise(cfg["allocations"]["default"][self.dim])
        st.update(status="supervisor", sup_alloc=alloc, incumbent=max(alloc, key=alloc.get), sup_seen=at,
                  active=[], urgent=False, overrides={})
        self.c.s.decide(now, self.c.wf, "adopt_supervisor",
                        f"{self.dim}: supervisor set {alloc}; adopting it as the baseline and pausing our test",
                        {"dim": self.dim, "alloc": alloc})

    def _challenge_supervisor(self, st, now):
        inc = st["incumbent"]
        strata = A.arm_strata(self.c.s, self.c.wf, self.dim, self.kpi, st["started"])
        opts = [o for o in self.c.spec["options"][self.dim] if o != inc]
        live = [(o, c) for o in opts if (c := A.compare(strata, o, inc)) and c.n_a >= self.MIN_N]
        if not live:
            return
        adj = A_holm([c.p_greater for _, c in live])
        best = max(zip(live, adj), key=lambda t: t[0][1].diff)
        (a, c), p = best
        if p < self.ALPHA_WIN and c.rel >= self.MIN_REL and self._guardrail_ok(a, inc, st["started"])[0]:
            st.update(status="promoted", winner=a, holdout=inc, promoted_at=now, vs_supervisor=True)
            self.c.s.decide(now, self.c.wf, "promote",
                            f"{self.dim}: {a} beats the supervisor's choice {inc} by {c.rel:+.1%} (Holm p={p:.4f}); "
                            f"proposing it for approval, explicitly as a reversal of their edit",
                            {"dim": self.dim, "winner": a, "incumbent": inc, **c.as_dict()})

    def _guardrail_ok(self, a, inc, since):
        strata = A.arm_strata(self.c.s, self.c.wf, self.dim, self.guard, since)
        c = A.compare(strata, a, inc)
        if c is None:
            return True, {"note": "no guardrail data"}
        lo, _ = c.ci(1.645)
        return lo > -self.c.guard_drop, {"guardrail": self.guard, "max_drop": self.c.guard_drop, **c.as_dict()}

    def _cohort_harm(self, a, inc, since) -> list[str]:
        """Cohorts where the winner is significantly worse than the incumbent (Holm across cohorts)."""
        cohorts = [r[0] for r in self.c.s.q("SELECT cohort_id FROM cohorts WHERE wf=?", (self.c.wf,))]
        tests = []
        for coh in cohorts:
            c = A.compare(A.arm_strata(self.c.s, self.c.wf, self.dim, self.kpi, since, cohort=coh), a, inc)
            if c and c.n_a >= 100 and c.n_b >= 100:
                tests.append((coh, c))
        if not tests:
            return []
        adj = A_holm([c.p_less for _, c in tests])
        return [coh for (coh, c), p in zip(tests, adj) if p < 0.05 and c.rel < -0.05]

    def _promoted(self, st, now):
        w, h = st["winner"], st["holdout"]
        since = max(st["promoted_at"], now - self.WATCH_WINDOW)
        c = A.compare(A.arm_strata(self.c.s, self.c.wf, self.dim, self.kpi, since), w, h)
        st["last_decay_check"] = c.as_dict() if c else None
        if c and c.n_b >= 120 and c.p_less < self.ALPHA_DECAY and c.rel < -0.03:
            st.update(status="concluded", incumbent=h, active=[], urgent=True, rolled_back_at=now,
                      dropped={**st["dropped"], w: {"why": "decayed after promotion", **c.as_dict()}})
            self.c.s.decide(now, self.c.wf, "rollback",
                            f"{self.dim}: {w} stopped winning — engagement {c.rel:+.1%} vs holdout {h} over the last "
                            f"{(now - since) // 60}h (p={c.p_less:.4f}); rolling back to {h}",
                            {"dim": self.dim, "winner": w, "holdout": h, **c.as_dict()})


def A_holm(p):
    from .stats import holm
    return holm(p)


# ============================================================================ caller-ID health (C5)
class CallerIds:
    """Caller-ID health and rotation.

    Observed in the data: a number's pickup falls the more volume it carries, and a fresh number starts
    high. So comparing a DID only against the others misfires (an ordinary number looks bad next to a
    fresh one). A DID is flagged only if it dropped against *its own* trailing rate AND sits below the
    other live numbers:
        last 6h vs its own previous 3 days: ≥ 25% lower, n ≥ 150, p < 1e-4   and   below the others by ≥ 20%
    Unused numbers go on probation at 10% (explore safely), become active after 300 dials unless clearly
    bad. Active numbers share traffic in proportion to recent pickup² (floor 10%), so volume spreads and no
    single number burns out. Re-weighting only happens when the target moves by > 0.15 TV and at most daily (no churn)."""
    PROBE = 0.1
    FLOOR = 0.1
    FLAG_WINDOW = 6 * 60
    HYSTERESIS = 0.15
    REBALANCE_EVERY = DAY   # rotation cadence a supervisor can follow; flags act immediately

    def __init__(self, ctx: WFContext):
        self.c = ctx
        self.key = "did"

    def init(self, cfg, now):
        opts = self.c.spec["options"]["caller_id"]
        used = {r[0]: r[1] for r in self.c.s.q("SELECT d, SUM(dials) FROM agg WHERE wf=? GROUP BY d", (self.c.wf,))}
        st = {o: {"status": "active" if used.get(o, 0) >= 300 else "unknown", "since": now} for o in opts}
        self.c.s.set_state(self.c.wf, self.key, st)
        self.update(now)
        return st

    def _rate(self, d, since, until=None):
        n, c, _, _ = A.window(self.c.s, self.c.wf, since, until, " AND d=?", (d,))
        return n, c

    def update(self, now):
        st = self.c.s.state(self.c.wf, self.key)
        if st is None:
            return
        changed = False
        live = [o for o, v in st.items() if v["status"] in ("active", "probation")]
        recent = A.arm_strata(self.c.s, self.c.wf, "caller_id", "pickup_rate", now - self.FLAG_WINDOW)
        for o in live:
            v = st[o]
            rest = {x for x in live if x != o}
            n1, c1 = self._rate(o, now - self.FLAG_WINDOW)
            n0, c0 = self._rate(o, now - self.FLAG_WINDOW - 3 * DAY, now - self.FLAG_WINDOW)
            own = None
            if n0 >= 300 and n1 >= 150:
                own = stratified_simple(n1, c1, n0, c0)
            vs = A.compare(recent, o, rest) if rest else None
            dropped_own = own and own.rel < -0.25 and own.p_less < 1e-4
            below_rest = vs is None or (vs.rel < -0.2 and vs.p_less < 0.01)
            if v["status"] == "active" and dropped_own and below_rest:
                v.update(status="flagged", since=now, urgent=True,
                         evidence={"vs_own_past": own.as_dict(), "vs_others": vs.as_dict() if vs else None})
                changed = True
                self.c.s.decide(now, self.c.wf, "did_flagged",
                                f"Caller ID {o}: pickup {own.rate_a:.1%} in the last 6h vs {own.rate_b:.1%} its own "
                                f"previous 3 days ({own.rel:+.0%}, n={own.n_a})" +
                                (f", {vs.rel:+.0%} vs other numbers" if vs else "") + " — stop dialling from it",
                                {"did": o, **v["evidence"]})
            elif v["status"] == "probation":
                full = A.compare(A.arm_strata(self.c.s, self.c.wf, "caller_id", "pickup_rate", v["since"]), o, rest) \
                    if rest else None
                nn, _ = self._rate(o, v["since"])
                if full and full.n_a >= 200 and full.rel < -0.3 and full.p_less < 0.01:
                    v.update(status="flagged", since=now, urgent=True, evidence=full.as_dict())
                    changed = True
                    self.c.s.decide(now, self.c.wf, "did_flagged",
                                    f"Caller ID {o} failed probation: pickup {full.rel:+.0%} vs other numbers (n={full.n_a})",
                                    {"did": o, **full.as_dict()})
                elif nn >= 300:
                    v.update(status="active", since=now, evidence=full.as_dict() if full else None)
                    changed = True
                    self.c.s.decide(now, self.c.wf, "did_active",
                                    f"Caller ID {o} passed probation" + (f" ({full.rel:+.0%} vs others, n={full.n_a})" if full else ""),
                                    {"did": o})
        for o, v in st.items():
            if v["status"] == "unknown":
                v.update(status="probation", since=now)
                changed = True
                self.c.s.decide(now, self.c.wf, "did_probe",
                                f"Caller ID {o}: unused so far — try it on {int(self.PROBE * 100)}% of dials before trusting it",
                                {"did": o})
        if changed:
            self.c.s.set_state(self.c.wf, self.key, st)

    def target(self, current: dict | None = None) -> tuple[dict | None, bool]:
        st = self.c.s.state(self.c.wf, self.key)
        if not st:
            return None, False
        active = [o for o, v in st.items() if v["status"] == "active"]
        probing = [o for o, v in st.items() if v["status"] == "probation"]
        if not active and not probing:
            return None, False
        w = {o: self.PROBE for o in probing}
        rest = max(0.0, 1 - sum(w.values()))
        if active:
            last = self.c.s.meta("last_minute", 0) or 0
            score = {}
            for o in active:
                n, c = self._rate(o, last - DAY)
                score[o] = (c / n) ** 2 if n >= 100 else None
            known = [x for x in score.values() if x is not None]
            mean = sum(known) / len(known) if known else 1.0
            score = {o: (x if x is not None else mean) for o, x in score.items()}
            tot = sum(score.values()) or 1
            raw = {o: max(self.FLOOR, x / tot) for o, x in score.items()}
            tr = sum(raw.values())
            for o in active:
                w[o] = rest * raw[o] / tr
        tgt = normalise(w)
        urgent = any(v.get("urgent") for v in st.values())
        from .config_ops import tv
        if current is not None and not urgent and set(tgt) == set(normalise(current)):
            # pure rebalancing among the same numbers: only if it moves enough, and not more than every 6h
            last_change = self.c.s.one("SELECT MAX(minute) FROM config_versions WHERE wf=? AND changed_dims LIKE '%caller_id%'",
                                       (self.c.wf,))[0]
            now = self.c.s.meta("last_minute", 0) or 0
            if tv(tgt, current) < self.HYSTERESIS or (last_change is not None and now - last_change < self.REBALANCE_EVERY):
                return normalise(current), False
        return tgt, urgent

    def clear_urgent(self):
        st = self.c.s.state(self.c.wf, self.key)
        for v in (st or {}).values():
            v.pop("urgent", None)
        self.c.s.set_state(self.c.wf, self.key, st)


def stratified_simple(n1, c1, n0, c0):
    from .stats import stratified
    return stratified([(n1, c1, n0, c0)])


# ============================================================================ call-time (C3, C4)
class CallTime:
    """Cohort × hour pickup from history → schedule.hour_weights.

    Weight = (shrunk pickup in hour h / cohort's own average)^2, clipped to [0.25, 4]. It shifts each cohort
    toward the hours where it is relatively most reachable; every in-window hour keeps weight > 0, so no
    dialler capacity is ever left idle. Verified after going live against the pickup guardrail."""
    SHRINK = 150
    GAMMA = 2.0

    def __init__(self, ctx: WFContext):
        self.c = ctx
        self.key = "schedule"

    def init(self, now):
        lo, hi = self.c.window
        rows = self.c.s.q("SELECT cohort, hour, SUM(dials), SUM(conn) FROM agg WHERE wf=? AND day < 0 GROUP BY cohort, hour",
                          (self.c.wf,))
        tab = {}
        for coh, h, d, c in rows:
            tab.setdefault(coh, {})[h] = (d, c)
        weights, detail = {}, {}
        for coh, hours in tab.items():
            D = sum(d for d, _ in hours.values())
            Cn = sum(c for _, c in hours.values())
            if D < 1000:
                continue
            p = Cn / D
            tbl = {}
            for h in range(lo, hi):
                d, c = hours.get(h, (0, 0))
                ph = (c + self.SHRINK * p) / (d + self.SHRINK)
                tbl[str(h)] = round(min(4.0, max(0.25, (ph / p) ** self.GAMMA)), 2)
            if max(tbl.values()) / min(tbl.values()) < 1.3:
                continue  # flat profile: nothing to gain, don't add config noise
            weights[coh] = tbl
            detail[coh] = {"hist_dials": D, "pickup": round(p, 4)}
        est = self.estimate(tab, weights)
        st = {"status": "proposed" if weights else "none", "weights": weights, "detail": detail,
              "estimate": est, "since": now}
        self.c.s.set_state(self.c.wf, self.key, st)
        if weights:
            self.c.s.decide(now, self.c.wf, "calltime_plan",
                            f"Call-time weights for {len(weights)} cohorts from {len(rows)} history cells; "
                            f"modelled pickup {est['old']:.3f} → {est['new']:.3f} (optimistic, ignores cadence)",
                            {"weights": weights, "detail": detail, "estimate": est})
        return st

    def estimate(self, tab, weights):
        """Dry run on history: re-weight each hour's dial mix across cohorts and recompute pickup."""
        lo, hi = self.c.window
        old_c = new_c = tot = 0.0
        for h in range(lo, hi):
            cells = {coh: hrs.get(h, (0, 0)) for coh, hrs in tab.items()}
            dh = sum(d for d, _ in cells.values())
            if dh == 0:
                continue
            # share of each cohort's leads in the whole pool ≈ its share of all dials
            pool = {coh: sum(d for d, _ in hrs.values()) for coh, hrs in tab.items()}
            P = sum(pool.values())
            wsum = sum(pool[c] / P * float(weights.get(c, {}).get(str(h), 1.0)) for c in pool)
            for coh, (d, c) in cells.items():
                rate = c / d if d else 0
                share_old = pool[coh] / P
                share_new = share_old * float(weights.get(coh, {}).get(str(h), 1.0)) / wsum
                old_c += dh * share_old * rate
                new_c += dh * share_new * rate
            tot += dh
        return {"old": round(old_c / tot, 4) if tot else 0, "new": round(new_c / tot, 4) if tot else 0}

    def target(self):
        st = self.c.s.state(self.c.wf, self.key)
        if not st or st["status"] == "none":
            return None          # not managed: leave whatever is there
        if st["status"] == "reverted":
            return {}            # clear our weights
        return st["weights"]

    def verify(self, now, live_since):
        """Guardrail on overall pickup after the schedule went live (≥ 1 full day), versus history."""
        st = self.c.s.state(self.c.wf, self.key)
        if not st or st["status"] != "live" or now - live_since < DAY:
            return
        d0, c0, _, _ = A.window(self.c.s, self.c.wf, -14 * DAY, 0)
        d1, c1, _, _ = A.window(self.c.s, self.c.wf, live_since, None)
        flagged = [o for o, v in (self.c.s.state(self.c.wf, "did") or {}).items() if v["status"] == "flagged"]
        if flagged:
            q = ",".join("?" * len(flagged))
            d1, c1, _, _ = A.window(self.c.s, self.c.wf, live_since, None, f" AND d NOT IN ({q})", flagged)
            d0, c0, _, _ = A.window(self.c.s, self.c.wf, -14 * DAY, 0, f" AND d NOT IN ({q})", flagged)
        if not d0 or not d1:
            return
        p0, p1 = c0 / d0, c1 / d1
        se = math.sqrt(p0 * (1 - p0) / d0 + p1 * (1 - p1) / d1)
        st["check"] = {"hist_pickup": round(p0, 4), "live_pickup": round(p1, 4), "n": d1, "at": now}
        if p1 - p0 < -self.c.guard_drop and (p1 - p0) / se < -3:
            st["status"] = "reverted"
            st["urgent"] = True
            self.c.s.decide(now, self.c.wf, "calltime_revert",
                            f"Call-time weights reverted: pickup {p1:.3f} vs {p0:.3f} before (n={d1})", st["check"])
        self.c.s.set_state(self.c.wf, self.key, st)

"""The controller loop: ingest → reconcile → learn → target → govern, for every workflow, every step."""
from __future__ import annotations

import copy
import json
import logging
import resource
import time
from collections import defaultdict
from pathlib import Path

from . import analysis as A
from . import cohorts as C
from . import config_ops as K
from .governance import Governance
from .incidents import Hallucination
from .ingest import Ingestor
from .planner import CallTime, CallerIds, Experiment, WFContext

log = logging.getLogger("loop")
DAY = 1440
EXP_DIMS = ["welcome_message", "voice"]


class Credits:
    def __init__(self, sim):
        self.sim = sim
        self.left = sim.get("/sim/state")["credits_remaining"]

    def refresh(self, st):
        self.left = st.get("credits_remaining", self.left)

    def remaining(self):
        return self.left

    def spent(self, c):
        self.left -= c


class Controller:
    def __init__(self, sim, store, prefs_path=None):
        self.sim, self.s = sim, store
        self.specs = {w["workflow_id"]: w for w in sim.get("/workflows")}
        for wf, sp in self.specs.items():
            store.x("INSERT OR REPLACE INTO workflows VALUES (?,?,?)", (wf, sp["tenant_id"], json.dumps(sp)))
        prefs = json.loads(Path(prefs_path).read_text()) if prefs_path and Path(prefs_path).exists() else {}
        self.prefs = {k: v for k, v in prefs.items() if not k.startswith("_")}
        self.ing = Ingestor(store, sim)
        self.credits = Credits(sim)
        self.gov = Governance(store, sim, self.specs, self.prefs)
        self.ctx = {wf: WFContext(store, wf, sp) for wf, sp in self.specs.items()}
        self.exps = {wf: [Experiment(c, d) for d in EXP_DIMS] for wf, c in self.ctx.items()}
        self.dids = {wf: CallerIds(c) for wf, c in self.ctx.items()}
        self.ct = {wf: CallTime(c) for wf, c in self.ctx.items()}
        self.hal = {wf: Hallucination(c, sim, self.credits) for wf, c in self.ctx.items()}

    # ------------------------------------------------------------------ bootstrap (C3)
    def bootstrap(self, now):
        if self.s.meta("bootstrapped"):
            return
        t0 = time.perf_counter()
        n = self.ing.ingest_history()
        s = self.s
        s.begin()
        try:
            for wf in self.specs:
                lead_cells = {c: k for c, k in s.q("SELECT cell, COUNT(*) FROM leads WHERE wf=? AND day < 0 GROUP BY cell", (wf,))}
                hour_rows = s.q("SELECT cell, hour, SUM(dials), SUM(conn) FROM hist WHERE wf=? GROUP BY cell, hour", (wf,))
                second, scores = C.choose_second_feature(hour_rows)
                part = C.build_partition(lead_cells, second)
                cmap = C.cohort_of_cell(part)
                s.set_state(wf, "partition", part)
                for c in part:
                    s.x("INSERT OR REPLACE INTO cohorts VALUES (?,?,?,?,?)",
                        (wf, c["id"], json.dumps(c["where"]), json.dumps(c["cells"]), c["share"]))
                # fold cell-level history into cohort-level aggregates
                acc = defaultdict(lambda: [0, 0, 0, 0])
                for day, hour, cell, w, v, d, p, a, b, e, h in s.q(
                        "SELECT day,hour,cell,w,v,d,p,dials,conn,eng,halluc FROM hist WHERE wf=?", (wf,)):
                    x = acc[(wf, day, hour, cmap.get(cell, "default"), w, v, d, p)]
                    x[0] += a; x[1] += b; x[2] += e; x[3] += h
                s.c.executemany("INSERT OR REPLACE INTO agg VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                [k + tuple(v) for k, v in acc.items()])
                for cell, coh in cmap.items():
                    s.x("UPDATE leads SET cohort=? WHERE wf=? AND cell=?", (coh, wf, cell))
                s.x("UPDATE leads SET cohort='default' WHERE wf=? AND cohort IS NULL", (wf,))
                s.decide(now, wf, "cohorts_defined",
                         f"{len(part)} cohorts on lang × {second} (covering {sum(c['share'] for c in part):.0%} of leads)",
                         {"second_feature": second, "hour_profile_chi2_per_df": scores,
                          "cohorts": [{k: c[k] for k in ('id', 'where', 'share')} for c in part]})
            s.commit()
        except BaseException:
            s.rollback()
            raise
        self.ing.hist_cells.clear()
        self.ing.load_maps()
        for wf in self.specs:
            cfg = self.sim.get(f"/workflows/{wf}/config")
            self._record_config(wf, cfg)
            for e in self.exps[wf]:
                if e.st() is None:
                    e.init(cfg["config"], now)
            if self.s.state(wf, "did") is None:
                self.dids[wf].init(cfg["config"], now)
            if self.s.state(wf, "schedule") is None:
                self.ct[wf].init(now)
            self.hal[wf].baseline()
        self.s.set_meta("bootstrapped", True)
        log.info("bootstrap: %d history events in %.1fs", n, time.perf_counter() - t0)

    # ------------------------------------------------------------------ one step
    def step(self, now):
        t0 = time.perf_counter()
        self.gov.reconcile(now)
        for wf in self.specs:
            cur = self.sim.get(f"/workflows/{wf}/config")
            self._record_config(wf, cur)
            look = self._daily_look_due(wf, now)
            for e in self.exps[wf]:
                e.update(now, look, cur["config"])
            self.dids[wf].update(now)
            self._schedule_status(wf, cur, now)
            self.hal[wf].update(now, cur["config"])
            target, parts = self.build_target(wf, cur, now)
            self.gov.act(wf, now, cur, target, parts,
                         lambda old, new, ps, wf=wf: self.describe(wf, old, new, ps, now))
            if look:
                self.s.set_state(wf, "last_look_day", now // DAY)
        return (time.perf_counter() - t0) * 1000

    def _daily_look_due(self, wf, now):
        lo, _ = self.gov.send_hours(wf)
        last = self.s.state(wf, "last_look_day", -1)
        return now // DAY > last and (now % DAY) // 60 >= lo - 1

    def _record_config(self, wf, cur):
        self.s.x("INSERT OR IGNORE INTO config_versions(wf,version,actor,minute) VALUES (?,?,?,?)",
                 (wf, cur["version"], cur["actor"], None))
        self.s.x("UPDATE config_versions SET config=? WHERE wf=? AND version=? AND config IS NULL",
                 (json.dumps(cur["config"]), wf, cur["version"]))

    def _schedule_status(self, wf, cur, now):
        st = self.s.state(wf, "schedule")
        if not st:
            return
        hw = cur["config"].get("schedule", {}).get("hour_weights", {})
        if st["status"] == "proposed" and st["weights"] and K.canon(_norm_hw(hw)) == K.canon(_norm_hw(st["weights"])):
            st.update(status="live", live_since=now)
            self.s.set_state(wf, "schedule", st)
            self.s.decide(now, wf, "calltime_live", "Call-time weights are live; guardrail check after 24h", {})
        elif st["status"] == "live":
            self.ct[wf].verify(now, st["live_since"])

    # ------------------------------------------------------------------ targets
    def build_target(self, wf, cur, now):
        cfg = cur["config"]
        ctx = self.ctx[wf]
        t = copy.deepcopy(cfg)
        parts = []
        part_ids = self.ing.partitions.get(wf, [])
        desired = C.config_cohorts(part_ids)
        if desired and not ctx.owned_by_supervisor("cohorts", now) and K.canon(t["cohorts"]) != K.canon(desired):
            t["cohorts"] = desired
            parts.append({"component": "cohorts", "dims": ["cohorts"], "protective": False,
                          "summary": f"define {len(desired)} customer groups (nothing customers hear changes)",
                          "gain": 0.0, "dry": {"cohorts": [c["id"] for c in desired]}})
        cohorts_ok = K.canon(t["cohorts"]) == K.canon(desired)
        ids = {c["id"] for c in part_ids}

        for e in self.exps[wf]:
            st = e.st()
            if st is None or ctx.owned_by_supervisor(e.dim, now):
                continue
            tgt = e.target(st)
            before = copy.deepcopy(t)
            t["allocations"]["default"][e.dim] = tgt
            if cohorts_ok:
                for coh in ids:
                    ov = st.get("overrides", {}).get(coh)
                    if ov:
                        t["allocations"].setdefault(coh, {})[e.dim] = ov
                    elif coh in t["allocations"]:
                        t["allocations"][coh].pop(e.dim, None)
                        if not t["allocations"][coh]:
                            del t["allocations"][coh]
            if K.max_shift(before, t) > 1e-6:
                parts.append(self._exp_part(wf, e, st, before, t))
            elif st.get("urgent") and K.max_shift(cfg, t) < 1e-6:
                st["urgent"] = False
                e.save(st)

        did_st = self.s.state(wf, "did") or {}
        cur_did = K.dist(cfg, "default", "caller_id")
        if any(v.get("urgent") for v in did_st.values()) and \
                all(cur_did.get(o, 0) == 0 for o, v in did_st.items() if v["status"] == "flagged"):
            self.dids[wf].clear_urgent()   # protective goal met: flagged numbers carry no traffic
        dtgt, durgent = self.dids[wf].target(K.dist(t, "default", "caller_id"))
        if dtgt and not ctx.owned_by_supervisor("caller_id", now):
            if K.tv(K.dist(t, "default", "caller_id"), dtgt) > 1e-6:
                before = copy.deepcopy(t)
                t["allocations"]["default"]["caller_id"] = dtgt
                parts.append(self._did_part(wf, before, t, durgent))
            elif durgent:
                self.dids[wf].clear_urgent()

        w = self.ct[wf].target()
        if w is not None and not ctx.owned_by_supervisor("schedule", now):
            hw = {k: v for k, v in w.items() if (cohorts_ok and k in ids) or k == "default"}
            if K.canon(_norm_hw(t["schedule"].get("hour_weights", {}))) != K.canon(_norm_hw(hw)):
                st = self.s.state(wf, "schedule")
                t["schedule"]["hour_weights"] = hw
                dials = self._per_day(wf)[0]
                est = st.get("estimate", {})
                gain = 0.5 * (est.get("new", 0) - est.get("old", 0)) * dials * self._eng_rate(wf) if hw else 0
                parts.append({"component": "schedule", "dims": ["schedule"], "protective": bool(st.get("urgent")),
                              "summary": ("call each group in the hours it picks up most (history-based weights)"
                                          if hw else "remove call-time weights (pickup guardrail tripped)"),
                              "gain": gain, "dry": {"model_on_history": est, "discount": 0.5,
                                                    "note": "replays 14 days of history; ignores cadence, so optimistic"}})

        fix, run_id = self.hal[wf].desired()
        if fix and t["prompt_version"] != fix:
            inc = self.s.state(wf, "incident")
            supervised = "prompt_version" in self.s.state(wf, "supervisor_dims", {})
            t["prompt_version"] = fix
            parts.append({"component": "prompt", "dims": ["prompt_version"], "protective": True,
                          "summary": f"switch script {inc['bad']} → {fix}: hallucination flags {inc['evidence']['rate']:.1%} "
                                     f"vs {inc['evidence']['baseline']:.1%} normal"
                                     + (" (this reverses a supervisor's script change)" if supervised else ""),
                          "gain": 0.0, "safety": True, "evidence": {"validation_run_id": run_id},
                          "dry": {"field": inc["evidence"], "validation": {"run": run_id, "pass_rate": inc.get("pass_rate")},
                                  "candidates_tried": inc.get("tried")}})
        return t, parts

    def _exp_part(self, wf, e, st, before, after):
        old, new = K.dist(before, "default", e.dim), K.dist(after, "default", e.dim)
        strata = A.arm_strata(self.s, wf, e.dim, "engagement_rate", st["started"])
        tot = A.arm_totals(strata)
        inc_rate = (tot.get(st["incumbent"], [0, 0])[1] / tot[st["incumbent"]][0]) if tot.get(st["incumbent"], [0])[0] else None
        rates = {o: (x / n if n else None) for o, (n, x) in tot.items()}

        def exp_rate(alloc):
            return sum(wt * (rates.get(o) if rates.get(o) is not None else (inc_rate or 0)) for o, wt in alloc.items())
        conn = self._per_day(wf)[1]
        gain = (exp_rate(new) - exp_rate(old)) * conn if inc_rate is not None else 0.0
        st_name = st["status"]
        stopped = [o for o in old if old.get(o, 0) > 0 and o not in new and o in st["dropped"]
                   and "decayed" not in st["dropped"][o].get("why", "")]
        if stopped and st_name in ("running", "concluded") and not [o for o in new if o not in old]:
            why = "; ".join(f"{o} {st['dropped'][o].get('rel', 0):+.0%} engagement vs {st['incumbent']}" for o in stopped)
            rest = f", keep testing {', '.join(st['active'])}" if st["active"] else f", {st['incumbent']} back to 100%"
            s = f"{e.dim.replace('_', ' ')}: stop testing {', '.join(stopped)} ({why}){rest}"
        elif st_name == "running":
            s = f"{e.dim.replace('_', ' ')}: test {', '.join(st['active'])} on {int(e.EXPLORE * 100)}% of calls ({st['incumbent']} keeps the rest)"
        elif st_name == "supervisor":
            s = f"{e.dim.replace('_', ' ')}: keep the supervisor's setting"
        elif st_name == "promoted" and st.get("vs_supervisor"):
            s = (f"{e.dim.replace('_', ' ')}: REVERSES the supervisor's choice {st['holdout']} — move to {st['winner']} "
                 f"(engagement lift proven against it), keep {st['holdout']} on {int(e.HOLDOUT * 100)}%")
        elif st_name == "promoted":
            s = f"{e.dim.replace('_', ' ')}: move to {st['winner']} (engagement lift proven), keep {st['holdout']} on {int(e.HOLDOUT * 100)}% as a check"
        else:
            last = [o for o, v in st["dropped"].items() if v.get("why") == "decayed after promotion"]
            s = (f"{e.dim.replace('_', ' ')}: roll back {last[-1]} → {st['incumbent']} (it stopped winning)" if last and st.get("urgent")
                 else f"{e.dim.replace('_', ' ')}: keep {st['incumbent']} only (no challenger won)")
        if st.get("overrides"):
            s += f"; cohorts {sorted(st['overrides'])} stay on {st['holdout'] or st['incumbent']}"
        return {"component": e.dim, "dims": [e.dim], "protective": bool(st.get("urgent")), "summary": s, "gain": gain,
                "dry": {"replay": "observed engagement per arm since experiment start, applied to old vs new allocation",
                        "arm_engagement": {o: (round(r, 4) if r is not None else None) for o, r in rates.items()},
                        "arm_connects": {o: v[0] for o, v in tot.items()},
                        "expected_engagement_old": round(exp_rate(old), 4) if inc_rate else None,
                        "expected_engagement_new": round(exp_rate(new), 4) if inc_rate else None,
                        "unobserved_arms_assumed_equal_to_incumbent": [o for o in new if rates.get(o) is None]}}

    def _did_part(self, wf, before, after, urgent):
        st = self.s.state(wf, "did")
        rows = self.s.q("SELECT d, SUM(dials), SUM(conn) FROM agg WHERE wf=? AND day >= ? GROUP BY d",
                        (wf, (self.s.meta("last_minute", 0) - DAY) // DAY))
        rates = {d: c / n for d, n, c in rows if n}
        old, new = K.dist(before, "default", "caller_id"), K.dist(after, "default", "caller_id")
        base = sum(rates.values()) / len(rates) if rates else 0

        def ep(alloc):
            return sum(w * rates.get(o, base) for o, w in alloc.items())
        dials = self._per_day(wf)[0]
        gain = (ep(new) - ep(old)) * dials * self._eng_rate(wf)
        flagged = [o for o, v in st.items() if v["status"] == "flagged" and old.get(o, 0) > 0]
        probing = [o for o, v in st.items() if v["status"] == "probation" and not old.get(o)]
        bits = []
        if flagged:
            bits.append(f"stop dialling from {', '.join(flagged)} (pickup collapsed)")
        if probing:
            bits.append(f"try {', '.join(probing)} on {int(CallerIds.PROBE * 100)}% of dials")
        if not bits:
            bits.append("spread dials across healthy numbers by recent pickup (a number wears out with volume)")
        return {"component": "caller_id", "dims": ["caller_id"], "protective": urgent,
                "summary": "caller ID: " + ("; ".join(bits) or "rebalance across healthy numbers"), "gain": gain,
                "dry": {"pickup_by_did_last_24h": {k: round(v, 4) for k, v in rates.items()},
                        "expected_pickup_old": round(ep(old), 4), "expected_pickup_new": round(ep(new), 4),
                        "did_status": {o: v["status"] for o, v in st.items()}}}

    def _per_day(self, wf):
        last = self.s.meta("last_minute", 0)
        since = max(last - 2 * DAY, -14 * DAY) if last > 0 else -3 * DAY
        d, c, e, _ = A.window(self.s, wf, since, last if last > 0 else 0)
        days = max((min(last, 2 * DAY) if last > 0 else 3 * DAY) / DAY, 1)
        return d / days, c / days, e / days

    def _eng_rate(self, wf):
        d, c, e = self._per_day(wf)
        return e / c if c else 0.5

    # ------------------------------------------------------------------ proposal content (C10)
    def describe(self, wf, old, new, parts, now):
        spec = self.specs[wf]
        ids = {c["id"]: c["share"] for c in self.ing.partitions.get(wf, [])}
        shares = dict(ids, default=max(0.0, 1 - sum(ids.values())))
        affected = set()
        for k in K.cohort_keys(old, new) | set(ids) | {"default"}:
            for d in K.AGENT_DIMS:
                if K.tv(K.dist(old, k, d), K.dist(new, k, d)) > 1e-6:
                    affected.add(k)
            ho = (old.get("schedule") or {}).get("hour_weights", {})
            hn = (new.get("schedule") or {}).get("hour_weights", {})
            if K.canon(ho.get(k)) != K.canon(hn.get(k)) or (k != "default" and K.canon(ho.get("default")) != K.canon(hn.get("default"))
                                                         and k not in hn):
                affected.add(k)
        if old.get("prompt_version") != new.get("prompt_version"):
            affected = set(shares)
        share = sum(shares.get(k, 0) for k in affected)
        span = len(spec["cadence"])
        today = max(now // DAY, 0)
        lc = self.s.one("SELECT COALESCE(SUM(leads),0), COALESCE(SUM(concluded),0) FROM daily WHERE wf=? AND day > ? AND day <= ?",
                        (wf, today - span, today))
        active = max(0, (lc[0] or 0) - (lc[1] or 0))
        affected_leads = int(round(share * (active + spec["daily_batch"]["size"])))
        gain = sum(p.get("gain", 0) for p in parts)
        impact = max(0.0, min(1.0, gain / (0.05 * spec["daily_batch"]["size"])))
        if any(p.get("safety") for p in parts):
            impact = max(impact, 0.9)
        impact = round(impact, 3)
        summary = "; ".join(p["summary"] for p in parts) or "config maintenance"
        rationale = (" | ".join(f"[{p['component']}] {p['summary']}" for p in parts) +
                     f" | impact_score = expected extra concluded leads/day ({gain:+.1f}) ÷ 5% of the daily batch"
                     + ("; safety fix, floored at 0.9" if any(p.get("safety") for p in parts) else ""))
        ev = {}
        for p in parts:
            ev.update(p.get("evidence") or {})
        return {"change_summary": summary[0].upper() + summary[1:], "diff": K.diff(old, new) or {"note": "no-op"},
                "affected_leads": affected_leads, "impact_score": impact, "rationale": rationale,
                "dry_run": {p["component"]: p["dry"] for p in parts} | {"affected": {
                    "cohorts": sorted(affected), "population_share": round(share, 3), "active_leads": active,
                    "next_batch": spec["daily_batch"]["size"]}},
                "evidence": ev or None}

    # ------------------------------------------------------------------ metrics (C14)
    def record_metrics(self, now, wall_ms, events, ingest_ms, decide_ms):
        lag = now - (self.s.meta("ingested_until", now) or now)   # sim-minutes of world we haven't seen
        pending = self.s.one("SELECT COUNT(*) FROM proposals WHERE status IN ('sending','pending','approved')")[0]
        dec = self.s.one("SELECT COUNT(*) FROM decisions WHERE minute > ?", (now - 60,))[0]
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        self.s.x("INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (now, wall_ms, events, ingest_ms, decide_ms, lag, self.credits.remaining(), pending, dec, rss))


def _norm_hw(hw):
    return {k: {str(h): round(float(w), 4) for h, w in tbl.items()} for k, tbl in (hw or {}).items()}

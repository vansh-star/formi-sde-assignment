"""Hallucination incident response (C8): detect → pick a fix → validate → propose with evidence → verify.

Detection is on populations: the live prompt's flag rate over the last 3h of connected calls, against the
history baseline (which already includes the detector's false-positive rate). Fire when n ≥ 200, the rate is
above max(2.5×base, base + 2pp) and a Poisson tail test gives p < 1e-5 (checked hourly, so the bar is high).

Fix order, cheapest correct first:
  1. the last prompt that was clean in the field (its engagement is known too) — one validation run
  2. only if none passes: ask the LLM, drop suggestions whose catalog summary signals risk (persuasive /
     urgency → hallucination; near-verbatim script → engagement collapse), validate the rest best-first
The LLM proposes; validation pass rate (a deterministic threshold) decides. Credits are capped per incident.
"""
from __future__ import annotations

from . import analysis as A
from .api import ApiError
from .stats import poisson_sf, wilson

DAY = 1440
RISKY_HALLUC = ("persuasive", "urgency", "freer", "benefit claims", "richer")
RISKY_ENGAGE = ("verbatim", "strict")
SAFE_HINTS = ("grounded", "restricted", "current production")


class Hallucination:
    WINDOW = 4 * 60
    MIN_N = 200
    MIN_N_SUSPECT = 100
    P_FIRE = 1e-5   # worst null window over a full no-incident run (public_01) was 7e-4; ~70x margin
    P_FIRE_SUSPECT = 1e-3  # prompt already failed synthetic validation: two independent signals
    VAL_N = 300
    INCIDENT_CREDIT_CAP = 20
    CREDIT_RESERVE = 15

    def __init__(self, ctx, sim, credits):
        self.c, self.sim, self.credits = ctx, sim, credits

    # -------------------------------------------------------------- baseline
    def baseline(self) -> float:
        b = self.c.s.state(self.c.wf, "halluc_base")
        if b is None:  # pre-D0 history only: the business default prompt, detector false positives included
            n, h = self.c.s.one("SELECT SUM(conn), SUM(halluc) FROM agg WHERE wf=? AND day < 0", (self.c.wf,))
            b = max((h or 0) / n if n else 0.01, 0.002)
            self.c.s.set_state(self.c.wf, "halluc_base", b)
        return b

    def threshold(self):
        b = self.baseline()
        return max(2.5 * b, b + 0.02)

    # -------------------------------------------------------------- loop
    def update(self, now, cfg):
        st = self.c.s.state(self.c.wf, "incident") or {"status": "none"}
        cur = cfg["prompt_version"]
        b, thr = self.baseline(), self.threshold()
        if st["status"] in ("none", "resolved"):
            self._maybe_suspect(now, cur, st)
            n, x = self._window(cur, now - self.WINDOW)
            sus = st.get("suspect") == cur
            min_n = self.MIN_N_SUSPECT if sus else self.MIN_N
            p_fire = self.P_FIRE_SUSPECT if sus else self.P_FIRE
            if n >= min_n and x / n > thr and poisson_sf(x, n * b) < p_fire:
                st = {"status": "detected", "bad": cur, "detected": now, "spent": 0, "tried": {},
                      "evidence": {"prompt": cur, "connected": n, "flagged": x, "rate": round(x / n, 4),
                                   "baseline": round(b, 4), "threshold": round(thr, 4)}}
                self.c.s.decide(now, self.c.wf, "halluc_detected",
                                f"Hallucination flags on {cur}: {x}/{n} = {x / n:.1%} in the last 4h vs baseline "
                                f"{b:.1%} — opening incident", st["evidence"])
        if st["status"] == "detected":
            if cur != st["bad"]:
                st["status"] = "resolved"
                self.c.s.decide(now, self.c.wf, "halluc_resolved",
                                f"Prompt changed away from {st['bad']} by someone else; closing incident", {})
            else:
                self._choose_fix(now, st)
        elif st["status"] == "ready" and cur == st["fix"]:
            st.update(status="verifying", applied=now)
            self.c.s.decide(now, self.c.wf, "halluc_fix_live", f"Fix {cur} is live; verifying in the field", {})
        elif st["status"] == "ready" and cur not in (st["bad"], st["fix"]):
            st["status"] = "resolved"
        elif st["status"] == "verifying":
            n, x = self._window(cur, st["applied"])
            if n >= 200:
                lo, hi = wilson(x, n)
                if hi <= thr or (n >= 400 and x / n <= thr):
                    st["status"] = "resolved"
                    self.c.s.decide(now, self.c.wf, "halluc_verified",
                                    f"Fix {cur} verified: {x}/{n} = {x / n:.1%} flagged since going live "
                                    f"(threshold {thr:.1%}); time to fix {(now - st['detected']) // 60}h",
                                    {"n": n, "x": x, "mttr_min": now - st["detected"]})
                elif lo > thr:
                    st["tried"][cur] = {"field_rate": round(x / n, 4), "failed": True}
                    st.update(status="detected", bad=cur)
                    self.c.s.decide(now, self.c.wf, "halluc_fix_failed",
                                    f"Fix {cur} still hallucinating in the field ({x / n:.1%}); trying next option", {})
        self.c.s.set_state(self.c.wf, "incident", st)

    def desired(self):
        st = self.c.s.state(self.c.wf, "incident") or {}
        if st.get("status") == "ready":
            return st["fix"], st.get("run_id")
        return None, None

    # -------------------------------------------------------------- internals
    def _window(self, prompt, since):
        r = self.c.s.one("SELECT SUM(conn), SUM(halluc) FROM agg WHERE wf=? AND p=? AND day >= ? AND day*1440+hour*60 >= ?",
                         (self.c.wf, prompt, since // DAY, since))
        return (r[0] or 0), (r[1] or 0)

    def _field_ok(self):
        """Prompts that ran clean in the field with enough volume."""
        thr = self.threshold()
        out = []
        for p, (n, h, e) in A.by_prompt(self.c.s, self.c.wf, -60 * DAY).items():
            if n >= 300 and h / n <= thr * 0.75:
                out.append((p, n, h / n, e / n))
        return sorted(out, key=lambda t: (t[2], -t[3]))

    def _maybe_suspect(self, now, cur, st):
        """A supervisor shipped a prompt we've never seen run: one small validation (2 credits) tells us
        whether to watch it with a lower sample threshold. Spent only when it can change a decision."""
        own = self.c.s.state(self.c.wf, "supervisor_dims", {})
        if "prompt_version" not in own or now - own["prompt_version"] > DAY or st.get("checked") == cur:
            return
        if any(p == cur for p, *_ in self._field_ok()):
            st["checked"] = cur
            return
        run = self._validate(cur, 200, st, now, cap=False)
        st["checked"] = cur
        if run and run["pass_rate"] < self.c.spec["governance"]["min_validation_pass_rate"]:
            st["suspect"] = cur
            self.c.s.decide(now, self.c.wf, "halluc_suspect",
                            f"Supervisor prompt {cur} failed validation ({run['pass_rate']:.3f}); watching it closely",
                            {"run": run})
        self.c.s.set_state(self.c.wf, "incident", st)

    def _validate(self, prompt, n, st, now, cap=True):
        cost = -(-n // 100)
        if self.credits.remaining() - cost < self.CREDIT_RESERVE:
            self.c.s.decide(now, self.c.wf, "credits_low", f"Skipping validation of {prompt}: credit reserve", {})
            return None
        if cap and st.get("spent", 0) + cost > self.INCIDENT_CREDIT_CAP:
            return None
        try:
            run = self.sim.post("/validation/runs", {"workflow_id": self.c.wf, "prompt_version": prompt, "n": n})
        except ApiError as e:
            self.c.s.decide(now, self.c.wf, "validation_error", str(e), {})
            return None
        st["spent"] = st.get("spent", 0) + cost
        self.credits.spent(cost)
        self.c.s.x("INSERT OR IGNORE INTO validation_runs VALUES (?,?,?,?,?,?)",
                   (run["validation_run_id"], self.c.wf, prompt, n, run["pass_rate"], now))
        return run

    def _choose_fix(self, now, st):
        need = self.c.spec["governance"]["min_validation_pass_rate"]
        cands = [p for p, *_ in self._field_ok() if p != st["bad"] and p not in st["tried"]]
        source = "field-proven"
        if not cands and not st.get("llm_asked"):
            if self.credits.remaining() - 3 >= self.CREDIT_RESERVE:
                try:
                    r = self.sim.post("/llm/suggest-fixes", {"workflow_id": self.c.wf, "problem":
                                      f"hallucination flag rate {st['evidence']['rate']:.1%} on {st['bad']} "
                                      f"vs {st['evidence']['baseline']:.1%} baseline"})
                    self.credits.spent(3)
                    st["spent"] = st.get("spent", 0) + 3
                    st["llm_asked"] = [s["prompt_version"] for s in r["suggestions"]]
                    st["llm_summaries"] = {s["prompt_version"]: s["summary"] for s in r["suggestions"]}
                except ApiError as e:
                    self.c.s.decide(now, self.c.wf, "llm_error", str(e), {})
                    st["llm_asked"] = []
        if not cands:
            source = "llm"
            cands = [p for p in st.get("llm_asked", []) if p != st["bad"] and p not in st["tried"]]
            cands.sort(key=self._risk)
        for p in cands:
            run = self._validate(p, self.VAL_N, st, now)
            if run is None:
                break
            st["tried"][p] = {"pass_rate": run["pass_rate"], "run": run["validation_run_id"]}
            if run["pass_rate"] >= need:
                st.update(status="ready", fix=p, run_id=run["validation_run_id"], source=source,
                          pass_rate=run["pass_rate"])
                self.c.s.decide(now, self.c.wf, "halluc_fix_chosen",
                                f"Fix for {st['bad']}: switch to {p} ({source}); validation pass rate "
                                f"{run['pass_rate']:.3f} on n={run['n']} (needs ≥ {need})",
                                {"fix": p, "run": run, "tried": st["tried"], "spent": st["spent"]})
                return
        if st["status"] == "detected" and not st.get("stuck_logged"):
            st["stuck_logged"] = True
            self.c.s.decide(now, self.c.wf, "halluc_no_fix",
                            "No candidate prompt passed validation; incident stays open (pages on-call)",
                            {"tried": st["tried"]})

    def _risk(self, p) -> int:
        """Ordering prior from the catalog summary only; validation still decides. Near-verbatim scripts go
        last because validation can't see engagement (Kestrel memo: compliant and nobody stays on)."""
        s = (self.c.spec.get("prompt_catalog", {}).get(p) or "").lower()
        if any(k in s for k in SAFE_HINTS):
            return 0
        if any(k in s for k in RISKY_ENGAGE):
            return 3
        if any(k in s for k in RISKY_HALLUC):
            return 2
        return 1

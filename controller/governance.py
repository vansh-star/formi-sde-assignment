"""Governance client (C9): every config change goes through here.

Outbox pattern. A proposal row (status `sending`) is committed *before* the HTTP call, with a proposal_id
derived from the DB. After a crash, `sending` rows are reconciled against the simulator (GET /approvals or
the config history) before anything is resent, so nothing is double-proposed or double-applied, and no
pending approval is forgotten.

Lifecycle (our row):
  sending → pending → approved → applied
                    ↘ rejected | expired              (target still wanted → rebuilt from fresh data later)
                               approved → lapsed | stale  (stale: base changed; rebuilt on the new config)
  sending → abandoned  (base moved before it was ever sent)
  auto:  sending → applied | refused
"""
from __future__ import annotations

import copy
import json
import logging

from . import config_ops as K
from .api import ApiError

log = logging.getLogger("gov")
DAY = 1440
OPEN = ("sending", "pending", "approved")


class Governance:
    def __init__(self, store, sim, specs: dict, overrides: dict):
        self.s, self.sim, self.specs = store, sim, specs
        self.ov = overrides

    # ------------------------------------------------------------------ policy helpers
    def pol(self, wf):
        return self.specs[wf]["governance"]

    def tenant_ov(self, wf):
        return self.ov.get(self.specs[wf]["tenant_id"], {})

    def daily_cap(self, wf):
        cap = self.pol(wf)["max_requests_per_24h"]
        vol = self.tenant_ov(wf).get("max_requests_per_day")
        return min(cap, vol) if vol else cap

    def send_hours(self, wf):
        lo, hi = self.pol(wf)["approver_hours"]
        return max(lo, self.tenant_ov(wf).get("earliest_request_hour", lo)), hi

    def sent_24h(self, wf, now):
        return self.s.one("SELECT COUNT(*) FROM proposals WHERE wf=? AND channel='approval' AND sent_minute > ? "
                          "AND status NOT IN ('abandoned','invalid')", (wf, now - DAY))[0]

    def open_rows(self, wf):
        return self.s.q("SELECT proposal_id, urgent, approval_id, status, base_version FROM proposals "
                        "WHERE wf=? AND channel='approval' AND status IN ('sending','pending','approved')", (wf,))

    # ------------------------------------------------------------------ reconcile
    def reconcile(self, now):
        for pid, wf, ch, status, aid, base, body in self.s.q(
                "SELECT proposal_id, wf, channel, status, approval_id, base_version, body FROM proposals "
                "WHERE status IN ('sending','pending','approved') ORDER BY created_minute"):
            if status == "sending":
                self._reconcile_sending(now, pid, wf, ch, base, json.loads(body))
                continue
            try:
                a = self.sim.get(f"/approvals/{aid}")
            except ApiError as e:
                if e.status == 404:   # the simulator doesn't know it (new world): don't let it block the workflow
                    self._set(pid, "abandoned", now, reasons=[str(e)])
                log.warning("approval %s: %s", aid, e)
                continue
            st = a["status"]
            if st == "approved":
                self._apply(now, pid, wf, aid)
            elif st in ("rejected", "expired", "lapsed"):
                self._set(pid, st, now, reasons=a.get("reasons"))
                self.s.decide(now, wf, f"approval_{st}", f"{aid} {st}" +
                              (f": {'; '.join(a.get('reasons') or [])}" if a.get("reasons") else "") +
                              (" — will re-propose with fresh data" if st != "lapsed" else ""),
                              {"approval": a}, pid)
            elif st == "applied" and status != "applied":   # we crashed between apply and recording it
                v = self._version_for(wf, pid)
                self._set(pid, "applied", now, applied_version=v)

    def _reconcile_sending(self, now, pid, wf, ch, base, body):
        if ch == "approval":
            found = [a for a in self.sim.get("/approvals", workflow_id=wf) if a["proposal_id"] == pid]
            if found:
                a = found[0]
                self._set(pid, a["status"], now, approval_id=a["approval_id"], sent_minute=now)
                return
        else:
            v = self._version_for(wf, pid)
            if v:
                self._set(pid, "applied", now, applied_version=v)
                return
        cur = self.sim.get(f"/workflows/{wf}/config")["version"]
        if cur != base:
            self._set(pid, "abandoned", now)
            return
        # never reached the simulator: resend with the same id (idempotent)
        self._send(now, pid, wf, ch, body)

    def _version_for(self, wf, pid):
        for v in self.sim.get(f"/workflows/{wf}/config/history"):
            if v.get("proposal_id") == pid:
                return v["version"]
        return None

    def _apply(self, now, pid, wf, aid):
        try:
            r = self.sim.post(f"/workflows/{wf}/apply", {"approval_id": aid})
        except ApiError as e:
            if e.code == "stale_base_version":
                self._set(pid, "stale", now)
                self.s.decide(now, wf, "apply_stale", f"{aid} approved but config moved underneath it "
                              f"(now v{(e.detail or {}).get('current_version')}); rebuilding on the new config", {}, pid)
            elif e.status == 410:
                self._set(pid, "lapsed", now)
                self.s.decide(now, wf, "approval_lapsed", f"{aid} lapsed before we applied it", {}, pid)
            elif e.code == "not_approved" and (e.detail or {}).get("status") == "applied":
                self._set(pid, "applied", now, applied_version=self._version_for(wf, pid))
            else:
                log.error("apply %s failed: %s", aid, e)
            return
        self._set(pid, "applied", now, applied_version=r["version"])
        self.s.decide(now, wf, "applied", f"Applied {aid} → v{r['version']} ({', '.join(r['changed_dims'])})",
                      r, pid)

    def _set(self, pid, status, now, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        vals = [json.dumps(v) if k == "reasons" else v for k, v in kw.items()]
        self.s.x(f"UPDATE proposals SET status=?, updated_minute=?{', ' + cols if cols else ''} WHERE proposal_id=?",
                 [status, now, *vals, pid])

    # ------------------------------------------------------------------ act
    def act(self, wf, now, cur: dict, target: dict, parts: list[dict], describe) -> str | None:
        """Decide how (and whether) to move `cur` toward `target` this loop. One change per loop."""
        cfg = cur["config"]
        if not K.changed_dims(cfg, target):
            return None
        pol = self.pol(wf)
        open_ = self.open_rows(wf)
        urgent = any(p["protective"] for p in parts)

        # 1) auto-apply: only dims the policy allows; customer-facing dims only for protective moves
        #    (Kestrel). Supervisor-owned dims never auto-change. One max_shift step per loop.
        auto = self._auto_part(wf, cfg, target, parts, now)
        if auto is not None:
            auto_protective = any(p["protective"] and set(p["dims"]) & set(K.changed_dims(cfg, auto)) for p in parts)
            if not open_ or auto_protective:
                ok, why = K.auto_permitted(pol, cfg, auto)
                if ok:
                    sub = [p for p in parts if set(p["dims"]) & set(K.changed_dims(cfg, auto))]
                    return self.submit(now, wf, "auto", cur, auto, sub, describe, urgent=auto_protective)

        # 2) approval path for the rest
        rem = K.changed_dims(cfg, target)
        auto_dims = set(pol["auto_apply"].get("allowed_dims", [])) if pol["auto_apply"].get("enabled") else set()
        needs_human = [d for d in rem if d not in auto_dims or d in K.CUSTOMER_FACING or self._owned(wf, d, now)]
        if not needs_human:
            return None   # the auto path will get there step by step
        sent = self.sent_24h(wf, now)
        cap = self.daily_cap(wf)
        if open_:
            if urgent and not any(u for _, u, *_ in open_) and sent < cap:
                return self.submit(now, wf, "approval", cur, target, parts, describe, urgent=True)
            return None
        if urgent:
            if sent < cap:
                return self.submit(now, wf, "approval", cur, target, parts, describe, urgent=True)
            self._once(now, wf, "fatigue_block", "Protective change waiting: approval budget for 24h used")
            return None
        lo, hi = self.send_hours(wf)
        h = (now % DAY) // 60
        reserve = 1 if cap > 1 else 0
        last = self.s.one("SELECT MAX(sent_minute) FROM proposals WHERE wf=? AND channel='approval'", (wf,))[0]
        if lo <= h < hi - 2 and sent < cap - reserve and (last is None or now - last >= 4 * 60):
            return self.submit(now, wf, "approval", cur, target, parts, describe, urgent=False)
        return None

    def _owned(self, wf, dim, now):
        own = self.s.state(wf, "supervisor_dims", {})
        return dim in own and now - own[dim] < DAY

    def _auto_part(self, wf, cfg, target, parts, now):
        pol = self.pol(wf)
        aa = pol["auto_apply"]
        if not aa.get("enabled"):
            return None
        allowed = set(aa.get("allowed_dims", []))
        protective_dims = {d for p in parts if p["protective"] for d in p["dims"]}
        out = copy.deepcopy(cfg)
        cohort_ids = {c["id"] for c in cfg.get("cohorts", [])}
        for d in K.AGENT_DIMS:
            if d not in allowed or self._owned(wf, d, now):
                continue
            if d in K.CUSTOMER_FACING and d not in protective_dims:
                continue
            for k in K.cohort_keys(target):
                if k != "default" and k not in cohort_ids:
                    continue
                tv_ = (target["allocations"].get(k) or {}).get(d)
                if tv_ is not None:
                    out["allocations"].setdefault(k, {})[d] = tv_
                elif k in out["allocations"]:
                    out["allocations"][k].pop(d, None)
        if "schedule" in allowed and not self._owned(wf, "schedule", now):
            hw = target.get("schedule", {}).get("hour_weights", {})
            if all(k == "default" or k in cohort_ids for k in hw):
                out["schedule"] = copy.deepcopy(target["schedule"])
        if not K.changed_dims(cfg, out):
            return None
        return K.step_towards(cfg, out, aa.get("max_shift", 0))

    def _once(self, now, wf, kind, summary):
        r = self.s.one("SELECT minute FROM decisions WHERE wf=? AND kind=? ORDER BY id DESC LIMIT 1", (wf, kind))
        if not r or now - r[0] >= 6 * 60:
            self.s.decide(now, wf, kind, summary, {})

    # ------------------------------------------------------------------ send
    def submit(self, now, wf, channel, cur, new_cfg, parts, describe, urgent):
        n = self.s.one("SELECT COUNT(*) FROM proposals")[0] + 1
        pid = f"{wf}-{channel[:4]}-{now:06d}-{n:04d}"
        meta = describe(cur["config"], new_cfg, parts)
        if channel == "approval":
            body = {"proposal_id": pid, "workflow_id": wf, "base_version": cur["version"],
                    "proposed_config": new_cfg, **meta,
                    "expires_in_hours": self.pol(wf)["proposal_ttl_hours"]}
        else:
            body = {"proposal_id": pid, "base_version": cur["version"], "proposed_config": new_cfg,
                    "rationale": meta["rationale"], "evidence": meta.get("evidence")}
        self.s.x("INSERT INTO proposals(proposal_id,wf,channel,urgent,base_version,config,body,parts,status,"
                 "created_minute,updated_minute) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, wf, channel, int(urgent), cur["version"], json.dumps(new_cfg), json.dumps(body),
                  json.dumps(parts, default=str), "sending", now, now))
        self.s.decide(now, wf, "propose" if channel == "approval" else "auto_apply",
                      ("[urgent] " if urgent else "") + meta["change_summary"],
                      {k: meta[k] for k in ("diff", "affected_leads", "dry_run", "impact_score", "rationale")}, pid)
        self._send(now, pid, wf, channel, body)
        return pid

    def _send(self, now, pid, wf, channel, body):
        try:
            if channel == "approval":
                a = self.sim.post("/approvals", body)
                self._set(pid, "pending", now, approval_id=a["approval_id"], sent_minute=now)
            else:
                r = self.sim.put(f"/workflows/{wf}/config", body)
                self._set(pid, "applied", now, applied_version=r["version"], sent_minute=now)
        except ApiError as e:
            if e.code == "duplicate_proposal_id":
                return  # it went through earlier; reconcile picks it up next loop
            status = {"stale_base_version": "abandoned", "auto_apply_not_permitted": "refused"}.get(e.code, "invalid")
            self._set(pid, status, now, reasons=[str(e)], sent_minute=now if status == "refused" else None)
            self.s.decide(now, wf, f"send_{status}", f"{pid}: {e}", {"error": str(e)}, pid)

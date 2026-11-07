"""Controller unit tests: the pieces where a bug would be silent. Run: python -m pytest -q tests"""
import json
import random

import pytest

from controller import config_ops as K
from controller.api import ApiError
from controller.db import Store
from controller.governance import Governance
from controller.stats import holm, poisson_sf, stratified


# ---------------------------------------------------------------- stats
def test_stratified_matches_simple_diff_with_one_stratum():
    c = stratified([(1000, 600, 1000, 500)])
    assert c.diff == pytest.approx(0.1)
    assert c.z > 4 and c.rel == pytest.approx(0.2)


def test_stratified_removes_simpsons_paradox():
    # A is better inside each day, but ran mostly on the bad day → pooled rates would say A is worse
    strata = [(900, 270, 100, 20), (100, 80, 900, 630)]   # day1 A 30% vs B 20%; day2 A 80% vs B 70%
    pooled_a, pooled_b = 350 / 1000, 650 / 1000
    assert pooled_a < pooled_b
    assert stratified(strata).diff > 0.05


def test_strata_where_only_one_arm_ran_are_ignored():
    # "Only what actually happened counts": an arm with no traffic in a stratum adds nothing
    assert stratified([(0, 0, 500, 250)]) is None


def test_holm_is_monotone_and_bounded():
    adj = holm([0.01, 0.04, 0.03, 0.2])
    assert adj[0] == pytest.approx(0.04)
    assert all(0 <= p <= 1 for p in adj)
    assert adj[1] >= adj[2] >= adj[0]


def test_null_false_positive_rate_of_stratified_z():
    rng = random.Random(0)
    hits = 0
    for _ in range(400):
        rows = [(300, sum(rng.random() < .5 for _ in range(300)), 300, sum(rng.random() < .5 for _ in range(300)))
                for _ in range(3)]
        hits += stratified(rows).p_greater < 0.05
    assert 0.02 < hits / 400 < 0.09


def test_poisson_tail():
    assert poisson_sf(0, 3) == 1.0
    assert poisson_sf(10, 2) < 1e-4


# ---------------------------------------------------------------- config ops
def _cfg(voice, did=None):
    return {"cohorts": [], "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
            "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": voice,
                                        "caller_id": did or {"D1": 0.5, "D2": 0.5}}}}


def test_tv_and_changed_dims():
    a, b = _cfg({"V1": 1.0}), _cfg({"V1": 0.7, "V2": 0.3})
    assert K.max_shift(a, b) == pytest.approx(0.3)
    assert K.changed_dims(a, b) == ["voice"]
    assert K.changed_dims(a, a) == []


def test_step_towards_respects_max_shift():
    a = _cfg({"V1": 1.0}, {"D1": 0.5, "D2": 0.5})
    b = _cfg({"V1": 1.0}, {"D1": 0.0001, "D3": 1.0})
    stepped = K.step_towards(a, b, 0.2)
    assert K.max_shift(a, stepped) <= 0.2 + 1e-9
    assert K.max_shift(stepped, b) < K.max_shift(a, b)


def test_auto_permitted_mirrors_policy():
    pol = {"auto_apply": {"enabled": True, "allowed_dims": ["caller_id"], "max_shift": 0.5}}
    a = _cfg({"V1": 1.0})
    ok, _ = K.auto_permitted(pol, a, _cfg({"V1": 1.0}, {"D1": 1.0}))
    assert ok
    ok, why = K.auto_permitted(pol, a, _cfg({"V2": 1.0}))
    assert not ok and "not allowed" in why[0]


def test_schedule_float_noise_is_not_a_change():
    a, b = _cfg({"V1": 1.0}), _cfg({"V1": 1.0})
    a["schedule"]["hour_weights"] = {"x": {"9": 1.0}}
    b["schedule"]["hour_weights"] = {"x": {9: 1.00000001}}
    assert K.changed_dims(a, b) == []


# ---------------------------------------------------------------- governance crash safety
class FakeSim:
    """Just enough of the API to test the outbox. `fail_after_post` simulates a crash after the request
    reached the server but before we recorded the response."""

    def __init__(self):
        self.version = 1
        self.approvals = {}
        self.posts = 0
        self.history = [{"version": 1, "proposal_id": None}]
        self.fail_after_post = False

    def get(self, path, **kw):
        if path.startswith("/approvals/"):
            return self.approvals[path.split("/")[-1]]
        if path == "/approvals":
            return list(self.approvals.values())
        if path.endswith("/config/history"):
            return self.history
        if path.endswith("/config"):
            return {"version": self.version, "config": _cfg({"V1": 1.0})}
        raise KeyError(path)

    def post(self, path, body):
        if path == "/approvals":
            if any(a["proposal_id"] == body["proposal_id"] for a in self.approvals.values()):
                raise ApiError(409, "duplicate_proposal_id")
            self.posts += 1
            aid = f"apr_{len(self.approvals) + 1:05d}"
            self.approvals[aid] = {"approval_id": aid, "proposal_id": body["proposal_id"], "status": "pending"}
            if self.fail_after_post:
                raise RuntimeError("connection reset after send")
            return self.approvals[aid]
        if path.endswith("/apply"):
            a = self.approvals[body["approval_id"]]
            if a["status"] != "approved":
                raise ApiError(409, "not_approved", {"status": a["status"]})
            a["status"] = "applied"
            self.version += 1
            self.history.append({"version": self.version, "proposal_id": a["proposal_id"]})
            return {"version": self.version, "changed_dims": ["voice"]}
        raise KeyError(path)


def _gov(tmp_path, sim):
    s = Store(tmp_path / "c.db")
    spec = {"tenant_id": "t", "governance": {"max_requests_per_24h": 3, "approver_hours": [10, 19],
                                             "proposal_ttl_hours": 24, "auto_apply": {"enabled": False}}}
    return s, Governance(s, sim, {"wf": spec}, {})


def _describe(old, new, parts):
    return {"change_summary": "s", "diff": {"x": 1}, "affected_leads": 1, "dry_run": {"x": 1},
            "impact_score": 0.1, "rationale": "r", "evidence": None}


def test_crash_after_post_does_not_double_propose_or_lose_approval(tmp_path):
    sim = FakeSim()
    s, g = _gov(tmp_path, sim)
    cur = sim.get("/workflows/wf/config")
    sim.fail_after_post = True
    with pytest.raises(RuntimeError):
        g.submit(600, "wf", "approval", cur, _cfg({"V2": 1.0}), [{"protective": False, "dims": ["voice"]}],
                 _describe, urgent=False)
    assert s.one("SELECT status FROM proposals")[0] == "sending"
    # "restart": a new Governance over the same store reconciles instead of resending
    sim.fail_after_post = False
    s2, g2 = s, Governance(s, sim, g.specs, {})
    g2.reconcile(615)
    assert sim.posts == 1
    row = s.one("SELECT status, approval_id FROM proposals")
    assert row == ("pending", "apr_00001")
    # approver says yes → we apply exactly once, even if reconcile runs twice
    sim.approvals["apr_00001"]["status"] = "approved"
    g2.reconcile(630)
    g2.reconcile(645)
    assert sim.version == 2 and s.one("SELECT status FROM proposals")[0] == "applied"


def test_fatigue_budget_and_reserve(tmp_path):
    sim = FakeSim()
    s, g = _gov(tmp_path, sim)
    cur = sim.get("/workflows/wf/config")
    parts = [{"protective": False, "dims": ["voice"]}]
    # 10:00, a planned change goes out; a second planned one waits (one open at a time)
    assert g.act("wf", 600, cur, _cfg({"V2": 1.0}), parts, _describe)
    assert g.act("wf", 660, cur, _cfg({"V3": 1.0}), parts, _describe) is None
    # nothing planned is sent at night
    sim.approvals["apr_00001"]["status"] = "rejected"
    g.reconcile(1300)
    assert g.act("wf", 1320, cur, _cfg({"V3": 1.0}), parts, _describe) is None
    # a protective change goes out at night, using the reserve
    assert g.act("wf", 1320, cur, _cfg({"V3": 1.0}), [{"protective": True, "dims": ["voice"]}], _describe)
    assert json.loads(s.one("SELECT body FROM proposals ORDER BY created_minute DESC")[0])["proposal_id"]

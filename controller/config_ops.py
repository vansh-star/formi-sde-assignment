"""Pure helpers over workflow configs. These mirror the published contract (docs/SIMULATOR.md §3–4) so we
can predict whether an auto-apply is permitted before trying it."""
from __future__ import annotations

import copy
import json

AGENT_DIMS = ["welcome_message", "voice", "caller_id"]
CHANGE_DIMS = ["cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule"]
CUSTOMER_FACING = {"welcome_message", "voice", "prompt_version"}


def dist(cfg: dict, cohort: str, dim: str) -> dict:
    return (cfg["allocations"].get(cohort) or {}).get(dim) or cfg["allocations"]["default"][dim]


def normalise(weights: dict) -> dict:
    s = sum(v for v in weights.values() if v > 0)
    return {k: round(v / s, 6) for k, v in sorted(weights.items()) if v > 0}


def tv(a: dict, b: dict) -> float:
    a, b = normalise(a), normalise(b)
    return 0.5 * sum(abs(a.get(o, 0) - b.get(o, 0)) for o in set(a) | set(b))


def cohort_keys(*cfgs) -> set:
    keys = {"default"}
    for c in cfgs:
        keys |= {x["id"] for x in c.get("cohorts", [])}
    return keys


def changed_dims(old: dict, new: dict) -> list[str]:
    ch = set()
    if old.get("cohorts", []) != new.get("cohorts", []):
        ch.add("cohorts")
    for k in cohort_keys(old, new):
        for d in AGENT_DIMS:
            if tv(dist(old, k, d), dist(new, k, d)) > 1e-6:
                ch.add(d)
    if old.get("prompt_version") != new.get("prompt_version"):
        ch.add("prompt_version")
    if canon(norm_schedule(old.get("schedule"))) != canon(norm_schedule(new.get("schedule"))):
        ch.add("schedule")
    return [d for d in CHANGE_DIMS if d in ch]


def max_shift(old: dict, new: dict) -> float:
    worst = 0.0
    for k in cohort_keys(old, new):
        for d in AGENT_DIMS:
            worst = max(worst, tv(dist(old, k, d), dist(new, k, d)))
    return worst


def norm_schedule(s) -> dict:
    s = s or {}
    hw = {k: {str(int(h)): round(float(w), 4) for h, w in (t or {}).items()} for k, t in (s.get("hour_weights") or {}).items()}
    return {"hour_weights": hw, "lead_preferred_hour": dict(s.get("lead_preferred_hour") or {})}


def canon(x) -> str:
    return json.dumps(x, sort_keys=True)


def auto_permitted(policy: dict, old: dict, new: dict) -> tuple[bool, list[str]]:
    aa = policy["auto_apply"]
    dims = changed_dims(old, new)
    problems = []
    if not aa.get("enabled"):
        problems.append("auto-apply disabled")
    bad = [d for d in dims if d not in aa.get("allowed_dims", [])]
    if bad:
        problems.append(f"dims not allowed: {bad}")
    if max_shift(old, new) > aa.get("max_shift", 0) + 1e-9:
        problems.append("shift too large")
    if not dims:
        problems.append("no change")
    return not problems, problems


def step_towards(old: dict, new: dict, max_tv: float) -> dict:
    """Move every agent-dim distribution from old toward new by at most max_tv (total variation).
    Used to respect auto-apply max_shift with one step per loop rather than one big jump."""
    out = copy.deepcopy(new)
    shift = max_shift(old, new)
    if shift <= max_tv + 1e-9:
        return out
    f = max_tv / shift * 0.999
    for k in cohort_keys(old, new):
        if k != "default" and k not in new["allocations"]:
            continue
        for d in AGENT_DIMS:
            a, b = normalise(dist(old, k, d)), normalise(dist(new, k, d))
            mixed = {o: a.get(o, 0) * (1 - f) + b.get(o, 0) * f for o in set(a) | set(b)}
            out["allocations"].setdefault(k, {})[d] = normalise(mixed)
    return out


def diff(old: dict, new: dict) -> dict:
    """Dotted-path diff a supervisor can read."""
    out = {}
    if old.get("cohorts", []) != new.get("cohorts", []):
        out["cohorts"] = {"from": [c["id"] for c in old.get("cohorts", [])],
                          "to": [c["id"] for c in new.get("cohorts", [])]}
    for k in sorted(cohort_keys(old, new)):
        for d in AGENT_DIMS:
            a, b = dist(old, k, d), dist(new, k, d)
            if tv(a, b) > 1e-6:
                out[f"allocations.{k}.{d}"] = {"from": normalise(a), "to": normalise(b)}
    if old.get("prompt_version") != new.get("prompt_version"):
        out["prompt_version"] = {"from": old.get("prompt_version"), "to": new.get("prompt_version")}
    os_, ns = old.get("schedule") or {}, new.get("schedule") or {}
    for key in sorted(set(os_.get("hour_weights", {})) | set(ns.get("hour_weights", {}))):
        a, b = os_.get("hour_weights", {}).get(key), ns.get("hour_weights", {}).get(key)
        if canon(a) != canon(b):
            out[f"schedule.hour_weights.{key}"] = {"from": a, "to": b}
    return out

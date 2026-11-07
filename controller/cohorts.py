"""The shared cohort object.

One partition per workflow, built once from pre-D0 history and then frozen, so detection, experiments, the
schedule and the supervisor view all mean the same thing by the same id.

Partition = lang × (one more feature). The second feature is the one whose values differ most in their
hour-of-day pickup profile in history (a chi-square on the hour × value table), because call-time is the
lever history can inform. Cells holding < MIN_SHARE of leads merge into a per-language remainder, and a
remainder that is still too small falls into `default`. Cohorts are disjoint, so config order doesn't matter.
"""
from __future__ import annotations

from collections import defaultdict

FEATURES = ["lang", "city_tier", "age_band", "value_band"]
SECOND = ["age_band", "city_tier", "value_band"]
SHORT = {"age_band": "age", "city_tier": "tier", "value_band": "val"}
MIN_SHARE = 0.06   # ~66 leads/day on a 1,100-lead batch; smaller cohorts can't support their own decisions
MAX_COHORTS = 16   # config allows 20; leave headroom


def cell_key(f: dict) -> str:
    return "|".join(str(f.get(k, "")) for k in FEATURES)


def cell_feats(cell: str) -> dict:
    return dict(zip(FEATURES, cell.split("|")))


def _slug(v: str) -> str:
    return v.replace("+", "plus").replace("-", "_")


def choose_second_feature(hour_rows) -> tuple[str, dict]:
    """hour_rows: iterable of (cell, hour, dials, conn). Returns the feature with the strongest
    hour × value interaction in pickup (chi-square per degree of freedom)."""
    scores = {}
    for feat in SECOND:
        idx = FEATURES.index(feat)
        tab = defaultdict(lambda: [0, 0])  # (value, hour) -> [dials, conn]
        for cell, hour, dials, conn in hour_rows:
            t = tab[(cell.split("|")[idx], hour)]
            t[0] += dials
            t[1] += conn
        vals = {v for v, _ in tab}
        hours = {h for _, h in tab}
        pv = {v: sum(tab[(v, h)][1] for h in hours) / max(1, sum(tab[(v, h)][0] for h in hours)) for v in vals}
        tot_d = sum(t[0] for t in tab.values())
        tot_c = sum(t[1] for t in tab.values())
        p = tot_c / max(1, tot_d)
        ph = {h: sum(tab[(v, h)][1] for v in vals) / max(1, sum(tab[(v, h)][0] for v in vals)) for h in hours}
        chi = 0.0
        for (v, h), (d, c) in tab.items():
            exp = d * pv[v] * ph[h] / p if p > 0 else 0
            if exp > 5:
                chi += (c - exp) ** 2 / exp
        df = max(1, (len(vals) - 1) * (len(hours) - 1))
        scores[feat] = round(chi / df, 3)
    best = max(scores, key=scores.get)
    return best, scores


def build_partition(lead_cells: dict[str, int], second: str) -> list[dict]:
    """lead_cells: cell -> lead count. Returns [{id, where, cells, share}] (disjoint)."""
    total = sum(lead_cells.values()) or 1
    idx = FEATURES.index(second)
    by_lang = defaultdict(lambda: defaultdict(list))
    for cell in lead_cells:
        parts = cell.split("|")
        by_lang[parts[0]][parts[idx]].append(cell)
    out = []
    for lang in sorted(by_lang):
        rest_vals, rest_cells = [], []
        for val in sorted(by_lang[lang]):
            cells = by_lang[lang][val]
            share = sum(lead_cells[c] for c in cells) / total
            if share >= MIN_SHARE:
                out.append({"id": f"{lang}_{SHORT[second]}{_slug(val)}", "where": {"lang": [lang], second: [val]},
                            "cells": sorted(cells), "share": round(share, 4)})
            else:
                rest_vals.append(val)
                rest_cells += cells
        if rest_cells:
            share = sum(lead_cells[c] for c in rest_cells) / total
            if share >= MIN_SHARE:
                out.append({"id": f"{lang}_rest", "where": {"lang": [lang], second: sorted(rest_vals)},
                            "cells": sorted(rest_cells), "share": round(share, 4)})
    out.sort(key=lambda c: -c["share"])
    return out[:MAX_COHORTS]


def cohort_of_cell(partition: list[dict]) -> dict[str, str]:
    m = {}
    for c in partition:
        for cell in c["cells"]:
            m[cell] = c["id"]
    return m


def assign(partition: list[dict], features: dict) -> str:
    """Cohort for a lead never seen in history (new feature combination)."""
    for c in partition:
        if all(str(features.get(k)) in v for k, v in c["where"].items()):
            return c["id"]
    return "default"


def config_cohorts(partition: list[dict]) -> list[dict]:
    return [{"id": c["id"], "where": c["where"]} for c in partition]

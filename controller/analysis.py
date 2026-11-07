"""Read-side queries over `agg`. Every learning decision is built from these, so they only see what was
actually served (calls record the variant they ran with): an arm that never ran has no rows."""
from __future__ import annotations

from collections import defaultdict

from .stats import stratified

COL = {"welcome_message": "w", "voice": "v", "caller_id": "d", "prompt_version": "p"}
KPI_COLS = {"engagement_rate": ("conn", "eng"), "pickup_rate": ("dials", "conn")}


def other_kpi(kpi: str) -> str:
    return "pickup_rate" if kpi == "engagement_rate" else "engagement_rate"


def arm_strata(s, wf, dim, kpi, since_minute, cohort=None):
    """{stratum: {option: [n, x]}} with strata = (day, hour, cohort, prompt, other agent-facing dim)."""
    col = COL[dim]
    others = [c for c in ("w", "v") if c != col] if dim in ("welcome_message", "voice") else []
    n_col, x_col = KPI_COLS[kpi]
    sel = ", ".join(["day", "hour", "cohort", "p"] + others)
    sql = (f"SELECT {sel}, {col}, SUM({n_col}), SUM({x_col}) FROM agg WHERE wf=? AND day >= ? AND day*1440+hour*60 >= ?"
           + (" AND cohort=?" if cohort else "") + f" GROUP BY {sel}, {col}")
    args = (wf, since_minute // 1440, since_minute) + ((cohort,) if cohort else ())
    out = defaultdict(dict)
    k = 4 + len(others)
    for row in s.q(sql, args):
        out[row[:k]][row[k]] = [row[k + 1], row[k + 2]]
    return out


def compare(strata, a, b_set):
    """a vs pooled set b_set (one option or several)."""
    if isinstance(b_set, str):
        b_set = {b_set}
    rows = []
    for arms in strata.values():
        if a not in arms:
            continue
        nb = sum(arms[o][0] for o in b_set if o in arms)
        xb = sum(arms[o][1] for o in b_set if o in arms)
        rows.append((arms[a][0], arms[a][1], nb, xb))
    return stratified(rows)


def arm_totals(strata):
    tot = defaultdict(lambda: [0, 0])
    for arms in strata.values():
        for o, (n, x) in arms.items():
            tot[o][0] += n
            tot[o][1] += x
    return dict(tot)


def window(s, wf, since_minute, until_minute=None, where="", args=()):
    sql = ("SELECT SUM(dials), SUM(conn), SUM(eng), SUM(halluc) FROM agg WHERE wf=? AND day >= ? AND day*1440+hour*60 >= ?"
           + (" AND day*1440+hour*60 < ?" if until_minute is not None else "") + where)
    a = (wf, since_minute // 1440, since_minute) + ((until_minute,) if until_minute is not None else ()) + tuple(args)
    r = s.one(sql, a)
    return [x or 0 for x in r]


def by_prompt(s, wf, since_minute):
    return {p: (n or 0, h or 0, e or 0) for p, n, h, e in s.q(
        "SELECT p, SUM(conn), SUM(halluc), SUM(eng) FROM agg WHERE wf=? AND day >= ? AND day*1440+hour*60 >= ? GROUP BY p",
        (wf, since_minute // 1440, since_minute))}


def per_day_rates(s, wf, since_day):
    return s.q("SELECT day, SUM(dials), SUM(conn), SUM(eng) FROM agg WHERE wf=? AND day>=? GROUP BY day ORDER BY day",
               (wf, since_day))

"""Audit queries (C12), answered from the controller's own store only.

  python -m controller.why --db state/controller.db change nw_renewal "D3 14:00"
      → which config was live, who changed it and when, why (decision + evidence), who approved it,
        and what the controller saw/decided afterwards
  python -m controller.why --db state/controller.db person nw_renewal:p0004567 [--day 3]
      → every call to that person: variant heard, config version, and the change that put it live
  python -m controller.why --db state/controller.db log nw_renewal [--kind promote]
"""
from __future__ import annotations

import argparse
import json
import sqlite3

DAY = 1440


def parse_t(s: str) -> int:
    d, hm = s.strip().upper().lstrip("D").split()
    h, m = hm.split(":")
    return int(d) * DAY + int(h) * 60 + int(m)


def ts(m):
    if m is None:
        return "—"
    d, r = divmod(m, DAY)
    return f"D{d} {r // 60:02d}:{r % 60:02d}"


def version_at(c, wf, minute):
    r = c.execute("SELECT version, actor, minute, changed_dims, proposal_id, approval_id FROM config_versions "
                  "WHERE wf=? AND (minute IS NULL OR minute <= ?) ORDER BY version DESC LIMIT 1", (wf, minute)).fetchone()
    return r


def explain_version(c, wf, row, out):
    v, actor, m, dims, pid, aid = row
    out.append(f"v{v} live since {ts(m)}, set by {actor}, dims {json.loads(dims or '[]')}")
    if pid:
        p = c.execute("SELECT channel, body, approval_id, sent_minute, status, reasons FROM proposals WHERE proposal_id=?",
                      (pid,)).fetchone()
        dec = c.execute("SELECT minute, kind, summary, detail FROM decisions WHERE proposal_id=? ORDER BY id", (pid,)).fetchall()
        if p:
            b = json.loads(p[1])
            out.append(f"  proposal {pid} via {p[0]}, sent {ts(p[3])}: {b.get('change_summary') or b.get('rationale')}")
            if p[0] == "approval":
                out.append(f"  approved by the client approver: {p[2]} (status {p[4]})")
                out.append(f"  impact_score={b.get('impact_score')} affected_leads={b.get('affected_leads')}")
            else:
                out.append("  auto-applied under the workflow's auto-apply policy (no human approval needed)")
        for m2, kind, summ, _ in dec:
            out.append(f"  {ts(m2)} [{kind}] {summ}")
        # the learning decisions that led to it: same wf, shortly before the proposal
        cause = c.execute("SELECT minute, kind, summary FROM decisions WHERE wf=? AND minute <= ? AND minute >= ? AND kind IN "
                          "('promote','rollback','arm_dropped','did_flagged','did_probe','halluc_fix_chosen','halluc_detected',"
                          "'experiment_start','calltime_plan','calltime_revert','cohorts_defined') ORDER BY id DESC LIMIT 6",
                          (wf, p[3] if p and p[3] is not None else m or 0, (p[3] or m or 0) - 2 * DAY)).fetchall()
        if cause:
            out.append("  evidence trail:")
            for m2, kind, summ in reversed(cause):
                out.append(f"    {ts(m2)} [{kind}] {summ}")
    elif actor == "supervisor":
        out.append("  a client supervisor edited this directly (bypasses governance); we recorded it and treat those dims as theirs for 24h")


def cmd_change(c, wf, t):
    minute = parse_t(t)
    out = [f"# {wf} at {ts(minute)}"]
    row = version_at(c, wf, minute)
    if not row:
        return "no config known"
    explain_version(c, wf, row, out)
    nxt = c.execute("SELECT minute, kind, summary FROM decisions WHERE wf=? AND minute > ? AND minute <= ? ORDER BY id",
                    (wf, row[2] or 0, (row[2] or 0) + 2 * DAY)).fetchall()
    out.append("what happened next (48h):")
    for m2, kind, summ in nxt[:25]:
        out.append(f"  {ts(m2)} [{kind}] {summ}")
    return "\n".join(out)


def cmd_person(c, person, day):
    q = "SELECT minute, wf, lead_id, cfgver, cohort, w, v, d, p, connected, engaged FROM calls WHERE person_id=?"
    args = [person]
    if day is not None:
        q += " AND minute >= ? AND minute < ?"
        args += [day * DAY, (day + 1) * DAY]
    out = [f"# calls to {person}"]
    seen = set()
    for m, wf, lid, ver, coh, w, v, d, p, conn, eng in c.execute(q + " ORDER BY minute", args):
        out.append(f"{ts(m)} {lid} cohort={coh} heard welcome={w} voice={v} script={p} from {d} "
                   f"→ {'connected' if conn else 'no answer'}{', engaged' if eng else ''} (config v{ver})")
        if (wf, ver) not in seen:
            seen.add((wf, ver))
            r = c.execute("SELECT version, actor, minute, changed_dims, proposal_id, approval_id FROM config_versions "
                          "WHERE wf=? AND version=?", (wf, ver)).fetchone()
            if r:
                sub = []
                explain_version(c, wf, r, sub)
                out += ["    " + x for x in sub]
    return "\n".join(out)


def cmd_log(c, wf, kind):
    q = "SELECT minute, kind, summary, proposal_id FROM decisions WHERE wf=?"
    args = [wf]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    return "\n".join(f"{ts(m)} [{k}] {s}" + (f"  ({p})" if p else "") for m, k, s, p in c.execute(q + " ORDER BY id", args))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/controller.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("change"); a1.add_argument("wf"); a1.add_argument("at")
    a2 = sub.add_parser("person"); a2.add_argument("person"); a2.add_argument("--day", type=int)
    a3 = sub.add_parser("log"); a3.add_argument("wf"); a3.add_argument("--kind")
    a = ap.parse_args(argv)
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    if a.cmd == "change":
        print(cmd_change(c, a.wf, a.at))
    elif a.cmd == "person":
        print(cmd_person(c, a.person, a.day))
    else:
        print(cmd_log(c, a.wf, a.kind))


if __name__ == "__main__":
    main()

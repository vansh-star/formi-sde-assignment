"""Supervisor view (C11) and loop health (C14): one page per run, regenerated every 6 simulated hours.

Per workflow: KPIs today vs history, what's being tested and what's winning, what we want from the
supervisor (open requests), what changed today and why, open incidents. Then the loop's own health.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import analysis as A

DAY = 1440


def ts(minute):
    if minute is None:
        return "—"
    d, m = divmod(minute, DAY)
    return f"D{d} {m // 60:02d}:{m % 60:02d}"


def write_status(s, ctl, now, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    L = [f"# Supervisor view — {s.meta('scenario')} — {ts(now)}", ""]
    for wf, spec in ctl.specs.items():
        L += _workflow(s, ctl, wf, spec, now)
    L += _health(s, ctl, now)
    (out / "status.md").write_text("\n".join(L))


def _rate(a, b):
    return f"{a / b:.1%}" if b else "—"


def _workflow(s, ctl, wf, spec, now):
    today = now // DAY
    L = [f"## {wf} ({spec['tenant_id']}) — primary: {spec['objective']['primary']}", ""]
    d0, c0, e0, _ = A.window(s, wf, -14 * DAY, 0)
    d1, c1, e1, _ = A.window(s, wf, today * DAY, None)
    dw, cw, ew, _ = A.window(s, wf, max(0, now - 7 * DAY), None)
    L += ["| | pickup | engagement |", "|---|---|---|",
          f"| history (14d, default config) | {_rate(c0, d0)} | {_rate(e0, c0)} |",
          f"| last 7 days | {_rate(cw, dw)} | {_rate(ew, cw)} |",
          f"| today so far | {_rate(c1, d1)} | {_rate(e1, c1)} |", ""]
    L.append("**Being tested / winning**")
    for e in ctl.exps[wf]:
        st = e.st() or {}
        if st.get("status") == "running":
            strata = A.arm_strata(s, wf, e.dim, "engagement_rate", st["started"])
            bits = []
            for a in st["active"]:
                c = A.compare(strata, a, st["incumbent"])
                bits.append(f"{a} {c.rel:+.1%} (n={c.n_a})" if c else f"{a} (no data yet)")
            L.append(f"- {e.dim}: testing vs {st['incumbent']}: " + (", ".join(bits) or "—"))
        elif st.get("status") == "promoted":
            dc = st.get("last_decay_check") or {}
            L.append(f"- {e.dim}: **{st['winner']}** promoted at {ts(st['promoted_at'])}; holdout {st['holdout']}; "
                     f"latest check {dc.get('rel', 0):+.1%} vs holdout")
        elif st:
            L.append(f"- {e.dim}: settled on {st['incumbent']}")
    did = s.state(wf, "did") or {}
    L.append("- caller IDs: " + ", ".join(f"{o} {v['status']}" for o, v in did.items()))
    sch = s.state(wf, "schedule") or {}
    L.append(f"- call-time weights: {sch.get('status', '—')}")
    inc = s.state(wf, "incident") or {}
    if inc.get("status") not in (None, "none", "resolved"):
        L.append(f"- **incident**: hallucination on {inc['bad']} — {inc['status']}")
    L += ["", "**Waiting on you**"]
    rows = s.q("SELECT proposal_id, approval_id, status, body, sent_minute FROM proposals WHERE wf=? AND channel='approval' "
               "AND status IN ('pending','approved') ORDER BY sent_minute", (wf,))
    for pid, aid, st_, body, sent in rows:
        b = json.loads(body)
        L.append(f"- {aid} ({st_}, sent {ts(sent)}, impact {b.get('impact_score')}): {b.get('change_summary')}")
    if not rows:
        L.append("- nothing")
    L += ["", "**Changed today**"]
    ch = s.q("SELECT version, actor, minute, changed_dims, proposal_id FROM config_versions WHERE wf=? AND minute >= ? "
             "ORDER BY version", (wf, today * DAY))
    for v, actor, m, dims, pid in ch:
        why = s.one("SELECT summary FROM decisions WHERE proposal_id=? AND kind IN ('propose','auto_apply')", (pid,)) if pid else None
        L.append(f"- {ts(m)} v{v} by {actor}: {', '.join(json.loads(dims or '[]'))}" + (f" — {why[0]}" if why else ""))
    if not ch:
        L.append("- nothing")
    L.append("")
    return L


def _health(s, ctl, now):
    rows = s.q("SELECT minute, wall_ms, events, ingest_ms, decide_ms, ingest_lag_min, credits, pending, rss_mb "
               "FROM metrics WHERE minute > ? ORDER BY minute", (now - DAY,))
    L = ["## Loop health (last 24 simulated hours)", ""]
    if rows:
        ev = sum(r[2] for r in rows)
        ing = sum(r[3] for r in rows) / 1000
        L += [f"- events ingested: {ev:,} in {ing:.1f}s ingest time ({ev / ing if ing else 0:,.0f} events/s)",
              f"- loop wall time: {sum(r[1] for r in rows) / 1000:.1f}s over {len(rows)} steps; "
              f"max decide {max(r[4] for r in rows):.0f} ms",
              f"- max ingest lag: {max(r[5] or 0 for r in rows)} sim-min · credits left: {rows[-1][6]:.0f} · "
              f"open proposals: {rows[-1][7]} · peak RSS {max(r[8] for r in rows):.0f} MB"]
    alerts = _alerts(s, ctl, now)
    L += ["", "**Alerts**"] + ([f"- {a}" for a in alerts] or ["- none"])
    return L


def _alerts(s, ctl, now):
    """Page-worthy only: something a human must act on now."""
    out = []
    for wf in ctl.specs:
        inc = s.state(wf, "incident") or {}
        if inc.get("status") == "detected" and inc.get("stuck_logged"):
            out.append(f"PAGE {wf}: hallucination incident with no validated fix — check prompt options / credits")
        blocked = s.one("SELECT minute FROM decisions WHERE wf=? AND kind='fatigue_block' AND minute > ?", (wf, now - 6 * 60))
        if blocked:
            out.append(f"PAGE {wf}: protective change blocked by approval budget — call the client approver")
        bad = s.one("SELECT COUNT(*) FROM proposals WHERE wf=? AND status='invalid' AND updated_minute > ?", (wf, now - DAY))[0]
        if bad:
            out.append(f"TICKET {wf}: {bad} proposals rejected by the API as invalid in 24h — controller bug")
    if ctl.credits.remaining() < 30:
        out.append(f"PAGE credits low: {ctl.credits.remaining():.0f} left")
    return out

"""Crash-recovery test (C13): SIGKILL the controller at random moments, restart it, let it finish, then
check invariants against the simulator's own records (not ours).

    python scripts/crash_test.py --scenario scenarios/public_02_incidents.yaml --kills 6 --port 8710

Invariants checked:
  1. the run finishes (scenario end reached)
  2. no event lost or double-counted: dials in our aggregates == call events in the simulator's chunks
  3. no double-apply: every proposal_id appears at most once in the simulator's config history
  4. no lost approval: every approval the simulator approved was applied by us (or lapsed only if the
     scenario ended inside its apply window)
  5. every controller config version has a matching proposal row in our store (auditable)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="scenarios/public_02_incidents.yaml")
    ap.add_argument("--kills", type=int, default=6)
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="reports/crash_test.md")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    run_dir, db = ROOT / "runs/crash", ROOT / "state/crash/controller.db"
    for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        p.unlink(missing_ok=True)
    url = f"http://127.0.0.1:{a.port}"
    sim = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", a.scenario, "--run-dir", str(run_dir),
                            "--force", "--port", str(a.port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log = []
    try:
        c = httpx.Client(base_url=url, timeout=60)
        for _ in range(120):
            try:
                c.get("/health"); break
            except httpx.TransportError:
                time.sleep(1)
        env = dict(os.environ, CONTROLLER_DB=str(db), CONTROLLER_STATUS=str(db.parent), LOG_LEVEL="WARNING")
        kills = 0
        while True:
            proc = subprocess.Popen(["./run.sh", url], cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                    stderr=open(ROOT / "logs/crash_ctl.log", "a"))
            if kills < a.kills:
                wait = rng.uniform(2.0, 7.0)
                try:
                    proc.wait(timeout=wait)
                except subprocess.TimeoutExpired:
                    st = c.get("/sim/state").json()
                    proc.send_signal(signal.SIGKILL)
                    proc.wait()
                    kills += 1
                    log.append(f"kill #{kills}: SIGKILL after {wait:.1f}s at sim {st['ts']} (minute {st['sim_minute']})")
                    continue
            rc = proc.wait()
            log.append(f"final controller process exited rc={rc}")
            break
        st = c.get("/sim/state").json()
        checks = verify(c, db, st)
    finally:
        sim.terminate()
        sim.wait()
    ok = all(v for _, v, _ in checks)
    out = ["# Crash-recovery test", "", f"Scenario `{a.scenario}`, {a.kills} SIGKILLs at random moments, seed {a.seed}.", "",
           "```", *log, "```", "", "| invariant | result | detail |", "|---|---|---|"]
    out += [f"| {name} | {'PASS' if v else 'FAIL'} | {d} |" for name, v, d in checks]
    out += ["", f"**Overall: {'PASS' if ok else 'FAIL'}**", "",
            f"Graded: `python -m sim grade --run runs/crash --baseline runs/base_public_02 --out reports/crash_grade.md`"]
    Path(ROOT / a.out).write_text("\n".join(out))
    print("\n".join(out))
    sys.exit(0 if ok else 1)


def verify(c, db, st):
    s = sqlite3.connect(db)
    checks = [("scenario finished", st["finished"], st["ts"])]
    # 2. events: count call events in the simulator's chunks vs our aggregates
    n_calls = 0
    for ch in c.get("/events/chunks", params={"kind": "events"}).json():
        n_calls += sum(1 for line in c.get(f"/events/chunks/{ch['chunk']}").text.splitlines() if '"type":"call"' in line)
    ours = s.execute("SELECT COALESCE(SUM(dials),0) FROM agg WHERE day >= 0").fetchone()[0]
    rows = s.execute("SELECT COUNT(*) FROM calls WHERE minute >= 0").fetchone()[0]
    checks.append(("no event lost or double-counted", n_calls == ours == rows,
                   f"sim call events {n_calls:,} · our aggregates {ours:,} · our call rows {rows:,}"))
    # 3/4/5 per workflow
    dup, lost, unaudited = [], [], []
    applied_total = 0
    for w in c.get("/workflows").json():
        wf = w["workflow_id"]
        hist = c.get(f"/workflows/{wf}/config/history").json()
        pids = Counter(v["proposal_id"] for v in hist if v.get("proposal_id"))
        dup += [p for p, k in pids.items() if k > 1]
        for v in hist:
            if v["actor"].startswith("controller"):
                applied_total += 1
                if not s.execute("SELECT 1 FROM proposals WHERE proposal_id=?", (v["proposal_id"],)).fetchone():
                    unaudited.append(v["proposal_id"])
        for ap in c.get("/approvals", params={"workflow_id": wf}).json():
            if ap["status"] == "approved":   # approved but never applied
                lost.append(ap["approval_id"])
            if ap["status"] == "lapsed":
                lost.append(ap["approval_id"] + "(lapsed)")
    checks.append(("no double-apply", not dup, f"{applied_total} controller versions, duplicate proposal ids: {dup or 'none'}"))
    checks.append(("no lost approval", not lost, f"approved-but-unapplied or lapsed: {lost or 'none'}"))
    checks.append(("every applied change auditable", not unaudited, f"missing proposal rows: {unaudited or 'none'}"))
    return checks


if __name__ == "__main__":
    main()

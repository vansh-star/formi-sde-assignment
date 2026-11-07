"""Entry point: python -m controller --url http://127.0.0.1:8700 [--db state/controller.db]"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from .api import Sim
from .db import Store
from .loop import Controller
from .report import write_status

log = logging.getLogger("main")
HERE = Path(__file__).parent


def open_store(sim, db_path: str) -> Store:
    """Reuse the store only if it belongs to this world (same scenario, clock not behind us).
    A restarted *simulator* is a new world, so the old store is archived rather than trusted."""
    st = sim.get("/sim/state")
    if Path(db_path).exists():
        s = Store(db_path)
        same = s.meta("scenario") == st["scenario"] and (s.meta("last_minute", 0) or 0) <= st["sim_minute"] \
            and s.meta("end_minute") == st["end_minute"]
        if same:
            log.info("resuming from %s at sim minute %s", db_path, st["sim_minute"])
            return s
        s.c.close()
        arch = f"{db_path}.{int(time.time())}.old"
        for suf in ("", "-wal", "-shm"):
            if Path(db_path + suf).exists():
                shutil.move(db_path + suf, arch + suf)
        log.warning("store belonged to another world; archived to %s", arch)
    s = Store(db_path)
    s.set_meta("scenario", st["scenario"])
    s.set_meta("end_minute", st["end_minute"])
    return s


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8700")
    ap.add_argument("--db", default=os.environ.get("CONTROLLER_DB", "state/controller.db"))
    ap.add_argument("--step", type=int, default=60, help="simulated minutes per loop")
    ap.add_argument("--status-dir", default=os.environ.get("CONTROLLER_STATUS", "state"))
    ap.add_argument("--stop-at", type=int, default=None, help="exit after this sim minute (testing)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)
    sim = Sim(a.url)
    store = open_store(sim, a.db)
    ctl = Controller(sim, store, HERE / "tenant_prefs.json")
    st = sim.get("/sim/state")
    ctl.bootstrap(st["sim_minute"])
    ctl.ing.load_maps()
    while True:
        t0 = time.perf_counter()
        events = ctl.ing.catch_up()
        t1 = time.perf_counter()
        st = sim.get("/sim/state")
        now = st["sim_minute"]
        ctl.credits.refresh(st)
        store.set_meta("last_minute", now)
        decide_ms = ctl.step(now)
        if now % (6 * 60) == 0 or st["finished"]:
            write_status(store, ctl, now, a.status_dir)
            ctl.ing.prune(now)
        ctl.record_metrics(now, (time.perf_counter() - t0) * 1000, events, (t1 - t0) * 1000, decide_ms)
        if st["finished"] or (a.stop_at is not None and now >= a.stop_at):
            log.info("done at minute %d: %s", now, st)
            break
        sim.advance(a.step, now)


if __name__ == "__main__":
    main()

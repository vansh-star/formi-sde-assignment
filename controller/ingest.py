"""Event ingestion: chunk by chunk, one transaction each, cursor committed with the data.

Idempotent by construction: a chunk's writes and the cursor move together, so a crash re-reads the chunk
from scratch. Calls carry no features, so each call is joined to its lead (lead events arrive at campaign
creation, before any call) through an in-memory map that is rebuilt from the `leads` table on restart.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import date

from . import cohorts as C

log = logging.getLogger("ingest")
D0 = date(2027, 1, 4)
_day_cache: dict[str, int] = {}


def minute_of(ts: str) -> int:
    d = ts[:10]
    day = _day_cache.get(d)
    if day is None:
        day = _day_cache[d] = (date.fromisoformat(d) - D0).days
    return day * 1440 + int(ts[11:13]) * 60 + int(ts[14:16])


class Ingestor:
    def __init__(self, store, sim):
        self.s, self.sim = store, sim
        self.lead_cohort: dict[str, str] = {}
        self.cell_map: dict[str, dict[str, str]] = {}
        self.partitions: dict[str, list] = {}
        self.hist_cells: dict[str, str] = {}   # lead -> cell during history ingest (spans chunks)
        self.events_total = 0

    # ------------------------------------------------------------------ startup
    def load_maps(self, keep_days: int = 5):
        """Rebuild the lead→cohort join after a restart (recent campaigns only)."""
        for wf, pj in self.s.q("SELECT wf, value FROM state WHERE key='partition'"):
            self.partitions[wf] = json.loads(pj)
            self.cell_map[wf] = C.cohort_of_cell(self.partitions[wf])
        last = self.s.meta("last_minute", 0)
        for lid, coh in self.s.q("SELECT lead_id, cohort FROM leads WHERE day >= ?", (last // 1440 - keep_days,)):
            self.lead_cohort[lid] = coh

    def cohort_for(self, wf: str, cell: str) -> str:
        m = self.cell_map.get(wf)
        if m is None:
            return "default"
        c = m.get(cell)
        if c is None:
            c = C.assign(self.partitions[wf], C.cell_feats(cell))
            m[cell] = c
        return c

    # ------------------------------------------------------------------ main entry
    def catch_up(self) -> int:
        """Ingest every chunk after the cursor. Returns events ingested."""
        cursor = self.s.meta("cursor", -1)
        n = 0
        for ch in self.sim.get("/events/chunks", kind="events", after=cursor):
            n += self.ingest_chunk(ch["chunk"], history=False, until=ch.get("until_minute"))
        return n

    def ingest_history(self) -> int:
        cursor = self.s.meta("hist_cursor", -1)
        if cursor >= 0 and not self.hist_cells:   # resumed mid-history after a crash
            self.hist_cells = dict(self.s.q("SELECT lead_id, cell FROM leads WHERE day < 0"))
        n = 0
        for ch in self.sim.get("/events/chunks", kind="history", after=cursor):
            n += self.ingest_chunk(ch["chunk"], history=True)
        return n

    def ingest_chunk(self, n: int, history: bool, until: int | None = None) -> int:
        t0 = time.perf_counter()
        text = self.sim.chunk_text(n)
        agg = defaultdict(lambda: [0, 0, 0, 0])
        leads, calls, cfg_events, appr_events = [], [], [], []
        daily = defaultdict(lambda: [0, 0])
        pending_cells = self.hist_cells
        count = 0
        last_min = None
        for line in text.splitlines():
            if not line:
                continue
            ev = json.loads(line)
            count += 1
            t = ev["type"]
            if t == "call":
                wf = ev["workflow_id"]
                m = minute_of(ev["ts"])
                last_min = m
                day, hour = m // 1440, (m % 1440) // 60
                var = ev["variant"]
                lid = ev["lead_id"]
                conn = 1 if ev["connected"] else 0
                eng = 1 if ev["engaged"] else 0
                hal = 1 if conn and ev.get("qa", {}).get("hallucination_flag") else 0
                key_unit = pending_cells.get(lid) if history else self.lead_cohort.get(lid)
                if key_unit is None:
                    key_unit = "?|?|?|?" if history else "default"
                a = agg[(wf, day, hour, key_unit, var["welcome_message"], var["voice"], var["caller_id"],
                         var["prompt_version"])]
                a[0] += 1; a[1] += conn; a[2] += eng; a[3] += hal
                calls.append((ev["event_id"], wf, m, lid, ev["person_id"], ev["config_version"],
                              ev["cohort_id"], var["welcome_message"], var["voice"], var["caller_id"],
                              var["prompt_version"], conn, eng, hal))
                if eng:
                    daily[(wf, day)][1] += 1
            elif t == "lead":
                wf = ev["workflow_id"]
                cell = C.cell_key(ev["features"])
                m = minute_of(ev["ts"])
                if history:
                    pending_cells[ev["lead_id"]] = cell
                    coh = None
                else:
                    coh = self.cohort_for(wf, cell)
                    self.lead_cohort[ev["lead_id"]] = coh
                leads.append((ev["lead_id"], wf, ev["campaign_id"], ev["person_id"], cell, coh, m // 1440))
                daily[(wf, m // 1440)][0] += 1
            elif t == "config_changed":
                cfg_events.append(ev)
            elif t == "approval_decided":
                appr_events.append(ev)

        s = self.s
        s.begin()
        try:
            s.c.executemany("INSERT OR IGNORE INTO leads VALUES (?,?,?,?,?,?,?)", leads)
            table = "hist" if history else "agg"
            s.c.executemany(
                f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "dials=dials+excluded.dials, conn=conn+excluded.conn, eng=eng+excluded.eng, "
                "halluc=halluc+excluded.halluc", [k + tuple(v) for k, v in agg.items()])
            s.c.executemany("INSERT OR IGNORE INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", calls)
            s.c.executemany("INSERT INTO daily(wf,day,leads,concluded) VALUES (?,?,?,?) ON CONFLICT DO UPDATE SET "
                            "leads=leads+excluded.leads, concluded=concluded+excluded.concluded",
                            [(wf, d, v[0], v[1]) for (wf, d), v in daily.items()])
            for ev in cfg_events:
                self._on_config_changed(ev)
            for ev in appr_events:
                s.x("UPDATE proposals SET reasons=COALESCE(?, reasons) WHERE approval_id=?",
                    (json.dumps(ev.get("reasons") or []), ev["approval_id"]))
            s.set_meta("hist_cursor" if history else "cursor", n)
            if not history and until is not None:
                s.set_meta("ingested_until", until)   # freshness: decisions see everything up to here
            s.commit()
        except BaseException:
            s.rollback()
            raise
        self.events_total += count
        log.info("chunk %d (%s): %d events, %d calls in %.2fs", n, "history" if history else "live",
                 count, len(calls), time.perf_counter() - t0)
        return count

    def _on_config_changed(self, ev):
        m = minute_of(ev["ts"])
        self.s.x("INSERT INTO config_versions(wf,version,actor,minute,changed_dims,proposal_id,approval_id)"
                 " VALUES (?,?,?,?,?,?,?) ON CONFLICT(wf,version) DO UPDATE SET actor=excluded.actor, "
                 "minute=excluded.minute, changed_dims=excluded.changed_dims, proposal_id=excluded.proposal_id, "
                 "approval_id=excluded.approval_id", (ev["workflow_id"], ev["version"], ev["actor"], m,
                                              json.dumps(ev.get("changed_dims") or []), ev.get("proposal_id"),
                                              ev.get("approval_id")))
        if ev["actor"] == "supervisor":
            # record ownership: dims a supervisor touched change only with a human yes from now on
            own = self.s.state(ev["workflow_id"], "supervisor_dims", {})
            for d in ev.get("changed_dims") or []:
                own[d] = m
            self.s.set_state(ev["workflow_id"], "supervisor_dims", own)
            self.s.decide(m, ev["workflow_id"], "supervisor_edit",
                          f"Supervisor changed {', '.join(ev.get('changed_dims') or [])} (v{ev['version']})",
                          {"version": ev["version"], "dims": ev.get("changed_dims")})

    # ------------------------------------------------------------------ housekeeping
    def prune(self, now_minute: int, keep_days: int = 5):
        cutoff = now_minute // 1440 - keep_days
        if not self.lead_cohort:
            return
        old = {r[0] for r in self.s.q("SELECT lead_id FROM leads WHERE day < ? AND day >= ?", (cutoff, cutoff - 2))}
        for lid in old:
            self.lead_cohort.pop(lid, None)

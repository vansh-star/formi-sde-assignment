"""SQLite store. Everything the controller knows or decided lives here, so a restart resumes from disk.

One file, WAL mode. Each ingested chunk is one transaction (aggregates + leads + calls + cursor), so a crash
mid-chunk leaves no partial data and the chunk is simply re-read. Tables map 1:1 to Postgres for production.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS workflows (wf TEXT PRIMARY KEY, tenant TEXT, spec TEXT);

-- the one shared cohort object: detection, experiments, schedule and the status page all use these ids
CREATE TABLE IF NOT EXISTS cohorts (
  wf TEXT, cohort_id TEXT, where_json TEXT, cells_json TEXT, lead_share REAL, PRIMARY KEY (wf, cohort_id));

CREATE TABLE IF NOT EXISTS leads (
  lead_id TEXT PRIMARY KEY, wf TEXT, campaign_id TEXT, person_id TEXT, cell TEXT, cohort TEXT, day INT);
CREATE INDEX IF NOT EXISTS leads_wf_day ON leads (wf, day);

-- cell-level history (pre-D0) used to build cohorts and call-time weights
CREATE TABLE IF NOT EXISTS hist (
  wf TEXT, day INT, hour INT, cell TEXT, w TEXT, v TEXT, d TEXT, p TEXT,
  dials INT, conn INT, eng INT, halluc INT, PRIMARY KEY (wf, day, hour, cell, w, v, d, p)) WITHOUT ROWID;

-- cohort-level aggregates, history + live; every analysis query reads this
CREATE TABLE IF NOT EXISTS agg (
  wf TEXT, day INT, hour INT, cohort TEXT, w TEXT, v TEXT, d TEXT, p TEXT,
  dials INT, conn INT, eng INT, halluc INT, PRIMARY KEY (wf, day, hour, cohort, w, v, d, p)) WITHOUT ROWID;

-- call-level record so compliance can ask "what did this customer hear on day X, and why"
CREATE TABLE IF NOT EXISTS calls (
  event_id INTEGER PRIMARY KEY, wf TEXT, minute INT, lead_id TEXT, person_id TEXT, cfgver INT,
  cohort TEXT, w TEXT, v TEXT, d TEXT, p TEXT, connected INT, engaged INT, halluc INT);
CREATE INDEX IF NOT EXISTS calls_person ON calls (person_id);

CREATE TABLE IF NOT EXISTS daily (
  wf TEXT, day INT, leads INT DEFAULT 0, concluded INT DEFAULT 0, PRIMARY KEY (wf, day));

CREATE TABLE IF NOT EXISTS config_versions (
  wf TEXT, version INT, actor TEXT, minute INT, changed_dims TEXT, proposal_id TEXT, approval_id TEXT,
  config TEXT, PRIMARY KEY (wf, version));

-- outbox + lifecycle of every change we tried to make
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY, wf TEXT, channel TEXT, urgent INT, base_version INT, config TEXT,
  body TEXT, parts TEXT, status TEXT, approval_id TEXT, created_minute INT, sent_minute INT,
  updated_minute INT, applied_version INT, reasons TEXT);
CREATE INDEX IF NOT EXISTS proposals_wf ON proposals (wf, status);

-- the audit log: one row per decision, with the evidence that drove it
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, minute INT, wf TEXT, kind TEXT, summary TEXT, detail TEXT,
  proposal_id TEXT);
CREATE INDEX IF NOT EXISTS decisions_wf ON decisions (wf, minute);

-- learned state (experiments, DID health, incidents, baselines), JSON per (wf, key)
CREATE TABLE IF NOT EXISTS state (wf TEXT, key TEXT, value TEXT, PRIMARY KEY (wf, key));

CREATE TABLE IF NOT EXISTS validation_runs (
  run_id TEXT PRIMARY KEY, wf TEXT, prompt TEXT, n INT, pass_rate REAL, minute INT);

CREATE TABLE IF NOT EXISTS metrics (
  minute INT PRIMARY KEY, wall_ms REAL, events INT, ingest_ms REAL, decide_ms REAL, ingest_lag_min INT,
  credits REAL, pending INT, decisions INT, rss_mb REAL);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.c = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.execute("PRAGMA temp_store=MEMORY")
        self.c.execute("PRAGMA cache_size=-200000")
        self.c.executescript(SCHEMA)

    # -- transactions
    def begin(self):
        self.c.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.c.execute("COMMIT")

    def rollback(self):
        self.c.execute("ROLLBACK")

    def x(self, sql, args=()):
        return self.c.execute(sql, args)

    def q(self, sql, args=()):
        return self.c.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.c.execute(sql, args).fetchone()

    # -- small key/value helpers
    def meta(self, k, default=None):
        r = self.one("SELECT v FROM meta WHERE k=?", (k,))
        return json.loads(r[0]) if r else default

    def set_meta(self, k, v):
        self.x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    def state(self, wf, key, default=None):
        r = self.one("SELECT value FROM state WHERE wf=? AND key=?", (wf, key))
        return json.loads(r[0]) if r else default

    def set_state(self, wf, key, value):
        self.x("INSERT INTO state(wf,key,value) VALUES(?,?,?) ON CONFLICT(wf,key) DO UPDATE SET value=excluded.value",
               (wf, key, json.dumps(value)))

    def decide(self, minute, wf, kind, summary, detail=None, proposal_id=None) -> int:
        cur = self.x("INSERT INTO decisions(minute,wf,kind,summary,detail,proposal_id) VALUES(?,?,?,?,?,?)",
                     (minute, wf, kind, summary, json.dumps(detail or {}, default=str), proposal_id))
        return cur.lastrowid

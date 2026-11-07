# Controller runbook

For the engineer who owns this when its author is on leave. Read SUBMISSION.md for *why*; this is *how*.

## Run it

```bash
pip install -r requirements.txt                 # only httpx is needed by the controller
python -m sim serve --scenario scenarios/public_02_incidents.yaml --run-dir runs/x --force   # :8700
./run.sh http://127.0.0.1:8700                  # runs until the scenario ends; state in ./state/
scripts/run_scenario.sh scenarios/public_02_incidents.yaml public_02 8702   # baseline + sim + controller + grade
python scripts/crash_test.py --kills 6          # kill -9 at random points, restart, check invariants
python -m pytest -q tests
```

Env: `CONTROLLER_DB` (default `state/controller.db`), `CONTROLLER_STATUS` (status page dir), `LOG_LEVEL`.

## Where things are

| file | what |
|---|---|
| `controller/__main__.py` | loop: ingest → step → advance 60 sim-min; store-vs-world check on start |
| `controller/ingest.py` | chunk → one transaction (aggregates, leads, calls, cursor) |
| `controller/cohorts.py` | the shared cohort partition (lang × one feature), built once from history |
| `controller/planner.py` | experiments (welcome, voice), caller-ID health, call-time weights |
| `controller/incidents.py` | hallucination detect → fix → validate → verify |
| `controller/governance.py` | outbox, approval lifecycle, auto-apply, budgets, crash reconcile |
| `controller/loop.py` | wires planners to governance; builds targets; dry run / impact / affected leads |
| `controller/report.py` | `state/status.md`: supervisor view + loop health + alerts |
| `controller/why.py` | audit queries |
| `controller/tenant_prefs.json` | voluntary per-tenant limits stricter than policy (e.g. Kestrel: 2/day, ≥ 11:00) |

## State (SQLite, `state/controller.db`)

`agg` (cohort × hour × variant counts — every decision reads this) · `calls` (per call, for compliance) ·
`leads` · `cohorts` · `config_versions` · `proposals` (outbox + lifecycle) · `decisions` (audit log) ·
`state` (JSON per workflow: `exp:welcome_message`, `exp:voice`, `did`, `schedule`, `incident`,
`supervisor_dims`, `partition`, `halluc_base`) · `validation_runs` · `metrics` (per loop).

Inspect: `sqlite3 state/controller.db "select key, value from state where wf='nw_renewal'"`.

## Answering "why did X change"

```bash
python -m controller.why change nw_renewal "D4 13:00"     # what was live, who/why/approved, what happened next
python -m controller.why person ks_cancel:p0010349 --day 3 # what this customer heard and why it was live
python -m controller.why log ks_cancel --kind halluc_detected
```

## Restart semantics

Kill it any time and run `./run.sh` again. It resumes because:
- chunk data and the cursor commit together, so a half-read chunk is re-read from scratch;
- every outgoing change is written to `proposals` (status `sending`) *before* the HTTP call, with an id
  derived from the DB. On start, `sending` rows are looked up in the simulator (`/approvals`, config
  history) before anything is resent; resending reuses the id, so the simulator dedups;
- approvals are polled every loop by id; approved → applied immediately (apply window is 2h);
- all learned state is in `state`, written in the same loop that learned it.

If the **simulator** restarted, it's a new world: the store is archived (`*.old`) and we start fresh.

## Alerts (`state/status.md` → "Alerts")

| alert | page? | what to do |
|---|---|---|
| hallucination incident with no validated fix | PAGE | check `why log <wf> --kind halluc_no_fix`; candidates tried and their pass rates are in the detail; consider a manual rollback request to the client |
| protective change blocked by approval budget | PAGE | the client's 24h request cap is used up and a rollback/DID fix is waiting: call the client approver |
| credits low (< 30) | PAGE | incident response will stop validating; top up or raise the reserve |
| proposals rejected by the API as invalid | TICKET | controller bug (malformed proposal) — next business day |
| judgement-call rejections, expiries | no | handled: re-proposed with fresh data at the next window |

## Common changes

- **New tenant preference** (e.g. "no requests before noon"): add to `tenant_prefs.json`.
- **Stricter/looser winner rule**: `Experiment.ALPHA_WIN`, `MIN_N`, `MIN_REL` in `planner.py`; re-run
  `scripts/false_winner_sim.py` and update the numbers in SUBMISSION.md §5.
- **Hallucination sensitivity**: `Hallucination.P_FIRE`. Before lowering it, run the null-window check in
  SUBMISSION.md §7 on a no-incident scenario.

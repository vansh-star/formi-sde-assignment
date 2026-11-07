# public_03_load — load-test numbers

Box: **2 vCPU** / 7.7 GB (the brief's box has 4 vCPU / 8 GB); the simulator ran on the same machine.
Source: the controller's own `metrics` table (`state/public_03/controller.db`), one row per 60-sim-min step.

| metric | value |
|---|---|
| workflows / days | 12 / 5 |
| history ingested at bootstrap | 5,536,955 events in 116 s |
| live events per simulated day | ~840,000 (peak hour 192,012) |
| ingest throughput | ~42,000 events/s (single thread) |
| ingest time per simulated day | ~20 s |
| decision time per simulated day (12 workflows) | 3.9 s (D0) · 9.1 · 12.2 · 11.8 · 10.4 s (D4) |
| controller loop per simulated day | 26.7 · 32.5 · 34.9 · 34.3 · 33.2 s |
| SRE target | < 10 wall-clock minutes per simulated day: **met with ~17× headroom** |
| decision freshness | 0 unread chunks at every decision (staleness = one 60-min step) |
| peak RSS | 1,104 MB (high-water during bootstrap) |
| store size after 5 days | 1.4 GB (per-call compliance table ≈ 85%) |
| end-to-end wall time | 342 s for 5 simulated days incl. bootstrap and simulator time |
| outcome | +22.4% conclusion rate vs baseline; 55 changes applied, 0 harmful, 0 lapsed, 0 apply conflicts |

What breaks first at 10×: bootstrap time (~18 min), then the single SQLite writer and `calls` growth
(~14 GB / 5 days), then decision queries that re-scan the experiment window. See SUBMISSION.md §8.

Reproduce: `scripts/run_scenario.sh scenarios/public_03_load.yaml public_03 8703`, then
`sqlite3 state/public_03/controller.db "select * from metrics"`.

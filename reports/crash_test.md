# Crash-recovery test

Scenario `scenarios/public_02_incidents.yaml`, 6 SIGKILLs at random moments, seed 7.

```
kill #1: SIGKILL after 3.6s at sim 2027-01-04T11:00:00+05:30 (minute 660)
kill #2: SIGKILL after 2.8s at sim 2027-01-05T17:00:00+05:30 (minute 2460)
kill #3: SIGKILL after 5.3s at sim 2027-01-07T09:00:00+05:30 (minute 4860)
kill #4: SIGKILL after 2.4s at sim 2027-01-07T21:00:00+05:30 (minute 5580)
kill #5: SIGKILL after 4.7s at sim 2027-01-08T23:00:00+05:30 (minute 7140)
kill #6: SIGKILL after 3.8s at sim 2027-01-09T20:00:00+05:30 (minute 8400)
final controller process exited rc=0
```

| invariant | result | detail |
|---|---|---|
| scenario finished | PASS | 2027-01-14T00:00:00+05:30 |
| no event lost or double-counted | PASS | sim call events 104,000 · our aggregates 104,000 · our call rows 104,000 |
| no double-apply | PASS | 24 controller versions, duplicate proposal ids: none |
| no lost approval | PASS | approved-but-unapplied or lapsed: none |
| every applied change auditable | PASS | missing proposal rows: none |

**Overall: PASS**

Graded: `python -m sim grade --run runs/crash --baseline runs/base_public_02 --out reports/crash_grade.md`
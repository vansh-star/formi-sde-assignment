# Crash-recovery test

Scenario `scenarios/public_02_incidents.yaml`, 6 SIGKILLs at random moments, seed 7.

```
kill #1: SIGKILL after 3.6s at sim 2027-01-04T00:00:00+05:30 (minute 0)
kill #2: SIGKILL after 2.8s at sim 2027-01-04T19:00:00+05:30 (minute 1140)
kill #3: SIGKILL after 5.3s at sim 2027-01-06T09:00:00+05:30 (minute 3420)
kill #4: SIGKILL after 2.4s at sim 2027-01-06T18:00:00+05:30 (minute 3960)
kill #5: SIGKILL after 4.7s at sim 2027-01-07T16:00:00+05:30 (minute 5280)
kill #6: SIGKILL after 3.8s at sim 2027-01-08T07:00:00+05:30 (minute 6180)
final controller process exited rc=0
```

| invariant | result | detail |
|---|---|---|
| scenario finished | PASS | 2027-01-14T00:00:00+05:30 |
| no event lost or double-counted | PASS | sim call events 104,000 · our aggregates 104,000 · our call rows 104,000 |
| no double-apply | PASS | 18 controller versions, duplicate proposal ids: none |
| no lost approval | PASS | approved-but-unapplied or lapsed: none |
| every applied change auditable | PASS | missing proposal rows: none |

**Overall: PASS**

Graded: `python -m sim grade --run runs/crash --baseline runs/base_public_02 --out reports/crash_grade.md`
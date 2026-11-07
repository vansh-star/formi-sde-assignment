# Grade report — public_03_load

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.401 | +17.7% | 0.264 | +7.7% | 0.538 | +8.2% | 53% | 402 (383) |
| arav_kyc_1 | 0.459 | +28.3% | 0.260 | +20.7% | 0.613 | +2.2% | 16% | 435 (349) |
| arav_renewal_0 | 0.480 | +18.8% | 0.287 | +8.9% | 0.591 | +7.9% | 42% | 495 (458) |
| kest_cancel_2 | 0.384 | +13.3% | 0.264 | +8.1% | 0.517 | +3.9% | 30% | 429 (388) |
| kest_kyc_1 | 0.483 | +36.1% | 0.273 | +27.8% | 0.608 | +1.9% | 15% | 441 (309) |
| kest_renewal_0 | 0.474 | +18.3% | 0.286 | +8.8% | 0.588 | +7.6% | 40% | 440 (430) |
| nort_cancel_2 | 0.397 | +17.1% | 0.263 | +8.0% | 0.533 | +7.3% | 53% | 431 (389) |
| nort_kyc_1 | 0.492 | +38.9% | 0.274 | +28.6% | 0.614 | +2.9% | 20% | 451 (346) |
| nort_renewal_0 | 0.474 | +18.2% | 0.287 | +8.7% | 0.587 | +7.6% | 41% | 423 (431) |
| tide_cancel_2 | 0.391 | +15.1% | 0.264 | +7.8% | 0.523 | +4.8% | 31% | 406 (406) |
| tide_kyc_1 | 0.463 | +30.4% | 0.260 | +21.7% | 0.616 | +3.1% | 20% | 413 (339) |
| tide_renewal_0 | 0.473 | +17.8% | 0.285 | +8.6% | 0.586 | +7.4% | 41% | 446 (422) |
| ALL | 0.448 | +22.4% | 0.272 | +13.2% | 0.577 | +5.8% | 33% | 5212 (4650) |

## Changes applied by the controller

55 applied — neutral: 16, beneficial: 39

- nort_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.020 → **neutral**
- nort_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- kest_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.020 → **neutral**
- kest_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- arav_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.020 → **neutral**
- arav_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- tide_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.020 → **neutral**
- tide_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- nort_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.072 → **beneficial**
- kest_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.070 → **beneficial**
- kest_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.023 → **beneficial**
- arav_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.079 → **beneficial**
- tide_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.080 → **beneficial**
- arav_cancel_2 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- tide_cancel_2 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- arav_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.024 → **beneficial**
- tide_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.024 → **beneficial**
- nort_cancel_2 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- nort_kyc_1 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.022 didΔ=+0.110 → **beneficial**
- arav_kyc_1 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.022 didΔ=+0.110 → **beneficial**
- tide_kyc_1 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.022 didΔ=+0.110 → **beneficial**
- kest_cancel_2 v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- nort_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.026 → **beneficial**
- nort_renewal_0 v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.030 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.030 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- kest_kyc_1 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.022 didΔ=+0.126 → **beneficial**
- kest_cancel_2 v5 (controller_auto) dims=['welcome_message'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v5 (controller_auto) dims=['welcome_message'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v5 (controller_auto) dims=['welcome_message'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v5 (controller_auto) dims=['welcome_message'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.093 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.100 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.092 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.100 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.100 didΔ=+0.000 → **beneficial**
- nort_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.327 → **beneficial**
- kest_renewal_0 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.100 didΔ=+0.000 → **beneficial**
- kest_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.351 → **beneficial**
- arav_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.287 → **beneficial**
- tide_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.294 → **beneficial**
- tide_cancel_2 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.063 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.064 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v4 (controller_approved) dims=['voice'] engageΔ=+0.051 didΔ=+0.000 → **beneficial**
- arav_kyc_1 v4 (controller_approved) dims=['voice'] engageΔ=+0.051 didΔ=+0.000 → **beneficial**
- nort_kyc_1 v4 (controller_approved) dims=['voice'] engageΔ=+0.051 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v6 (controller_approved) dims=['voice'] engageΔ=-0.000 didΔ=+0.000 → **neutral**
- arav_renewal_0 v6 (controller_approved) dims=['voice'] engageΔ=-0.000 didΔ=+0.000 → **neutral**
- tide_renewal_0 v6 (controller_approved) dims=['voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- nort_kyc_1 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.056 didΔ=+0.000 → **beneficial**
- kest_kyc_1 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.110 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v6 (controller_approved) dims=['voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- tide_kyc_1 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.056 didΔ=+0.000 → **beneficial**
- arav_kyc_1 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.056 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 37,
  "decisions": {
    "approved": 35,
    "rejected": 2,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 2
  },
  "applied_via_approval": 35,
  "auto_applied": 20,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nort_kyc_1": 1,
    "nort_cancel_2": 1,
    "arav_kyc_1": 1,
    "arav_cancel_2": 1,
    "tide_kyc_1": 1,
    "tide_cancel_2": 1,
    "nort_renewal_0": 2,
    "kest_renewal_0": 1,
    "kest_kyc_1": 1,
    "kest_cancel_2": 1,
    "arav_renewal_0": 2,
    "tide_renewal_0": 2
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nort_renewal_0:welcome_message=W3": {
    "3": 0.902,
    "4": 0.902
  },
  "kest_renewal_0:welcome_message=W3": {
    "3": 0.897,
    "4": 0.899
  },
  "arav_renewal_0:welcome_message=W3": {
    "3": 0.898,
    "4": 0.9
  },
  "tide_renewal_0:welcome_message=W3": {
    "3": 0.9,
    "4": 0.9
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0
"""Deterministic statistics. No LLM touches any of this."""
from __future__ import annotations

import math
from dataclasses import dataclass


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


@dataclass
class Comparison:
    diff: float      # absolute difference A − B (stratified)
    se: float
    z: float
    rel: float       # diff / rate(B)
    n_a: int         # trials in strata where both arms ran
    n_b: int
    rate_a: float
    rate_b: float

    @property
    def p_greater(self) -> float:  # one-sided: A > B
        return norm_sf(self.z)

    @property
    def p_less(self) -> float:     # one-sided: A < B
        return norm_sf(-self.z)

    def ci(self, z=1.96):
        return self.diff - z * self.se, self.diff + z * self.se

    def as_dict(self):
        lo, hi = self.ci()
        return {"diff": round(self.diff, 4), "ci95": [round(lo, 4), round(hi, 4)], "z": round(self.z, 2),
                "rel": round(self.rel, 4), "n_a": self.n_a, "n_b": self.n_b,
                "rate_a": round(self.rate_a, 4), "rate_b": round(self.rate_b, 4)}


def stratified(strata) -> Comparison | None:
    """Cochran–Mantel–Haenszel-style difference of proportions. `strata` = iterable of (nA, xA, nB, xB).

    Strata are (day, hour, cohort, prompt, other-dim). Comparing only within strata where both arms ran
    makes the estimate robust to time trends, allocation changes mid-experiment, supervisor prompt edits
    and the other factor of the factorial design."""
    sw = swd = swv = 0.0
    na = xa = nb = xb = 0
    for nA, xA, nB, xB in strata:
        if nA <= 0 or nB <= 0:
            continue
        na += nA; xa += xA; nb += nB; xb += xB
        w = nA * nB / (nA + nB)
        pbar = (xA + xB) / (nA + nB)
        sw += w
        swd += w * (xA / nA - xB / nB)
        swv += w * w * pbar * (1 - pbar) * (1 / nA + 1 / nB)
    if sw == 0 or nb == 0:
        return None
    d = swd / sw
    se = math.sqrt(swv) / sw if swv > 0 else float("inf")
    z = d / se if se not in (0, float("inf")) else 0.0
    rb = xb / nb
    return Comparison(d, se, z, d / rb if rb > 0 else 0.0, na, nb, xa / na, rb)


def holm(pvals: list[float]) -> list[float]:
    """Holm step-down adjusted p-values (controls family-wise error across arms/cohorts)."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvals[i]))
        adj[i] = running
    return adj


def poisson_sf(x: int, lam: float) -> float:
    """P(X >= x) for X ~ Poisson(lam). Used for rare-event (hallucination) counts."""
    if x <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    term = math.exp(-lam)
    cdf = term
    for k in range(1, x):
        term *= lam / k
        cdf += term
    return max(0.0, 1.0 - cdf)


def wilson(x: int, n: int, z=1.96):
    if n == 0:
        return 0.0, 1.0
    p = x / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return c - h, c + h


def n_per_arm(p: float, rel_lift: float, alpha: float = 0.01, power: float = 0.8) -> int:
    """Two-proportion sample size per arm (normal approximation)."""
    za = -_ppf(alpha)
    zb = -_ppf(1 - power)
    d = p * rel_lift
    return math.ceil(2 * (za + zb) ** 2 * p * (1 - p) / (d * d))


def _ppf(q: float) -> float:
    # inverse normal CDF via bisection; only used for sizing
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if 1 - norm_sf(mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2

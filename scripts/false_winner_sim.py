"""Monte Carlo of the promotion rule (daily looks, Holm across challengers, MIN_N, MIN_REL) to measure
false-winner rate under the null and power under a real lift. Uses the controller's own stats code.
    python scripts/false_winner_sim.py
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller.stats import holm, stratified  # noqa: E402


def run(alpha, lifts, days=8, inc_conn=650, arm_conn=120, base=0.55, min_n=400, min_rel=0.03, trials=2000, seed=1):
    rng = random.Random(seed)
    k = len(lifts)
    wins = [0] * k
    any_false = 0
    for _ in range(trials):
        strata = []
        won = None
        for d in range(days):
            ni = inc_conn
            xi = sum(rng.random() < base for _ in range(ni))
            arms = []
            for L in lifts:
                xa = sum(rng.random() < base * (1 + L) for _ in range(arm_conn))
                arms.append((arm_conn, xa))
            strata.append((ni, xi, arms))
            comps = [stratified([(s[2][j][0], s[2][j][1], s[0], s[1]) for s in strata]) for j in range(k)]
            live = [(j, c) for j, c in enumerate(comps) if c.n_a >= min_n]
            if not live:
                continue
            adj = holm([c.p_greater for _, c in live])
            for (j, c), p in sorted(zip(live, adj), key=lambda t: -t[0][1].diff):
                if p < alpha and c.rel >= min_rel:
                    won = j
                    break
            if won is not None:
                break
        if won is not None:
            wins[won] += 1
            if lifts[won] <= 0:
                any_false += 1
    return any_false / trials, [w / trials for w in wins]


if __name__ == "__main__":
    for alpha in (0.01, 0.025, 0.05):
        fw, _ = run(alpha, [0, 0, 0], trials=1500)
        _, pw = run(alpha, [0.12, 0, 0], trials=800, seed=2)
        _, pw2 = run(alpha, [0.08, 0, 0], trials=800, seed=3)
        print(f"alpha={alpha}: false-winner (3 null arms, 8 daily looks) = {fw:.1%}; "
              f"power +12% = {pw[0]:.0%}, +8% = {pw2[0]:.0%}")

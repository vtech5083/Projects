"""
Study question 1: does the cheap-to-expensive ranking survive when the
shared assumptions change?  Every company gets the same change at the same
time, then we compare the new ranking with today's.
Run from the engine folder:  python3 study/ranking_stability.py
"""
import itertools, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import valuation

d = json.load(open("runs/latest.json"))
H = {t: r for t, r in d.items() if r["health"]["gate"] != "STOP"}
T = sorted(H)


def spearman(x, y):
    rx = {t: i for i, t in enumerate(sorted(x, key=x.get))}
    ry = {t: i for i, t in enumerate(sorted(y, key=y.get))}
    n = len(x)
    return 1 - 6 * sum((rx[t] - ry[t]) ** 2 for t in x) / (n * (n * n - 1))


def vp(shift=0.0, terminal=None, premium=0.0, growth3=False):
    out = {}
    for t, r in H.items():
        a = dict(r["assumptions"])
        a["wacc"] += shift
        a["wacc_mature"] += shift
        if terminal is not None:
            a["terminal_growth"] = terminal
        a["terminal_roic_premium"] = premium
        if growth3 and a.get("growth_3yr") is not None:
            a["growth"] = min(max(a["growth_3yr"], -0.05), a["growth_cap"])
        if a["wacc_mature"] - max(a["terminal_growth"], 0.02) < 0.01:
            a["terminal_growth"] = a["wacc_mature"] - 0.01
        out[t] = valuation.project(a)[0] / r["data"]["price"]
    return out


base = vp()
order = sorted(T, key=lambda t: -base[t])
print("Today's ranking (value / price):", ", ".join(f"{t} {base[t]:.2f}" for t in order))

# ---- the grid ------------------------------------------------------------
grid = list(itertools.product([-0.02, -0.01, 0.0, 0.01, 0.02],    # discount rate shift
                              [0.02, 0.03, 0.04, 0.05],            # long-run growth
                              [0.0, 0.025, 0.05],                  # lasting-advantage premium
                              [False, True]))                      # growth rule
rhos, top3, bottom3, levels = [], {t: 0 for t in T}, {t: 0 for t in T}, []
for shift, term, prem, g3 in grid:
    v = vp(shift, term, prem, g3)
    rhos.append(spearman(base, v))
    ranked = sorted(T, key=lambda t: -v[t])
    for t in ranked[:3]: top3[t] += 1
    for t in ranked[-3:]: bottom3[t] += 1
    levels.append(sorted(v.values())[len(v) // 2])
n = len(grid)
rhos.sort()
print(f"\n{n} combinations of shared assumptions")
print(f"rank agreement with today's ranking (Spearman, 1 = identical): "
      f"worst {rhos[0]:.2f}, 10th pct {rhos[n // 10]:.2f}, median {rhos[n // 2]:.2f}")
levels.sort()
print(f"median value/price across companies ranges from {levels[0]:.2f} to {levels[-1]:.2f} "
      f"-- the LEVEL moves a lot")
print("\nshare of combinations in which each company is in the top 3 / bottom 3:")
for t in order:
    print(f"  {t:5s} top3 {top3[t] / n:5.0%}   bottom3 {bottom3[t] / n:5.0%}")

# ---- duration: who is hurt most by a higher discount rate? ----------------
print("\nSensitivity to a 1-point higher discount rate, against how much value lies after year 10")
print("  ticker  value change   share of value after year 10")
rows = []
up = vp(0.01)
for t in T:
    rows.append((t, up[t] / base[t] - 1, H[t]["valuation"]["share_of_value_after_year_ten"]))
for t, ch, sh in sorted(rows, key=lambda x: x[1]):
    print(f"  {t:6s} {ch:+8.1%}        {sh:5.0%}")
xs, ys = [r[2] for r in rows], [r[1] for r in rows]
mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
corr = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / (
    (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5)
print(f"  correlation between the two columns: {corr:.2f}")

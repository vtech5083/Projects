"""
Results of backtest 2. Reads study/backtest2_results.csv.
Rank correlation (information coefficient, IC): +1 = the signal's order
matched the next year's returns exactly, 0 = no link.
Run from the engine folder:  python3 study/backtest2_analysis.py
"""
import math
import pandas as pd

df = pd.read_csv("study/backtest2_results.csv")
grid = [c for c in df.columns if c.startswith("vp_grid")]

def ranks(g, col):
    return g[col].rank(pct=True)

# combined quality + value: average of the two percentile ranks
df["value_plus_quality"] = df.groupby("date", group_keys=False).apply(
    lambda g: (ranks(g, "vp") + ranks(g, "gross_prof")) / 2)

SIGNALS = {"vp": "DCF value / price (today's engine)",
           "vp_nocap": "DCF value / price, no growth size cap",
           "fcf_yield": "Free-cash-flow yield",
           "earnings_yield": "Earnings yield (E/P)",
           "gross_prof": "Gross profitability (quality)",
           "value_plus_quality": "DCF value + gross profitability"}


def ic(g, col, ret="ret_1y"):
    m = g[col].notna() & g[ret].notna() & (g[col].abs() != float("inf"))
    return g.loc[m, col].rank().corr(g.loc[m, ret].rank()) if m.sum() >= 10 else float("nan")


def spread(g, col, ret="ret_1y"):
    g = g[g[col].notna() & g[ret].notna()].sort_values(col, ascending=False)
    k = max(len(g) // 5, 1)
    return g[ret].head(k).mean() - g[ret].tail(k).mean(), g[ret].head(k).mean() - g[ret].mean()


def summary(sub, title, ret="ret_1y"):
    print(f"\n=== {title}  (return horizon {ret[4:]}) ===")
    print(f"{'signal':40s} {'mean IC':>8s} {'t-stat':>7s} {'IC>0':>6s}  {'top-bottom fifth':>17s} {'top fifth vs avg':>17s}")
    for col, name in SIGNALS.items():
        ics, sps, exc = [], [], []
        for _, g in sub.groupby("date"):
            v = ic(g, col, ret)
            if not math.isnan(v):
                ics.append(v)
                s, e = spread(g, col, ret)
                sps.append(s); exc.append(e)
        n = len(ics)
        if n < 2:
            continue
        mean = sum(ics) / n
        sd = (sum((x - mean) ** 2 for x in ics) / (n - 1)) ** 0.5
        t = mean / (sd / n ** 0.5) if sd else float("nan")
        print(f"{name:40s} {mean:+8.3f} {t:7.2f} {sum(x > 0 for x in ics):3d}/{n:<2d}  "
              f"{sum(sps) / n:+16.1%} {sum(exc) / n:+16.1%}")


# A company that failed only the "data" check failed because our parsing of
# its filings was incomplete, not because the business was weak.
df["fails"] = df["fails"].fillna("") if "fails" in df else ""
df["data_only"] = (df.gate == "STOP") & (df["fails"] == "data")
healthy = df[df.gate != "STOP"]
summary(healthy, "companies that passed the health check")
summary(df[~df.data_only], "all companies (excluding data-only failures)")
summary(healthy, "companies that passed the health check", "ret_3y")

print("\n=== by year: DCF value/price vs free-cash-flow yield (1-year IC, healthy companies) ===")
for d, g in healthy.groupby("date"):
    print(f"{d}  n={len(g):3d}  DCF {ic(g, 'vp'):+.2f}  no-cap {ic(g, 'vp_nocap'):+.2f}  "
          f"FCF {ic(g, 'fcf_yield'):+.2f}  E/P {ic(g, 'earnings_yield'):+.2f}   "
          f"universe avg {g.ret_1y.mean():+.1%}  SPY {g.spy_1y.iloc[0]:+.1%}")

print(f"\nfailed the health check: {(df.gate == 'STOP').sum()} company-dates, "
      f"of which {df.data_only.sum()} failed only on data")
print("\n=== health check (real failures only): average 1-year return ===")
diffs = []
for d, g in df[~df.data_only].groupby("date"):
    s, o = g[g.gate == "STOP"].ret_1y.mean(), g[g.gate != "STOP"].ret_1y.mean()
    diffs.append(o - s)
    print(f"{d}  failed {s:+.1%} (n={(g.gate == 'STOP').sum():2d})  passed {o:+.1%}  difference {o - s:+.1%}")
n = len(diffs); m = sum(diffs) / n; sd = (sum((x - m) ** 2 for x in diffs) / (n - 1)) ** 0.5
print(f"average difference {m:+.1%}, t-stat {m / (sd / n ** 0.5):.2f}, passed beat failed in {sum(x > 0 for x in diffs)}/{n} years")

print("\n=== high conviction: in the top fifth by value/price under all 6 assumption sets ===")
rows = []
for d, g in healthy.groupby("date"):
    g = g.dropna(subset=grid + ["ret_1y"])
    top = pd.Series(True, index=g.index)
    for c in grid:
        top &= g[c].rank(ascending=False, pct=True) <= 0.2
    hc = g[top]
    rows.append((d, len(hc), hc.ret_1y.mean() if len(hc) else float("nan"), g.ret_1y.mean(), ", ".join(hc.ticker)))
for d, k, r, avg, names in rows:
    print(f"{d}  {k:2d} names  {r:+.1%} vs universe {avg:+.1%}   {names}")
ok = [(r, a) for _, k, r, a, _ in rows if k]
print(f"average: high conviction {sum(r for r, _ in ok) / len(ok):+.1%} vs universe {sum(a for _, a in ok) / len(ok):+.1%}; "
      f"beat it in {sum(r > a for r, a in ok)}/{len(ok)} years")

print("\n=== labels and the next year ===")
print(df.groupby("label").ret_1y.agg(["count", "mean", "median"]).round(3).to_string())

"""
Did cheaper (higher value / price) companies earn more afterwards?
Reads study/backtest_<date>.csv. Compares our DCF ranking with a plain
free-cash-flow yield ranking built from the same as-of filings.
Run from the engine folder:  python3 study/backtest_analysis.py
"""
import glob, json
import pandas as pd

df = pd.concat(pd.read_csv(f) for f in sorted(glob.glob("study/backtest_20*.csv")))

# simple yardstick: free cash flow (TTM, as filed then) / market value then
def fcf_yield(row):
    try:
        q = pd.read_csv(f"/tmp/valuation_backtest/{row.date}/{row.ticker}_quarterly.csv", index_col="line_item")["TTM"]
        mk = json.load(open(f"/tmp/valuation_backtest/{row.date}/market_inputs.json"))
        return (q["cash_from_operations"] - q["capex"]) / (mk["prices"][row.ticker] * q["diluted_shares"])
    except Exception:
        return float("nan")
df["fcf_yield"] = df.apply(fcf_yield, axis=1)


def spearman(a, b):
    m = a.notna() & b.notna()
    return a[m].rank().corr(b[m].rank()) if m.sum() >= 5 else float("nan")


def report(sub, name):
    print(f"\n=== {name} ===")
    print(f"{'formation':11s} {'n':>3s} {'rank corr 1y':>12s} {'2y':>6s} {'3y':>6s}   "
          f"{'cheapest third 1y':>17s} {'dearest third 1y':>16s} {'S&P 500 1y':>10s}   {'FCF-yield corr 1y':>17s}")
    for date, g in sub.groupby("date"):
        g = g.sort_values("value_to_price", ascending=False)
        k = max(len(g) // 3, 1)
        print(f"{date:11s} {len(g):3d} {spearman(g.value_to_price, g.ret_1y):12.2f} "
              f"{spearman(g.value_to_price, g.get('ret_2y', pd.Series(dtype=float))):6.2f} "
              f"{spearman(g.value_to_price, g.get('ret_3y', pd.Series(dtype=float))):6.2f}   "
              f"{g.ret_1y.head(k).mean():17.1%} {g.ret_1y.tail(k).mean():16.1%} {g.spy_1y.iloc[0]:10.1%}   "
              f"{spearman(g.fcf_yield, g.ret_1y):17.2f}")
    c1 = [spearman(g.value_to_price, g.ret_1y) for _, g in sub.groupby("date")]
    f1 = [spearman(g.fcf_yield, g.ret_1y) for _, g in sub.groupby("date")]
    print(f"average rank correlation, 1 year: DCF value/price {sum(c1) / len(c1):+.2f}, FCF yield {sum(f1) / len(f1):+.2f}")


report(df[df.gate != "STOP"], "companies that passed the health check")
report(df, "all companies")

# did the health check itself help? returns of STOP vs the rest
print("\n=== health check: average 1-year return ===")
for date, g in df.groupby("date"):
    s, o = g[g.gate == "STOP"], g[g.gate != "STOP"]
    print(f"{date}  failed check: {s.ret_1y.mean():+.1%} (n={len(s)})   passed: {o.ret_1y.mean():+.1%} (n={len(o)})")

# labels
print("\n=== labels and what followed (1 year) ===")
print(df.groupby("label").ret_1y.agg(["count", "mean", "median"]).round(3).to_string())
df.to_csv("study/backtest_all.csv", index=False)

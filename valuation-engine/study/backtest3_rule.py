"""
Yearly portfolios for Vivek's rule, from study/backtest_rule_results.csv
(written by study/backtest_rule.py, which uses quality.py's verdicts).
Each year-end every group is an equal-weighted portfolio held one year; the
yearly results are chained to show what $1 invested at the end of 2013 became.
Run from the engine folder after backtest_rule.py.
"""
import math
import pandas as pd

df = pd.read_csv("study/backtest_rule_results.csv")
ok = df.growth_verdict == "plausible"
groups = {
    "All companies (equal weight)": lambda d: d,
    "Good business": lambda d: d[d.good_business],
    "Not a good business (Avoid)": lambda d: d[~d.good_business],
    "RULE: good business + realistic growth": lambda d: d[d.good_business & (d.growth_verdict == "plausible")],
    "RULE, and below our base-case value": lambda d: d[d.verdict == "Candidate"],
    "Good business, price needs rare growth": lambda d: d[d.good_business & d.growth_verdict.isin(["stretched", "implausible"])],
}
spy = df.groupby("date").spy_1y.first()
print(f"{len(df)} company-dates, {df.date.nunique()} year-ends (prices: IBKR)\n")
print(f"{'group':42s} {'avg n':>6s} {'per year':>9s} {'beat all':>9s} {'$1 became':>10s} {'worst yr':>9s} {'edge t':>7s}")
for name, f in groups.items():
    yearly, edges, sizes = [], [], []
    for d, g in df.dropna(subset=["ret_1y"]).groupby("date"):
        sub = f(g)
        sizes.append(len(sub))
        if len(sub):
            yearly.append(sub.ret_1y.mean())
            edges.append(sub.ret_1y.mean() - g.ret_1y.mean())
    n = len(edges); m = sum(edges) / n
    sd = (sum((x - m) ** 2 for x in edges) / (n - 1)) ** 0.5 if n > 1 else float("nan")
    t = m / (sd / n ** 0.5) if sd else float("nan")
    print(f"{name:42s} {sum(sizes) / len(sizes):6.1f} {sum(yearly) / len(yearly):+9.1%} "
          f"{sum(e > 0 for e in edges):4d}/{n:<4d} {math.prod(1 + y for y in yearly):10.2f} {min(yearly):+9.1%} {t:+7.2f}")
print(f"{'S&P 500 (SPY, with dividends)':42s} {'':6s} {spy.mean():+9.1%} {'':9s} {math.prod(1 + spy):10.2f} {spy.min():+9.1%}")

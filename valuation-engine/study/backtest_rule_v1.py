"""Rule v1 (frozen 20 Nov 2026) on the backtest, after the data fixes. Reported as is:
no variants are compared, so nothing here can be used to tune the rule.
Each year-end 2013-2024: the top-100 US non-financial companies; equal-weighted
portfolio of the companies that pass the rule (Candidate or Fair), held one year."""
import json, math
from collections import Counter
from pathlib import Path
import pandas as pd

W = Path("/tmp/valuation_backtest2")
res = pd.read_csv("study/backtest2_results.csv")
rows = []
for d, g in res.groupby("date"):
    runs = json.loads((W / d / "runs" / "latest.json").read_text())
    for _, r in g.iterrows():
        rec = runs.get(r.ticker)
        if not rec:
            continue
        rows.append({"date": d, "ticker": r.ticker, "verdict": rec["verdict"], "passes": rec.get("passes_rule", False),
                     "pv": (rec.get("valuation") or {}).get("price_to_value"),
                     "ret_1y": r.ret_1y, "spy_1y": r.spy_1y, "ret_3y": r.ret_3y})
df = pd.DataFrame(rows)
df.to_csv("study/backtest_rule_v1_results.csv", index=False)

out = []
p = lambda *a: out.append(" ".join(str(x) for x in a))
p(f"company-dates: {len(df)}")
p("verdicts:", dict(Counter(df.verdict)))
susp = df[(df.pv.notna()) & (df.pv < 0.2)]
p(f"left after the fixes with our value over 5x the price: {len(susp)}",
  "" if susp.empty else susp[["date", "ticker", "verdict", "pv"]].to_string(index=False))
p("\nyear  picks  rule   all    SPY")
yr, al, sp, n3 = [], [], [], []
for d, g in df.dropna(subset=["ret_1y"]).groupby("date"):
    k = g[g.passes]
    a, s = g.ret_1y.mean(), g.spy_1y.iloc[0]
    r = k.ret_1y.mean() if len(k) else 0.0     # no picks: sit in cash at 0%
    yr.append(r); al.append(a); sp.append(s)
    p(f"{d[:4]}  {len(k):5d}  {r:+.1%} {a:+.1%} {s:+.1%}")
chain = lambda xs: math.prod(1 + x for x in xs)
avg = lambda xs: sum(xs) / len(xs)
p(f"\nrule       {avg(yr):+.1%}/yr   $1 -> {chain(yr):.2f}   worst year {min(yr):+.1%}")
p(f"all        {avg(al):+.1%}/yr   $1 -> {chain(al):.2f}   worst year {min(al):+.1%}")
p(f"SPY        {avg(sp):+.1%}/yr   $1 -> {chain(sp):.2f}   worst year {min(sp):+.1%}")
p(f"rule beat all companies in {sum(r > a for r, a in zip(yr, al))} of {len(yr)} years; beat SPY in {sum(r > s for r, s in zip(yr, sp))}")
diff = [r - a for r, a in zip(yr, al)]
sd = (sum((x - avg(diff)) ** 2 for x in diff) / (len(diff) - 1)) ** 0.5
p(f"edge over all companies {avg(diff):+.1%}/yr, t = {avg(diff) / (sd / len(diff) ** 0.5):.2f} (12 years: below 2 is within luck)")
t3 = df.dropna(subset=["ret_3y"])
p(f"3-year: rule picks {t3[t3.passes].ret_3y.mean():+.1%} vs all {t3.ret_3y.mean():+.1%}")
Path("study/backtest_rule_v1_report.txt").write_text("\n".join(out) + "\n")
print("\n".join(out))

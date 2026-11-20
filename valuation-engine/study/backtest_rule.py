"""
Backtest of Vivek's rule (quality.py) on the backtest-2 company-dates:
good business (ten-year record) + realistic growth implied by the price.
Uses the statements and engine runs saved by study/backtest2.py.
Note: base rates use the full 2011-2024 SEC sample, a mild look-ahead.
"""
import json, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import quality as qa

WORK = Path("/tmp/valuation_backtest2")
df = pd.read_csv("study/backtest2_results.csv")
rows = []
for d in sorted(df.date.unique()):
    runs = json.loads((WORK / d / "runs/latest.json").read_text())
    for t, r in runs.items():
        annual = pd.read_csv(WORK / d / f"{t}_statements.csv", index_col="line_item")
        b = qa.business_check(annual, r["assumptions"]["wacc"])
        need = r["valuation"]["growth_price_requires"]
        g = qa.growth_check(r["assumptions"]["revenue"], need)
        v = r["valuation"]["base_value_per_share"]; p = r["data"]["price"]
        verdict, _ = qa.verdict(b, g, v / p if v is not None and p else None)
        rows.append({"date": d, "ticker": t, "good_business": b.get("good_business", False),
                     "business_score": b.get("score", 0), "growth_verdict": g["verdict"], "verdict": verdict})
rule = pd.DataFrame(rows)
m = df.merge(rule, on=["date", "ticker"])
m.to_csv("study/backtest_rule_results.csv", index=False)

def table(col, ret):
    # average return of each group, minus the average of all companies that year
    out = {}
    for d, g in m.dropna(subset=[ret]).groupby("date"):
        avg = g[ret].mean()
        for k, gg in g.groupby(col):
            out.setdefault(k, []).append((gg[ret].mean() - avg, len(gg)))
    print(f"\n{col}: return vs the year's average company, {ret[4:]}")
    for k, v in sorted(out.items(), key=lambda kv: -sum(x for x, _ in kv[1]) / len(kv[1])):
        mean = sum(x for x, _ in v) / len(v)
        sd = (sum((x - mean) ** 2 for x, _ in v) / max(len(v) - 1, 1)) ** 0.5
        t = mean / (sd / len(v) ** 0.5) if sd else float("nan")
        span = "over 1 year" if ret == "ret_1y" else "over 3 years"
        print(f"  {str(k):16s} {mean:+7.1%} {span} (t {t:+.2f}), ahead in {sum(x > 0 for x, _ in v)}/{len(v)} years, "
              f"about {sum(n for _, n in v) / len(v):.0f} companies a year")

for ret in ("ret_1y", "ret_3y"):
    table("good_business", ret)
    table("growth_verdict", ret)
    table("verdict", ret)

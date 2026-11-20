"""Pressure test of the draft's 'generous margin' (10-year high or today's margin).
Compares the growth a price needs at: normal margin, peak margin (draft), and the
average of the company's best three years (one spike year cannot carry the verdict).
Backtest: top-100 US non-financial companies, year-ends 2013-2024, IBKR prices."""
import importlib.util, json, math, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, ".")
import valuation
spec = importlib.util.spec_from_file_location("draft", "study/quality_draft_2026-11-20.py")
draft = importlib.util.module_from_spec(spec); spec.loader.exec_module(draft)
W = Path("/tmp/valuation_backtest2")

def yearly_margins(an, fy_end):
    rev, op = an.loc["revenue"], an.loc["operating_income"]
    cols = [c for c in an.columns if c <= fy_end][-10:]
    m = (op[cols] / rev[cols]).dropna()
    return m[(rev[cols] > 0).reindex(m.index, fill_value=False)]

def assess(a, price, an):
    offset = a["margin"] - a["margin_reported"]          # R&D and lease add-backs, applied to every year
    m = yearly_margins(an, "9999") + offset
    best3 = m.sort_values().iloc[-3:].mean() if len(m) >= 3 else a["target_margin"]
    peak = max(a.get("margin_high_10yr") or a["margin"], a["margin"], a["target_margin"])
    robust = max(best3, a["target_margin"])
    peak_year = m.idxmax()[:4] if len(m) else None
    out = {"peak_margin": peak, "best3_margin": robust, "normal_margin": a["target_margin"],
           "peak_year": peak_year, "cyclical": a.get("cyclical"),
           "spike_pts": peak - (a.get("margin_10yr_avg") or peak)}
    for name, mg in (("normal", a["target_margin"]), ("peak", peak), ("best3", robust)):
        g = valuation.implied_growth(dict(a, target_margin=mg), price)
        out[f"g_{name}"] = g
        out[f"real_{name}"] = draft.share_that_grew(a["revenue"], g) >= draft.LIMITS["rare_growth"]
    return out

res = pd.read_csv("study/backtest2_results.csv")
rows = []
for d, g in res.groupby("date"):
    runs = json.loads((W / d / "runs" / "latest.json").read_text())
    for _, r in g.iterrows():
        rec = runs.get(r.ticker)
        if not rec or rec.get("assumptions") is None: continue
        a, p = rec["assumptions"], rec["data"]["price"]
        try:
            an = pd.read_csv(W / d / f"{r.ticker}_statements.csv", index_col="line_item")
            x = assess(a, p, an)
            w = a["wacc"] if a["wacc"] is not None else float("inf")
            x["good"] = draft.good_business(an, w)["good_business"]
        except Exception as e:
            continue
        x.update(date=d, ticker=r.ticker, ret_1y=r.ret_1y, ret_3y=r.ret_3y)
        rows.append(x)
df = pd.DataFrame(rows)
df.to_csv("study/peak_margin_test.csv", index=False)

def port(mask):
    yearly = [g.ret_1y.mean() for _, g in df[mask].dropna(subset=["ret_1y"]).groupby("date")]
    allm = [g.ret_1y.mean() for _, g in df.dropna(subset=["ret_1y"]).groupby("date")]
    return sum(yearly) / len(yearly), math.prod(1 + y for y in yearly), len(df[mask])

print(f"company-dates assessed: {len(df)}")
print(f"peak margin more than 5 points above 10-yr average: {(df.spike_pts > 0.05).sum()}")
print(f"engine-flagged cyclical: {df.cyclical.sum()}")
for v in ("normal", "peak", "best3"):
    print(f"'realistic' at {v:6s} margin: {df[f'real_{v}'].sum():4d} company-dates")
flip = df.real_peak & ~df.real_best3
print(f"\nrealistic ONLY because of peak margin: {flip.sum()}")
print(f"  their avg 1y return {df[flip].ret_1y.mean():+.1%}, 3y {df[flip].ret_3y.mean():+.1%}")
print(f"  realistic under both: 1y {df[df.real_best3].ret_1y.mean():+.1%}, 3y {df[df.real_best3].ret_3y.mean():+.1%}")
print(f"  all company-dates:    1y {df.ret_1y.mean():+.1%}, 3y {df.ret_3y.mean():+.1%}")
print(df[flip & df.good][["date","ticker","peak_year","normal_margin","best3_margin","peak_margin","g_peak","g_best3","ret_1y","ret_3y"]]
      .round(3).to_string(index=False))
for v in ("peak", "best3", "normal"):
    avg, grow, n = port(df.good & df[f"real_{v}"])
    print(f"\nrule (good business + realistic at {v} margin): {avg:+.1%}/yr, $1 -> {grow:.2f}, {n} picks")

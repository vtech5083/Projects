"""Why does draft+normal beat Mac+normal? Compute every component of both business tests and
both growth tests per company-date, then swap one piece at a time."""
import importlib.util, json, math, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, ".")
import quality as mac, valuation
spec = importlib.util.spec_from_file_location("draft", "study/quality_draft_2026-11-20.py")
draft = importlib.util.module_from_spec(spec); spec.loader.exec_module(draft)
W = Path("/tmp/valuation_backtest2")
res = pd.read_csv("study/backtest2_results.csv")
rows = []
for d, g in res.groupby("date"):
    runs = json.loads((W / d / "runs" / "latest.json").read_text())
    for _, r in g.iterrows():
        rec = runs.get(r.ticker)
        if not rec or rec.get("assumptions") is None: continue
        a, p = rec["assumptions"], rec["data"]["price"]
        an = pd.read_csv(W / d / f"{r.ticker}_statements.csv", index_col="line_item")
        w = a["wacc"] if a["wacc"] is not None else float("inf")
        x = dict(date=d, ticker=r.ticker, ret_1y=r.ret_1y, ret_3y=r.ret_3y)
        try:
            mb = mac.business_check(an, a["wacc"])
            for t in mb.get("tests", []): x["m_" + t["test"]] = bool(t["met"])
            x["mac_good"] = bool(mb.get("good_business"))
        except Exception as e: x["mac_good"] = False
        try:
            db = draft.good_business(an, w)
            for k in ("Q1 cash", "Q2 returns", "Q3 profit", "Q4 debt", "Q5 owners"): x[k] = bool(db.get(k, False))
            x["draft_good"] = bool(db["good_business"])
        except Exception: x["draft_good"] = False
        try:
            x["mac_growth_ok"] = mac.growth_check(a["revenue"], rec["valuation"]["growth_price_requires"])["verdict"] == "plausible"
            x["g_engine"] = rec["valuation"]["growth_price_requires"]
        except Exception: x["mac_growth_ok"] = False
        try:
            g0 = valuation.implied_growth(a, p); x["g_draft_normal"] = g0
            x["draft_growth_ok"] = draft.share_that_grew(a["revenue"], g0) >= 0.10
        except Exception: x["draft_growth_ok"] = False
        rows.append(x)
df = pd.DataFrame(rows).fillna({c: False for c in []})
df.to_csv("study/business_test_ablation.csv", index=False)

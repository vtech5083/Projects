"""Which change moved the result: the price source (Yahoo vs IBKR) or the test definitions
(quality.py vs the 20 Nov draft)? Equal-weighted 1-year portfolios, 2014-2025."""
import importlib.util, json, math, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, ".")
import quality as mac
spec = importlib.util.spec_from_file_location("draft", "study/quality_draft_2026-11-20.py")
draft = importlib.util.module_from_spec(spec); spec.loader.exec_module(draft)
W = Path("/tmp/valuation_backtest2")

def evaluate(results_csv, runs_dir):
    res = pd.read_csv(results_csv)
    out = {}
    for version in ("mac", "draft"):
        yearly, alls, three, three_all = [], [], [], []
        for d, g in res.dropna(subset=["ret_1y"]).groupby("date"):
            runs = json.loads((W / d / runs_dir / "latest.json").read_text())
            keep = []
            for _, r in g.iterrows():
                rec = runs.get(r.ticker)
                if not rec: continue
                a = rec["assumptions"]; p = rec["data"]["price"]
                an = pd.read_csv(W / d / f"{r.ticker}_statements.csv", index_col="line_item")
                try:
                    if version == "mac":
                        ok = mac.business_check(an, a["wacc"]).get("good_business") and \
                             mac.growth_check(a["revenue"], rec["valuation"]["growth_price_requires"])["verdict"] == "plausible"
                    else:
                        w = a["wacc"] if a["wacc"] is not None else float("inf")
                        ok = draft.good_business(an, w)["good_business"] and draft.realistic_growth(a, p)["realistic"]
                except Exception:
                    ok = False
                if ok: keep.append(r)
            k = pd.DataFrame(keep)
            if len(k):
                yearly.append(k.ret_1y.mean()); alls.append(g.ret_1y.mean())
                if k.ret_3y.notna().any(): three.append(k.ret_3y.mean()); three_all.append(g.ret_3y.mean())
        out[version] = (sum(yearly) / len(yearly), math.prod(1 + y for y in yearly),
                        sum(y > a for y, a in zip(yearly, alls)), sum(alls) / len(alls),
                        sum(three) / len(three) - sum(three_all) / len(three_all))
    return out

for label, csv, runs in (("Yahoo prices", "study/backtest2_results_yahoo.csv", "runs_yahoo"),
                         ("IBKR prices", "study/backtest2_results.csv", "runs")):
    o = evaluate(csv, runs)
    for v, (avg, grow, beat, allavg, edge3) in o.items():
        print(f"{label:13s} {('Mac quality.py' if v == 'mac' else 'draft tests'):15s} rule {avg:+.1%}/yr, $1 -> {grow:.2f}, "
              f"beat all in {beat}/12 (all companies {allavg:+.1%}/yr); 3-year edge {edge3:+.1%}")

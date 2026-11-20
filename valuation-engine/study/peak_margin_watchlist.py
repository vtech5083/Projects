"""Same pressure test on today's 20-company watchlist (runs/latest.json, 17 Nov prices)."""
import importlib.util, json, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, "."); sys.path.insert(0, "study")
spec = importlib.util.spec_from_file_location("pm", "study/peak_margin_test.py")
src = Path("study/peak_margin_test.py").read_text().split("res = pd.read_csv")[0]
exec(src)
D = Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/Projects/valuation-engine")
runs = json.loads((D / "runs/latest.json").read_text())
rows = []
for t, rec in runs.items():
    a = rec.get("assumptions"); p = rec.get("data", {}).get("price")
    if not a or not p: rows.append({"ticker": t, "note": "no valuation"}); continue
    an = pd.read_csv(D / f"{t}_statements.csv", index_col="line_item")
    x = assess(a, p, an)
    w = a["wacc"] if a["wacc"] is not None else float("inf")
    x["good"] = draft.good_business(an, w)["good_business"]
    x["ticker"] = t; rows.append(x)
df = pd.DataFrame(rows)
cols = ["ticker", "good", "cyclical", "peak_year", "normal_margin", "best3_margin", "peak_margin",
        "g_normal", "g_best3", "g_peak", "real_normal", "real_best3", "real_peak"]
print(df[cols].round(3).to_string(index=False))

"""
Backtest: would our cheap-to-expensive ranking have predicted later returns?

For each formation date (31 December 2021-2024):
  1. Keep only SEC facts FILED on or before that date (no hindsight).
  2. Rebuild the statements and run the unchanged engine as of that date,
     with that date's Treasury rate and Damodaran premium.
  3. Rank companies by value / price, and record returns over the next
     1, 2 and 3 years (price change plus dividends paid).

Simplifications, stated plainly:
  - Beta is set to 1 for every company (five years of prior prices are not
    in the data for the early dates).
  - The 20 companies were picked in 2026, so the list itself carries
    hindsight (survivorship bias). This is a test of the method, not proof.
Run from the engine folder:  python3 study/backtest.py
"""
import json, os, shutil, subprocess, sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = Path("/tmp/valuation_backtest")   # scratch space; only the results file is kept
TICKERS = ["ADBE", "AMD", "AVGO", "BYND", "CAT", "COST", "DIS", "GOOG", "HD", "INTC",
           "JNJ", "LULU", "MCD", "MSFT", "NKE", "NVDA", "PFE", "PTON", "UNP", "WMT"]
# formation date -> (index into the 60 monthly closes that start Nov 2021,
#                    10-year Treasury, Damodaran implied premium at that year end)
COHORTS = {"2021-12-31": (1, 0.0151, 0.0424), "2022-12-31": (13, 0.0388, 0.0594),
           "2023-12-31": (25, 0.0388, 0.0460), "2024-12-31": (37, 0.0458, 0.0433)}

mk_now = json.loads((ROOT / "market_inputs.json").read_text())
closes = mk_now["monthly_closes"]
cik = {v["ticker"]: v["cik_str"] for v in json.loads((ROOT / "edgar_cache/company_tickers.json").read_text()).values()}


def facts_as_of(ticker, date):
    """The company's SEC facts, keeping only what was filed by `date`."""
    f = json.loads((ROOT / f"edgar_cache/facts_{cik[ticker]}.json").read_text())
    for ns in f.get("facts", {}).values():
        for tag in list(ns):
            for unit in list(ns[tag]["units"]):
                kept = [x for x in ns[tag]["units"][unit] if x.get("filed", "9999") <= date]
                if kept:
                    ns[tag]["units"][unit] = kept
                else:
                    del ns[tag]["units"][unit]
            if not ns[tag]["units"]:
                del ns[tag]
    return f


def split_factor(ticker, folder):
    """
    How many of today's shares one share at the formation date became.
    IBKR prices are adjusted for later splits; the old filings are not.
    Compare the share count the engine used then (latest quarter) with
    today's restated count for the nearest fiscal year, and accept only a
    whole split ratio; anything else is ordinary buybacks or issuance.
    """
    q = pd.read_csv(folder / f"{ticker}_quarterly.csv", index_col="line_item")
    then = q.loc["diluted_shares", "TTM"]
    now = pd.read_csv(ROOT / f"{ticker}_statements.csv", index_col="line_item").loc["diluted_shares"].dropna()
    latest_q = [c for c in q.columns if c != "TTM"][-1]
    later = [c for c in now.index if c >= latest_q] or list(now.index[-1:])
    if pd.isna(then) or then <= 0 or not later:
        return 1.0
    r = now[later[0]] / then
    k = round(r) if r >= 1 else 1 / round(1 / r)
    return float(k) if abs(r - k) / k < 0.15 else 1.0


def build(date, idx, rf, erp):
    folder = OUT / date
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    mk = json.loads(json.dumps(mk_now))
    mk.update({"as_of": date, "rates_as_of": date, "risk_free_rate": rf,
               "equity_risk_premium": erp, "beta_fixed": 1.0,
               "prices": {}, "prices_as_of": {}})
    ready = []
    for t in TICKERS:
        c = closes.get(t)
        if not c or c[idx] is None:
            continue
        ff = folder / f"facts_{t}.json"
        ff.write_text(json.dumps(facts_as_of(t, date)))
        ok = True
        for script, extra in (("edgar_statements.py", []), ("edgar_quarterly.py", ["--quarters", "8"])):
            r = subprocess.run([sys.executable, str(ROOT / script), t, "--facts-file", str(ff),
                                "--email", "x", "--out-dir", str(folder)] + extra,
                               capture_output=True, text=True)
            if "Traceback" in r.stderr or not (folder / f"{t}_statements.csv").exists():
                ok = False
        ff.unlink()
        if not ok:
            print(f"  {date} {t}: no usable filings yet")
            continue
        # price in the share units the filings used at that time
        mk["prices"][t] = c[idx] * split_factor(t, folder)
        mk["prices_as_of"][t] = date
        ready.append(t)
    (folder / "market_inputs.json").write_text(json.dumps(mk, indent=2))
    env = dict(os.environ, VALUATION_AS_OF=date + "T21:00:00")
    subprocess.run([sys.executable, str(ROOT / "run.py")] + ready, cwd=folder, env=env,
                   capture_output=True, text=True)
    return json.loads((folder / "runs/latest.json").read_text())


def dividend_per_share(ticker, start, end):
    """Dividends paid in fiscal years ending between start and end, per share (today's units)."""
    s = pd.read_csv(ROOT / f"{ticker}_statements.csv", index_col="line_item")
    total = 0.0
    for fy in s.columns:
        if start < fy <= end and pd.notna(s.loc["dividends_paid", fy]) and pd.notna(s.loc["diluted_shares", fy]):
            total += abs(s.loc["dividends_paid", fy]) / s.loc["diluted_shares", fy]
    return total


rows = []
wanted = sys.argv[1:] or list(COHORTS)          # one formation date per call keeps each run short
for date, (idx, rf, erp) in ((d, COHORTS[d]) for d in wanted):
    print(f"building {date} ...")
    res = build(date, idx, rf, erp)
    for t, r in res.items():
        v, p = r["valuation"]["base_value_per_share"], r["data"]["price"]
        row = {"date": date, "ticker": t, "gate": r["health"]["gate"], "label": r["label"],
               "value_to_price": v / p if (v is not None and p) else None}
        start_price = closes[t][idx]
        for years in (1, 2, 3):
            j = idx + 12 * years
            if j < len(closes[t]):
                end_date = str(int(date[:4]) + years) + date[4:]
                div = dividend_per_share(t, date, end_date)
                row[f"ret_{years}y"] = (closes[t][j] + div) / start_price - 1
                row[f"spy_{years}y"] = closes["SPY"][j] / closes["SPY"][idx] - 1
        rows.append(row)

df = pd.DataFrame(rows)
for date, part in df.groupby("date"):
    part.to_csv(ROOT / "study" / f"backtest_{date}.csv", index=False)
print(f"saved {len(df)} company-dates")
print(df[["date", "ticker", "gate", "label", "value_to_price", "ret_1y"]].round(2).to_string(index=False))

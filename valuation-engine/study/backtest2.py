"""
Backtest 2: a wider, fairer test of ranking by value.

Universe, chosen without hindsight: on each 31 December from 2013 to 2024,
the 100 largest US non-financial companies by revenue in the calendar year
just ended, from the SEC "frames" data (every company that filed that year).
Companies with no price history today (taken over, private, bankrupt) have
to be dropped; the number dropped is reported for each date.

For each company and date: SEC facts filed by that date only, the unchanged
engine, that year-end's Treasury rate and Damodaran premium, a beta measured
from the 60 months before the date (against SPY), and returns over the next
1, 2 and 3 years including dividends (Yahoo adjusted closes).

Usage (from the engine folder):
    python3 study/backtest2.py build   -> statements for every company-date
    python3 study/backtest2.py run     -> engine run for every date
    python3 study/backtest2.py collect -> study/backtest2_results.csv
"""
import json, os, subprocess, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from multiprocessing import Pool
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(os.environ.get("BACKTEST_WORK", "/tmp/valuation_backtest2"))
N = 100
# year end -> (10-year Treasury, Damodaran implied premium), from his histimpl table
RATES = {2013: (0.0304, 0.0496), 2014: (0.0217, 0.0578), 2015: (0.0227, 0.0612),
         2016: (0.0245, 0.0569), 2017: (0.0241, 0.0508), 2018: (0.0268, 0.0596),
         2019: (0.0192, 0.0520), 2020: (0.0093, 0.0472), 2021: (0.0151, 0.0424),
         2022: (0.0388, 0.0594), 2023: (0.0388, 0.0460), 2024: (0.0458, 0.0433)}

universe = json.loads((ROOT / "study/universe.json").read_text())


def revenue_frame(cy):
    best = {}
    for f in (ROOT / "edgar_cache/frames").glob(f"*_CY{cy}.json"):
        for x in json.loads(f.read_text())["data"]:
            if x.get("loc", "").startswith("US"):
                best[str(x["cik"])] = max(best.get(str(x["cik"]), 0), x["val"])
    return best


def prices(ticker):
    f = ROOT / "prices" / f"{ticker}.csv"
    if not f.exists():
        return None
    p = pd.read_csv(f).drop_duplicates("Date", keep="last").set_index("Date")
    return p


def cohort(year):
    """[(cik, ticker)] for the formation date at the end of `year`, plus counts."""
    rev = revenue_frame(year - 1)
    ranked = sorted(rev, key=rev.get, reverse=True)
    chosen, dropped, seen = [], [], set()
    for cik in ranked:
        u = universe.get(cik)
        if u is None:                       # financial company, or outside the yearly top 150
            continue
        t = u.get("price_ticker")
        p = prices(t) if t else None
        if t in seen:
            continue
        if p is None or f"{year}-12" not in p.index or (ROOT / f"edgar_cache/facts_{cik}.json").exists() is False:
            dropped.append(u["name"])
        else:
            chosen.append((cik, t))
            seen.add(t)
        if len(chosen) + len(dropped) >= N:
            break
    return chosen, dropped


def facts_as_of(cik, date):
    f = json.loads((ROOT / f"edgar_cache/facts_{cik}.json").read_text())
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


def build_one(job):
    cik, ticker, dates = job
    done = []
    for date in dates:
        folder = WORK / date
        folder.mkdir(parents=True, exist_ok=True)
        if (folder / f"{ticker}_quarterly.csv").exists():
            done.append(date)
            continue
        ff = folder / f"facts_{ticker}.json"
        ff.write_text(json.dumps(facts_as_of(cik, date)))
        for script, extra in (("edgar_statements.py", []), ("edgar_quarterly.py", ["--quarters", "8"])):
            subprocess.run([sys.executable, str(ROOT / script), ticker, "--facts-file", str(ff),
                            "--email", "x", "--out-dir", str(folder)] + extra,
                           capture_output=True, text=True)
        ff.unlink()
        if (folder / f"{ticker}_quarterly.csv").exists():
            done.append(date)
    return ticker, done


def split_events_for(ticker):
    """Stock splits of this company, from the restated figures in its SEC filings
    (every registration it has filed under)."""
    import edgar_statements as es
    u = json.loads((ROOT / "study/universe.json").read_text())
    gl = []
    for cik, x in u.items():
        f = ROOT / f"edgar_cache/facts_{cik}.json"
        if x.get("price_ticker") == ticker and f.exists():
            gl.append(json.loads(f.read_text())["facts"].get("us-gaap", {}))
    return es.split_events(gl)


def facts_for(ticker):
    u = json.loads((ROOT / "study/universe.json").read_text())
    return [json.loads((ROOT / f"edgar_cache/facts_{cik}.json").read_text()) for cik, x in u.items()
            if x.get("price_ticker") == ticker and (ROOT / f"edgar_cache/facts_{cik}.json").exists()]


def split_factor(ticker, date):
    """IBKR prices are on today's share basis; the filings known on `date` are on
    the basis of that time. Multiply the price by every split reported later."""
    import edgar_statements as es
    return es.split_factor_since(split_events_for(ticker), date)


def run_date(year):
    date = f"{year}-12-31"
    folder = WORK / date
    chosen = json.loads((WORK / f"cohort_{year}.json").read_text())["chosen"]
    rf, erp = RATES[year]
    mk = json.loads((ROOT / "market_inputs.json").read_text())
    mk.update({"as_of": date, "rates_as_of": date, "risk_free_rate": rf, "equity_risk_premium": erp,
               "prices": {}, "prices_as_of": {}, "monthly_closes": {}, "market_value": {}})
    spy = prices("SPY")
    months = [m for m in spy.index if m <= f"{year}-12"][-61:]
    mk["monthly_closes"]["SPY"] = spy.loc[months, "AdjClose"].tolist()
    ready = []
    for cik, t in chosen:
        if not (folder / f"{t}_quarterly.csv").exists():
            continue
        p = prices(t)
        if p is None or f"{year}-12" not in p.index:      # no price on the formation date at this source
            mk.setdefault("skipped_no_price", []).append(t)
            continue
        hist = [m for m in months if m in p.index]
        if len(hist) < 24:                    # too little history to measure beta
            mk.setdefault("beta_by_ticker", {})[t] = None
        mk["monthly_closes"][t] = p.loc[hist, "AdjClose"].tolist()
        mk["prices"][t] = float(p.loc[f"{year}-12", "Close"]) * split_factor(t, date)
        mk["prices_as_of"][t] = date
        import health_check as hc
        mk.setdefault("market_value", {})[t] = hc.market_value_inputs(facts_for(t), p, date)
        ready.append(t)
    # beta must be measured on the same months for stock and market
    for t in ready:
        p = prices(t)
        common = [m for m in months if m in p.index]
        mk["monthly_closes"][t] = p.loc[common, "AdjClose"].tolist()
    mk["monthly_closes_by_ticker_months"] = {t: len(mk["monthly_closes"][t]) for t in ready}
    (folder / "market_inputs.json").write_text(json.dumps(mk))
    env = dict(os.environ, VALUATION_AS_OF=date + "T21:00:00", VALUATION_SIM_RUNS="2000")
    r = subprocess.run([sys.executable, str(ROOT / "run.py")] + ready, cwd=folder, env=env,
                       capture_output=True, text=True)
    (folder / "run_output.txt").write_text(r.stdout[-200000:] + "\n" + r.stderr[-20000:])
    return year, len(ready)


def collect():
    import valuation
    spy = prices("SPY")
    rows = []
    for year in RATES:
        date = f"{year}-12-31"
        folder = WORK / date
        latest = folder / "runs/latest.json"
        if not latest.exists():
            continue
        res = json.loads(latest.read_text())
        mk = json.loads((folder / "market_inputs.json").read_text())
        rank = {t: i + 1 for i, (c, t) in enumerate(json.loads((WORK / f"cohort_{year}.json").read_text())["chosen"])}
        for t, r in res.items():
            a, v, p = r["assumptions"], r["valuation"]["base_value_per_share"], r["data"]["price"]
            q = pd.read_csv(folder / f"{t}_quarterly.csv", index_col="line_item")["TTM"]
            mcap = p * q.get("diluted_shares", float("nan"))
            try:
                nocap = valuation.project(dict(a, growth=min(max(a["growth_uncapped"], -0.05), 0.40)))[0] / p
            except Exception:
                nocap = float("nan")
            grid = []
            for shift in (-0.01, 0.0, 0.01):
                for prem in (0.0, 0.05):
                    try:
                        grid.append(valuation.project(dict(a, wacc=a["wacc"] + shift,
                                    wacc_mature=a["wacc_mature"] + shift, terminal_roic_premium=prem))[0] / p)
                    except Exception:
                        grid.append(float("nan"))
            fails = [c["name"] for c in r["health"]["checks"] if c["status"] == "FAIL"]
            row = {"date": date, "ticker": t, "size_rank": rank.get(t), "gate": r["health"]["gate"],
                   "fails": ";".join(fails),
                   "label": r["label"], "beta": r["cost_of_capital"].get("beta_used"),
                   "vp": v / p if v is not None and p else float("nan"), "vp_nocap": nocap,
                   **{f"vp_grid{i}": g for i, g in enumerate(grid)},
                   "fcf_yield": (q.get("cash_from_operations") - q.get("capex")) / mcap,
                   "earnings_yield": q.get("net_income") / mcap,
                   "gross_prof": q.get("gross_profit") / q.get("total_assets"),
                   "terminal_share": r["valuation"].get("share_of_value_after_year_ten")}
            px = prices(t)
            start = f"{year}-12"
            for n in (1, 2, 3):
                end = f"{year + n}-12"
                if end in px.index and end in spy.index:
                    row[f"ret_{n}y"] = px.loc[end, "AdjClose"] / px.loc[start, "AdjClose"] - 1
                    row[f"spy_{n}y"] = spy.loc[end, "AdjClose"] / spy.loc[start, "AdjClose"] - 1
            rows.append(row)
    df = pd.DataFrame(rows)
    df.to_csv(ROOT / "study/backtest2_results.csv", index=False)
    print(f"{len(df)} company-dates saved to study/backtest2_results.csv")


if __name__ == "__main__":
    step = sys.argv[1]
    if step == "build":
        jobs, report = {}, {}
        for year in RATES:
            chosen, dropped = cohort(year)
            (WORK).mkdir(parents=True, exist_ok=True)
            (WORK / f"cohort_{year}.json").write_text(json.dumps({"chosen": chosen, "dropped": dropped}))
            report[year] = (len(chosen), len(dropped))
            for cik, t in chosen:
                jobs.setdefault((cik, t), []).append(f"{year}-12-31")
        for y, (c, d) in report.items():
            print(f"{y}: {c} companies, {d} dropped for lack of price history")
        print(f"{len(jobs)} distinct companies; building statements ...", flush=True)
        with Pool(int(os.environ.get('BACKTEST_WORKERS', '2'))) as pool:
            for i, (t, done) in enumerate(pool.imap_unordered(build_one, [(c, t, d) for (c, t), d in jobs.items()])):
                if i % 20 == 0:
                    print(f"  {i + 1}/{len(jobs)} {t}: {len(done)} dates", flush=True)
        print("build finished")
    elif step == "run":
        for year in sorted(RATES):
            print(run_date(year), flush=True)
        print("run finished")
    elif step == "collect":
        collect()

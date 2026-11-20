#!/usr/bin/env python3
"""
run.py  --  One command per company. Runs every step and saves a record.

    python run.py NVDA
    python run.py AVGO MSFT GOOG LULU NVDA

Steps, in order
---------------
  1. Read the saved statements (annual and quarterly).
  2. Working capital and stock-based pay.
  3. Credit rating and discount rate.
  4. Valuation: base case, the growth the price requires, and a grid.
  5. Health check: the gate. A company that fails it gets no label.
  6. Simulation: the range of values.
  7. Save a record of all of the above.

What gets saved
---------------
  runs/<TICKER>/<date>_<time>.json   one file per run, never edited afterwards
  runs/latest.json                   the newest record for every company (the dashboard reads this)
  runs/history.csv                   one line per run, for tracking over time

Every record carries the date, the data it used, every assumption, and a
fingerprint of the engine's code, so any past result can be traced and
compared like with like.

If a company's statements are not downloaded yet, its ticker is added to
fetch_requests.txt for fetch_queue.py to pick up. No internet is used here.
"""

import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import analyze_working_capital as wc
import cost_of_capital as cc
import quality as qa
import health_check as hc
import monte_carlo as mc
import valuation as va

FOLDER = Path(".")
RUNS = FOLDER / "runs"
ENGINE_FILES = ["edgar_statements.py", "edgar_quarterly.py", "analyze_working_capital.py",
                "cost_of_capital.py", "valuation.py", "health_check.py", "monte_carlo.py", "quality.py", "run.py"]
SIM_RUNS = int(os.environ.get("VALUATION_SIM_RUNS", "10000"))   # backtests may use fewer


def engine_version():
    h = hashlib.sha256()
    for name in ENGINE_FILES:
        h.update((Path(__file__).resolve().parent / name).read_bytes())
    return h.hexdigest()[:10]


def clean(x):
    """Make a value safe to store as JSON."""
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        x = float(x)
        if math.isnan(x):
            return None
        if math.isinf(x):
            return "infinite" if x > 0 else "-infinite"
        return x
    return x


# Decision rule, from the assumption rules spec (draft 2).
MARGIN_OF_SAFETY = {0: 0.20, 1: 0.30}      # by number of risk markers; two or more -> 40%
MARGIN_OF_SAFETY_MAX = 0.40
PESSIMISTIC_FLOOR = 0.70                   # the pessimistic value must be at least this share of the price
OVERPRICED_OVER_MEDIAN = 1.20
HIGH_BETA = 1.3


def decide(price, pct, pessimistic_value, gate, markers):
    """
    Turn the range of values into a label, and say why.

    Buy candidate needs all four: no failed health check; price below the 25th
    percentile; price at least the margin of safety under the median; and a
    pessimistic-case value of at least 70% of the price. The margin of safety
    is 20%, 30% or 40% for zero, one, or two or more risk markers. Because the
    25th percentile falls as the range widens, more uncertainty demands a
    lower price.
    """
    safety = MARGIN_OF_SAFETY.get(len(markers), MARGIN_OF_SAFETY_MAX)
    median = pct[50]
    buy_below = min(pct[25], median * (1 - safety)) if median > 0 else None
    tests = [
        {"test": "No failed health check", "met": gate != "STOP",
         "detail": "health check: " + gate},
        {"test": "Price below the 25th percentile of simulated values", "met": price < pct[25],
         "detail": f"price ${price:,.2f} against ${pct[25]:,.2f}"},
        {"test": f"Price at least {safety * 100:.0f}% under the median value", "met": median > 0 and price <= median * (1 - safety),
         "detail": f"price ${price:,.2f} against a limit of ${median * (1 - safety):,.2f}" if median > 0 else "the median value is not positive"},
        {"test": f"Pessimistic-case value at least {PESSIMISTIC_FLOOR * 100:.0f}% of the price", "met": pessimistic_value >= PESSIMISTIC_FLOOR * price,
         "detail": f"pessimistic value ${pessimistic_value:,.2f} against a floor of ${PESSIMISTIC_FLOOR * price:,.2f}"},
    ]
    if gate == "STOP":
        label = "No label"
    elif all(t["met"] for t in tests):
        label = "Buy candidate"
    elif price > pct[75] and price > OVERPRICED_OVER_MEDIAN * median:
        label = "Overpriced"
    else:
        label = "Hold or watch"
    return {"label": label, "margin_of_safety": safety, "risk_markers": markers, "tests": tests,
            "tests_met": sum(1 for t in tests if t["met"]), "buy_below": buy_below,
            "overpriced_above": max(pct[75], OVERPRICED_OVER_MEDIAN * median) if median > 0 else None}


def one_column(ticker, values):
    return pd.DataFrame({ticker: pd.Series(values)})


def run_company(ticker, mk, overrides, version, now):
    statements = FOLDER / f"{ticker}_statements.csv"
    quarterly_file = FOLDER / f"{ticker}_quarterly.csv"
    if not statements.exists() or not quarterly_file.exists():
        queue = FOLDER / "fetch_requests.txt"
        waiting = queue.read_text().split() if queue.exists() else []
        if ticker not in waiting:
            with queue.open("a") as f:
                f.write(ticker + "\n")
        return None, f"{ticker}: statements not downloaded yet. Added to fetch_requests.txt."
    if ticker not in mk["prices"] or ticker not in mk["monthly_closes"]:
        return None, f"{ticker}: share price or price history missing from market_inputs.json."

    price = mk["prices"][ticker]
    annual, quarterly = wc.load(ticker, FOLDER)
    q_cols = [c for c in quarterly.columns if c != "TTM"]

    # 2. working capital and stock pay
    metrics = wc.analyse(annual, quarterly)
    same_q = wc.same_quarter(quarterly)
    latest_col = "TTM" if "TTM" in metrics.columns else metrics.columns[-1]

    # 3. discount rate
    capital = cc.analyse(ticker, FOLDER, mk)
    coc = one_column(ticker, {k: v for k, v in capital.items() if isinstance(v, (int, float))})

    # 4. valuation
    a, value_notes = va.assumptions(ticker, FOLDER, mk, coc, overrides)
    base_value, detail = va.project(a)
    required_growth = va.implied_growth(a, price)
    at_edge = required_growth in (-0.30, 1.00)
    grid_g = [a["growth"] - 0.05, a["growth"], a["growth"] + 0.05]
    grid_m = [a["target_margin"] - 0.05, a["target_margin"], a["target_margin"] + 0.05]
    grid = [[va.project(a, growth=g, target_margin=m)[0] for m in grid_m] for g in grid_g]
    # Self-check: valuing the company at the growth the price requires must give back the price.
    roundtrip = va.project(a, growth=required_growth)[0]
    roundtrip_ok = at_edge or abs(roundtrip - price) <= 0.005 * max(abs(price), 1)

    # 5. health check
    val = one_column(ticker, {"growth_ttm": a["growth_ttm"], "roic": a["roic"], "wacc": a["wacc"]})
    checks = hc.check(ticker, FOLDER, coc, val, now.date())
    # 21-22: market value against the company's reported public float; value against price
    shares_now = quarterly.loc["diluted_shares", "TTM"] if "diluted_shares" in quarterly.index else None
    checks.append(hc.market_value_check(price, shares_now, mk.get("market_value", {}).get(ticker)))
    value_row, quarantine = hc.value_check(price, base_value)
    checks.append(value_row)
    if quarantine is None and checks[-2][1] == "FAIL":
        quarantine = ("Check the data", checks[-2][2])
    fails = [c for c in checks if c[1] == "FAIL"]
    watches = [c for c in checks if c[1] == "WATCH"]
    gate = "STOP" if fails else "CAUTION" if len(watches) >= 3 else "CLEAR"

    # 5a. what the price implies, side by side with our assumptions
    implied = va.market_implied(a, price)
    ours_steady = implied["steady_state_value"]
    market_vs_ours = {
        "growth": {"market": implied["growth"], "edge": implied["growth_edge"], "ours": a["growth"]},
        "margin": {"market": implied["margin"], "edge": implied["margin_edge"], "ours": a["target_margin"],
                   "best_10yr": a["margin_high_10yr"]},
        "discount_rate": {"market": implied["discount_rate"], "edge": implied["discount_edge"], "ours": a["wacc"]},
        "growth_share": {"market": implied["growth_share_of_price"],
                         "ours": 1 - ours_steady / base_value if base_value and base_value > 0 else None},
        "steady_state_value": ours_steady,
    }

    # 5b. Vivek's rule: a good business, at a price needing only realistic growth
    business = qa.business_check(annual, a["wacc"], mk["marginal_tax_rate"])
    growth_test = qa.price_check(a, annual, price)
    vp = base_value / price if price and base_value is not None else None
    verdict, verdict_why = qa.verdict(business, growth_test, vp, quarantine, [c[0] for c in fails])

    # 6. simulation
    sd = mc.spreads(ticker, a, FOLDER, coc, mk)
    values, drawn = mc.simulate(a, sd, SIM_RUNS, np.random.default_rng(mc.SEED))
    pct = {q: float(np.percentile(values, q)) for q in (10, 25, 50, 75, 90)}
    order = pd.Series(values).rank()
    drivers = {}
    for k, v in drawn.items():
        ranks = pd.Series(v).rank()
        # An input that did not vary in this run cannot have moved the value.
        drivers[k] = float(np.corrcoef(ranks, order)[0, 1]) if ranks.std() > 0 and order.std() > 0 else 0.0

    # Scenarios, set by rule in valuation.assumptions.
    sc = a["scenarios"]
    scenario_values = {
        "pessimistic": va.project(a, growth=sc["pessimistic"]["growth"], target_margin=sc["pessimistic"]["margin"])[0],
        "base": base_value,
        "optimistic": va.project(a, growth=sc["optimistic"]["growth"], target_margin=sc["optimistic"]["margin"])[0],
    }

    # Risk markers set the margin of safety.
    markers = []
    if capital["beta_used"] > HIGH_BETA:
        markers.append(f"high beta ({capital['beta_used']:.2f})")
    if a["cyclical"]:
        markers.append(f"cyclical margins (swing of about {a['margin_swing'] * 100:.0f} points)")
    if a["peak_margin"]:
        markers.append("margin at or near a peak")
    if a["early_stage"]:
        markers.append("short history or a recent loss")
    decision = decide(price, pct, scenario_values["pessimistic"], gate, markers)
    label = decision["label"]
    label_basis = "failed the health check" if gate == "STOP" else "decision rule, spec draft 2; paper period, not for action yet"
    if detail["terminal_share"] > 0.75:
        a["flags"].append(f"{detail['terminal_share'] * 100:.0f}% of the value comes from after year ten, so it rests mostly on long-run assumptions.")

    problems = []
    if not roundtrip_ok:
        problems.append("self-check failed: the required-growth valuation does not reproduce the price")
    if base_value <= 0:
        problems.append("the base-case value is zero or negative; the valuation is not meaningful")
    if at_edge:
        problems.append("no growth rate in the range tested reproduces the price")

    record = {
        "ticker": ticker,
        "run_at": now.isoformat(timespec="seconds"),
        "engine_version": version,
        "data": {
            "latest_quarter": q_cols[-1],
            "fiscal_years": [annual.columns[0], annual.columns[-1]],
            "price": price,
            "price_date": mk.get("prices_as_of", {}).get(ticker, mk["as_of"]),
            "market_inputs_date": mk["as_of"],
            "risk_free_rate": mk["risk_free_rate"],
            "equity_risk_premium": mk["equity_risk_premium"],
            "tax_rate": mk["marginal_tax_rate"],
        },
        "market_vs_ours": market_vs_ours,
        "verdict": verdict,
        "verdict_why": verdict_why,
        "passes_rule": qa.passes_rule(verdict),
        "rule": qa.RULE_VERSION,
        "business": business,
        "growth_test": growth_test,
        "health": {
            "quarantine": quarantine[0] if quarantine else None,
            "gate": gate, "fails": len(fails), "watches": len(watches), "passes": len(checks) - len(fails) - len(watches),
            "checks": [{"name": n, "status": s, "why": w} for n, s, w in checks],
        },
        "cost_of_capital": {k: v for k, v in capital.items() if k != "history"},
        "rating_history": {y: {"coverage": c, "rating": r} for y, (c, r) in capital["history"].items()},
        "assumptions": {**a, "source": "overrides: " + ", ".join(a["overridden"]) if a["overridden"] else "mechanical defaults",
                        "notes": value_notes},
        "valuation": {
            "base_value_per_share": base_value, "price_to_value": price / base_value if base_value > 0 else None,
            "growth_price_requires": None if at_edge else required_growth,
            "value_next_ten_years": detail["pv_ten_years"], "value_after_year_ten": detail["pv_terminal"],
            "share_of_value_after_year_ten": detail["terminal_share"], "equity_value": detail["equity_value"],
            "grid": {"growth": grid_g, "margin": grid_m, "value_per_share": grid},
            "year_by_year": [dict(zip(["year", "growth", "revenue", "margin", "after_tax_profit",
                                       "reinvestment", "free_cash_flow", "discount_rate"], r)) for r in detail["rows"]],
        },
        "simulation": {
            "runs": SIM_RUNS, "seed": mc.SEED, "percentiles": pct,
            "share_of_runs_above_price": float((values > price).mean()),
            "share_of_runs_20pct_below_price": float((values < 0.8 * price).mean()),
            "ranges": {"growth": sd[0], "margin": sd[1], "discount_rate": sd[2], "reinvestment": sd[3]},
            "what_moves_the_value": drivers,
        },
        "working_capital": {
            "latest": metrics[latest_col].to_dict(),
            "same_quarter_days": None if same_q is None else {c: same_q[c].to_dict() for c in same_q.columns},
            "flags": wc.flags(metrics, ticker, same_q),
        },
        "scenarios": {k: {"growth": (a["growth"] if k == "base" else sc[k]["growth"]),
                          "margin": (a["target_margin"] if k == "base" else sc[k]["margin"]),
                          "value_per_share": v} for k, v in scenario_values.items()},
        "decision": decision,
        "label": label, "label_basis": label_basis, "problems": problems,
    }
    return clean(record), None


def save(record):
    ticker = record["ticker"]
    folder = RUNS / ticker
    folder.mkdir(parents=True, exist_ok=True)
    stamp = record["run_at"][:19].replace(":", "").replace("T", "_") + "Z"
    path = folder / f"{stamp}.json"
    if path.exists():
        raise FileExistsError(f"{path} already exists; records are never overwritten")
    path.write_text(json.dumps(record, indent=1))

    latest_file = RUNS / "latest.json"
    latest = json.loads(latest_file.read_text()) if latest_file.exists() else {}
    latest[ticker] = record
    latest_file.write_text(json.dumps(latest, indent=1))

    row = {
        "run_at": record["run_at"], "ticker": ticker, "engine_version": record["engine_version"],
        "latest_quarter": record["data"]["latest_quarter"], "price": record["data"]["price"],
        "gate": record["health"]["gate"], "fails": record["health"]["fails"], "watches": record["health"]["watches"],
        "base_value": record["valuation"]["base_value_per_share"],
        "growth_assumed": record["assumptions"]["growth"],
        "growth_price_requires": record["valuation"]["growth_price_requires"],
        "verdict": record["verdict"],
        "margin_assumed": record["assumptions"]["target_margin"], "wacc": record["assumptions"]["wacc"],
        "p10": record["simulation"]["percentiles"]["10"], "p25": record["simulation"]["percentiles"]["25"],
        "median": record["simulation"]["percentiles"]["50"], "p75": record["simulation"]["percentiles"]["75"],
        "p90": record["simulation"]["percentiles"]["90"],
        "pessimistic_value": record["scenarios"]["pessimistic"]["value_per_share"],
        "optimistic_value": record["scenarios"]["optimistic"]["value_per_share"],
        "margin_of_safety": record["decision"]["margin_of_safety"], "tests_met": record["decision"]["tests_met"],
        "label": record["label"], "label_basis": record["label_basis"], "record": str(path),
    }
    history = RUNS / "history.csv"
    pd.DataFrame([row]).to_csv(history, mode="a", header=not history.exists(), index=False)
    return path


def show(r):
    v, s, h = r["valuation"], r["simulation"], r["health"]
    pc = s["percentiles"]
    need = v["growth_price_requires"]
    print(f"\n{r['ticker']}   price ${r['data']['price']:,.2f}   data to {r['data']['latest_quarter']}")
    print(f"  Health check   {h['gate']}  ({h['fails']} fail, {h['watches']} watch, {h['passes']} pass)")
    for c in h["checks"]:
        if c["status"] == "FAIL":
            print(f"                 FAIL  {c['name']}: {c['why']}")
    print(f"  Base value     ${v['base_value_per_share']:,.2f}   "
          f"(growth {r['assumptions']['growth'] * 100:.1f}%, margin {r['assumptions']['target_margin'] * 100:.1f}%, "
          f"discount rate {r['assumptions']['wacc'] * 100:.1f}%)")
    print("  Price requires " + (f"{need * 100:.1f}% growth a year for five years" if need is not None else "no growth rate in the range tested"))
    print(f"  Range          ${pc['10']:,.0f} / ${pc['50']:,.0f} / ${pc['90']:,.0f}  (10th / median / 90th), "
          f"{s['share_of_runs_above_price'] * 100:.0f}% of runs above the price")
    sv = r["scenarios"]
    print(f"  Scenarios      ${sv['pessimistic']['value_per_share']:,.0f} / ${sv['base']['value_per_share']:,.0f} / "
          f"${sv['optimistic']['value_per_share']:,.0f}  (pessimistic / base / optimistic)")
    b, gt = r["business"], r["growth_test"]
    mv = r["market_vs_ours"]
    def show_pct(x, edge=None):
        if x is None:
            return "n/a"
        s = f"{x * 100:.1f}%"
        return ("over " if edge == "high" else "under " if edge == "low" else "") + s
    print("  Market vs us   (each market figure holds the others at our assumptions)")
    print(f"                 growth, years 1-5   market {show_pct(mv['growth']['market'], mv['growth']['edge']):>12s}   ours {show_pct(mv['growth']['ours'])}")
    print(f"                 margin by year 5    market {show_pct(mv['margin']['market'], mv['margin']['edge']):>12s}   ours {show_pct(mv['margin']['ours'])}"
          f"   (best in 10 years {show_pct(mv['margin']['best_10yr'])})")
    print(f"                 discount rate       market {show_pct(mv['discount_rate']['market'], mv['discount_rate']['edge']):>12s}   ours {show_pct(mv['discount_rate']['ours'])}")
    print(f"                 price paid for growth  {show_pct(mv['growth_share']['market'])} of the price"
          f"   (ours: {show_pct(mv['growth_share']['ours'])} of our value);"
          f" today's profit held flat is worth ${mv['steady_state_value']:,.2f} a share")
    print(f"  Verdict        {r['verdict']}: {r['verdict_why']}")
    print(f"  Good business  {'yes' if b.get('good_business') else 'no'} "
          f"({b.get('score', 0)} of {b.get('out_of', 6)} tests, {b.get('years', 0)} years of filings)")
    for t in b.get("tests", []):
        print(f"                 {'met    ' if t['met'] else 'NOT MET'}  {t['test']}: {t['why']}")
    print(f"  Growth test    {gt['verdict']}: {gt['why']}")
    dec = r["decision"]
    print(f"  Label          {r['label']}  ({r['label_basis']})")
    if r["label"] != "No label":
        print(f"                 margin of safety {dec['margin_of_safety'] * 100:.0f}%"
              + (f" for: {', '.join(dec['risk_markers'])}" if dec["risk_markers"] else " (no risk markers)"))
        for t in dec["tests"]:
            print(f"                 {'met    ' if t['met'] else 'NOT MET'}  {t['test']}: {t['detail']}")
    for f in r["assumptions"]["flags"]:
        print(f"  Note           {f}")
    for p in r["problems"]:
        print(f"  PROBLEM        {p}")


def main():
    tickers = [t.upper() for t in sys.argv[1:] if not t.startswith("-")]
    if not tickers:
        sys.exit("Usage: python run.py TICKER [TICKER ...]")
    mk = json.loads((FOLDER / "market_inputs.json").read_text())
    o_file = FOLDER / "valuation_overrides.json"
    overrides = json.loads(o_file.read_text()) if o_file.exists() else {}
    version, now = engine_version(), datetime.now(timezone.utc)
    if os.environ.get("VALUATION_AS_OF"):          # backtests: pretend it is this date
        now = datetime.fromisoformat(os.environ["VALUATION_AS_OF"]).replace(tzinfo=timezone.utc)
    print(f"Engine version {version}   run at {now:%Y-%m-%d %H:%M} UTC   market inputs as of {mk['as_of']}")
    done = 0
    for t in tickers:
        try:
            record, message = run_company(t, mk, overrides, version, now)
        except Exception as e:                     # one broken company must not stop the others
            print(f"\n{t}: could not be valued ({type(e).__name__}: {e})")
            continue
        if record is None:
            print(f"\n{message}")
            continue
        path = save(record)
        try:
            show(record)
        except (TypeError, ValueError):
            print(f"\n{t}: saved, but its summary has missing numbers (see the record)")
        print(f"  Saved          {path}")
        done += 1
    print(f"\n{done} of {len(tickers)} companies run. Records are in {RUNS}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

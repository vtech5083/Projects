#!/usr/bin/env python3
"""
monte_carlo.py  --  Analysis layer, step 4: a range of values instead of one.

valuation.py gives one value from one set of assumptions. This script runs
the same model 10,000 times, each time drawing the uncertain inputs from a
range, and reports how the resulting values are spread out.

What is varied, and how wide
----------------------------
  growth (years 1-5)   centred on the base case. Width = half the gap between
                       last-twelve-months growth and the three-year pace
                       (between 3 and 10 points): the more the two disagree,
                       the less sure we are.
  margin by year 5     centred on today's adjusted margin. Width = how much the
                       company's own margin has moved over the last five
                       fiscal years (between 2 and 8 points).
  discount rate        shifted up or down. Width comes from how imprecisely
                       beta was measured (at least half a point).
  reinvestment         revenue per dollar reinvested, varied by about 25%.

The inputs are not drawn independently (see CORRELATIONS below): a company
that grows faster than expected usually also earns better margins, and
periods of high interest rates tend to go with slower growth.

What the output means
---------------------
The percentages are statements about THESE ASSUMPTIONS, not about the future.
"70% of runs are above the price" means: if my ranges are right, the model
values the company above its price in 70% of cases. If the ranges are wrong,
so is the percentage. The same seed is used every run, so results repeat.

Buy / Hold / Sell bands follow the rule chosen in the design document:
  Strong Buy  price below the 10th percentile of values
  Buy         between the 10th and 25th
  Hold        between the 25th and 75th
  Sell        above the 75th

Run cost_of_capital.py first. No internet needed.

Usage
-----
    python monte_carlo.py AVGO MSFT GOOG LULU
    python monte_carlo.py AVGO --runs 50000
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from valuation import assumptions, project

SEED = 20261110
#                growth  margin  discount
CORRELATIONS = [[1.0,    0.3,   -0.2],     # growth
                [0.3,    1.0,    0.0],     # margin
                [-0.2,   0.0,    1.0]]     # discount rate


def spreads(ticker, a, folder, coc, mk):
    """How wide to draw each input, taken from the company's own data."""
    annual = pd.read_csv(folder / f"{ticker}_statements.csv", index_col="line_item")
    gap = abs(a["growth_ttm"] - a["growth_3yr"]) / 2 if not (pd.isna(a["growth_ttm"]) or pd.isna(a["growth_3yr"])) else 0.05
    sd_growth = float(np.clip(gap, 0.03, 0.10))
    hist = (annual.loc["operating_income"] / annual.loc["revenue"]).dropna().iloc[-5:]
    sd_margin = float(np.clip(hist.std() if len(hist) >= 3 else 0.04, 0.02, 0.08))
    beta_err = float(coc.loc["beta_std_error", ticker])
    sd_discount = max(0.005, 0.67 * beta_err * mk["equity_risk_premium"])
    return sd_growth, sd_margin, sd_discount, 0.25


def simulate(a, sd, runs, rng):
    sd_g, sd_m, sd_w, sd_s = sd
    z = rng.standard_normal((runs, 3)) @ np.linalg.cholesky(np.array(CORRELATIONS)).T
    z = np.clip(z, -3, 3)
    growth = np.clip(a["growth"] + sd_g * z[:, 0], -0.20, 0.60)
    # A loss-making company keeps its negative margin; only a profitable one is floored at 2%.
    floor = 0.02 if a["target_margin"] > 0.02 else a["target_margin"] - 3 * sd_m
    margin = np.clip(a["target_margin"] + sd_m * z[:, 1], floor, 0.80)
    shift = sd_w * z[:, 2]
    s2c = np.clip(a["sales_to_capital"] * np.exp(sd_s * np.clip(rng.standard_normal(runs), -3, 3)), 0.3, 6.0)
    values = np.empty(runs)
    for i in range(runs):
        b = dict(a, wacc=a["wacc"] + shift[i], wacc_mature=a["wacc_mature"] + shift[i],
                 sales_to_capital=s2c[i])
        values[i] = project(b, growth=growth[i], target_margin=margin[i])[0]
    return values, {"growth": growth, "margin": margin, "discount shift": shift, "reinvestment": s2c}


def band(price, p):
    if price < p[10]:
        return "Strong Buy"
    if price < p[25]:
        return "Buy"
    if price <= p[75]:
        return "Hold"
    return "Sell"


def main():
    args = sys.argv[1:]
    runs = 10000
    if "--runs" in args:
        i = args.index("--runs")
        runs = int(args[i + 1])
        del args[i:i + 2]
    tickers = [t.upper() for t in args]
    if not tickers:
        sys.exit("Usage: python monte_carlo.py TICKER [TICKER ...] [--runs N]")
    folder = Path(".")
    mk = json.loads((folder / "market_inputs.json").read_text())
    coc = pd.read_csv(folder / "cost_of_capital.csv", index_col="metric")
    o_file = folder / "valuation_overrides.json"
    overrides = json.loads(o_file.read_text()) if o_file.exists() else {}

    print(f"{runs:,} runs per company, seed {SEED}, market inputs as of {mk['as_of']}")
    out = {}
    for t in tickers:
        a, _ = assumptions(t, folder, mk, coc, overrides)
        sd = spreads(t, a, folder, coc, mk)
        rng = np.random.default_rng(SEED)
        values, drawn = simulate(a, sd, runs, rng)
        price = mk["prices"][t]
        p = {q: float(np.percentile(values, q)) for q in (10, 25, 50, 75, 90)}
        above = float((values > price).mean())
        far_below = float((values < 0.8 * price).mean())
        verdict = band(price, p)

        # Which input moves the value most (rank correlation with the result)?
        order = pd.Series(values).rank()
        drivers = {k: float(np.corrcoef(pd.Series(v).rank(), order)[0, 1]) for k, v in drawn.items()}

        print(f"\n{'=' * 72}\n{t}   share price ${price:,.2f}\n")
        print(f"  Ranges drawn (one standard deviation)")
        print(f"    growth        {a['growth'] * 100:5.1f}%  +/- {sd[0] * 100:.1f} points")
        print(f"    margin        {a['target_margin'] * 100:5.1f}%  +/- {sd[1] * 100:.1f} points")
        print(f"    discount rate {a['wacc'] * 100:5.1f}%  +/- {sd[2] * 100:.1f} points")
        print(f"    reinvestment  {a['sales_to_capital']:5.2f}   +/- {sd[3] * 100:.0f}%")
        print(f"  Value per share across the runs")
        print("    " + "   ".join(f"{q}th: ${p[q]:,.0f}" for q in (10, 25, 50, 75, 90)))
        print(f"  Runs valuing it above today's price      {above * 100:.0f}%")
        print(f"  Runs valuing it 20%+ below today's price {far_below * 100:.0f}%")
        print(f"  Median value against price               {(p[50] / price - 1) * 100:+.0f}%")
        print(f"  Bands   Strong Buy below ${p[10]:,.0f} | Buy ${p[10]:,.0f}-${p[25]:,.0f} | "
              f"Hold ${p[25]:,.0f}-${p[75]:,.0f} | Sell above ${p[75]:,.0f}")
        print(f"  Today's price falls in: {verdict}")
        print("  What moves the value most: " + ", ".join(
            f"{k} ({v:+.2f})" for k, v in sorted(drivers.items(), key=lambda kv: -abs(kv[1]))))

        out[t] = {"share_price": price, "p10": p[10], "p25": p[25], "median": p[50], "p75": p[75],
                  "p90": p[90], "share_of_runs_above_price": above,
                  "share_of_runs_20pct_below_price": far_below, "band": verdict,
                  "sd_growth": sd[0], "sd_margin": sd[1], "sd_discount": sd[2], "runs": runs,
                  "seed": SEED, "as_of": mk["as_of"]}
    pd.DataFrame(out).to_csv(folder / "monte_carlo.csv", index_label="metric")
    print("\nSaved monte_carlo.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())

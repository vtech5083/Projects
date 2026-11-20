"""
quality.py -- the two tests behind "pay only for a good business, and never
for growth that is almost never achieved".

1. Good business (consistency over up to ten fiscal years, at least five):
   Q1 cash      free cash flow positive in at least 80% of years
   Q2 returns   return on capital above the cost of capital in at least 70% of years
   Q3 profit    operating profit positive in every year but at most one
   Q4 debt      net debt no more than 4 years of average free cash flow (last 3 years)
   Q5 owners    share count grows no more than 1% a year over five years
   A good business passes all five.

2. Realistic growth: the five-year revenue growth today's price requires,
   computed with the most generous margin the company has shown (its ten-year
   high or today's, whichever is higher), compared with how often US companies
   of the same size actually grew that fast (base rates from SEC data,
   2011-2024, study/base_rates.json). The price fails if fewer than 10% of
   companies that size ever did it.
"""
import json
import math
from pathlib import Path

import pandas as pd

NAN = float("nan")
BASE_RATES = json.loads((Path(__file__).resolve().parent / "base_rates.json").read_text())
SIZE_BUCKETS = [(1e10, "$1-10B"), (5e10, "$10-50B"), (1e11, "$50-100B"), (float("inf"), "over $100B")]
GROWTH_PERCENTILES = ["median", "p75", "p90", "p95"]
LIMITS = {"fcf_share": 0.80, "roic_share": 0.70, "losses_allowed": 1,
          "debt_years": 4.0, "dilution": 0.01, "min_years": 5, "rare_growth": 0.10}


def num(s, k):
    v = s.get(k, NAN)
    return NAN if pd.isna(v) else float(v)


def good_business(annual, wacc, tax=0.25):
    """annual: the engine's <T>_statements.csv as a DataFrame (items x fiscal years)."""
    years = list(annual.columns)[-10:]
    out, ok = {}, True
    if len(years) < LIMITS["min_years"]:
        return {"good_business": False, "reason": f"only {len(years)} years of filings"}
    fcf = [num(annual[y], "cash_from_operations") - num(annual[y], "capex") for y in years]
    fcf = [v for v in fcf if not math.isnan(v)]
    op = [num(annual[y], "operating_income") for y in years]
    op = [v for v in op if not math.isnan(v)]
    roic = []
    for y in years:
        c = annual[y]
        ic = num(c, "total_equity") + num(c, "total_debt") - num(c, "cash") - (num(c, "short_term_investments") if not math.isnan(num(c, "short_term_investments")) else 0)
        if ic > 0 and not math.isnan(num(c, "operating_income")):
            roic.append(num(c, "operating_income") * (1 - tax) / ic)
    tests = {
        "Q1 cash": (sum(v > 0 for v in fcf) / len(fcf) >= LIMITS["fcf_share"]) if fcf else False,
        "Q2 returns": (sum(r > wacc for r in roic) / len(roic) >= LIMITS["roic_share"]) if roic else False,
        "Q3 profit": (sum(v <= 0 for v in op) <= LIMITS["losses_allowed"]) if op else False,
    }
    last = annual[years[-1]]
    net_debt = num(last, "total_debt") - num(last, "cash") - (num(last, "short_term_investments") if not math.isnan(num(last, "short_term_investments")) else 0)
    avg_fcf = sum(fcf[-3:]) / len(fcf[-3:]) if fcf else NAN
    tests["Q4 debt"] = bool(net_debt <= 0 or (avg_fcf > 0 and net_debt / avg_fcf <= LIMITS["debt_years"]))
    sh = [num(annual[y], "diluted_shares") for y in years[-6:]]
    sh = [v for v in sh if not math.isnan(v) and v > 0]
    # yearly changes, leaving out a jump by a whole split ratio the filings did not tag
    ratios = []
    for a0, a1 in zip(sh, sh[1:]):
        r = a1 / a0
        k = r if r >= 1 else 1 / r
        if not (k >= 1.8 and abs(k - round(k)) / round(k) < 0.06):
            ratios.append(r)
    growth = math.prod(ratios) ** (1 / len(ratios)) - 1 if ratios else NAN
    tests["Q5 owners"] = bool(not math.isnan(growth) and growth <= LIMITS["dilution"])
    out.update(tests)
    out["passes"] = sum(bool(v) for v in tests.values())
    out["good_business"] = all(tests.values())
    out["roic_median"] = sorted(roic)[len(roic) // 2] if roic else NAN
    return out


def size_bucket(revenue):
    for limit, name in SIZE_BUCKETS:
        if revenue < limit:
            return name
    return SIZE_BUCKETS[-1][1]


def share_that_grew(revenue, growth):
    """Roughly what share of US companies this size grew at least `growth` a year for five years."""
    br = BASE_RATES[size_bucket(revenue)]
    points = sorted([(0.10, br["ge10"]), (0.15, br["ge15"]), (0.20, br["ge20"]),
                     (0.25, br["ge25"]), (0.35, br["ge35"]), (br["median"], 0.5)])
    if growth <= points[0][0]:
        return max(points[0][1], 0.5 if growth <= br["median"] else points[0][1])
    for (g0, s0), (g1, s1) in zip(points, points[1:]):
        if g0 <= growth <= g1:
            return s0 + (s1 - s0) * (growth - g0) / (g1 - g0)
    return points[-1][1]


def realistic_growth(a, price):
    """Growth the price requires at the most generous margin, and how rare that growth is."""
    import valuation
    generous = max(a.get("margin_high_10yr") or a["margin"], a["margin"], a["target_margin"])
    g = valuation.implied_growth(dict(a, target_margin=generous), price)
    share = share_that_grew(a["revenue"], g)
    return {"growth_required_generous": g, "margin_used": generous,
            "share_of_peers_that_grew_that_fast": share,
            "realistic": share >= LIMITS["rare_growth"]}

"""
quality.py -- the two tests behind Vivek's rule:
  1. Is the growth the share price needs realistic for a company this size?
  2. Is this a good business (cash and profit, year after year), so that a low
     price is not a value trap?

Both use only the company's filings and the base rates in base_rates.json.
"""
import bisect
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent

# ---- test 1: growth the price needs, against what companies this size did ----
PLAUSIBLE_SHARE = 0.10     # at least 1 in 10 companies this size grew that fast: plausible
STRETCHED_SHARE = 0.02     # 1 in 50 to 1 in 10: stretched; rarer than 1 in 50: implausible

_rates = None


def _base_rates():
    global _rates
    if _rates is None:
        _rates = json.loads((ROOT / "base_rates.json").read_text())["groups"]
    return _rates


def growth_check(revenue, required_growth):
    """How many companies of this size grew at least `required_growth` a year for five years."""
    groups = _base_rates()
    name = next((n for n, g in groups.items() if g["min_revenue"] <= revenue < g["max_revenue"]), "$1-10B")
    pcts = groups[name]["percentiles"]                       # 1st..99th percentile
    if required_growth is None:
        return {"size_group": name, "share_achieving": None, "verdict": "not measurable",
                "why": "no growth rate reproduces the price"}
    below = bisect.bisect_left(pcts, required_growth)        # percentiles under the required growth
    share = max(0.0, 1 - (below + 0.5) / 100) if below < 99 else 0.005
    if required_growth <= pcts[49]:
        share = max(share, 0.5)
    verdict = ("plausible" if share >= PLAUSIBLE_SHARE else
               "stretched" if share >= STRETCHED_SHARE else "implausible")
    return {"size_group": name, "share_achieving": share, "verdict": verdict,
            "median_growth": pcts[49], "growth_90th": pcts[89],
            "why": (f"the price needs {required_growth * 100:.1f}% revenue growth a year for five years; "
                    f"about {share * 100:.0f}% of US companies with revenue of {name} have done that "
                    f"(median {pcts[49] * 100:.1f}%, 90th percentile {pcts[89] * 100:.1f}%)")}


# ---- test 2: good business, judged over up to ten fiscal years -------------
MIN_YEARS = 5


def _row(annual, item):
    return annual.loc[item] if item in annual.index else pd.Series(float("nan"), index=annual.columns)


def business_check(annual, wacc, tax=0.25):
    if wacc is None or pd.isna(wacc):      # cost of capital not measured: returns test cannot pass
        wacc = float("inf")
    years = list(annual.columns)[-10:]
    a = annual[years]
    rev, op = _row(a, "revenue"), _row(a, "operating_income")
    fcf = _row(a, "free_cash_flow")
    cash = _row(a, "cash").fillna(0) + _row(a, "short_term_investments").fillna(0)
    ic = _row(a, "total_equity") + _row(a, "total_debt").fillna(0) - cash
    shares = _row(a, "diluted_shares")
    n = int(rev.notna().sum())
    tests = []

    def add(name, ok, why, must=False):
        tests.append({"test": name, "met": bool(ok), "why": why, "must_have": must})

    if n < MIN_YEARS:
        return {"years": n, "good_business": False, "score": 0, "tests": [],
                "why": f"only {n} fiscal years of filings; at least {MIN_YEARS} are needed"}

    f = fcf.dropna()
    add("generates cash", len(f) and (f > 0).mean() >= 0.8,
        f"free cash flow positive in {int((f > 0).sum())} of {len(f)} years", must=True)
    o = op.dropna()
    add("stays profitable", len(o) and (o > 0).mean() >= 0.9,
        f"operating profit positive in {int((o > 0).sum())} of {len(o)} years", must=True)
    roic = (op * (1 - tax)) / ic
    paired = [(r, i, p) for r, i, p in zip(roic, ic, op) if pd.notna(p)]
    good = sum(1 for r, i, p in paired if (pd.notna(i) and i <= 0 and p > 0) or (pd.notna(r) and r > wacc))
    add("earns more than its cost of capital", paired and good / len(paired) >= 0.7,
        f"return on capital above {wacc * 100:.1f}% in {good} of {len(paired)} years")
    m = (op / rev).dropna()
    add("steady margins", len(m) >= MIN_YEARS and m.std() <= 0.08,
        f"operating margin {m.min() * 100:.0f}% to {m.max() * 100:.0f}% (spread {m.std() * 100:.1f} points)")
    recent_fcf = f.iloc[-3:].mean() if len(f) else float("nan")
    net_debt = _row(a, "total_debt").fillna(0).iloc[-1] - cash.iloc[-1]
    add("debt it can repay", net_debt <= 0 or (recent_fcf > 0 and net_debt / recent_fcf <= 4),
        "net cash" if net_debt <= 0 else
        (f"net debt is {net_debt / recent_fcf:.1f} years of free cash flow" if recent_fcf > 0
         else "net debt with no free cash flow"))
    s = shares.dropna()
    growth = (s.iloc[-1] / s.iloc[max(0, len(s) - 6)]) ** (1 / min(5, len(s) - 1)) - 1 if len(s) >= 3 else 0.0
    add("does not keep issuing shares", growth <= 0.01,
        f"share count changed {growth * 100:+.1f}% a year over the last {min(5, len(s) - 1)} years")
    score = sum(t["met"] for t in tests)
    must = all(t["met"] for t in tests if t["must_have"])
    return {"years": n, "score": score, "out_of": len(tests), "good_business": must and score >= 5,
            "tests": tests}


def verdict(business, growth, value_to_price):
    """Vivek's rule: a good business, at a price that needs only realistic growth."""
    if not business.get("good_business"):
        return "Avoid", "not a good business on its ten-year record (value-trap risk)"
    g = growth.get("verdict")
    if g == "implausible":
        return "Too expensive", "a good business, but the price needs growth almost no company this size has achieved"
    if g == "stretched":
        return "Watch", "a good business; the price needs growth only a few companies this size have achieved"
    if g == "plausible":
        if value_to_price is not None and value_to_price >= 1:
            return "Candidate", "a good business at a price below our base-case value, needing only realistic growth"
        return "Fair", "a good business at a price that needs only realistic growth, above our base-case value"
    return "Watch", "the growth test could not be measured"

"""
quality.py -- Vivek's rule, frozen 20 Nov 2026 ("rule v1").

    Buy-list candidate = a good business AT a price that needs only realistic growth.

Written down from first principles before the backtest was rerun; it is not
to be tuned on backtest results. Changes only for bugs, recorded in the
assumption rules spec with a date.

  1. Good business -- all five, over the last ten fiscal years (at least five):
       cash        free cash flow (the engine's: cash from operations - capex)
                   positive in at least 80% of years
       profit      operating profit positive in at least 90% of years
       returns     return on capital above the cost of capital in at least 70% of
                   years; a profitable year with more cash than equity + debt
                   (invested capital zero or negative) counts as a pass
       debt        net debt no more than 4 years of free cash flow (latest three
                   years' average), or net cash
       owners      share count grows no more than 1% a year over five years,
                   leaving out a jump by a whole stock-split ratio
     (The old "steady margins" test is dropped; one great year is handled by 2.)

  2. Realistic price -- the yearly revenue growth for five years that makes our
     value equal the price, computed at the company's best-three-year average
     operating margin (or its normal margin if higher), must have been reached
     by at least 10% of US companies of the same size (base_rates.json, SEC
     data 2011-2024). The normal-margin answer is shown beside it, not used.

  3. Data gate -- a failed health check blocks the verdict ("Failed health
     check"); and the health check holds back any company whose data or value
     cannot be trusted (value more than 5x the price, no value, value zero or
     negative, or market value that does not match the company's reported
     public float). Those get "Check the data" or "Model does not fit", never a buy.
"""
import bisect
import json
import math
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
RULE_VERSION = "rule v1 (frozen 2026-11-20)"

LIMITS = {"min_years": 5, "years": 10, "cash_share": 0.80, "profit_share": 0.90,
          "returns_share": 0.70, "debt_years": 4, "dilution": 0.01, "dilution_years": 5,
          "plausible_share": 0.10, "stretched_share": 0.02}
PLAUSIBLE_SHARE = LIMITS["plausible_share"]
STRETCHED_SHARE = LIMITS["stretched_share"]
SPLIT_RATIOS = (1.5, 2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40, 50, 100)

_rates = None


def _base_rates():
    global _rates
    if _rates is None:
        _rates = json.loads((ROOT / "base_rates.json").read_text())["groups"]
    return _rates


def _row(annual, item):
    return annual.loc[item] if item in annual.index else pd.Series(float("nan"), index=annual.columns)


# ---- 1. good business -------------------------------------------------------
def _looks_like_split(r):
    k = r if r >= 1 else 1 / r
    return any(abs(k - s) / s < 0.06 for s in SPLIT_RATIOS)


def business_check(annual, wacc, tax=0.25):
    if wacc is None or pd.isna(wacc):      # cost of capital not measured: returns test cannot pass
        wacc = float("inf")
    years = list(annual.columns)[-LIMITS["years"]:]
    a = annual[years]
    rev, op = _row(a, "revenue"), _row(a, "operating_income")
    fcf = _row(a, "free_cash_flow")
    cash = _row(a, "cash").fillna(0) + _row(a, "short_term_investments").fillna(0)
    ic = _row(a, "total_equity") + _row(a, "total_debt").fillna(0) - cash
    n = int(rev.notna().sum())
    tests = []

    def add(name, ok, why):
        tests.append({"test": name, "met": bool(ok), "why": why, "must_have": True})

    if n < LIMITS["min_years"]:
        return {"years": n, "good_business": False, "score": 0, "out_of": 5, "tests": [],
                "rule": RULE_VERSION,
                "why": f"only {n} fiscal years of filings; at least {LIMITS['min_years']} are needed"}

    f = fcf.dropna()
    add("generates cash", len(f) and (f > 0).mean() >= LIMITS["cash_share"],
        f"free cash flow positive in {int((f > 0).sum())} of {len(f)} years")
    o = op.dropna()
    add("stays profitable", len(o) and (o > 0).mean() >= LIMITS["profit_share"],
        f"operating profit positive in {int((o > 0).sum())} of {len(o)} years")
    roic = (op * (1 - tax)) / ic
    paired = [(r, i, p) for r, i, p in zip(roic, ic, op) if pd.notna(p)]
    good = sum(1 for r, i, p in paired if (pd.notna(i) and i <= 0 and p > 0) or (pd.notna(r) and i > 0 and r > wacc))
    add("earns more than its cost of capital", paired and good / len(paired) >= LIMITS["returns_share"],
        f"return on capital above {wacc * 100:.1f}% in {good} of {len(paired)} years"
        " (a profitable year with more cash than capital counts as a pass)")
    recent_fcf = f.iloc[-3:].mean() if len(f) else float("nan")
    net_debt = _row(a, "total_debt").fillna(0).iloc[-1] - cash.iloc[-1]
    add("debt it can repay", net_debt <= 0 or (recent_fcf > 0 and net_debt / recent_fcf <= LIMITS["debt_years"]),
        "net cash" if net_debt <= 0 else
        (f"net debt is {net_debt / recent_fcf:.1f} years of free cash flow" if recent_fcf > 0
         else "net debt with no free cash flow"))
    s = _row(a, "diluted_shares").dropna()
    s = s[s > 0].iloc[-(LIMITS["dilution_years"] + 1):]
    ratios = [s.iloc[i + 1] / s.iloc[i] for i in range(len(s) - 1)]
    kept = [r for r in ratios if not _looks_like_split(r)]
    growth = math.prod(kept) ** (1 / len(kept)) - 1 if kept else float("nan")
    note = " (a jump matching a stock split was left out)" if len(kept) < len(ratios) else ""
    add("does not keep issuing shares", pd.notna(growth) and growth <= LIMITS["dilution"],
        f"share count changed {growth * 100:+.1f}% a year over the last {len(ratios)} years{note}"
        if pd.notna(growth) else "share history not available")
    score = sum(t["met"] for t in tests)
    return {"years": n, "score": score, "out_of": len(tests), "good_business": score == len(tests),
            "tests": tests, "rule": RULE_VERSION}


# ---- 2. realistic price -----------------------------------------------------
def best3_margin(a, annual):
    """Average of the company's three best operating margins in the last ten years,
    on the engine's adjusted basis (R&D and leases added back), or its normal
    margin if that is higher. One spike year cannot carry the verdict."""
    years = list(annual.columns)[-LIMITS["years"]:]
    rev, op = _row(annual[years], "revenue"), _row(annual[years], "operating_income")
    m = (op / rev)[rev > 0].dropna()
    offset = (a.get("margin") or 0) - (a.get("margin_reported") or 0)
    if len(m) < 3:
        return a["target_margin"], "fewer than three years of margins; normal margin used"
    best = m.sort_values().iloc[-3:]
    value = float(best.mean() + offset)
    if value < a["target_margin"]:
        return a["target_margin"], "normal margin (above the best-three-year average)"
    yrs = ", ".join(sorted(str(c)[:4] for c in best.index))
    return value, f"average of the best three years ({yrs})"


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


def price_check(a, annual, price):
    """Growth the price needs at the best-three-year margin, judged against base rates;
    the normal-margin answer alongside as a stricter second opinion."""
    import valuation as va
    margin, how = best3_margin(a, annual)
    g = va.implied_growth(dict(a, target_margin=margin), price)
    out = growth_check(a["revenue"], g)
    out.update({"margin_used": margin, "margin_basis": how, "growth_required": g, "rule": RULE_VERSION})
    if g <= -0.30:      # the search floor: the price needs no growth at all
        out["why"] = "the price needs no growth: even a 30% yearly revenue decline would be worth more than the price"
    elif g >= 1.00:     # the search ceiling: more than 100% a year
        out.update({"verdict": "implausible", "share_achieving": 0.0,
                    "why": "the price needs more than 100% revenue growth a year for five years"})
    g0 = va.implied_growth(a, price)
    out["normal_margin"] = {"margin": a["target_margin"],
                            "growth_required": None if g0 <= -0.30 or g0 >= 1.00 else g0,
                            "verdict": growth_check(a["revenue"], None if g0 <= -0.30 or g0 >= 1.00 else g0)["verdict"]}
    return out


# ---- the verdict -------------------------------------------------------------
def verdict(business, growth, value_to_price, quarantine=None, health_fails=None):
    """Vivek's rule: a good business, at a price that needs only realistic growth.
    'Candidate' and 'Fair' both pass the rule; 'Candidate' is also below our base value.
    The data gate comes first: a quarantine, then any failed health check."""
    if quarantine:
        kind, why = quarantine
        return kind, why
    if health_fails:
        return "Failed health check", "the health check failed on: " + ", ".join(health_fails)
    if not business.get("good_business"):
        failed = [t["test"] for t in business.get("tests", []) if not t["met"]]
        return "Avoid", ("not a good business on its ten-year record (value-trap risk)"
                         + (": fails " + ", ".join(failed) if failed else ""))
    g = growth.get("verdict")
    if g == "implausible":
        return "Too expensive", "a good business, but the price needs growth almost no company this size has achieved"
    if g == "stretched":
        return "Too expensive", "a good business; the price needs growth only a few companies this size have achieved"
    if g == "plausible":
        if value_to_price is not None and value_to_price >= 1:
            return "Candidate", "a good business at a price below our base-case value, needing only realistic growth"
        return "Fair", "a good business at a price that needs only realistic growth, above our base-case value"
    return "Check the data", "the growth the price needs could not be measured"


def passes_rule(verdict_label):
    return verdict_label in ("Candidate", "Fair")

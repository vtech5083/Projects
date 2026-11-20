#!/usr/bin/env python3
"""
tests.py  --  Checks that the engine's arithmetic does what it claims.

Each test uses numbers small enough to verify by hand, or checks a property
that must always hold (a higher discount rate must lower the value).
Run this after any change to the engine:

    python tests.py
"""

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import cost_of_capital as cc
import edgar_quarterly as eq
import edgar_statements as es
import run
import valuation as va

RESULTS = []


def test(name):
    def wrap(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except Exception as error:  # report every failure, keep going
            RESULTS.append((name, False, f"{type(error).__name__}: {error}"))
        return fn
    return wrap


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


BASE = {"revenue": 1000.0, "margin": 0.20, "target_margin": 0.20, "rd_uplift": 0.0, "growth": 0.10,
        "terminal_growth": 0.04, "sales_to_capital": 2.0, "tax_rate": 0.25, "wacc": 0.10,
        "wacc_mature": 0.09, "terminal_roic_premium": 0.0, "cash": 50.0, "debt": 150.0, "shares": 10.0}


# ---- R&D as an investment ---------------------------------------------------
@test("R&D: steady spending leaves profit unchanged")
def _():
    asset, amort, full = va.research([100] * 6, 5)
    assert close(amort, 100) and close(asset, 300) and full   # 100 + 100*(4+3+2+1)/5


@test("R&D: growing spending raises profit by spend minus write-off")
def _():
    asset, amort, _ = va.research([10, 20, 30, 40, 50, 60], 5)
    assert close(amort, (50 + 40 + 30 + 20 + 10) / 5)             # 30
    assert close(asset, 60 + 50 * .8 + 40 * .6 + 30 * .4 + 20 * .2 + 10 * 0)


@test("R&D: short history is reported as incomplete")
def _():
    assert va.research([100, 100], 5)[2] is False


# ---- the valuation model ----------------------------------------------------
@test("valuation: matches an independent year-by-year calculation")
def _():
    a = BASE
    rev, disc, pv = a["revenue"], 1.0, 0.0
    for year in range(1, 11):
        fade = max(0, year - 5) / 5
        g = a["growth"] + (a["terminal_growth"] - a["growth"]) * fade
        w = a["wacc"] + (a["wacc_mature"] - a["wacc"]) * fade
        new = rev * (1 + g)
        nopat = new * a["margin"] * (1 - a["tax_rate"])
        fcf = nopat - (new - rev) / a["sales_to_capital"]
        disc /= 1 + w
        pv += fcf * disc
        rev = new
    nopat11 = nopat * 1.04
    tv = nopat11 * (1 - 0.04 / 0.09) / (0.09 - 0.04)
    expected = (pv + tv * disc + a["cash"] - a["debt"]) / a["shares"]
    assert close(va.project(a)[0], expected, 1e-9), (va.project(a)[0], expected)


@test("valuation: a higher margin raises the value")
def _():
    assert va.project(BASE, target_margin=0.25)[0] > va.project(BASE)[0] > va.project(BASE, target_margin=0.15)[0]


@test("valuation: a higher discount rate lowers the value")
def _():
    assert va.project(dict(BASE, wacc=0.12, wacc_mature=0.11))[0] < va.project(BASE)[0]


@test("valuation: more debt lowers the value one for one")
def _():
    assert close(va.project(dict(BASE, debt=250.0))[0], va.project(BASE)[0] - 100.0 / 10.0)


@test("valuation: the R&D uplift is not taxed twice")
def _():
    with_uplift = dict(BASE, margin=0.25, target_margin=0.25, rd_uplift=0.05)
    year1 = va.project(with_uplift)[1]["rows"][0]
    assert close(year1[4], year1[2] * (0.20 * 0.75 + 0.05))      # after-tax profit in year 1


@test("valuation: the growth the price requires gives back the price")
def _():
    target = va.project(BASE, growth=0.17)[0]
    assert close(va.implied_growth(BASE, target), 0.17, 1e-6)


@test("valuation: slow growers do not speed up in the long run")
def _():
    rows = va.project(BASE, growth=0.01)[1]["rows"]
    assert close(rows[-1][1], 0.02)                              # fades to 2%, not 4%


# ---- credit rating and discount rate ---------------------------------------
TABLE = json.loads(Path("market_inputs.json").read_text())["rating_table"]


@test("rating: coverage maps to the right band at the edges")
def _():
    assert cc.rating_for(8.5, TABLE)[0] == "Aaa/AAA" and cc.rating_for(8.49, TABLE)[0] == "Aa2/AA"
    assert cc.rating_for(2.49, TABLE)[0] == "Ba1/BB+" and cc.rating_for(-5, TABLE)[0] == "D2/D"


@test("rating: leases lower the coverage ratio")
def _():
    no_lease = cc.coverage_and_rating(100, 20, 0, 0.05, TABLE)[0]
    with_lease = cc.coverage_and_rating(100, 20, 500, 0.05, TABLE)[0]
    assert close(no_lease, 5.0) and with_lease < no_lease


@test("beta: a stock that moves exactly with the market has a beta of 1")
def _():
    m = [100 * 1.01 ** i * (1 + 0.03 * math.sin(i)) for i in range(61)]
    assert close(cc.beta_from_prices(m, m)[0], 1.0, 1e-9)


@test("beta: twice the market's moves gives a beta of 2")
def _():
    rng = np.random.default_rng(1)
    r = rng.normal(0.01, 0.04, 60)
    market = np.concatenate([[100], 100 * np.cumprod(1 + r)])
    stock = np.concatenate([[50], 50 * np.cumprod(1 + 2 * r)])
    assert close(cc.beta_from_prices(list(stock), list(market))[0], 2.0, 1e-9)


# ---- reading the filings ----------------------------------------------------
def entry(start, end, val, form="10-K", filed="2025-01-01"):
    e = {"end": end, "val": val, "form": form, "filed": filed}
    if start:
        e["start"] = start
    return e


@test("annual data: a restated figure replaces the original; quarters are ignored")
def _():
    gaap = {"Revenues": {"units": {"USD": [
        entry("2023-01-01", "2023-12-31", 100, filed="2024-02-01"),
        entry("2023-01-01", "2023-12-31", 95, filed="2025-02-01"),
        entry("2023-10-01", "2023-12-31", 30, filed="2024-02-01"),
        entry("2023-01-01", "2023-12-31", 999, form="8-K", filed="2026-01-01")]}}}
    assert es.annual_values(gaap, "Revenues", "flow", "USD") == {"2023-12-31": 95}


@test("quarterly data: a quarter is the running total minus the one before")
def _():
    spans = {("2025-01-01", "2025-03-31"): 10, ("2025-01-01", "2025-06-30"): 25, ("2025-01-01", "2025-09-30"): 45}
    assert eq.quarter_value(spans, "2025-03-31", True) == (10, "reported")
    assert eq.quarter_value(spans, "2025-06-30", True) == (15, "year-to-date difference")
    assert eq.quarter_value(spans, "2025-09-30", True) == (20, "year-to-date difference")
    assert eq.quarter_value(spans, "2025-09-30", False) == (None, None)     # share counts do not subtract


def shares_frame(values):
    v = pd.DataFrame({k: [x] for k, x in values.items()}, index=["diluted_shares"], dtype="float64")
    s = pd.DataFrame("tag", index=["diluted_shares"], columns=v.columns)
    return v, s


def split_tag(*pairs):
    return {"StockholdersEquityNoteStockSplitConversionRatio1": {"units": {"pure": [
        {"end": d, "val": r, "form": "10-Q", "filed": d} for d, r in pairs]}}}


@test("splits: older years are scaled up, restated years are left alone")
def _():
    v, s = shares_frame({"2021-12-31": 100, "2022-12-31": 102, "2023-12-31": 1040, "2024-12-31": 1050})
    v, _, _ = es.adjust_for_splits(split_tag(("2024-06-30", 10)), v, s)
    assert list(v.loc["diluted_shares"]) == [1000, 1020, 1040, 1050]


@test("splits: two splits stack on the oldest years")
def _():
    v, s = shares_frame({"2019-01-31": 25, "2020-01-31": 100, "2022-01-31": 102, "2023-01-31": 1010, "2026-01-31": 1000})
    v, _, _ = es.adjust_for_splits(split_tag(("2021-07-19", 4), ("2024-06-10", 10)), v, s)
    assert list(v.loc["diluted_shares"]) == [1000, 1000, 1020, 1010, 1000]


@test("splits: a reverse split with restated and unrestated quarters mixed")
def _():
    v, s = shares_frame({"2025-06-30": 3000, "2025-09-30": 95, "2026-03-31": 3300, "2026-06-30": 3400, "2026-09-30": 105})
    v, _, _ = es.adjust_for_splits(split_tag(("2026-08-13", 0.03)), v, s)
    assert [round(x) for x in v.loc["diluted_shares"]] == [90, 95, 99, 102, 105]


@test("splits: a split after the last period rescales everything")
def _():
    v, s = shares_frame({"2024-12-31": 100, "2025-12-31": 110})
    v, _, _ = es.adjust_for_splits(split_tag(("2026-08-13", 0.5)), v, s)
    assert list(v.loc["diluted_shares"]) == [50, 55]


@test("splits: ordinary growth in the share count is not treated as a split")
def _():
    v, s = shares_frame({"2022-12-31": 100, "2023-12-31": 150, "2024-12-31": 155})
    v, _, _ = es.adjust_for_splits(split_tag(("2019-01-02", 1.5)), v, s)
    assert list(v.loc["diluted_shares"]) == [100, 150, 155]


# ---- the decision rule -------------------------------------------------------
PCT = {10: 60.0, 25: 80.0, 50: 100.0, 75: 130.0, 90: 160.0}


@test("decision: a cheap, sound company with a firm downside is a Buy candidate")
def _():
    d = run.decide(70.0, PCT, 55.0, "CLEAR", [])
    assert d["label"] == "Buy candidate" and d["margin_of_safety"] == 0.20 and d["buy_below"] == 80.0


@test("decision: risk markers raise the margin of safety to 30% then 40%")
def _():
    assert run.decide(75.0, PCT, 60.0, "CLEAR", ["a"])["label"] == "Hold or watch"       # needs 70 or less
    assert run.decide(69.0, PCT, 60.0, "CLEAR", ["a"])["label"] == "Buy candidate"
    assert run.decide(65.0, PCT, 60.0, "CLEAR", ["a", "b"])["label"] == "Hold or watch"  # needs 60 or less
    assert run.decide(59.0, PCT, 60.0, "CLEAR", ["a", "b", "c"])["margin_of_safety"] == 0.40


@test("decision: a weak pessimistic case blocks a Buy candidate")
def _():
    assert run.decide(70.0, PCT, 40.0, "CLEAR", [])["label"] == "Hold or watch"          # 40 < 70% of 70


@test("decision: a failed health check gives no label, however cheap")
def _():
    assert run.decide(10.0, PCT, 55.0, "STOP", [])["label"] == "No label"


@test("decision: Overpriced needs both the 75th percentile and 120% of the median")
def _():
    assert run.decide(131.0, PCT, 55.0, "CLEAR", [])["label"] == "Overpriced"
    assert run.decide(125.0, PCT, 55.0, "CLEAR", [])["label"] == "Hold or watch"         # over 120 but not over 130


@test("decision: a wider range demands a lower price")
def _():
    wide = {10: 30.0, 25: 55.0, 50: 100.0, 75: 170.0, 90: 250.0}
    assert run.decide(70.0, PCT, 60.0, "CLEAR", [])["label"] == "Buy candidate"
    assert run.decide(70.0, wide, 60.0, "CLEAR", [])["label"] == "Hold or watch"


@test("growth cap: tightens as revenue grows")
def _():
    caps = [next(cap for limit, cap in va.GROWTH_CAPS if revenue < limit) for revenue in (5e8, 5e9, 5e10, 5e11)]
    assert caps == [0.25, 0.20, 0.15, 0.10]


# ---- records ----------------------------------------------------------------
@test("records: missing and infinite numbers are stored safely")
def _():
    out = run.clean({"a": float("nan"), "b": float("inf"), "c": np.float64(1.5), "d": [np.int64(2), None]})
    assert out == {"a": None, "b": "infinite", "c": 1.5, "d": [2, None]}
    json.dumps(out)


# ---- market-implied figures ---------------------------------------------------
def _sample():
    return dict(growth=0.10, target_margin=0.40, terminal_growth=0.04, tax_rate=0.25, sales_to_capital=1.0,
                revenue=100e9, margin=0.35, rd_uplift=0.0, wacc=0.09, wacc_mature=0.09,
                terminal_roic_premium=0.0, cash=10e9, debt=20e9, shares=1e9)


@test("market implied: each figure reproduces the price it came from")
def _():
    a = _sample()
    for price in (va.project(a)[0] * k for k in (0.5, 1.0, 2.0)):
        m = va.market_implied(a, price)
        assert abs(va.project(dict(a, growth=m["growth"]))[0] / price - 1) < 1e-6
        assert abs(va.project(dict(a, target_margin=m["margin"]))[0] / price - 1) < 1e-6
        d = m["discount_rate"] - a["wacc"]
        assert abs(va.project(dict(a, wacc=a["wacc"] + d, wacc_mature=a["wacc_mature"] + d))[0] / price - 1) < 1e-6


@test("market implied: at our own value it returns our own assumptions")
def _():
    a = _sample()
    m = va.market_implied(a, va.project(a)[0])
    assert abs(m["growth"] - a["growth"]) < 1e-6 and abs(m["margin"] - a["target_margin"]) < 1e-6
    assert abs(m["discount_rate"] - a["wacc"]) < 1e-6


@test("market implied: a higher price means more growth, a fatter margin and a lower return")
def _():
    a = _sample()
    v = va.project(a)[0]
    lo, hi = va.market_implied(a, v * 0.8), va.market_implied(a, v * 1.5)
    assert hi["growth"] > lo["growth"] and hi["margin"] > lo["margin"]
    assert hi["discount_rate"] < lo["discount_rate"] and hi["growth_share_of_price"] > lo["growth_share_of_price"]


@test("good business: a steady cash generator passes, a loss-maker fails the must-haves")
def _():
    import quality
    years = [f"{y}-12-31" for y in range(2016, 2026)]
    good = pd.DataFrame({y: {"revenue": 100.0 + i, "operating_income": 20.0, "free_cash_flow": 15.0,
                             "total_equity": 60.0, "total_debt": 20.0, "cash": 10.0,
                             "diluted_shares": 100.0 - i} for i, y in enumerate(years)})
    assert quality.business_check(good, 0.09)["good_business"]
    bad = good.copy()
    bad.loc["operating_income"] = -5.0
    bad.loc["free_cash_flow"] = -3.0
    assert not quality.business_check(bad, 0.09)["good_business"]


def _good_company():
    years = [f"{y}-12-31" for y in range(2016, 2026)]
    return pd.DataFrame({y: {"revenue": 100.0 + i, "operating_income": 20.0, "free_cash_flow": 15.0,
                             "total_equity": 60.0, "total_debt": 20.0, "cash": 10.0,
                             "diluted_shares": 100.0 - i} for i, y in enumerate(years)})


@test("rule v1: failing any one of the five tests fails the business (no 5-of-6)")
def _():
    import quality
    heavy = _good_company()
    heavy.loc["total_debt"] = 200.0          # net debt 190 = 12.7 years of free cash flow
    heavy.loc["total_equity"] = -120.0       # same capital base, so only the debt test changes
    r = quality.business_check(heavy, 0.09)
    assert r["score"] == 4 and not r["good_business"], r


@test("rule v1: a stock split is not counted as issuing shares")
def _():
    import quality
    c = _good_company()
    c.loc["diluted_shares", "2023-12-31":] = c.loc["diluted_shares", "2023-12-31":] * 20
    assert quality.business_check(c, 0.09)["good_business"]


@test("rule v1: a loss year with negative capital does not count as beating the cost of capital")
def _():
    import quality
    c = _good_company()
    for y in list(c.columns)[:4]:
        c.loc["operating_income", y] = -5.0
        c.loc["total_equity", y] = -50.0
    t = {x["test"]: x["met"] for x in quality.business_check(c, 0.09)["tests"]}
    assert not t["earns more than its cost of capital"], t


@test("rule v1: one spike year does not set the growth-test margin")
def _():
    import quality
    c = _good_company()
    c.loc["operating_income", "2021-12-31"] = 60.0      # one 57% year among 20% years
    a = {"target_margin": 0.19, "margin": 0.20, "margin_reported": 0.20}
    m, _ = quality.best3_margin(a, c)
    assert m < 0.35, m


@test("splits: found from restated filings, not guessed from buybacks")
def _():
    import edgar_statements as es
    def e(v, filed, end="2020-12-31", start="2020-01-01"):
        return {"val": v, "filed": filed, "end": end, "start": start}
    shares = [e(700e6, "2021-02-01"), e(14e9, "2022-07-27"), e(690e6, "2021-04-27", "2021-03-31", "2021-01-01"),
              e(13.8e9, "2022-07-27", "2021-03-31", "2021-01-01")]
    eps = [e(58.6, "2021-02-01"), e(2.93, "2022-07-27"), e(26.3, "2021-04-27", "2021-03-31", "2021-01-01"),
           e(1.315, "2022-07-27", "2021-03-31", "2021-01-01")]
    gaap = {"WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": shares}},
            "EarningsPerShareDiluted": {"units": {"USD/shares": eps}}}
    ev = es.split_events([gaap])
    assert len(ev) == 1 and ev[0]["ratio"] == 20, ev
    assert es.split_factor_since(ev, "2020-12-31") == 20 and es.split_factor_since(ev, "2023-01-01") == 1
    # a 30% buyback over years is not a split: different periods are never compared
    assert es.split_events([{"WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
        e(1000e6, "2015-02-01", "2014-12-31", "2014-01-01"), e(700e6, "2021-02-01")]}}}]) == []


@test("share units: a quarter filed in thousands is converted using net income / EPS")
def _():
    import edgar_statements as es
    cols = ["2013-03-31", "2013-06-30"]
    v = pd.DataFrame({c: {"diluted_shares": s, "net_income": 2.1e9, "eps_diluted": 1.70,
                          "revenue": 5e10, "interest_expense": 1.0, "interest_paid": 1.0}
                      for c, s in zip(cols, [1235907.0, 1237157000.0])})
    src = pd.DataFrame({c: {i: "tag" for i in v.index} for c in cols})
    v2, _ = es.fix_share_units(v.copy(), src)
    assert abs(v2.loc["diluted_shares", "2013-03-31"] - 1235907000.0) < 1, v2.loc["diluted_shares"]


@test("market value: a 20x split mismatch fails, insiders owning a third passes")
def _():
    import health_check as hc
    mv = {"public_float": 1.0e12, "float_date": "2020-06-30", "close_then": 70.0, "close_now": 87.6}
    assert hc.market_value_check(87.6, 686e6, mv)[1] == "FAIL"            # adjusted price, unadjusted shares
    assert hc.market_value_check(1752.0, 686e6, mv)[1] == "PASS"          # both on the same basis
    assert hc.market_value_check(87.6 * 1.5, 686e6 * 20, mv)[1] == "PASS"


@test("quarantine: value over 5x the price or below zero is held back, not bought")
def _():
    import health_check as hc, quality
    biz = {"good_business": True, "tests": []}
    ok = {"verdict": "plausible"}
    _, q = hc.value_check(10.0, 60.0, 0.02)
    assert q and quality.verdict(biz, ok, 6.0, q)[0] == "Check the data"
    _, q = hc.value_check(10.0, -3.0, 0.02)
    assert quality.verdict(biz, ok, None, q)[0] == "Model does not fit"
    _, q = hc.value_check(10.0, 30.0)        # 3x the price: cheap, not quarantined
    assert q is None
    _, q = hc.value_check(10.0, None)
    assert q[0] == "Check the data"
    _, q = hc.value_check(10.0, 14.0, 0.02)
    assert q is None and quality.verdict(biz, ok, 1.4, q)[0] == "Candidate"


@test("shrinking releases no capital: value rises with growth for a capital-heavy company")
def _():
    import valuation as va
    a = {"growth": 0.02, "target_margin": 0.06, "margin": 0.06, "rd_uplift": 0.0, "tax_rate": 0.25,
         "sales_to_capital": 0.5, "revenue": 1e10, "wacc": 0.08, "wacc_mature": 0.08, "terminal_growth": 0.025,
         "terminal_roic_premium": 0.0, "cash": 0.0, "debt": 0.0, "shares": 1e8}
    v = [va.project(a, growth=g)[0] for g in (-0.30, -0.10, 0.0)]
    assert v[0] < v[1] < v[2], v


@test("data gate: a failed health check blocks a passing verdict")
def _():
    import quality
    biz = {"good_business": True, "tests": []}
    assert quality.verdict(biz, {"verdict": "plausible"}, 1.5, None, ["debt load"])[0] == "Failed health check"


def main():
    width = max(len(n) for n, _, _ in RESULTS)
    for name, ok, why in RESULTS:
        print(f"  {'pass' if ok else 'FAIL'}  {name.ljust(width)}  {why}")
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"\n{len(RESULTS) - failed} of {len(RESULTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

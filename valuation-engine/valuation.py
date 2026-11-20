#!/usr/bin/env python3
"""
valuation.py  --  Analysis layer, step 3: cash flow valuation.

The method (Damodaran's standard model)
---------------------------------------
1. Start from the latest twelve months of revenue.
2. Project ten years: revenue grows at a chosen rate for five years, then
   the growth fades to a long-run rate the economy can sustain.
3. Each year:  cash flow = after-tax operating profit - reinvestment.
   Reinvestment is what the company must spend to get the extra revenue.
4. Discount each year's cash flow back to today at the WACC.
5. Add the value of everything after year ten (the "terminal value").
6. Add cash, subtract debt and leases, divide by the share count.

Adjustments carried in from the earlier steps
---------------------------------------------
* R&D is treated as an investment: it is spread over several years instead
  of being charged all at once. This raises operating profit for a company
  whose R&D is growing, and adds a "research asset" to its capital.
* Leases are debt.
* Stock-based pay stays in as a cost (it is never added back).

What a valuation can and cannot tell you
----------------------------------------
The answer depends mostly on two inputs nobody can know: future growth and
future margins. So the script reports three things, not one number:
  a) a base-case value from mechanical defaults taken from the company's history
  b) the growth rate today's share price requires (a "reverse" valuation)
  c) a grid showing how the value moves when growth and margin change
Defaults can be overridden per company in valuation_overrides.json, e.g.
  {"AVGO": {"growth": 0.15, "target_margin": 0.45, "sales_to_capital": 1.5}}

Inputs: <T>_statements.csv, <T>_quarterly.csv, cost_of_capital.csv,
market_inputs.json. Run cost_of_capital.py first. No internet needed.

Usage
-----
    python valuation.py AVGO MSFT GOOG LULU
"""

import json
import sys
from pathlib import Path

import pandas as pd

NAN = float("nan")

# Starting values from the assumption rules spec (draft 2). Change them there first.
GROWTH_CAPS = [(1e9, 0.25), (1e10, 0.20), (1e11, 0.15), (float("inf"), 0.10)]   # revenue below -> cap
S2C_FLOOR, S2C_CAP, S2C_CAP_MAX = 0.5, 4.0, 10.0
ACQUISITION_SHARE = 0.20      # above this share of reinvestment, the 3-year figure is not used   # limits on revenue gained per dollar reinvested
CYCLICAL_SWING = 0.08        # margin swing (one standard deviation) that marks a cyclical business
PEAK_MARGIN_GAP = 0.10       # today's margin this far above the five-year average is "at a peak"
MARGIN_CEILING_GAP = 0.05    # the target margin stays this far under the gross margin


def num(series, item, default=NAN):
    v = series.get(item, NAN)
    return default if pd.isna(v) else float(v)


# ---------------------------------------------------------------------------
# R&D as an investment
# ---------------------------------------------------------------------------
def research(rd_by_year, life):
    """
    For the most recent year in `rd_by_year` (oldest first):
      asset        = the part of past R&D not yet written off
      amortization = this year's write-off of earlier years' R&D
    R&D spent `i` years ago is written off evenly over `life` years.
    """
    rd = [0.0 if pd.isna(v) else float(v) for v in rd_by_year]
    current = rd[-1]
    past = rd[-2::-1][:life]                      # last year, the year before, ...
    asset = current + sum(v * (life - i) / life for i, v in enumerate(past, start=1))
    amortization = sum(v / life for v in past)
    return asset, amortization, len(past) == life


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------
def project(a, growth=None, target_margin=None):
    """Return (value per share, details) for one set of assumptions."""
    g = a["growth"] if growth is None else growth
    target = a["target_margin"] if target_margin is None else target_margin
    g_end = a["terminal_growth"] if g >= a["terminal_growth"] else max(g, 0.02)
    tax, s2c = a["tax_rate"], a["sales_to_capital"]

    revenue, discount, pv_sum, rows = a["revenue"], 1.0, 0.0, []
    for year in range(1, 11):
        fade = max(0, year - 5) / 5                      # 0 for years 1-5, then 0.2 .. 1.0
        g_year = g + (g_end - g) * fade
        margin = a["margin"] + (target - a["margin"]) * min(year, 5) / 5
        wacc = a["wacc"] + (a["wacc_mature"] - a["wacc"]) * fade
        new_revenue = revenue * (1 + g_year)
        # Tax is charged on reported profit; the R&D uplift carries no extra tax.
        nopat = new_revenue * ((margin - a["rd_uplift"]) * (1 - tax) + a["rd_uplift"])
        # A shrinking company does not get its capital back as cash: plants, stores and
        # aircraft rarely sell at book value. So reinvestment never goes below zero.
        # (Without this floor a capital-heavy company was worth more shrinking 30% a
        # year than growing, because each lost dollar of sales "released" $2 of capital.)
        reinvestment = max(new_revenue - revenue, 0.0) / s2c
        fcf = nopat - reinvestment
        discount /= (1 + wacc)
        pv_sum += fcf * discount
        rows.append((year, g_year, new_revenue, margin, nopat, reinvestment, fcf, wacc))
        revenue = new_revenue

    # After year ten: growth g_end forever, earning exactly the cost of capital
    # on new investment (plus any premium set in the assumptions).
    wacc_end = a["wacc_mature"]
    roic_end = wacc_end + a["terminal_roic_premium"]
    nopat_11 = nopat * (1 + g_end)
    fcf_11 = nopat_11 * (1 - g_end / roic_end)
    terminal_value = fcf_11 / (wacc_end - g_end)
    pv_terminal = terminal_value * discount

    operating_value = pv_sum + pv_terminal
    equity_value = operating_value + a["cash"] - a["debt"]
    per_share = equity_value / a["shares"]
    return per_share, {
        "rows": rows, "pv_ten_years": pv_sum, "pv_terminal": pv_terminal,
        "operating_value": operating_value, "equity_value": equity_value,
        "terminal_share": pv_terminal / operating_value if operating_value else NAN,
    }


def implied_growth(a, price):
    """The five-year growth rate at which the model's value equals the price."""
    lo, hi = -0.30, 1.00
    if project(a, growth=lo)[0] > price:
        return lo
    if project(a, growth=hi)[0] < price:
        return hi
    for _ in range(60):
        mid = (lo + hi) / 2
        if project(a, growth=mid)[0] < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _solve(f, lo, hi, price, rising=True):
    """Bisection for f(x) = price, where f rises (or falls) with x. Returns (x, edge)."""
    flo, fhi = f(lo), f(hi)
    if rising and price <= flo or not rising and price >= flo:
        return lo, "low"
    if rising and price >= fhi or not rising and price <= fhi:
        return hi, "high"
    for _ in range(60):
        mid = (lo + hi) / 2
        if (f(mid) < price) == rising:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, None


def market_implied(a, price):
    """
    Reverse-engineer today's price, one unknown at a time, the others held at
    our assumptions (a price is one number, so it can reveal only one).

      growth         5-year revenue growth that makes our value equal the price
      margin         operating margin by year 5 that does it, at our growth
      discount_rate  the rate that does it, at our cash flows: the yearly
                     return a buyer at this price earns if our forecast is right
      growth_share   the part of the price paid for growth: price minus the
                     value of today's after-tax operating profit held flat forever
    Each comes with "edge": "low" or "high" when no value in the range tested
    reproduces the price (then the figure is a bound, not an answer).
    """
    out = {}
    g, edge = _solve(lambda x: project(a, growth=x)[0], -0.30, 1.00, price)
    out["growth"], out["growth_edge"] = g, edge
    m, edge = _solve(lambda x: project(a, target_margin=x)[0], -0.50, 0.95, price)
    out["margin"], out["margin_edge"] = m, edge
    g_end = a["terminal_growth"] if a["growth"] >= a["terminal_growth"] else max(a["growth"], 0.02)
    floor = g_end + 0.005 - a["wacc_mature"]                    # keep the long-run rate above long-run growth
    def at_shift(d):
        return project(dict(a, wacc=a["wacc"] + d, wacc_mature=a["wacc_mature"] + d))[0]
    d, edge = _solve(at_shift, floor, 0.30, price, rising=False)
    out["discount_rate"], out["discount_edge"] = a["wacc"] + d, edge   # "low": the market's rate is below this figure
    tax = a["tax_rate"]
    nopat = a["revenue"] * ((a["margin"] - a["rd_uplift"]) * (1 - tax) + a["rd_uplift"])
    steady = (nopat / a["wacc"] + a["cash"] - a["debt"]) / a["shares"]
    out["steady_state_value"] = steady
    out["growth_share_of_price"] = 1 - steady / price if price > 0 else NAN
    return out


# ---------------------------------------------------------------------------
# Building the assumptions from the data
# ---------------------------------------------------------------------------
def assumptions(ticker, folder, mk, coc, overrides):
    annual = pd.read_csv(folder / f"{ticker}_statements.csv", index_col="line_item")
    quarterly = pd.read_csv(folder / f"{ticker}_quarterly.csv", index_col="line_item")
    ttm = quarterly["TTM"]
    q = [c for c in quarterly.columns if c != "TTM"]
    years = list(annual.columns)
    fy = annual[years[-1]]
    o = overrides.get(ticker, {})
    notes = []

    # ---- R&D adjustment, measured on the last full fiscal year --------------
    life = int(o.get("rd_life", 5))
    rd_hist = [num(annual[y], "rd_expense", 0.0) for y in years]
    asset_fy, amort_fy, full = research(rd_hist, life)
    rd_fy, rev_fy = rd_hist[-1], num(fy, "revenue")
    rd_uplift = (rd_fy - amort_fy) / rev_fy if rd_fy > 0 else 0.0
    scale = (num(ttm, "rd_expense", 0.0) / rd_fy) if rd_fy > 0 else 0.0
    research_asset = asset_fy * scale
    if rd_fy > 0 and not full:
        notes.append(f"Fewer than {life} earlier years of R&D were available; the R&D adjustment is understated.")

    # ---- starting point -----------------------------------------------------
    revenue = num(ttm, "revenue")
    margin_reported = num(ttm, "operating_income") / revenue
    # Leases are counted as debt below, so the interest hidden inside lease
    # rent is added back to operating profit (Damodaran's lease adjustment);
    # otherwise the leases would be charged twice. Unlike the R&D uplift, this
    # is ordinary taxable profit.
    lease_interest = num(coc[ticker], "lease_interest_estimate", 0.0)
    lease_uplift = lease_interest / revenue if revenue > 0 else 0.0
    margin = margin_reported + rd_uplift + lease_uplift

    # ---- growth default: the lower of recent growth and the 3-year pace -----
    ttm_growth = NAN
    if len(q) >= 8:
        prev = quarterly.loc["revenue", q[-8:-4]]
        if prev.notna().all():
            ttm_growth = revenue / prev.sum() - 1
    cagr = NAN
    if len(years) >= 4:
        cagr = (rev_fy / num(annual[years[-4]], "revenue")) ** (1 / 3) - 1
    candidates = [v for v in (ttm_growth, cagr) if not pd.isna(v)]
    # Rule G2: the lower of the two. Rule G3: capped by how large the company
    # already is, because very large companies rarely sustain high growth.
    growth_uncapped = max(min(candidates), -0.05) if candidates else 0.05
    growth_cap = next(cap for limit, cap in GROWTH_CAPS if revenue < limit)
    growth_default = min(growth_uncapped, growth_cap)

    # ---- margin history (rules M1 to M4) ------------------------------------
    # Each fiscal year's reported operating margin, plus today's R&D uplift.
    # The reported margin is used for the history because working out the
    # R&D write-off for a year needs five earlier years, which would leave
    # only half the history. Adding today's uplift keeps it comparable with
    # the adjusted margin used everywhere else.
    history = []
    for y in years:
        rev_i, op_i = num(annual[y], "revenue"), num(annual[y], "operating_income")
        if pd.isna(rev_i) or pd.isna(op_i) or rev_i <= 0:
            continue
        history.append(op_i / rev_i + rd_uplift + lease_uplift)
    last5, last10 = history[-5:], history[-10:]
    avg5 = sum(last5) / len(last5) if last5 else margin
    avg10 = sum(last10) / len(last10) if last10 else margin
    low10, high10 = (min(last10), max(last10)) if last10 else (margin, margin)
    sd10 = float(pd.Series(last10).std()) if len(last10) >= 3 else 0.0
    cyclical = sd10 > CYCLICAL_SWING
    target_default = avg10 if cyclical else avg5
    peak_margin = margin > avg5 + PEAK_MARGIN_GAP
    gross = num(ttm, "gross_profit") / revenue
    ceiling = NAN if pd.isna(gross) else gross - MARGIN_CEILING_GAP
    ceiling_applied = not pd.isna(ceiling) and target_default > ceiling
    if ceiling_applied:
        target_default = ceiling
    losses = [num(annual[y], "net_income") for y in years[-3:]]
    early_stage = len(years) < 5 or any((not pd.isna(v)) and v < 0 for v in losses)

    # ---- reinvestment: revenue gained per dollar reinvested, last 3 years ---
    reinvested, acquired, usable = 0.0, 0.0, len(years) >= life + 4
    nwc = lambda col: num(col, "receivables", 0) + num(col, "inventory", 0) - num(col, "accounts_payable", 0)
    for i in range(len(years) - 3, len(years)):
        col, before = annual[years[i]], annual[years[i - 1]]
        _, amort_i, _ = research(rd_hist[:i + 1], life)
        dep = num(col, "depreciation_only")
        if pd.isna(dep):                      # many companies report only the combined figure
            dep = num(col, "depreciation_amortization")
        if pd.isna(dep):
            usable = False
            dep = 0.0
        reinvested += (num(col, "capex", 0) - dep + nwc(col) - nwc(before)
                       + (rd_hist[i] - amort_i) + num(col, "acquisitions", 0))
        acquired += num(col, "acquisitions", 0)
    rev_gain = rev_fy - num(annual[years[-4]], "revenue") if len(years) >= 4 else NAN
    s2c_measured = rev_gain / reinvested if usable and reinvested > 0 and rev_gain > 0 else NAN

    # ---- balance sheet bridge ----------------------------------------------
    cash = num(ttm, "cash", 0) + num(ttm, "short_term_investments", 0)
    debt = num(ttm, "total_debt", 0) + num(ttm, "op_lease_liability", 0)
    equity_book = num(ttm, "total_equity")
    invested_capital = equity_book + debt - cash + research_asset
    # Revenue gained per dollar reinvested. First choice: what the last three
    # years show. Otherwise: the revenue the company gets today from each dollar
    # of capital it has. The upper limit is 4, or the company's own figure today
    # if that is higher (retailers turn their capital over many times a year),
    # and never above 10.
    turnover = revenue / invested_capital if invested_capital > 0 else NAN
    s2c_cap = S2C_CAP if pd.isna(turnover) else min(max(S2C_CAP, turnover), S2C_CAP_MAX)
    # Large acquisitions distort the three-year figure: the money paid counts
    # as reinvestment, but the revenue bought often arrives late or is small.
    # Then the company's own revenue per dollar of capital today is used.
    bought = reinvested > 0 and acquired > ACQUISITION_SHARE * reinvested
    if bought and not pd.isna(turnover):
        s2c_measured = NAN
        notes.append(f"Acquisitions were {acquired / reinvested:.0%} of reinvestment over three years, so "
                     "revenue per dollar reinvested is taken from the capital the company uses today "
                     f"({min(max(turnover, S2C_FLOOR), s2c_cap):.2f}).")
    if not pd.isna(s2c_measured):
        s2c_default = min(max(s2c_measured, S2C_FLOOR), s2c_cap)
    elif not pd.isna(turnover):
        s2c_default = min(max(turnover, S2C_FLOOR), s2c_cap)
        notes.append("Reinvestment history was not usable; revenue per dollar reinvested is taken from "
                     f"the capital the company uses today ({s2c_default:.1f}).")
    else:
        s2c_default = 1.5
        notes.append("Reinvestment history was incomplete; revenue per dollar reinvested set to a generic 1.5.")
    tax = mk["marginal_tax_rate"]
    # After year ten the company is treated as average risk (beta of 1) but
    # keeps its own mix of debt and equity, so its cheaper debt still counts.
    # It is never moved above the rate it has today.
    wacc_now = float(coc.loc["wacc", ticker])
    we, wd = num(coc[ticker], "weight_equity", 1.0), num(coc[ticker], "weight_debt", 0.0)
    kd = num(coc[ticker], "cost_of_debt_aftertax", 0.0)
    mature_wacc = min(wacc_now, we * (mk["risk_free_rate"] + mk["equity_risk_premium"]) + wd * kd)
    nopat_now = revenue * ((margin - rd_uplift) * (1 - tax) + rd_uplift)

    rf, erp = mk["risk_free_rate"], mk["equity_risk_premium"]
    growth = float(o.get("growth", growth_default))
    target = float(o.get("target_margin", target_default))
    terminal = float(o.get("terminal_growth", min(rf, 0.04)))
    s2c = float(o.get("sales_to_capital", s2c_default))
    roic_now = nopat_now / invested_capital if invested_capital > 0 else NAN

    # ---- scenarios, set by rule ----------------------------------------------
    recent = growth if pd.isna(ttm_growth) else ttm_growth
    scenarios = {
        "pessimistic": {"growth": max(0.5 * growth, terminal) if growth > terminal else growth,
                        "margin": min(low10, target)},
        "optimistic": {"growth": max(growth, min(recent, 1.5 * growth_cap)),
                       "margin": max(margin, target)},
    }

    # ---- flags: things a reader should know about these assumptions ----------
    flags = []
    if "growth" not in o and growth_uncapped > growth_cap:
        flags.append(f"Growth capped at {growth_cap * 100:.0f}% for a company of this size; "
                     f"its own history points to {growth_uncapped * 100:.1f}%.")
    if "growth" in o and growth > growth_cap:
        flags.append(f"Growth override of {growth * 100:.1f}% is above the {growth_cap * 100:.0f}% cap for a company of this size.")
    if peak_margin:
        flags.append(f"Margin is at or near a peak: {margin * 100:.1f}% now against a five-year average of {avg5 * 100:.1f}%.")
    if cyclical:
        flags.append(f"Margins have swung widely (about {sd10 * 100:.0f} points), so the ten-year average is used.")
    if ceiling_applied:
        flags.append(f"Margin held to {ceiling * 100:.1f}%, five points under the gross margin.")
    if target > margin + 0.02:
        flags.append(f"The base case assumes the margin recovers from {margin * 100:.1f}% today to {target * 100:.1f}%.")
    implied_return = ((target - rd_uplift) * (1 - tax) + rd_uplift) * s2c
    if implied_return > 0.50 or (not pd.isna(roic_now) and roic_now > 0 and implied_return > 2 * roic_now):
        flags.append(f"Growth, margin and reinvestment together imply a {implied_return * 100:.0f}% return on new investment"
                     + ("" if pd.isna(roic_now) else f", against {roic_now * 100:.0f}% earned today") + ".")
    if reinvested > 0 and acquired / reinvested > 0.20:
        flags.append(f"Acquisitions made up {acquired / reinvested * 100:.0f}% of reinvestment over three years, "
                     "so past growth overstates what the business grows on its own.")
    flags.append("Market size is not checked yet: the industry comparison in the spec is not built.")

    a = {
        "revenue": revenue, "margin_reported": margin_reported, "rd_uplift": rd_uplift, "lease_uplift": lease_uplift,
        "margin": margin, "target_margin": target,
        "growth": growth,
        "growth_ttm": ttm_growth, "growth_3yr": cagr,
        "growth_uncapped": growth_uncapped, "growth_cap": growth_cap,
        "margin_5yr_avg": avg5, "margin_10yr_avg": avg10, "margin_low_10yr": low10, "margin_high_10yr": high10,
        "margin_swing": sd10, "cyclical": cyclical, "peak_margin": peak_margin, "early_stage": early_stage,
        "gross_margin": gross, "implied_return_on_new_investment": implied_return,
        "scenarios": scenarios, "flags": flags,
        "terminal_growth": terminal,
        "sales_to_capital": s2c,
        "tax_rate": tax, "wacc": float(coc.loc["wacc", ticker]),
        "wacc_mature": mature_wacc,
        "terminal_roic_premium": float(o.get("terminal_roic_premium", 0.0)),
        "cash": cash, "debt": debt, "shares": num(ttm, "diluted_shares"),
        "research_asset": research_asset, "invested_capital": invested_capital,
        "roic": roic_now,
        "rd_life": life, "overridden": sorted(o),
    }
    return a, notes


def show(ticker, a, price, notes):
    pct = lambda v: "-" if pd.isna(v) else f"{v * 100:.1f}%"
    bn = lambda v: f"${v / 1e9:,.1f}B"
    value, d = project(a)
    g_implied = implied_growth(a, price)

    print(f"\n{'=' * 76}\n{ticker}\n")
    print("  Starting point (latest twelve months)")
    print(f"    Revenue                         {bn(a['revenue'])}")
    print(f"    Operating margin, reported      {pct(a['margin_reported'])}")
    print(f"    Operating margin, R&D-adjusted  {pct(a['margin'])}   (R&D spread over {a['rd_life']} years)")
    print(f"    Return on capital               {pct(a['roic'])}   against a WACC of {pct(a['wacc'])}")
    print("  Assumptions" + (f"   (overridden: {', '.join(a['overridden'])})" if a["overridden"] else "   (mechanical defaults)"))
    print(f"    Growth, years 1-5               {pct(a['growth'])}   "
          f"(last twelve months {pct(a['growth_ttm'])}, three-year pace {pct(a['growth_3yr'])})")
    print(f"    Growth after year 10            {pct(a['terminal_growth'] if a['growth'] >= a['terminal_growth'] else max(a['growth'], 0.02))}")
    print(f"    Margin by year 5                {pct(a['target_margin'])}")
    print(f"    Revenue per $1 reinvested       {a['sales_to_capital']:.2f}")
    print(f"    Discount rate                   {pct(a['wacc'])} fading to {pct(a['wacc_mature'])}")
    print("  Result")
    print(f"    Value of the next ten years     {bn(d['pv_ten_years'])}")
    print(f"    Value of everything after       {bn(d['pv_terminal'])}   ({pct(d['terminal_share'])} of the total)")
    print(f"    Plus cash, minus debt + leases  {bn(a['cash'])} - {bn(a['debt'])}")
    print(f"    Value per share                 ${value:,.2f}")
    print(f"    Share price                     ${price:,.2f}   (price is {price / value:.2f}x the base-case value)"
          if value > 0 else f"    Share price                     ${price:,.2f}")
    edge = "  (at the edge of the range tested)" if g_implied in (-0.30, 1.00) else ""
    print(f"    Growth the price requires       {pct(g_implied)} a year for five years{edge}")

    steps_g = [a["growth"] - 0.05, a["growth"], a["growth"] + 0.05, g_implied]
    steps_m = [a["target_margin"] - 0.05, a["target_margin"], a["target_margin"] + 0.05]
    print("\n  Value per share for other growth and margin assumptions")
    print("    " + "growth \\ margin".ljust(22) + "".join(f"{pct(m):>12}" for m in steps_m))
    for g in steps_g:
        label = f"{pct(g)}" + ("  (price-implied)" if g is g_implied else "")
        print("    " + label.ljust(22) + "".join(f"{project(a, growth=g, target_margin=m)[0]:>12,.0f}" for m in steps_m))
    for n in notes:
        print(f"  [NOTE] {n}")
    return {"value_per_share": value, "share_price": price, "price_to_value": price / value if value > 0 else NAN,
            "implied_growth": g_implied, "growth_assumed": a["growth"], "growth_ttm": a["growth_ttm"],
            "growth_3yr": a["growth_3yr"], "margin_reported": a["margin_reported"], "margin_adjusted": a["margin"],
            "target_margin": a["target_margin"], "sales_to_capital": a["sales_to_capital"], "roic": a["roic"],
            "wacc": a["wacc"], "terminal_share_of_value": d["terminal_share"],
            "equity_value": d["equity_value"], "research_asset": a["research_asset"]}


def main():
    tickers = [t.upper() for t in sys.argv[1:] if not t.startswith("-")]
    if not tickers:
        sys.exit("Usage: python valuation.py TICKER [TICKER ...]")
    folder = Path(".")
    mk = json.loads((folder / "market_inputs.json").read_text())
    coc_file = folder / "cost_of_capital.csv"
    if not coc_file.exists():
        sys.exit("Run cost_of_capital.py first.")
    coc = pd.read_csv(coc_file, index_col="metric")
    o_file = folder / "valuation_overrides.json"
    overrides = json.loads(o_file.read_text()) if o_file.exists() else {}
    out = {}
    for t in tickers:
        if t not in coc.columns:
            sys.exit(f"{t} is not in cost_of_capital.csv; run cost_of_capital.py with it first.")
        a, notes = assumptions(t, folder, mk, coc, overrides)
        out[t] = show(t, a, mk["prices"][t], notes)
    pd.DataFrame(out).to_csv(folder / "valuation.csv", index_label="metric")
    print("\nSaved valuation.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())

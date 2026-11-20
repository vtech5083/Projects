#!/usr/bin/env python3
"""
health_check.py  --  The gate before any valuation.

A valuation answers "what is it worth if the business keeps working".
This script asks the earlier question: "is the business working, and is
anything in the three statements likely to break the thesis?"

Each check returns one of:
  PASS    nothing unusual
  WATCH   worth reading about in the filing before trusting a valuation
  FAIL    a serious problem; do not rely on a valuation until it is explained

The checks, by the question each one answers
--------------------------------------------
Is the profit real?
  1. cash vs profit        cash from operations compared with net income
  2. profit outside ops    how much pre-tax profit came from outside the business
Is cash getting stuck?
  3. customer payments     days to collect, same quarter in successive years
  4. inventory             days inventory sits, same quarter in successive years
  5. invoices vs sales     unpaid invoices growing much faster than revenue
Can it carry its debt?
  6. interest coverage     operating profit / interest (leases included)
  7. debt load             net debt compared with a year of operating cash profit
  8. debt coming due       debt due within two years against cash plus a year of free cash flow
  9. short-term cover      current assets / current liabilities
Is the business getting better or worse?
 10. gross margin trend    now against the best of the last three fiscal years
 11. operating margin      now against the best of the last three fiscal years
 12. revenue direction     growth over the latest twelve months
 13. return on capital     against the cost of capital
Are owners being treated well?
 14. share count           average yearly change over three years
 15. stock pay             as a share of free cash flow
 16. payouts               buybacks + dividends against free cash flow, and whether debt paid for them
Is there free cash flow at all?
 17. free cash flow        level and direction
Will it survive?
 18. cash runway           for a company burning cash, years of cash left at the current pace
 19. book value            whether liabilities exceed assets
Can the numbers be trusted?
 20. data                  how old the latest quarter is, and what is missing
 21. market value          price x shares against the market value the company itself
                           reports (public float, from its 10-K), moved forward by the
                           share price since that date: catches split and unit errors
 22. value vs price        quarantine: our value over 5x the price, or no value, or a value
                           of zero or below -- the data or the model is wrong far more
                           often than the market

The thresholds are starting rules of thumb, set in LIMITS below. A statement
screen cannot see fraud, lawsuits, customer concentration or anything else
that lives in the text of a filing. It narrows the list; it does not clear a company.

Run the other scripts first. No internet needed.

Usage
-----
    python health_check.py AVGO MSFT GOOG LULU NVDA
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd

from analyze_working_capital import same_quarter

NAN = float("nan")
LIMITS = {
    "cash_vs_profit_watch": 0.80, "cash_vs_profit_fail": 0.50,
    "outside_profit_watch": 0.25,
    "days_rise_watch": 0.20, "days_rise_min": 5,
    "invoice_gap_watch": 0.20,
    "coverage_fail": 3.0, "coverage_watch": 5.0,
    "debt_load_watch": 3.0, "debt_load_fail": 5.0,
    "due_watch": 0.5, "due_fail": 1.0,
    "current_ratio_watch": 1.0,
    "margin_drop_watch": 0.05,
    "roic_cushion_watch": 0.02,
    "shares_watch": 0.02, "shares_fail": 0.05,
    "stock_pay_watch": 0.25,
    "stale_days_watch": 135,
    "runway_fail": 2.0, "runway_watch": 4.0,
    # market value / public float. The float leaves out insiders' shares, so the
    # market value is at or above it: below 0.8x is impossible (an error); above
    # 3x means insiders own two-thirds, rare in large companies, or an error.
    "float_low_fail": 0.80, "float_high_watch": 2.0, "float_high_fail": 3.0,
    "value_over_price_quarantine": 5.0,
}


def num(series, item):
    v = series.get(item, NAN)
    return NAN if pd.isna(v) else float(v)


def nz(v):
    return 0.0 if pd.isna(v) else v


def check(ticker, folder, coc, val, today):
    annual = pd.read_csv(folder / f"{ticker}_statements.csv", index_col="line_item")
    quarterly = pd.read_csv(folder / f"{ticker}_quarterly.csv", index_col="line_item")
    ttm, q = quarterly["TTM"], [c for c in quarterly.columns if c != "TTM"]
    years, fy = list(annual.columns), annual.iloc[:, -1]
    L, out = LIMITS, []
    bn = lambda v: f"${v / 1e9:,.1f}B"

    def add(name, status, why):
        out.append((name, status, why))

    def row(df, item):
        return df.loc[item] if item in df.index else pd.Series(NAN, index=df.columns)

    # 1. cash vs profit
    ni = row(annual, "net_income")
    ratios = (row(annual, "cash_from_operations") / ni.where(ni > 0)).iloc[-3:].dropna()
    now = num(ttm, "cash_from_operations") / num(ttm, "net_income") if num(ttm, "net_income") > 0 else NAN
    avg = ratios.mean() if len(ratios) else NAN
    seen = [v for v in (avg, now) if not pd.isna(v)]
    if not seen:
        add("cash vs profit", "FAIL", "no positive net income to compare against")
    else:
        worst = min(seen)
        add("cash vs profit",
            "FAIL" if worst < L["cash_vs_profit_fail"] else "WATCH" if worst < L["cash_vs_profit_watch"] else "PASS",
            f"cash from operations is {now:.2f}x net income over the latest twelve months, "
            f"{avg:.2f}x on average over three fiscal years")

    # 2. profit from outside the business
    op, pre = num(ttm, "operating_income"), num(ttm, "pretax_income")
    if pd.isna(op):
        add("profit outside ops", "WATCH", "operating profit missing")
    elif op <= 0:
        add("profit outside ops", "FAIL", f"operating profit is {bn(op)}")
    elif pd.isna(pre):
        add("profit outside ops", "PASS", "pre-tax profit not reported")
    else:
        share = (pre - op) / op
        add("profit outside ops", "WATCH" if abs(share) > L["outside_profit_watch"] else "PASS",
            f"pre-tax profit is {bn(pre)} against operating profit of {bn(op)} "
            f"({share * 100:+.0f}% from outside the business)")

    # 3-4. days outstanding, same quarter in successive years
    sq = same_quarter(quarterly)
    for name, metric in (("customer payments", "dso_days"), ("inventory", "dio_days")):
        s = sq.loc[metric].dropna() if sq is not None else pd.Series(dtype=float)
        if len(s) < 2:
            add(name, "PASS", "not applicable or not reported")
            continue
        cur, prev = s.iloc[-1], s.iloc[-2]
        rising = cur > (1 + L["days_rise_watch"]) * prev and cur - prev >= L["days_rise_min"]
        steady = (len(s) == 3 and s.iloc[0] < prev < cur and cur > 1.2 * s.iloc[0]
                  and cur - s.iloc[0] >= 2 * L["days_rise_min"])
        add(name, "WATCH" if rising or steady else "PASS",
            " -> ".join(f"{v:.0f}" for v in s) + " days in the same quarter of successive years")

    # 5. invoices against sales
    done = False
    if len(q) >= 5:
        ar0, ar1 = num(quarterly[q[-5]], "receivables"), num(quarterly[q[-1]], "receivables")
        r0, r1 = num(quarterly[q[-5]], "revenue"), num(quarterly[q[-1]], "revenue")
        if ar0 > 0 and r0 > 0 and not pd.isna(ar1) and not pd.isna(r1):
            gap = (ar1 / ar0 - 1) - (r1 / r0 - 1)
            add("invoices vs sales", "WATCH" if gap > L["invoice_gap_watch"] else "PASS",
                f"unpaid invoices grew {(ar1 / ar0 - 1) * 100:.0f}% in a year, revenue {(r1 / r0 - 1) * 100:.0f}%")
            done = True
    if not done:
        add("invoices vs sales", "PASS", "not applicable or not reported")

    # 6. interest coverage
    cov = float(coc.loc["interest_coverage", ticker])
    add("interest coverage",
        "FAIL" if cov < L["coverage_fail"] else "WATCH" if cov < L["coverage_watch"] else "PASS",
        "no interest to cover" if cov == float("inf") else f"operating profit covers interest {cov:.1f} times")

    # 7. debt load
    cash = nz(num(ttm, "cash")) + nz(num(ttm, "short_term_investments"))
    borrowings, leases = nz(num(ttm, "total_debt")), nz(num(ttm, "op_lease_liability"))
    debt = borrowings + leases
    ebitda = nz(op) + nz(num(ttm, "depreciation_amortization"))
    net = debt - cash
    if net <= 0:
        add("debt load", "PASS", f"more cash ({bn(cash)}) than debt and leases ({bn(debt)})")
    elif ebitda <= 0:
        add("debt load", "FAIL", f"net debt of {bn(net)} with no operating cash profit")
    else:
        m = net / ebitda
        add("debt load", "FAIL" if m > L["debt_load_fail"] else "WATCH" if m > L["debt_load_watch"] else "PASS",
            f"net debt of {bn(net)} is {m:.1f} years of operating cash profit")

    # 8. debt coming due
    due = [num(fy, "debt_due_year_1"), num(fy, "debt_due_year_2")]
    fcf = num(ttm, "free_cash_flow")
    cover = cash + max(nz(fcf), 0)
    if borrowings <= 0:
        add("debt coming due", "PASS", "no borrowings")
    elif all(pd.isna(d) for d in due):
        soon = nz(num(ttm, "long_term_debt_current"))
        add("debt coming due", "WATCH" if soon > cover else "PASS",
            f"year-by-year repayment schedule not found in the data; {bn(soon)} is due within a year, "
            f"against {bn(cover)} of cash plus a year of free cash flow")
    else:
        owed = sum(nz(d) for d in due)
        r = owed / cover if cover > 0 else float("inf")
        add("debt coming due", "FAIL" if r > L["due_fail"] else "WATCH" if r > L["due_watch"] else "PASS",
            f"{bn(owed)} due within two years of {years[-1]}, against {bn(cover)} of cash plus a year of free cash flow")

    # 9. short-term cover
    ca, cl = num(quarterly[q[-1]], "current_assets"), num(quarterly[q[-1]], "current_liabilities")
    if cl > 0 and not pd.isna(ca):
        add("short-term cover", "WATCH" if ca / cl < L["current_ratio_watch"] else "PASS",
            f"current assets are {ca / cl:.1f}x current liabilities")
    else:
        add("short-term cover", "PASS", "not reported")

    # 10-11. margin trends: now against the best of the last three fiscal years,
    # so a fall from a recent high is caught even if the level is still above
    # where it was three years ago.
    rev = num(ttm, "revenue")
    for name, item in (("gross margin trend", "gross_profit"), ("operating margin", "operating_income")):
        hist = (row(annual, item) / row(annual, "revenue")).iloc[-3:].dropna()
        b = num(ttm, item) / rev
        if not len(hist) or pd.isna(b):
            add(name, "PASS", "not reported")
            continue
        best, when = hist.max(), hist.idxmax()[:4]
        add(name, "FAIL" if b < 0 else "WATCH" if best - b > L["margin_drop_watch"] else "PASS",
            f"{b * 100:.1f}% now against a high of {best * 100:.1f}% in fiscal {when}")

    # 12. revenue direction
    g = float(val.loc["growth_ttm", ticker])
    add("revenue direction", "WATCH" if not pd.isna(g) and g < 0 else "PASS",
        f"revenue changed {g * 100:+.1f}% over the latest twelve months" if not pd.isna(g) else "not available")

    # 13. return on capital
    roic, wacc = float(val.loc["roic", ticker]), float(val.loc["wacc", ticker])
    if pd.isna(roic):
        add("return on capital", "WATCH", "could not be measured")
    else:
        add("return on capital",
            "FAIL" if roic < wacc else "WATCH" if roic < wacc + L["roic_cushion_watch"] else "PASS",
            f"earns {roic * 100:.0f}% on its capital against a {wacc * 100:.1f}% cost")

    # 14. share count
    ch = row(annual, "diluted_shares").pct_change(fill_method=None).iloc[-3:].dropna()
    # A one-year jump by a whole split ratio (2-for-1, 3-for-1, 20-for-1 ...)
    # is a stock split the filings did not tag, not new shares. Leave it out.
    def looks_like_split(c):
        r = 1 + c
        k = r if r >= 1 else 1 / r
        return k >= 1.8 and abs(k - round(k)) / round(k) < 0.06
    splits = [c for c in ch if looks_like_split(c)]
    ch = ch[[not looks_like_split(c) for c in ch]]
    if len(ch):
        m = ch.mean()
        note = " (a jump matching an untagged stock split was left out)" if splits else ""
        add("share count", "FAIL" if m > L["shares_fail"] else "WATCH" if m > L["shares_watch"] else "PASS",
            f"changed {m * 100:+.1f}% a year on average over three fiscal years{note}")
    else:
        add("share count", "WATCH", "share history not available")

    # 15. stock pay
    sbc = num(ttm, "stock_based_comp")
    if pd.isna(sbc) or pd.isna(fcf):
        add("stock pay", "PASS", "not reported")
    elif fcf <= 0:
        add("stock pay", "WATCH", f"stock pay of {bn(sbc)} with no free cash flow to compare against")
    else:
        add("stock pay", "WATCH" if sbc / fcf > L["stock_pay_watch"] else "PASS",
            f"stock pay is {sbc / fcf * 100:.0f}% of free cash flow" + (", more than all of it" if sbc > fcf else ""))

    # 16. payouts
    pay = (row(annual, "buybacks").fillna(0) + row(annual, "dividends_paid").fillna(0)).iloc[-3:].sum()
    f3 = row(annual, "free_cash_flow").iloc[-3:].sum()
    d_then = num(annual[years[-4]], "total_debt") if len(years) >= 4 else NAN
    grew = nz(num(fy, "total_debt")) - nz(d_then)
    add("payouts", "WATCH" if pay > f3 and grew > 0 else "PASS",
        f"paid out {bn(pay)} over three fiscal years against {bn(f3)} of free cash flow; debt changed by {bn(grew)}")

    # 17. free cash flow
    prev_fcf = NAN
    if len(q) >= 8 and quarterly.loc["free_cash_flow", q[-8:-4]].notna().all():
        prev_fcf = quarterly.loc["free_cash_flow", q[-8:-4]].sum()
    if pd.isna(fcf):
        add("free cash flow", "WATCH", "not available")
    elif fcf < 0:
        add("free cash flow", "FAIL", f"negative: {bn(fcf)} over the latest twelve months")
    else:
        falling = not pd.isna(prev_fcf) and prev_fcf > 0 and fcf < 0.7 * prev_fcf and not pd.isna(g) and g > 0
        add("free cash flow", "WATCH" if falling else "PASS",
            f"{bn(fcf)} over the latest twelve months"
            + (f", against {bn(prev_fcf)} the year before" if not pd.isna(prev_fcf) else ""))

    # 18. cash runway: for a company burning cash, how long the cash lasts
    if not pd.isna(fcf) and fcf < 0:
        years_left = cash / -fcf
        add("cash runway", "FAIL" if years_left < L["runway_fail"] else "WATCH" if years_left < L["runway_watch"] else "PASS",
            f"{bn(cash)} of cash against {bn(-fcf)} burned in a year: {years_left:.1f} years at this pace")
    else:
        add("cash runway", "PASS", "not burning cash")

    # 19. owners' stake on the books
    eq = num(quarterly[q[-1]], "total_equity")
    if pd.isna(eq):
        add("book value", "PASS", "not reported")
    else:
        add("book value", "WATCH" if eq < 0 else "PASS",
            f"liabilities exceed assets by {bn(-eq)}" if eq < 0 else f"assets exceed liabilities by {bn(eq)}")

    # 20. data
    age = (today - date.fromisoformat(q[-1])).days
    core = ["revenue", "operating_income", "net_income", "cash_from_operations", "capex", "total_assets", "diluted_shares"]
    gone = [i for i in core if pd.isna(num(ttm, i))]
    # Quarters that do not add up to the audited year mean the recent numbers
    # mix two versions of the company (a spin-off or a restatement is typical).
    notes, broken, estimated = [], False, False
    checks_file = folder / f"{ticker}_quarterly_checks.csv"
    if checks_file.exists():
        found = pd.read_csv(checks_file)
        bad = found[(found["level"] == "FAIL") & found["message"].str.contains("quarters sum")]
        if len(bad):
            broken = True
            items = sorted({m.split(": ")[1].split(" quarters")[0] for m in bad["message"]})
            notes.append(f"quarterly {' and '.join(items)} do not add up to the audited year "
                         "(a spin-off or restatement is the usual cause), so recent figures cannot be relied on")
    src_file = folder / f"{ticker}_quarterly_sources.csv"
    if src_file.exists():
        src = pd.read_csv(src_file, index_col="line_item")
        if "operating_income" in src.index and src.loc["operating_income"].astype(str).str.contains("estimate").any():
            estimated = True
            notes.append("operating profit is our estimate; the company does not report that line")
    if gone:
        notes.insert(0, f"missing: {', '.join(gone)}")
    notes.append(f"latest quarter ended {q[-1]}, {age} days ago")
    status = ("FAIL" if gone or broken else
              "WATCH" if age > L["stale_days_watch"] or estimated else "PASS")
    add("data", status, "; ".join(notes))
    return out


# ---------------------------------------------------------------------------
# 21-22. checks that need the price (called by run.py)
# ---------------------------------------------------------------------------
def public_float(facts_list, as_of):
    """The latest public float the company reported in filings up to `as_of`:
    (date it was measured, dollars). facts_list: SEC companyfacts of each
    registration the company has filed under."""
    best = None
    for f in facts_list:
        for e in f.get("facts", {}).get("dei", {}).get("EntityPublicFloat", {}).get("units", {}).get("USD", []):
            if e.get("filed", "9999") <= as_of and e.get("val", 0) > 0 and e.get("end"):
                if best is None or (e["filed"], e["end"]) > (best["filed"], best["end"]):
                    best = e
    return (best["end"], float(best["val"])) if best else None


def market_value_inputs(facts_list, closes, as_of):
    """What check 21 needs, from the SEC facts and a monthly price table
    (index 'YYYY-MM', column 'Close' -- split-adjusted, so the ratio of two
    closes is a true price change)."""
    pf = public_float(facts_list, as_of)
    if pf is None or closes is None:
        return None
    then, now = pf[0][:7], as_of[:7]
    have = [m for m in closes.index if m <= now]
    if then not in closes.index or not have:
        return None
    return {"public_float": pf[1], "float_date": pf[0], "close_then": float(closes.loc[then, "Close"]),
            "close_now": float(closes.loc[have[-1], "Close"]), "close_now_month": have[-1]}


def market_value_check(price, shares, mv):
    bn = lambda v: f"${v / 1e9:,.1f}B"
    if not mv or not price or not shares or pd.isna(shares):
        return ("market value", "WATCH", "no reported public float to compare against")
    ours = price * shares
    moved = mv["public_float"] * mv["close_now"] / mv["close_then"]
    r = ours / moved
    why = (f"price x shares {bn(ours)} against the {bn(mv['public_float'])} public float the company "
           f"reported for {mv['float_date']} (about {bn(moved)} at today's price): {r:.2f}x")
    L = LIMITS
    if r < L["float_low_fail"]:
        return ("market value", "FAIL", why + "; the share count and price are probably on different bases "
                "(a stock split, spin-off or units error) -- check before using any per-share number")
    if r > L["float_high_fail"]:
        return ("market value", "FAIL", why + "; either one owner holds most of the shares (check the 10-K cover), "
                "or the share count or the reported float is wrong -- check before using any per-share number")
    if r > L["float_high_watch"]:
        return ("market value", "WATCH", why + "; insiders may own over half the shares, or the share count is off")
    return ("market value", "PASS", why)


def value_check(price, value, required_growth=None):
    """Check 22. Returns (check, quarantine) where quarantine is None or
    (verdict, reason) for quality.verdict. (A price that even a 30% yearly
    decline would justify is not quarantined by itself: for capital-heavy
    companies shrinking frees capital, so the value barely falls.)"""
    if value is None or pd.isna(value):
        why = "no value could be computed (a core figure such as the share count is missing)"
        return ("value vs price", "FAIL", why), ("Check the data", why)
    if value <= 0:
        why = "our value is zero or negative: the model does not fit this business (thin margins, heavy reinvestment or losses)"
        return ("value vs price", "FAIL", why), ("Model does not fit", why)
    k = value / price
    if k > LIMITS["value_over_price_quarantine"]:
        why = (f"our value is {k:.1f}x the price; for a large listed company that is far more often a data "
               "error than a bargain -- held back for a manual check of the filings")
        return ("value vs price", "FAIL", why), ("Check the data", why)
    return ("value vs price", "PASS", f"our value is {k:.2f}x the price"), None


def main():
    tickers = [t.upper() for t in sys.argv[1:] if not t.startswith("-")]
    if not tickers:
        sys.exit("Usage: python health_check.py TICKER [TICKER ...]")
    folder, today = Path("."), date.today()
    coc = pd.read_csv(folder / "cost_of_capital.csv", index_col="metric")
    val = pd.read_csv(folder / "valuation.csv", index_col="metric")
    table, reasons, summary = {}, {}, {}
    for t in tickers:
        if t not in coc.columns or t not in val.columns:
            sys.exit(f"{t}: run cost_of_capital.py and valuation.py with it first.")
        results = check(t, folder, coc, val, today)
        fails = [r for r in results if r[1] == "FAIL"]
        watches = [r for r in results if r[1] == "WATCH"]
        verdict = ("STOP: explain the failures before valuing" if fails else
                   "CAUTION: read the filing on the flagged items" if len(watches) >= 3 else
                   "CLEAR on the statements" if not watches else "MOSTLY CLEAR: a few items to read about")
        print(f"\n{'=' * 78}\n{t}   {verdict}   ({len(fails)} fail, {len(watches)} watch, "
              f"{len(results) - len(fails) - len(watches)} pass)\n")
        for name, status, why in results:
            if status != "PASS":
                print(f"  [{status:5}] {name}: {why}")
        table[t] = {name: status for name, status, _ in results}
        reasons[t] = {name: why for name, _, why in results}
        summary[t] = verdict
    out = pd.DataFrame(table)
    out.loc["VERDICT"] = pd.Series(summary)
    out.to_csv(folder / "health_check.csv", index_label="check")
    pd.DataFrame(reasons).to_csv(folder / "health_check_reasons.csv", index_label="check")
    print(f"\n{'=' * 78}\nSummary  (. = pass)\n")
    print(out.drop(index="VERDICT").replace({"PASS": "."}).to_string())
    print("\nSaved health_check.csv and health_check_reasons.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())

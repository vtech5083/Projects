#!/usr/bin/env python3
"""
cost_of_capital.py  --  Analysis layer, step 2: credit rating and discount rate.

What it works out, per company
------------------------------
1. Interest coverage  = operating profit / interest cost.
   Leases count as debt, so the interest hidden inside rent is added to
   both the top and the bottom of that ratio.
2. A credit rating, by looking the coverage up in Damodaran's table.
3. Cost of debt       = risk-free rate + the rating's spread.
4. Beta               = how much the share price moves when the market moves,
                        from 60 months of returns against the S&P 500.
5. Cost of equity     = risk-free rate + beta x equity risk premium.
6. WACC               = the two costs, weighted by how much of the company is
                        funded by shareholders (at market value) and by lenders.

"Dynamic" means none of this is typed in by hand per company. Change the
statements, the share price or market_inputs.json and every number moves.
The script also shows the rating each past fiscal year would have earned.

Inputs: <TICKER>_statements.csv, <TICKER>_quarterly.csv, market_inputs.json.
No internet connection is used.

Usage
-----
    python cost_of_capital.py AVGO MSFT GOOG LULU
"""

import json
import sys
from pathlib import Path

import pandas as pd

NAN = float("nan")


def rating_for(coverage, table):
    """Look up (rating, spread) for an interest coverage ratio."""
    rating, spread = table[0][1], table[0][2]
    for floor, name, sp in table:
        if coverage >= floor:
            rating, spread = name, sp
    return rating, spread


def get(series, item):
    v = series.get(item, NAN)
    return NAN if pd.isna(v) else float(v)


def coverage_and_rating(ebit, interest, lease_debt, rf, table):
    """
    Coverage with leases treated as debt.

    Rent on an operating lease is charged in full against operating profit,
    but part of it is really interest. That part = lease liability x cost of
    debt, and the cost of debt depends on the rating, which depends on the
    coverage. So the three are solved together by repeating the calculation
    until the rating stops changing.
    """
    interest = 0.0 if pd.isna(interest) else interest
    lease_debt = 0.0 if pd.isna(lease_debt) else lease_debt
    rating, spread = rating_for(100000, table)   # start from the best rating
    for _ in range(20):
        lease_interest = lease_debt * (rf + spread)
        total_interest = interest + lease_interest
        if total_interest <= 0:
            coverage = float("inf") if ebit > 0 else -1.0
        else:
            coverage = (ebit + lease_interest) / total_interest
        new_rating, new_spread = rating_for(coverage, table)
        if new_rating == rating:
            break
        rating, spread = new_rating, new_spread
    return coverage, rating, spread, lease_interest


def beta_from_prices(stock, market):
    """Slope of the stock's monthly returns against the market's."""
    s = pd.Series(stock, dtype="float64").pct_change().dropna()
    m = pd.Series(market, dtype="float64").pct_change().dropna()
    n = min(len(s), len(m))
    s, m = s.iloc[-n:].reset_index(drop=True), m.iloc[-n:].reset_index(drop=True)
    cov = ((s - s.mean()) * (m - m.mean())).sum() / (n - 1)
    var = ((m - m.mean()) ** 2).sum() / (n - 1)
    beta = cov / var
    resid = (s - s.mean()) - beta * (m - m.mean())
    std_err = ((resid ** 2).sum() / (n - 2) / ((m - m.mean()) ** 2).sum()) ** 0.5
    return beta, std_err, n


def analyse(ticker, folder, mk):
    annual = pd.read_csv(folder / f"{ticker}_statements.csv", index_col="line_item")
    q_file = folder / f"{ticker}_quarterly.csv"
    quarterly = pd.read_csv(q_file, index_col="line_item") if q_file.exists() else None
    latest = quarterly["TTM"] if quarterly is not None else annual.iloc[:, -1]
    basis = "latest twelve months" if quarterly is not None else f"fiscal year {annual.columns[-1][:4]}"

    rf, erp, tax = mk["risk_free_rate"], mk["equity_risk_premium"], mk["marginal_tax_rate"]
    table = mk["rating_table"]
    notes = []

    # ---- rating and cost of debt -------------------------------------------
    ebit, interest = get(latest, "operating_income"), get(latest, "interest_expense")
    debt, leases = get(latest, "total_debt"), get(latest, "op_lease_liability")
    if pd.isna(debt):
        debt = 0.0
        notes.append("No borrowings were found in the filings; debt is taken as zero (leases still count).")
    if pd.isna(leases):
        leases = 0.0
    if pd.isna(interest) and debt > 0:
        notes.append("Debt exists but no interest cost was found; the rating is overstated.")
    coverage, rating, spread, lease_interest = coverage_and_rating(ebit, interest, leases, rf, table)
    cost_of_debt = rf + spread
    total_debt = debt + leases

    # ---- cost of equity ----------------------------------------------------
    price = mk["prices"].get(ticker)
    closes, market = mk["monthly_closes"].get(ticker), mk["monthly_closes"].get("SPY")
    if price is None or closes is None:
        sys.exit(f"{ticker}: add its price and monthly closes to market_inputs.json first.")
    if mk.get("beta_fixed") is not None:           # backtests: one beta for every company
        raw_beta, beta_err, months = float(mk["beta_fixed"]), 0.0, 0
    else:
        raw_beta, beta_err, months = beta_from_prices(closes, market)
    # A measured beta is a noisy estimate, and extreme ones tend to drift back
    # toward 1 over time. This standard adjustment leans one third toward 1.
    # (Spec rule D1: when the measurement is imprecise, lean two thirds toward 1.)
    beta = (raw_beta + 2) / 3 if beta_err > 0.25 else 0.67 * raw_beta + 0.33
    if mk.get("beta_fixed") is not None:
        beta = raw_beta
    cost_of_equity = rf + beta * erp

    # ---- weights and WACC --------------------------------------------------
    shares = get(latest, "diluted_shares")
    equity_value = price * shares
    total = equity_value + total_debt
    w_e, w_d = equity_value / total, total_debt / total
    wacc = w_e * cost_of_equity + w_d * cost_of_debt * (1 - tax)

    if equity_value < 5e9:
        notes.append("Market value is under $5B. Damodaran's table is calibrated on large firms; "
                     "a smaller firm with the same coverage usually borrows at a wider spread.")
    if beta_err > 0.25:
        notes.append(f"Beta is imprecise (measured {raw_beta:.2f}, give or take {beta_err:.2f}).")

    # ---- rating history ----------------------------------------------------
    history = {}
    for year in annual.columns:
        col = annual[year]
        e = get(col, "operating_income")
        if pd.isna(e):
            continue
        cov, rat, _, _ = coverage_and_rating(e, get(col, "interest_expense"),
                                             get(col, "op_lease_liability"), rf, table)
        history[year[:4]] = (cov, rat)

    return {
        "basis": basis, "operating_profit": ebit, "interest_cost": interest,
        "lease_interest_estimate": lease_interest, "interest_coverage": coverage,
        "rating": rating, "spread": spread, "cost_of_debt_pretax": cost_of_debt,
        "cost_of_debt_aftertax": cost_of_debt * (1 - tax),
        "beta_measured": raw_beta, "beta_std_error": beta_err, "beta_used": beta,
        "cost_of_equity": cost_of_equity, "share_price": price, "shares": shares,
        "market_value_equity": equity_value, "borrowings": debt, "lease_debt": leases,
        "debt_total": total_debt, "weight_equity": w_e, "weight_debt": w_d, "wacc": wacc,
        "notes": notes, "history": history,
    }


def show(ticker, r, mk):
    pct = lambda v: f"{v * 100:.2f}%"
    bn = lambda v: "-" if pd.isna(v) else f"${v / 1e9:,.1f}B"
    cov = "no interest to cover" if r["interest_coverage"] == float("inf") else f"{r['interest_coverage']:.1f}x"
    print(f"\n{'=' * 72}\n{ticker}   (based on the {r['basis']})\n")
    print(f"  Operating profit            {bn(r['operating_profit'])}")
    print(f"  Interest on borrowings      {bn(r['interest_cost'])}")
    print(f"  Interest inside lease rent  {bn(r['lease_interest_estimate'])}  (estimate)")
    print(f"  Interest coverage           {cov}")
    print(f"  Rating                      {r['rating']}   (spread {pct(r['spread'])})")
    print(f"  Cost of debt                {pct(r['cost_of_debt_pretax'])} before tax, "
          f"{pct(r['cost_of_debt_aftertax'])} after tax")
    print(f"  Beta                        {r['beta_used']:.2f}   "
          f"(measured {r['beta_measured']:.2f} +/- {r['beta_std_error']:.2f})")
    print(f"  Cost of equity              {pct(r['cost_of_equity'])}")
    print(f"  Market value of shares      {bn(r['market_value_equity'])}  "
          f"({r['shares'] / 1e6:,.0f}M shares x ${r['share_price']:,.2f})")
    print(f"  Debt incl. leases           {bn(r['debt_total'])}  "
          f"(borrowings {bn(r['borrowings'])} + leases {bn(r['lease_debt'])})")
    print(f"  Funding mix                 {r['weight_equity'] * 100:.1f}% shareholders, "
          f"{r['weight_debt'] * 100:.1f}% lenders")
    print(f"  WACC                        {pct(r['wacc'])}")
    hist = "  ".join(f"{y}: {rat.split('/')[1]} ({'n/a' if c == float('inf') else f'{c:.1f}x'})"
                     for y, (c, rat) in list(r["history"].items())[-6:])
    print(f"\n  Rating each fiscal year would have earned (at today's rates):\n    {hist}")
    for n in r["notes"]:
        print(f"  [NOTE] {n}")


def main():
    tickers = [t.upper() for t in sys.argv[1:] if not t.startswith("-")]
    if not tickers:
        sys.exit("Usage: python cost_of_capital.py TICKER [TICKER ...]")
    folder = Path(".")
    mk = json.loads((folder / "market_inputs.json").read_text())
    print(f"Market inputs as of {mk['as_of']}: risk-free rate {mk['risk_free_rate'] * 100:.2f}%, "
          f"equity risk premium {mk['equity_risk_premium'] * 100:.2f}%, "
          f"tax rate {mk['marginal_tax_rate'] * 100:.0f}%")
    rows = {}
    for t in tickers:
        r = analyse(t, folder, mk)
        show(t, r, mk)
        rows[t] = {k: v for k, v in r.items() if k not in ("notes", "history", "basis")}
    out = pd.DataFrame(rows)
    out.to_csv(folder / "cost_of_capital.csv", index_label="metric")
    print(f"\nSaved cost_of_capital.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())

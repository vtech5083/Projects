#!/usr/bin/env python3
"""
analyze_working_capital.py  --  Analysis layer, step 1.

Reads the files written by edgar_statements.py (and edgar_quarterly.py, if
present) and works out two things per company:

1. Working capital and the cash conversion cycle
   How many days cash is tied up between paying suppliers and being paid
   by customers.

     DSO  days sales outstanding  = receivables      / revenue          x 365
     DIO  days inventory          = inventory        / cost of revenue  x 365
     DPO  days payables           = accounts payable / cost of revenue  x 365
     CCC  cash conversion cycle   = DSO + DIO - DPO

   Balances are averaged over the opening and closing dates of each year,
   so a single unusual year-end does not distort the result.

2. Stock-based pay as a real cost
   Reported operating profit already subtracts it. Reported cash flow adds
   it back, which makes cash flow look better than it is for owners. So the
   script shows free cash flow both ways, and tracks whether the share
   count is actually growing.

It needs no internet connection.

Usage
-----
    python analyze_working_capital.py AVGO
    python analyze_working_capital.py AVGO MSFT GOOG LULU     (adds a comparison table)
"""

import sys
from pathlib import Path

import pandas as pd

NAN = float("nan")


def load(ticker, folder):
    annual_file = folder / f"{ticker}_statements.csv"
    if not annual_file.exists():
        sys.exit(f"{annual_file.name} not found. Run edgar_statements.py {ticker} first.")
    data = pd.read_csv(annual_file, index_col="line_item")
    quarterly_file = folder / f"{ticker}_quarterly.csv"
    quarterly = pd.read_csv(quarterly_file, index_col="line_item") if quarterly_file.exists() else None
    return data, quarterly


def row(df, item):
    return df.loc[item] if item in df.index else pd.Series(NAN, index=df.columns)


def averaged(series):
    """Average of this period's and the previous period's balance."""
    prev = series.shift(1)
    return ((series + prev) / 2).where(prev.notna(), series)


def analyse(data, quarterly):
    """Return a table of metrics: one column per fiscal year, plus TTM if available."""
    a = data.copy()

    # Add a TTM column built from the quarterly file: flows are the last four
    # quarters, balances are the latest quarter averaged with a year earlier.
    ttm_balance_avg = {}
    if quarterly is not None and "TTM" in quarterly.columns:
        a["TTM"] = quarterly["TTM"].reindex(a.index)
        q_cols = [c for c in quarterly.columns if c != "TTM"]
        if len(q_cols) >= 5:
            for item in ["receivables", "inventory", "accounts_payable"]:
                now, year_ago = row(quarterly, item)[q_cols[-1]], row(quarterly, item)[q_cols[-5]]
                if pd.notna(now) and pd.notna(year_ago):
                    ttm_balance_avg[item] = (now + year_ago) / 2

    def avg_balance(item):
        s = averaged(row(a, item).drop(labels=["TTM"], errors="ignore"))
        if "TTM" in a.columns:
            s["TTM"] = ttm_balance_avg.get(item, row(a, item)["TTM"])
        return s

    revenue, cost = row(a, "revenue"), row(a, "cost_of_revenue")
    ar, inv, ap = row(a, "receivables"), row(a, "inventory"), row(a, "accounts_payable")
    cfo, capex, fcf = row(a, "cash_from_operations"), row(a, "capex"), row(a, "free_cash_flow")
    sbc, net_income = row(a, "stock_based_comp"), row(a, "net_income")
    shares, buybacks = row(a, "diluted_shares"), row(a, "buybacks")

    m = pd.DataFrame(index=a.columns, dtype="float64")

    # ---- Working capital ----------------------------------------------------
    m["dso_days"] = avg_balance("receivables") / revenue * 365
    m["dio_days"] = avg_balance("inventory") / cost * 365
    m["dpo_days"] = avg_balance("accounts_payable") / cost * 365
    # A company that reports no inventory (software, services) has none to wait on.
    never_has_inventory = inv.drop(labels=["TTM"], errors="ignore").isna().all()
    dio_for_cycle = m["dio_days"].fillna(0) if never_has_inventory else m["dio_days"]
    m["ccc_days"] = m["dso_days"] + dio_for_cycle - m["dpo_days"]

    nwc = ar.fillna(0) + inv.fillna(0) - ap.fillna(0)
    nwc = nwc.where(ar.notna() | inv.notna() | ap.notna())
    m["working_capital"] = nwc
    m["working_capital_pct_revenue"] = nwc / revenue * 100
    yearly = [c for c in a.columns if c != "TTM"]
    m.loc[yearly, "cash_absorbed_by_working_capital"] = nwc[yearly].diff()
    m["cash_from_ops_to_net_income"] = cfo / net_income.where(net_income > 0)

    # ---- Stock-based pay ----------------------------------------------------
    m["sbc"] = sbc
    m["sbc_pct_revenue"] = sbc / revenue * 100
    m["free_cash_flow"] = fcf
    m["fcf_after_sbc"] = fcf - sbc
    m["sbc_pct_fcf"] = (sbc / fcf.where(fcf > 0)) * 100
    m["fcf_margin_pct"] = fcf / revenue * 100
    m["fcf_after_sbc_margin_pct"] = (fcf - sbc) / revenue * 100
    m["diluted_shares"] = shares
    m.loc[yearly, "share_count_change_pct"] = shares[yearly].pct_change(fill_method=None) * 100
    m["buybacks"] = buybacks
    m["buybacks_minus_sbc"] = buybacks - sbc

    return m.T


def same_quarter(quarterly):
    """
    Days outstanding for the latest quarter and the same quarter one and two
    years earlier, each measured against that quarter's own sales.

    This is the fair way to spot a change. Comparing the latest twelve months
    with a fiscal year mixes different points in the season, and for a fast
    grower it measures today's balance against sales from a smaller company.
    """
    if quarterly is None:
        return None
    q = [c for c in quarterly.columns if c != "TTM"]
    picks = [q[i] for i in (-9, -5, -1) if len(q) >= -i]
    out = pd.DataFrame(index=["dso_days", "dio_days", "dpo_days"], columns=picks, dtype="float64")
    for c in picks:
        rev, cost = row(quarterly, "revenue")[c], row(quarterly, "cost_of_revenue")[c]
        out.loc["dso_days", c] = row(quarterly, "receivables")[c] / rev * 91
        out.loc["dio_days", c] = row(quarterly, "inventory")[c] / cost * 91
        out.loc["dpo_days", c] = row(quarterly, "accounts_payable")[c] / cost * 91
    return out


def flags(m, ticker, sq=None):
    """Plain-language warnings. Thresholds are screening rules of thumb, not laws."""
    out = []
    years = [c for c in m.columns if c != "TTM"]
    latest = "TTM" if "TTM" in m.columns else years[-1]

    def get(metric, col):
        v = m.loc[metric, col]
        return None if pd.isna(v) else float(v)

    for metric, label in [("dso_days", "Customers are taking longer to pay"),
                          ("dio_days", "Inventory is sitting longer")]:
        if sq is not None and sq.shape[1] >= 2:
            now, year_ago = sq.loc[metric].iloc[-1], sq.loc[metric].iloc[-2]
            if pd.notna(now) and pd.notna(year_ago) and now > 1.2 * year_ago and now - year_ago >= 5:
                out.append(f"{label}: {now:.0f} days in the latest quarter, against {year_ago:.0f} days "
                           f"in the same quarter a year earlier.")
        else:
            base = m.loc[metric, years[-4:-1]].dropna()
            now = get(metric, years[-1])
            if now is not None and len(base) >= 2 and base.mean() > 5 and now > 1.2 * base.mean():
                out.append(f"{label}: {now:.0f} days in the latest year, against a {base.mean():.0f}-day "
                           f"average over the three years before.")

    ratio = m.loc["cash_from_ops_to_net_income", years[-3:]].dropna()
    if len(ratio) and ratio.mean() < 0.8:
        out.append(f"Profit is not fully turning into cash: cash from operations has averaged "
                   f"{ratio.mean():.2f}x net income over the last three years.")

    v = get("sbc_pct_revenue", latest)
    if v is not None and v > 10:
        out.append(f"Stock-based pay is {v:.0f}% of revenue (above 10% is heavy).")
    v = get("sbc_pct_fcf", latest)
    fcf_now, after_now = get("free_cash_flow", latest), get("fcf_after_sbc", latest)
    if fcf_now is not None and after_now is not None and fcf_now > 0 > after_now:
        out.append("Stock-based pay is larger than free cash flow: free cash flow turns negative "
                   "once stock pay is counted as a cost.")
    elif v is not None and v > 25:
        out.append(f"Stock-based pay equals {v:.0f}% of free cash flow, so reported free cash flow "
                   f"overstates what owners keep by that share.")

    change = m.loc["share_count_change_pct", years[-3:]].dropna()
    spent = m.loc["buybacks", years[-3:]].dropna()
    if len(change) and change.mean() > 1:
        if len(spent) and spent.sum() > 0:
            out.append(f"Share count has risen {change.mean():.1f}% a year on average over three years "
                       f"despite ${spent.sum() / 1e9:,.1f}B of buybacks. Stock pay, or shares issued "
                       f"to buy another company, outweighed the buybacks.")
        else:
            out.append(f"Share count is rising {change.mean():.1f}% a year (owners are being diluted).")
    return out


def show(m, ticker):
    money = {"working_capital", "cash_absorbed_by_working_capital", "sbc", "free_cash_flow",
             "fcf_after_sbc", "buybacks", "buybacks_minus_sbc", "diluted_shares"}
    table = m.copy()
    for item in table.index:
        if item in money:
            table.loc[item] = table.loc[item] / 1e6
    table.columns = [c[:4] if c != "TTM" else c for c in table.columns]

    def fmt(item, v):
        if pd.isna(v):
            return "-"
        if item == "cash_from_ops_to_net_income":
            return f"{v:.2f}x"
        if item.endswith("_pct") or "_pct_" in item:
            return f"{v:.1f}%"
        if item.endswith("_days"):
            return f"{v:.0f}"
        return f"{v:,.0f}"

    text = pd.DataFrame({c: [fmt(i, table.loc[i, c]) for i in table.index] for c in table.columns},
                        index=table.index)
    print(f"\n{'=' * 78}\n{ticker}   (money in $ millions, shares in millions; column = fiscal year)\n")
    print(text.iloc[:, -7:].to_string())


def main():
    tickers = [t.upper() for t in sys.argv[1:] if not t.startswith("-")]
    if not tickers:
        sys.exit("Usage: python analyze_working_capital.py TICKER [TICKER ...]")
    folder = Path(".")
    summary = {}
    for ticker in tickers:
        data, quarterly = load(ticker, folder)
        m = analyse(data, quarterly)
        show(m, ticker)
        sq = same_quarter(quarterly)
        if sq is not None:
            print("\nSame quarter, year over year (days, measured against that quarter's own sales)\n")
            print(sq.round(0).to_string(na_rep="-", float_format=lambda x: f"{x:.0f}"))
        notes = flags(m, ticker, sq)
        print("\nFlags")
        for n in notes:
            print(f"  - {n}")
        if not notes:
            print("  none")
        m.to_csv(folder / f"{ticker}_working_capital.csv", index_label="metric")
        latest = "TTM" if "TTM" in m.columns else m.columns[-1]
        summary[ticker] = m[latest]

    if len(summary) > 1:
        keep = ["dso_days", "dio_days", "dpo_days", "ccc_days", "working_capital_pct_revenue",
                "sbc_pct_revenue", "sbc_pct_fcf", "fcf_margin_pct", "fcf_after_sbc_margin_pct"]
        comp = pd.DataFrame(summary).loc[keep]
        comp.to_csv(folder / "comparison_working_capital.csv", index_label="metric")
        print(f"\n{'=' * 78}\nComparison, latest twelve months\n")
        print(comp.round(1).to_string(na_rep="-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

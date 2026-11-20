#!/usr/bin/env python3
"""
edgar_quarterly.py  --  Quarterly statements and the latest twelve months.

Uses the same mapping table and checks as edgar_statements.py (keep the two
files in the same folder).

Why this needs its own logic
----------------------------
Quarterly filings (10-Q) do not hand over clean three-month numbers:

  * The cash flow statement is reported year-to-date only (6 months,
    9 months), so a single quarter = this year-to-date minus the previous one.
  * There is no fourth-quarter filing. Q4 = the full year (10-K) minus the
    first nine months.
  * Balance sheet items need no arithmetic; they are a snapshot on one day.

Output
------
  <TICKER>_quarterly.csv          last N quarters plus a TTM column
  <TICKER>_quarterly_sources.csv  the SEC tag and method behind each number

TTM ("trailing twelve months") = the last four quarters added together for
income and cash flow items, and the latest snapshot for balance sheet items.

Usage
-----
    python edgar_quarterly.py AVGO --email you@example.com
    python edgar_quarterly.py AVGO --email you@example.com --quarters 16
"""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from edgar_statements import (
    DERIVED, FACTS_URL, LINE_ITEMS, MISSING, SUBMISSIONS_URL,
    add_derived, adjust_for_splits, fix_share_units, TAKE_LARGEST, NOT_FOR_LARGEST, fill_shares_from_cover, industry_code, get_json, run_checks, ticker_to_cik,
)

FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A"}
DERIVED_ITEMS = {"total_equity", "op_lease_liability", "total_debt", "free_cash_flow"}
# Footnote items that companies normally publish once a year only.
ANNUAL_ONLY = {"debt_face_value"} | {k for k in LINE_ITEMS if k.startswith("debt_due_")}


def _days(start, end):
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def snapshots(gaap, tag, unit):
    """
    {date: value} for a balance sheet tag. For the same date, the audited
    annual filing beats a quarterly one; within each, the newest filing wins.
    """
    best = {}
    for e in gaap.get(tag, {}).get("units", {}).get(unit, []):
        if e.get("form") not in FORMS or "start" in e or "end" not in e:
            continue
        rank = (e["form"].startswith("10-K"), e.get("filed", ""))
        if e["end"] not in best or rank > best[e["end"]][1]:
            best[e["end"]] = (e["val"], rank)
    return {k: v for k, (v, _) in best.items()}


def periods(gaap, tag, unit):
    """{(start, end): value} for an income or cash flow tag, newest filing wins."""
    best = {}
    for e in gaap.get(tag, {}).get("units", {}).get(unit, []):
        if e.get("form") not in FORMS or "start" not in e or "end" not in e:
            continue
        key = (e["start"], e["end"])
        if key not in best or e.get("filed", "") > best[key][1]:
            best[key] = (e["val"], e.get("filed", ""))
    return {k: v for k, (v, _) in best.items()}


def quarter_value(spans, end, additive):
    """
    The three-month value for the quarter ending on `end`.
    Returns (value, method) or (None, None).
    """
    here = {s: v for (s, e), v in spans.items() if e == end}
    # 1. Reported directly as a three-month figure (13 or 14 weeks).
    for start, value in here.items():
        if 80 <= _days(start, end) <= 100:
            return value, "reported"
    if not additive:
        return None, None  # share counts cannot be found by subtraction
    # 2. Year-to-date (or full year) minus the year-to-date one quarter earlier.
    for start, value in sorted(here.items(), key=lambda kv: -_days(kv[0], end)):
        if _days(start, end) > 380:
            continue
        earlier = [(e, v) for (s, e), v in spans.items()
                   if s == start and e < end and 80 <= _days(e, end) <= 100]
        if earlier:
            return value - earlier[0][1], "year-to-date difference"
    return None, None


ALWAYS_POSITIVE = {"interest_expense", "interest_paid"}


def build_quarterly(gaap, quarter_ends):
    values = pd.DataFrame(index=list(LINE_ITEMS), columns=quarter_ends, dtype="float64")
    sources = pd.DataFrame(MISSING, index=list(LINE_ITEMS), columns=quarter_ends)
    for item, (_, kind, unit, tags) in LINE_ITEMS.items():
        if kind == "stock":
            per_tag = [(t, snapshots(gaap, t, unit)) for t in tags]
        else:
            per_tag = [(t, periods(gaap, t, unit)) for t in tags]
            if item in ALWAYS_POSITIVE:      # some filings sign these as negative, some as positive
                per_tag = [(t, {k: abs(v) for k, v in d.items()}) for t, d in per_tag]
        for end in quarter_ends:
            hits = []
            for tag, data in per_tag:
                if kind == "stock":
                    value, how = (data[end], "reported") if end in data else (None, None)
                else:
                    value, how = quarter_value(data, end, additive=(unit == "USD"))
                if value is not None:
                    hits.append((value, tag if how == "reported" else f"{tag} ({how})"))
                    if item not in TAKE_LARGEST:
                        break
            if hits:
                tag_hits = [(h[1].split(" (")[0], h[0], h[1]) for h in hits]
                pool = [h for h in tag_hits if h[0] not in NOT_FOR_LARGEST] or tag_hits
                best = max(pool, key=lambda h: h[1]) if item in TAKE_LARGEST else tag_hits[0]
                values.loc[item, end], sources.loc[item, end] = best[1], best[2]
    # A company sometimes switches tags between filings (one tag in the
    # quarterly reports, another in the annual one). Then no single tag can
    # produce the quarter, so fall back to: a running total under any tag,
    # minus the earlier quarters of that same stretch that were already found.
    for item, (_, kind, unit, tags) in LINE_ITEMS.items():
        if kind == "stock" or unit != "USD":
            continue
        all_spans = [(tag, periods(gaap, tag, unit)) for tag in tags]
        if item in ALWAYS_POSITIVE:
            all_spans = [(t, {k: abs(v) for k, v in d.items()}) for t, d in all_spans]
        for i, end in enumerate(quarter_ends):
            if pd.notna(values.loc[item, end]):
                continue
            for tag, spans in all_spans:
                done = False
                for (start, e), total in spans.items():
                    length = _days(start, e)
                    if e != end or not 150 <= length <= 380:
                        continue
                    n = round(length / 91) - 1          # earlier quarters in the stretch
                    if i < n:
                        continue
                    prior = values.loc[item, quarter_ends[i - n:i]]
                    if prior.notna().all() and quarter_ends[i - n] > start:
                        values.loc[item, end] = total - prior.sum()
                        sources.loc[item, end] = f"{tag} (running total minus {n} earlier quarter{'s' if n > 1 else ''})"
                        done = True
                        break
                if done:
                    break
    return values, sources


# Slow-moving items some companies report only once a year. When the last four
# quarters are not available, the latest full-year figure stands in.
YEARLY_STAND_INS = ["interest_expense", "interest_paid", "interest_income", "stock_based_comp",
                    "rd_expense", "depreciation_amortization", "depreciation_only",
                    "amortization_intangibles"]


def latest_full_year(gaap, item):
    """(value, year end) of the most recent full-year figure for a line item."""
    best = None
    for tag in LINE_ITEMS[item][3]:
        for (s0, e), v in periods(gaap, tag, "USD").items():
            if 340 <= _days(s0, e) <= 380 and (best is None or e > best[1]
                                               or (e == best[1] and item in TAKE_LARGEST and v > best[0])):
                best = (v, e)
    return best


def add_ttm(values, sources, amortization_hint=None, gaap=None, sic=None):
    """Add a TTM column: last four quarters summed, or the latest snapshot."""
    last4 = list(values.columns[-4:])
    latest = values.columns[-1]
    ttm, how = {}, {}
    for item in values.index:
        if item in DERIVED_ITEMS:
            continue
        kind, unit = LINE_ITEMS[item][1], LINE_ITEMS[item][2]
        if kind == "stock" and pd.notna(values.loc[item, latest]):
            ttm[item], how[item] = values.loc[item, latest], f"latest quarter ({latest})"
        elif kind == "stock" or unit != "USD":
            known = values.loc[item].dropna()
            ttm[item] = known.iloc[-1] if len(known) else float("nan")
            how[item] = f"last reported, as of {known.index[-1]}" if len(known) else MISSING
        elif len(last4) == 4 and values.loc[item, last4].notna().all():
            ttm[item], how[item] = values.loc[item, last4].sum(), "sum of last four quarters"
        else:
            ttm[item], how[item] = float("nan"), MISSING
            stand_in = latest_full_year(gaap, item) if gaap and item in YEARLY_STAND_INS else None
            if stand_in and stand_in[1] >= values.columns[0]:
                ttm[item], how[item] = abs(stand_in[0]), f"latest full year (to {stand_in[1]}); not reported quarterly"
    for item in ("interest_expense", "interest_paid"):
        if item in ttm:
            ttm[item] = abs(ttm[item])
    values["TTM"] = pd.Series(ttm)
    sources["TTM"] = pd.Series(how)
    # Recompute the derived rows for the TTM column from its own inputs.
    t_vals, t_src = add_derived(values[["TTM"]].drop(index=list(DERIVED_ITEMS)).copy(),
                                sources[["TTM"]].drop(index=list(DERIVED_ITEMS)).copy(),
                                amortization_hint=amortization_hint, sic=sic)
    values["TTM"], sources["TTM"] = t_vals["TTM"], t_src["TTM"]
    return values, sources


def annual_tieout(gaap, values):
    """Do four quarters add back to the audited full-year figure?"""
    results = []
    quarter_cols = [c for c in values.columns if c != "TTM"]
    for item in ["revenue", "operating_income", "net_income", "cash_from_operations", "capex"]:
        spans = {}
        for tag in LINE_ITEMS[item][3]:
            if item in TAKE_LARGEST and tag in NOT_FOR_LARGEST:
                continue
            for (s, e), v in periods(gaap, tag, "USD").items():
                if 340 <= _days(s, e) <= 380:
                    if item in TAKE_LARGEST:          # same choice as the quarters
                        spans[e] = max(spans.get(e, v), v)
                    else:
                        spans.setdefault(e, v)
        for fy_end, annual in spans.items():
            if fy_end not in quarter_cols:
                continue
            i = quarter_cols.index(fy_end)
            if i < 3:
                continue
            four = values.loc[item, quarter_cols[i - 3:i + 1]]
            if four.isna().any():
                results.append(("WARN", f"{fy_end}: {item} has a missing quarter in that fiscal year"))
            elif abs(four.sum() - annual) > 0.005 * max(abs(annual), 1.0):
                # Over 5% off is a real break (spin-off, big restatement);
                # smaller gaps are usually a restated year and only a warning.
                big = abs(four.sum() - annual) > 0.05 * max(abs(annual), 1.0)
                results.append(("FAIL" if big else "WARN", f"{fy_end}: {item} quarters sum to {four.sum():,.0f} "
                                        f"but the annual figure is {annual:,.0f}"))
            else:
                results.append(("OK", f"{fy_end}: {item} quarters add up to the year"))
    return results


def main():
    parser = argparse.ArgumentParser(description="Quarterly statements and TTM from SEC EDGAR.")
    parser.add_argument("ticker")
    parser.add_argument("--email", default=os.environ.get("SEC_EMAIL"))
    parser.add_argument("--quarters", type=int, default=12)
    parser.add_argument("--facts-file", help="use a saved companyfacts JSON instead of downloading")
    parser.add_argument("--out-dir", default=".")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    if args.facts_file:
        facts = json.loads(Path(args.facts_file).read_text())
        name = facts.get("entityName", ticker)
    else:
        if not args.email:
            sys.exit("Please pass --email you@example.com (the SEC requires a contact address).")
        cik, name = ticker_to_cik(ticker, args.email)
        facts = get_json(FACTS_URL.format(cik=cik), args.email, f"facts_{cik}.json")
    gaap = facts.get("facts", {}).get("us-gaap", {})
    if not gaap:
        sys.exit("No US accounting data found for this company.")

    ends = sorted(snapshots(gaap, "Assets", "USD"))
    if not ends:
        sys.exit("No balance sheet dates found for this company.")
    # Build four extra quarters at the front, then drop them. A fourth quarter
    # is found by subtracting the three before it, so the first quarters shown
    # need their own lead-in.
    lead = min(4, max(0, len(ends) - args.quarters))
    quarter_ends = ends[-(args.quarters + lead):]

    values, sources = build_quarterly(gaap, quarter_ends)
    values, sources = fill_shares_from_cover(facts, values, sources)
    values, sources = fix_share_units(values, sources)
    values, sources = values.iloc[:, lead:], sources.iloc[:, lead:]
    quarter_ends = quarter_ends[lead:]
    values, sources, split_notes = adjust_for_splits(gaap, values, sources)
    # The latest full-year write-off of bought intangibles, for companies that
    # stop reporting it quarter by quarter (see add_derived).
    yearly = {e: v for (s0, e), v in periods(gaap, "AmortizationOfIntangibleAssets", "USD").items()
              if 340 <= _days(s0, e) <= 380}
    hint = yearly[max(yearly)] if yearly else None
    sic = industry_code(facts)
    values, sources = add_derived(values, sources,
                                  amortization_hint=None if hint is None else hint / 4, sic=sic)

    checks = run_checks(values.drop(index=list(ANNUAL_ONLY)).reindex(values.index))
    checks += annual_tieout(gaap, values)
    values, sources = add_ttm(values, sources, amortization_hint=hint, gaap=gaap, sic=sic)

    print(f"\n{name} ({ticker})   last {len(quarter_ends)} quarters, plus the trailing twelve months (TTM)")
    pd.set_option("display.width", 250, "display.max_rows", 200, "display.max_columns", 50)
    print("\nAll figures in millions (dollars, or shares for diluted_shares)\n")
    shown = values.drop(index=[i for i in ANNUAL_ONLY if i in values.index]) / 1e6
    print(shown.to_string(na_rep="-", float_format=lambda x: f"{x:,.0f}"))

    print("\nChecks")
    counts = {"OK": 0, "WARN": 0, "FAIL": 0}
    for level, message in checks:
        counts[level] += 1
        if level != "OK":
            print(f"  [{level}] {message}")
    print(f"  {counts['OK']} passed, {counts['WARN']} warnings, {counts['FAIL']} failures")
    for note in split_notes:
        print(f"  [NOTE] {note}")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    values.to_csv(out / f"{ticker}_quarterly.csv", index_label="line_item")
    sources.to_csv(out / f"{ticker}_quarterly_sources.csv", index_label="line_item")
    # Keep the problems found, so the health check can refuse to trust bad data.
    pd.DataFrame([c for c in checks if c[0] != "OK"], columns=["level", "message"]).to_csv(
        out / f"{ticker}_quarterly_checks.csv", index=False)
    print(f"\nSaved {ticker}_quarterly.csv and {ticker}_quarterly_sources.csv in {out.resolve()}")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())

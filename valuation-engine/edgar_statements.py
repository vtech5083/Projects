#!/usr/bin/env python3
"""
edgar_statements.py  --  Data layer, step 1 of the valuation engine.

What it does
------------
1. Looks up a US company on SEC EDGAR by ticker.
2. Downloads every number the company has tagged in its filings.
3. Keeps the annual (10-K) figures for the last N fiscal years.
4. Maps the many possible SEC tag names onto one standard set of line items.
5. Runs sanity checks (does the balance sheet balance? is anything missing?).
6. Saves two CSV files:
     <TICKER>_statements.csv  the numbers, in raw dollars
     <TICKER>_sources.csv     which SEC tag each number came from

Usage
-----
    pip install requests pandas
    python edgar_statements.py MSFT --email you@example.com
    python edgar_statements.py MSFT --email you@example.com --years 10

The SEC asks every automated user to identify themselves with a contact
email. It is sent in the request header and nowhere else. You can also set
it once as an environment variable:  export SEC_EMAIL=you@example.com
"""

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
CACHE_DIR = Path("edgar_cache")
ANNUAL_FORMS = {"10-K", "10-K/A"}

# ---------------------------------------------------------------------------
# The mapping table.
#
# Each standard line item lists the SEC tags that can mean that item, in
# order of preference. For every year, the first tag that has a value wins.
#
#   "flow"  = measured over a year (revenue, cash flow)
#   "stock" = measured on one day  (anything on the balance sheet)
#
# When a company comes back with something MISSING, the fix is almost always
# to add one more tag name to the right list here.
# ---------------------------------------------------------------------------
LINE_ITEMS = {
    # ---- Income statement -------------------------------------------------
    "revenue": ("income", "flow", "USD", [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueGoodsNet",
    ]),
    "cost_of_revenue": ("income", "flow", "USD", [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
    ]),
    "gross_profit": ("income", "flow", "USD", ["GrossProfit"]),
    "rd_expense": ("income", "flow", "USD", [
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        "ResearchAndDevelopmentExpenseSoftwareExcludingAcquiredInProcessCost",
    ]),
    "sga_expense": ("income", "flow", "USD", [
        "SellingGeneralAndAdministrativeExpense",
    ]),
    "selling_marketing_expense": ("income", "flow", "USD", [
        "SellingAndMarketingExpense",
    ]),
    "general_admin_expense": ("income", "flow", "USD", [
        "GeneralAndAdministrativeExpense",
    ]),
    "operating_income": ("income", "flow", "USD", ["OperatingIncomeLoss"]),
    "costs_and_expenses": ("income", "flow", "USD", ["CostsAndExpenses"]),
    "interest_expense": ("income", "flow", "USD", [
        "InterestExpense",
        "InterestExpenseNonoperating",
        "InterestExpenseDebt",
        "InterestAndDebtExpense",
    ]),
    # Cash actually paid in interest: a stand-in when no interest cost line is tagged.
    "interest_paid": ("income", "flow", "USD", ["InterestPaidNet", "InterestPaid"]),
    "interest_income": ("income", "flow", "USD", [
        "InvestmentIncomeInterest",
        "InvestmentIncomeInterestAndDividend",
    ]),
    "pretax_income": ("income", "flow", "USD", [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ]),
    "income_tax": ("income", "flow", "USD", ["IncomeTaxExpenseBenefit"]),
    "net_income": ("income", "flow", "USD", [
        "NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic",
    ]),
    "eps_diluted": ("income", "flow", "USD/shares", ["EarningsPerShareDiluted"]),
    "diluted_shares": ("income", "flow", "shares", [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    ]),

    # ---- Balance sheet ----------------------------------------------------
    "cash": ("balance", "stock", "USD", [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ]),
    "short_term_investments": ("balance", "stock", "USD", [
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "DebtSecuritiesCurrent",
    ]),
    "receivables": ("balance", "stock", "USD", [
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
        "AccountsAndOtherReceivablesNetCurrent",
        "AccountsNotesAndLoansReceivableNetCurrent",
    ]),
    "inventory": ("balance", "stock", "USD", ["InventoryNet"]),
    "ppe_net": ("balance", "stock", "USD", [
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
    ]),
    "current_assets": ("balance", "stock", "USD", ["AssetsCurrent"]),
    "total_assets": ("balance", "stock", "USD", ["Assets"]),
    "accounts_payable": ("balance", "stock", "USD", ["AccountsPayableCurrent"]),
    "current_liabilities": ("balance", "stock", "USD", ["LiabilitiesCurrent"]),
    "debt_current_all": ("balance", "stock", "USD", ["DebtCurrent"]),
    "long_term_debt_current": ("balance", "stock", "USD", [
        "LongTermDebtCurrent",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
    ]),
    "short_term_borrowings": ("balance", "stock", "USD", [
        "ShortTermBorrowings",
        "CommercialPaper",
    ]),
    "long_term_debt_noncurrent": ("balance", "stock", "USD", [
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligations",
    ]),
    "long_term_debt_incl_current": ("balance", "stock", "USD", [
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
    ]),
    # Amount owed at face value, from the debt footnote. Used only as a
    # cross-check on total_debt, never as an input.
    "debt_face_value": ("balance", "stock", "USD", [
        "DebtInstrumentCarryingAmount",
        "DebtLongtermAndShorttermCombinedAmount",
    ]),
    "op_lease_liability_current": ("balance", "stock", "USD", [
        "OperatingLeaseLiabilityCurrent",
    ]),
    "op_lease_liability_noncurrent": ("balance", "stock", "USD", [
        "OperatingLeaseLiabilityNoncurrent",
    ]),
    "op_lease_liability_total": ("balance", "stock", "USD", [
        "OperatingLeaseLiability",
        # Last resort: the lease payments still owed, before discounting (a little too high).
        "LesseeOperatingLeaseLiabilityPaymentsDue",
    ]),
    "total_liabilities": ("balance", "stock", "USD", ["Liabilities"]),
    "temporary_equity": ("balance", "stock", "USD", [
        "TemporaryEquityCarryingAmountAttributableToParent",
        "RedeemableNoncontrollingInterestEquityCarryingAmount",
    ]),
    "stockholders_equity": ("balance", "stock", "USD", ["StockholdersEquity"]),
    "equity_incl_minority": ("balance", "stock", "USD", [
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ]),
    "liabilities_and_equity": ("balance", "stock", "USD", [
        "LiabilitiesAndStockholdersEquity",
    ]),

    # ---- Cash flow statement ----------------------------------------------
    "cash_from_operations": ("cashflow", "flow", "USD", [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ]),
    "capex": ("cashflow", "flow", "USD", [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ]),
    "depreciation_amortization": ("cashflow", "flow", "USD", [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
    ]),
    "depreciation_only": ("cashflow", "flow", "USD", ["Depreciation"]),
    "amortization_intangibles": ("cashflow", "flow", "USD", [
        "AmortizationOfIntangibleAssets",
    ]),
    "stock_based_comp": ("cashflow", "flow", "USD", [
        "ShareBasedCompensation",
        "AllocatedShareBasedCompensationExpense",
    ]),
    "dividends_paid": ("cashflow", "flow", "USD", [
        "PaymentsOfDividends",
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfOrdinaryDividends",
    ]),
    "buybacks": ("cashflow", "flow", "USD", [
        "PaymentsForRepurchaseOfCommonStock",
    ]),
    "acquisitions": ("cashflow", "flow", "USD", [
        "PaymentsToAcquireBusinessesNetOfCashAcquired",
        "PaymentsToAcquireBusinessesAndInterestInAffiliates",
    ]),

    # ---- Debt due by year (from the footnotes; often incomplete) ----------
    "debt_due_year_1": ("debt_schedule", "stock", "USD", [
        "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",
    ]),
    "debt_due_year_2": ("debt_schedule", "stock", "USD", [
        "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo",
    ]),
    "debt_due_year_3": ("debt_schedule", "stock", "USD", [
        "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree",
    ]),
    "debt_due_year_4": ("debt_schedule", "stock", "USD", [
        "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour",
    ]),
    "debt_due_year_5": ("debt_schedule", "stock", "USD", [
        "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive",
    ]),
    "debt_due_after_year_5": ("debt_schedule", "stock", "USD", [
        "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive",
    ]),
}

# If any of these is missing for a year, the later valuation steps cannot run.
REQUIRED = [
    "revenue", "operating_income", "net_income", "total_assets",
    "liabilities_and_equity", "cash_from_operations", "capex",
]

MISSING = "MISSING"
DERIVED = "derived"

# For these items a company can use two tags in the same year, one for the
# whole amount and one for a small part of it. Take the larger figure.
TAKE_LARGEST = {"rd_expense", "revenue"}
# Revenue including excise taxes is never preferred over revenue without them.
NOT_FOR_LARGEST = {"RevenueFromContractWithCustomerIncludingAssessedTax"}


def pick(item, hits):
    """hits: list of (tag, value). The first in priority order, or the largest where allowed."""
    if not hits:
        return None
    if item in TAKE_LARGEST:
        pool = [h for h in hits if h[0] not in NOT_FOR_LARGEST] or hits
        return max(pool, key=lambda h: h[1])
    return hits[0]


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------
def get_json(url, email, cache_name, max_age_hours=24):
    """Download a JSON file from the SEC, keeping a local copy for a day."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / cache_name
    if cache_file.exists():
        age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
        if age_hours < max_age_hours:
            return json.loads(cache_file.read_text())

    headers = {"User-Agent": f"valuation-engine research {email}"}
    response = requests.get(url, headers=headers, timeout=30)
    if response.status_code == 403:
        sys.exit("The SEC refused the request (403). Check that --email is a "
                 "real contact address; the SEC blocks requests without one.")
    if response.status_code == 404:
        sys.exit(f"Not found on EDGAR: {url}")
    response.raise_for_status()
    time.sleep(0.2)  # stay far below the SEC limit of 10 requests a second
    data = response.json()
    cache_file.write_text(json.dumps(data))
    return data


def ticker_to_cik(ticker, email):
    """EDGAR identifies companies by a number (the CIK), not by ticker."""
    table = get_json(TICKER_URL, email, "company_tickers.json", max_age_hours=168)
    wanted = ticker.upper().replace(".", "-")
    for row in table.values():
        if row["ticker"].upper() == wanted:
            return int(row["cik_str"]), row["title"]
    sys.exit(f"Ticker '{ticker}' was not found in the SEC ticker list.")


# ---------------------------------------------------------------------------
# Picking the right numbers out of the raw data
# ---------------------------------------------------------------------------
def _days(start, end):
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def annual_values(gaap, tag, kind, unit, forms=None):
    """
    Return {period_end_date: value} for one SEC tag, annual figures only.

    A 10-K repeats earlier years next to the new one, and later filings
    sometimes restate old numbers. When the same period appears more than
    once, the most recently filed version is kept.
    """
    if forms is None:
        forms = ANNUAL_FORMS
    entries = gaap.get(tag, {}).get("units", {}).get(unit, [])
    best = {}
    for e in entries:
        if e.get("form") not in forms or "end" not in e:
            continue
        has_start = "start" in e
        if kind == "flow":
            # A full year only (52 or 53 weeks), not a quarter.
            if not has_start or not 340 <= _days(e["start"], e["end"]) <= 380:
                continue
        elif has_start:
            continue
        end, filed = e["end"], e.get("filed", "")
        if end not in best or filed > best[end][1]:
            best[end] = (e["val"], filed)
    return {end: val for end, (val, _) in best.items()}


def fiscal_year_ends(gaap, years):
    """The last N fiscal year-end dates, taken from when total assets was reported."""
    ends = sorted(annual_values(gaap, "Assets", "stock", "USD", forms=ANNUAL_FORMS))
    if not ends:
        sys.exit("No annual 'Assets' figure found. This company may not file "
                 "10-Ks with tagged data (foreign filers use other forms).")
    return ends[-years:]


def build_tables(gaap, year_ends):
    """Fill the standard line items for each year, remembering the source tag."""
    values = pd.DataFrame(index=list(LINE_ITEMS), columns=year_ends, dtype="float64")
    sources = pd.DataFrame(MISSING, index=list(LINE_ITEMS), columns=year_ends)

    for item, (_, kind, unit, tags) in LINE_ITEMS.items():
        per_tag = [(tag, annual_values(gaap, tag, kind, unit)) for tag in tags]
        # A year-end balance sheet figure is sometimes tagged only in the next
        # quarterly filing, where it appears as the comparison column. Those
        # are used as a last resort: the audited annual filing always wins.
        from_10q = []
        if kind == "stock":
            from_10q = [(tag, annual_values(gaap, tag, kind, unit, forms={"10-Q", "10-Q/A"}))
                        for tag in tags]
        for end in year_ends:
            hit = pick(item, [(tag, found[end]) for tag, found in per_tag if end in found])
            if hit:
                values.loc[item, end] = hit[1]
                sources.loc[item, end] = hit[0]
            else:
                for tag, found in from_10q:
                    if end in found:
                        values.loc[item, end] = found[end]
                        sources.loc[item, end] = f"{tag} (from a later 10-Q)"
                        break
    return values, sources


def cover_shares(facts, period_end):
    """
    Shares outstanding from the cover page of the filing for `period_end`,
    summed over share classes. A stand-in when no total diluted share count is
    tagged (Alphabet before 2022 reported each class separately).
    """
    rows = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {})
    rows = rows.get("units", {}).get("shares", [])
    best = None
    for x in rows:
        days = (pd.Timestamp(x["end"]) - pd.Timestamp(period_end)).days
        if 0 <= days <= 120 and (best is None or x["end"] < best[0]):
            best = (x["end"], x["accn"])
    if best is None:
        return None
    return sum(x["val"] for x in rows if x["end"] == best[0] and x["accn"] == best[1])


def fill_shares_from_cover(facts, values, sources):
    for end in values.columns:
        if end == "TTM" or not pd.isna(values.loc["diluted_shares", end]):
            continue
        n = cover_shares(facts, end)
        if n:
            values.loc["diluted_shares", end] = float(n)
            sources.loc["diluted_shares", end] = "dei:EntityCommonStockSharesOutstanding (cover page, all classes; not diluted)"
    return values, sources


def fix_share_units(values, sources):
    """
    A few companies file their share count in millions (751.8) in some years
    and in single shares (751,800,000) in others. Put them all in single shares.
    """
    # A few companies file interest cost as a negative number. It is a cost either way.
    for item in ("interest_expense", "interest_paid"):
        values.loc[item] = values.loc[item].abs()

    row = values.loc["diluted_shares"]
    known = row.dropna()
    if known.empty:
        return values, sources
    if known.max() < 1e6:
        # Every period is small. If the company is large, the whole row is in millions.
        revenue = values.loc["revenue"].dropna()
        if len(revenue) and revenue.max() > 1e8 and known.max() < 1e5:
            values.loc["diluted_shares"] = row * 1e6
            for end in known.index:
                sources.loc["diluted_shares", end] += " (filed in millions; converted)"
        return values, sources
    # A period filed in thousands (1,235,907 for 1.24 billion) or millions sits
    # far below the true count. The yardstick for each period is net income /
    # diluted EPS, which is always in single shares; where that is missing, the
    # cover-page count, and only then the median of the row.
    cover = [known[e] for e in known.index if "cover page" in str(sources.loc["diluted_shares", e])]
    fallback = sorted(cover)[len(cover) // 2] if cover else known.median()
    for end in known.index:
        typical = fallback
        if "eps_diluted" in values.index and "net_income" in values.index:
            ni, eps = values.loc["net_income", end], values.loc["eps_diluted", end]
            if pd.notna(ni) and pd.notna(eps) and abs(eps) >= 0.05 and abs(ni) > 0:
                typical = abs(ni / eps)
        if known[end] < typical / 100:
            for scale in (1e6, 1e3):
                if 0.5 < known[end] * scale / typical < 2:
                    values.loc["diluted_shares", end] = known[end] * scale
                    sources.loc["diluted_shares", end] += f" (filed in units of {scale:,.0f}; converted)"
                    break
    return values, sources


# ---------------------------------------------------------------------------
# Stock splits
# ---------------------------------------------------------------------------
SPLIT_RATIOS = (1.5, 2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40, 50, 100)
SPLIT_MIN_SUPPORT = 4      # real splits show up in 14+ restated figures; 2-3 can be coincidence
SPLIT_EVIDENCE = ("WeightedAverageNumberOfDilutedSharesOutstanding",
                  "WeightedAverageNumberOfSharesOutstandingBasic",
                  "EarningsPerShareDiluted", "EarningsPerShareBasic")


def split_events(gaap_list):
    """
    Stock splits, found from the filings themselves: after a split, the next
    filings restate earlier periods' share counts (and earnings per share) by
    exactly the split ratio. Two filings reporting the same period, one with
    a count k times the other (k a usual split ratio), date a split between
    their filing dates. Buybacks cannot fake this, because the period is the
    same. Returns [{"ratio", "after", "by", "support"}], where the split took
    effect after `after` and the first filing on the new basis was filed `by`.
    A ratio below 1 is a reverse split. gaap_list: the us-gaap facts of one or
    more SEC registrations of the same company (an old and a new parent).
    """
    seen = []
    for gaap in gaap_list:
        for concept in SPLIT_EVIDENCE:
            per_share = concept.startswith("EarningsPerShare")
            for entries in gaap.get(concept, {}).get("units", {}).values():
                groups = {}
                for e in entries:
                    v = e.get("val")
                    if isinstance(v, (int, float)) and v and e.get("filed") and e.get("end"):
                        groups.setdefault((e.get("start"), e["end"]), []).append((e["filed"], float(v)))
                for obs in groups.values():
                    obs.sort()
                    for (f0, v0), (f1, v1) in zip(obs, obs[1:]):
                        if f1 <= f0 or (v0 > 0) != (v1 > 0):
                            continue
                        r = (v0 / v1) if per_share else (v1 / v0)
                        k = r if r >= 1 else 1 / r
                        if per_share and abs(v1) < 0.10:      # tiny EPS is too rounded to read a ratio from
                            continue
                        tol = 0.03 if per_share else 0.02
                        match = next((s for s in SPLIT_RATIOS if abs(k - s) / s < tol), None)
                        if match:
                            seen.append((match if r >= 1 else 1 / match, f0, f1))
    # one split shows up in many periods and concepts: merge overlapping windows
    events = []
    for ratio, after, by in sorted(seen, key=lambda x: (x[2], x[1])):
        for ev in events:
            if ev["ratio"] == ratio and after < ev["by"] and by > ev["after"]:
                ev["after"], ev["by"] = max(ev["after"], after), min(ev["by"], by)
                ev["support"] += 1
                break
        else:
            events.append({"ratio": ratio, "after": after, "by": by, "support": 1})
    return sorted([e for e in events if e["support"] >= SPLIT_MIN_SUPPORT], key=lambda e: e["by"])


def split_factor_since(events, as_of):
    """How many of today's shares one share was at `as_of`, judged from filings
    up to that date: the product of every split first reported after it."""
    f = 1.0
    for e in events:
        if e["by"] > as_of:
            f *= e["ratio"]
    return f


def adjust_for_splits(gaap, values, sources):
    """
    Put every period's share count on today's basis.

    After a split (or a reverse split), a company restates only the periods
    shown as comparisons in its newer filings. Older periods stay on the old
    basis, and restated and unrestated periods can be mixed together.

    Working back from the newest period, each older period is multiplied by
    whichever combination of the company's own reported split ratios makes
    it line up best with the period after it. Only splits dated after a
    period can apply to it. The multiplier carried over from the newer
    period is kept unless another one fits clearly better, so ordinary
    changes in the share count are not mistaken for splits.
    """
    from itertools import combinations
    from math import log, prod

    by_ratio = {}
    for unit_entries in gaap.get("StockholdersEquityNoteStockSplitConversionRatio1", {}).get("units", {}).values():
        for e in unit_entries:
            v = e.get("val")
            if isinstance(v, (int, float)) and v > 0 and abs(v - 1) > 1e-9 and e.get("end"):
                by_ratio[float(v)] = max(by_ratio.get(float(v), ""), e["end"])
    splits = [(d, r) for r, d in by_ratio.items()]
    notes = []
    if not splits:
        return values, sources, notes

    cols = [c for c in values.columns if pd.notna(values.loc["diluted_shares", c]) and values.loc["diluted_shares", c] > 0]
    if not cols:
        return values, sources, notes

    def apply(col, raw, mult):
        if mult != 1.0:
            values.loc["diluted_shares", col] = raw * mult
            sources.loc["diluted_shares", col] += f" (split-adjusted x{mult:g})"

    last = cols[-1]
    state = prod([r for d, r in splits if d > last]) if any(d > last for d, _ in splits) else 1.0
    newer = float(values.loc["diluted_shares", last]) * state
    if state != 1.0:
        notes.append(f"{last}: share count multiplied by {state:g} for a split after that date")
    apply(last, float(values.loc["diluted_shares", last]), state)

    prev_end = last
    for col in reversed(cols[:-1]):
        raw = float(values.loc["diluted_shares", col])
        after = [r for d, r in splits if d > col]
        newly = [r for d, r in splits if col < d <= prev_end]
        default = state * (prod(newly) if newly else 1.0)
        options = {default, 1.0}
        for n in range(1, min(len(after), 4) + 1):
            for combo in combinations(after, n):
                options.add(prod(combo))
        score = lambda m: abs(log(newer / (raw * m)))
        best = min(options, key=score)
        if not (score(default) > log(1.6) and score(best) < score(default) - log(1.5)):
            best = default
        if best != state:
            notes.append(f"{col} and earlier: share count multiplier set to {best:g}")
        apply(col, raw, best)
        state, newer, prev_end = best, raw * best, col
    return values, sources, notes


# ---------------------------------------------------------------------------
# Derived line items (simple arithmetic on the raw ones)
# ---------------------------------------------------------------------------
# Oil and gas: "cost of goods" there means oil and gas bought, so gross
# profit minus overheads is far too high (crude production, refining,
# oilfield services, pipelines, wholesale fuel).
OIL_GAS_SIC = {1311, 1381, 1382, 1389, 2911, 2990, 4612, 4613, 4922, 4923, 5171, 5172}


def industry_code(facts):
    """The company's SEC industry code, from its saved profile, if there is one."""
    try:
        info = json.loads((Path(__file__).resolve().parent / "edgar_cache" / f"info_{int(facts['cik'])}.json").read_text())
        return int(info["sic"]) if str(info.get("sic", "")).isdigit() else None
    except (KeyError, ValueError, OSError):
        return None


def add_derived(values, sources, amortization_hint=None, sic=None):
    """`amortization_hint`: a recent yearly write-off of bought intangibles, used
    only to estimate operating profit when the year itself has no such figure."""
    last_amortization = amortization_hint

    def have(item, end):
        return pd.notna(values.loc[item, end])

    gross_route = []      # periods whose operating profit came from gross profit

    def val(item, end):
        v = values.loc[item, end]
        return 0.0 if pd.isna(v) else float(v)

    for item in ["total_equity", "op_lease_liability", "total_debt", "free_cash_flow"]:
        values.loc[item] = float("nan")
        sources.loc[item] = MISSING

    # Spending on equipment, when the company used its own tag for it (NVIDIA
    # 2016-2021): change in net equipment plus this period's depreciation.
    cols = [c for c in values.columns if c != "TTM"]
    for before, end in zip(cols, cols[1:]):
        if have("capex", end) or not (have("ppe_net", end) and have("ppe_net", before)):
            continue
        dep = (val("depreciation_only", end) if have("depreciation_only", end) else
               val("depreciation_amortization", end) - val("amortization_intangibles", end))
        if dep <= 0:
            continue
        values.loc["capex", end] = max(val("ppe_net", end) - val("ppe_net", before) + dep, 0.0)
        sources.loc["capex", end] = f"{DERIVED}: change in net equipment + depreciation (estimate; not tagged)"

    for end in values.columns:
        # Gross profit, when the company doesn't report it as its own line.
        if not have("gross_profit", end) and have("revenue", end) and have("cost_of_revenue", end):
            values.loc["gross_profit", end] = val("revenue", end) - val("cost_of_revenue", end)
            sources.loc["gross_profit", end] = DERIVED

        # Share count from earnings per share, when no total is tagged
        # (Alphabet before 2022 reported each share class separately).
        if (end != "TTM" and not have("diluted_shares", end) and have("eps_diluted", end)
                and have("net_income", end) and val("eps_diluted", end) != 0):
            values.loc["diluted_shares", end] = val("net_income", end) / val("eps_diluted", end)
            sources.loc["diluted_shares", end] = f"{DERIVED}: net income / diluted EPS"

        # Interest cost, when the company only tags the cash it paid in interest.
        if not have("interest_expense", end) and have("interest_paid", end):
            values.loc["interest_expense", end] = val("interest_paid", end)
            sources.loc["interest_expense", end] = f"{DERIVED}: InterestPaidNet (cash paid, used as a stand-in)"

        # Selling + general costs, when the company reports them as two lines.
        if (not have("sga_expense", end) and have("selling_marketing_expense", end)
                and have("general_admin_expense", end)):
            values.loc["sga_expense", end] = (
                val("selling_marketing_expense", end) + val("general_admin_expense", end))
            sources.loc["sga_expense", end] = (
                f"{DERIVED}: SellingAndMarketingExpense + GeneralAndAdministrativeExpense")

        # Depreciation + amortization, when only the two halves are reported.
        if (not have("depreciation_amortization", end) and have("depreciation_only", end)
                and have("amortization_intangibles", end)):
            values.loc["depreciation_amortization", end] = (
                val("depreciation_only", end) + val("amortization_intangibles", end))
            sources.loc["depreciation_amortization", end] = (
                f"{DERIVED}: Depreciation + AmortizationOfIntangibleAssets")

        # Depreciation alone, when the company has no amortization to report.
        if not have("depreciation_amortization", end) and have("depreciation_only", end):
            values.loc["depreciation_amortization", end] = val("depreciation_only", end)
            sources.loc["depreciation_amortization", end] = f"{DERIVED}: Depreciation only"

        # Operating profit, when the company's income statement has no such line
        # (common for drug makers and some consumer companies):
        #   gross profit - selling and general costs - research.
        # Some companies also show the write-off of bought intangibles as its own
        # cost line. We cannot see the layout, so we take whichever version sits
        # closer to profit before tax and interest.
        # Operating profit from total costs, when the company reports its costs
        # as one total (oil majors). If that total already includes interest
        # (revenue minus costs is close to profit before tax), interest is added back.
        if not have("operating_income", end) and have("costs_and_expenses", end) and have("revenue", end):
            op = val("revenue", end) - val("costs_and_expenses", end)
            how = "revenue - total costs"
            if (have("pretax_income", end) and have("interest_expense", end)
                    and abs(op - val("pretax_income", end)) <= 0.1 * abs(val("pretax_income", end))):
                op += val("interest_expense", end)
                how += " + interest expense"
            values.loc["operating_income", end] = op
            sources.loc["operating_income", end] = f"{DERIVED}: {how} (estimate; not reported by the company)"

        if (not have("operating_income", end) and have("gross_profit", end)
                and have("sga_expense", end) and sic not in OIL_GAS_SIC):
            plain = val("gross_profit", end) - val("sga_expense", end) - val("rd_expense", end)
            how = "gross profit - SG&A - R&D"
            if have("amortization_intangibles", end):
                last_amortization = val("amortization_intangibles", end)
            if last_amortization is not None and have("pretax_income", end):
                guide = (val("pretax_income", end) + val("interest_expense", end)
                         - val("interest_income", end))
                lower = plain - last_amortization
                if abs(lower - guide) < abs(plain - guide):
                    plain, how = lower, how + " - amortization of intangibles"
            values.loc["operating_income", end] = plain
            sources.loc["operating_income", end] = f"{DERIVED}: {how} (estimate; not reported by the company)"
            gross_route.append(end)

        # Last resort for companies whose income statement has no operating
        # line and no gross profit (oil majors): profit before tax plus
        # interest paid, minus interest received.
        if not have("operating_income", end) and have("pretax_income", end):
            values.loc["operating_income", end] = (val("pretax_income", end) + val("interest_expense", end)
                                                   - val("interest_income", end))
            sources.loc["operating_income", end] = (f"{DERIVED}: profit before tax + interest expense - interest "
                                                    "income (estimate; not reported by the company)")

        # Equity including outside owners' share of subsidiaries, if reported.
        for item in ["equity_incl_minority", "stockholders_equity"]:
            if have(item, end):
                values.loc["total_equity", end] = val(item, end)
                sources.loc["total_equity", end] = f"{DERIVED}: {item}"
                break

        # Many companies never tag total liabilities, so back into it.
        if (not have("total_liabilities", end) and have("liabilities_and_equity", end)
                and have("total_equity", end)):
            values.loc["total_liabilities", end] = (
                val("liabilities_and_equity", end) - val("total_equity", end)
                - val("temporary_equity", end))
            sources.loc["total_liabilities", end] = DERIVED

        # Operating lease liability = the part due this year + the rest.
        both_parts = have("op_lease_liability_current", end) and have("op_lease_liability_noncurrent", end)
        if have("op_lease_liability_total", end) and not both_parts:
            values.loc["op_lease_liability", end] = val("op_lease_liability_total", end)
            sources.loc["op_lease_liability", end] = f"{DERIVED}: OperatingLeaseLiability"
        elif have("op_lease_liability_current", end) or have("op_lease_liability_noncurrent", end):
            values.loc["op_lease_liability", end] = (
                val("op_lease_liability_current", end)
                + val("op_lease_liability_noncurrent", end))
            sources.loc["op_lease_liability", end] = DERIVED

        # Total debt. Companies tag debt in overlapping ways, so the order
        # below is chosen to avoid counting the same borrowing twice.
        if have("debt_current_all", end):
            short_part, short_src = val("debt_current_all", end), "DebtCurrent"
        else:
            short_part = val("long_term_debt_current", end) + val("short_term_borrowings", end)
            short_src = "LongTermDebtCurrent + ShortTermBorrowings"
        if have("long_term_debt_noncurrent", end):
            total, how = val("long_term_debt_noncurrent", end) + short_part, f"LongTermDebtNoncurrent + {short_src}"
        elif have("long_term_debt_incl_current", end):
            total = val("long_term_debt_incl_current", end) + val("short_term_borrowings", end)
            how = "LongTermDebt + ShortTermBorrowings"
        elif short_part:
            total, how = short_part, short_src
        else:
            total, how = None, None
        if total is not None:
            values.loc["total_debt", end] = total
            sources.loc["total_debt", end] = f"{DERIVED}: {how}"

        # Free cash flow = cash from the business minus spending on equipment.
        if have("cash_from_operations", end) and have("capex", end):
            values.loc["free_cash_flow", end] = val("cash_from_operations", end) - val("capex", end)
            sources.loc["free_cash_flow", end] = DERIVED

    return values, sources


# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
def run_checks(values, tolerance=0.001):
    """Return a list of (level, message). Levels: OK, WARN, FAIL."""
    results = []

    def close(a, b):
        return abs(a - b) <= tolerance * max(abs(a), abs(b), 1.0)

    for end in values.columns:
        col = values[end]

        # 1. Assets must equal liabilities plus equity.
        a, le = col["total_assets"], col["liabilities_and_equity"]
        if pd.isna(a) or pd.isna(le):
            results.append(("FAIL", f"{end}: cannot test the balance sheet (total missing)"))
        elif close(a, le):
            results.append(("OK", f"{end}: balance sheet balances"))
        else:
            results.append(("FAIL", f"{end}: assets {a:,.0f} != liabilities + equity {le:,.0f}"))

        # 2. The pieces should add up to the total as well.
        parts = [col["total_liabilities"], col["total_equity"]]
        if not pd.isna(a) and not any(pd.isna(p) for p in parts):
            pieces = sum(parts) + (0 if pd.isna(col["temporary_equity"]) else col["temporary_equity"])
            if not close(a, pieces):
                results.append(("WARN", f"{end}: liabilities + equity pieces sum to {pieces:,.0f} "
                                        f"but assets are {a:,.0f} (gap {a - pieces:,.0f})"))

        # 3. Revenue minus cost should equal gross profit.
        r, c, g = col["revenue"], col["cost_of_revenue"], col["gross_profit"]
        if not any(pd.isna(x) for x in (r, c, g)) and not close(r - c, g):
            results.append(("WARN", f"{end}: revenue - cost = {r - c:,.0f} but gross profit = {g:,.0f}; "
                                    "the revenue and cost tags may not be a matching pair"))

        # 4. Total debt should be close to what the debt footnote says is owed.
        due = [col[f"debt_due_year_{n}"] for n in range(1, 6)] + [col["debt_due_after_year_5"]]
        footnote = col["debt_face_value"]
        if pd.isna(footnote) and not any(pd.isna(d) for d in due):
            footnote = sum(due)
        # The footnote covers long-term borrowings at face value, so compare
        # like with like: leave out short-term paper, and allow for the gap
        # between face value and the discounted amount on the balance sheet.
        td = col["total_debt"]
        if not pd.isna(td) and not pd.isna(col["short_term_borrowings"]) and pd.isna(col["debt_current_all"]):
            td = td - col["short_term_borrowings"]
        if not pd.isna(footnote) and footnote > 0:
            if pd.isna(td):
                results.append(("WARN", f"{end}: no total debt found, but the debt footnote shows {footnote:,.0f}"))
            elif abs(td - footnote) > 0.20 * footnote:
                results.append(("WARN", f"{end}: long-term debt {td:,.0f} is far from the debt footnote "
                                        f"{footnote:,.0f}; a debt tag is probably missing from the mapping"))

        # 5. Anything the valuation cannot do without.
        gone = [item for item in REQUIRED if pd.isna(col[item])]
        if gone:
            results.append(("FAIL", f"{end}: missing required items: {', '.join(gone)}"))

    # 6. Share count should not jump between years unless a split explains it.
    cols = [c for c in values.columns if not pd.isna(values.loc["diluted_shares", c])]
    for older, newer in zip(cols, cols[1:]):
        a, b = values.loc["diluted_shares", older], values.loc["diluted_shares", newer]
        if a > 0 and not 0.6 <= b / a <= 1.7:
            results.append(("WARN", f"{newer}: share count changed {b / a:.1f}x from {older}; "
                                    "a split missing from the data, or a large share issue"))

    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def print_report(name, ticker, sic, values, checks):
    print(f"\n{name} ({ticker})   fiscal years ending: {', '.join(values.columns)}")
    if sic and 6000 <= sic <= 6799:
        print("\n  WARNING: this is a financial company (bank, insurer, real estate).\n"
              "  The standard valuation steps do not apply to it.")

    pd.set_option("display.width", 200, "display.max_rows", 200)
    print("\nAll figures in millions (dollars, or shares for diluted_shares)\n")
    print((values / 1e6).to_string(na_rep="-", float_format=lambda x: f"{x:,.0f}"))

    print("\nChecks")
    counts = {"OK": 0, "WARN": 0, "FAIL": 0}
    for level, message in checks:
        counts[level] += 1
        if level != "OK":
            print(f"  [{level}] {message}")
    print(f"  {counts['OK']} passed, {counts['WARN']} warnings, {counts['FAIL']} failures")


def main():
    parser = argparse.ArgumentParser(description="Pull standardized annual statements from SEC EDGAR.")
    parser.add_argument("ticker")
    parser.add_argument("--email", default=os.environ.get("SEC_EMAIL"),
                        help="contact email the SEC requires (or set SEC_EMAIL)")
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--facts-file", help="use a saved companyfacts JSON instead of downloading")
    parser.add_argument("--out-dir", default=".")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    sic = None
    if args.facts_file:
        facts = json.loads(Path(args.facts_file).read_text())
        name = facts.get("entityName", ticker)
        sic = industry_code(facts)
    else:
        if not args.email:
            sys.exit("Please pass --email you@example.com (the SEC requires a contact address).")
        cik, name = ticker_to_cik(ticker, args.email)
        facts = get_json(FACTS_URL.format(cik=cik), args.email, f"facts_{cik}.json")
        info = get_json(SUBMISSIONS_URL.format(cik=cik), args.email, f"info_{cik}.json")
        sic = int(info["sic"]) if str(info.get("sic", "")).isdigit() else None

    gaap = facts.get("facts", {}).get("us-gaap", {})
    if not gaap:
        sys.exit("No US accounting data found for this company.")

    year_ends = fiscal_year_ends(gaap, args.years)
    values, sources = build_tables(gaap, year_ends)
    values, sources = fill_shares_from_cover(facts, values, sources)
    values, sources = fix_share_units(values, sources)
    values, sources, split_notes = adjust_for_splits(gaap, values, sources)
    values, sources = add_derived(values, sources, sic=sic)
    checks = run_checks(values)
    print_report(name, ticker, sic, values, checks)
    for note in split_notes:
        print(f"  [NOTE] {note}")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    values.to_csv(out / f"{ticker}_statements.csv", index_label="line_item")
    sources.to_csv(out / f"{ticker}_sources.csv", index_label="line_item")
    print(f"\nSaved {ticker}_statements.csv and {ticker}_sources.csv in {out.resolve()}")

    return 1 if any(level == "FAIL" for level, _ in checks) else 0


if __name__ == "__main__":
    sys.exit(main())

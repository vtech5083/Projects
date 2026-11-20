#!/usr/bin/env python3
"""
make_dashboard_data.py  --  Turn the saved run records into the three tables
the dashboard reads. Run after run.py:

    python make_dashboard_data.py

Reads   runs/latest.json  (the newest record for each company)
Writes  dashboard_data/companies.json    one row per company
        dashboard_data/checks.json       one row per health check per company
        dashboard_data/projection.json   the ten-year projection, one row per year
        dashboard_data/decision.json     the tests behind each verdict (the five business
                                         tests, the price test and the data gate; rule v1)
        dashboard_data/notes.json        what to know about each company's assumptions

Numbers are stored in the units the dashboard shows (percent, dollars per
share, $ millions), rounded to what is displayed, so every figure on the page
can be traced to one cell. The columns ending in _raw are unrounded and are
what the "your assumptions" calculator uses.
"""
import json
from pathlib import Path

HERE = Path(".")
OUT = HERE / "dashboard_data"


def pct(x, digits=1):
    return None if x is None else round(x * 100, digits)


def musd(x):
    return None if x is None else round(x / 1e6)


def money(x, digits=2):
    return None if x is None else round(x, digits)


def market_vs_ours(m):
    g, mg, dr, sh = (m.get(k) or {} for k in ("growth", "margin", "discount_rate", "growth_share"))
    def f(x):
        return None if x is None else pct(x)
    return {
        "mkt_growth_pct": f(g.get("market")), "mkt_growth_edge": g.get("edge"),
        "our_growth_pct": f(g.get("ours")),
        "mkt_margin_pct": f(mg.get("market")), "mkt_margin_edge": mg.get("edge"),
        "our_margin_pct": f(mg.get("ours")), "margin_best_10yr_pct": f(mg.get("best_10yr")),
        "mkt_rate_pct": f(dr.get("market")), "mkt_rate_edge": dr.get("edge"),
        "our_rate_pct": f(dr.get("ours")),
        "mkt_growth_share_pct": f(sh.get("market")), "our_growth_share_pct": f(sh.get("ours")),
        "no_growth_value": None if m.get("steady_state_value") is None else money(m["steady_state_value"]),
    }


def company_row(ticker, r, names, a, v, s, h, d, p, price):
    return {
        "ticker": ticker, "name": names.get(ticker, ticker).title(),
        "price": money(price), "price_date": d["price_date"], "latest_quarter": d["latest_quarter"],
        "health": h["gate"], "fails": h["fails"], "watches": h["watches"], "passes": h["passes"],
        # The verdict (quality.py) is the engine's answer; the older DCF label is kept for reference.
        "label": r["verdict"], "verdict_why": r["verdict_why"],
        "label_basis": (r.get("rule") or "rule") + ": good business + realistic price; paper period, not for action yet",
        "passes_rule": "yes" if r.get("passes_rule") else "no",
        "quarantine": h.get("quarantine") or "",
        "dcf_label": r["label"],
        "good_business": "yes" if r["business"].get("good_business") else "no",
        "business_score": r["business"].get("score", 0),
        "growth_verdict": r["growth_test"]["verdict"],
        "growth_share_pct": None if r["growth_test"].get("share_achieving") is None
                            else pct(r["growth_test"]["share_achieving"], 0),
        "growth_test_margin_pct": pct(r["growth_test"].get("margin_used")),
        "growth_test_margin_basis": r["growth_test"].get("margin_basis", ""),
        "growth_needed_best3_pct": pct(r["growth_test"].get("growth_required")),
        "growth_needed_normal_pct": pct((r["growth_test"].get("normal_margin") or {}).get("growth_required")),
        "growth_verdict_normal": (r["growth_test"].get("normal_margin") or {}).get("verdict", ""),
        # Reverse engineering: what today's price assumes, next to what we assume.
        # An edge of "high"/"low" means the true figure is beyond the searched range.
        **market_vs_ours(r.get("market_vs_ours") or {}),
        "base_value": money(v["base_value_per_share"]),
        "p10": money(p["10"]), "p25": money(p["25"]), "median": money(p["50"]),
        "p75": money(p["75"]), "p90": money(p["90"]),
        "price_vs_median_pct": None if p["50"] is None or p["50"] <= 0 else pct(price / p["50"] - 1),
        "runs_above_price_pct": pct(s["share_of_runs_above_price"], 0),
        "growth_required_pct": pct(v["growth_price_requires"]),
        "growth_assumed_pct": pct(a["growth"]), "growth_ttm_pct": pct(a["growth_ttm"]),
        "growth_3yr_pct": pct(a["growth_3yr"]),
        "margin_reported_pct": pct(a["margin_reported"]), "margin_adjusted_pct": pct(a["margin"]),
        "margin_target_pct": pct(a["target_margin"]),
        "wacc_pct": pct(a["wacc"]), "wacc_mature_pct": pct(a["wacc_mature"]), "roic_pct": pct(a["roic"]),
        "terminal_growth_pct": pct(a["terminal_growth"] if a["growth"] >= a["terminal_growth"] else max(a["growth"], 0.02)),
        "tax_pct": pct(a["tax_rate"], 0), "sales_to_capital": round(a["sales_to_capital"], 2),
        "rd_life_years": a["rd_life"],
        "revenue_musd": round(a["revenue"] / 1e6), "cash_musd": round(a["cash"] / 1e6),
        "debt_musd": round(a["debt"] / 1e6), "shares_m": round(a["shares"] / 1e6, 1),
        "value_after_year_ten_pct": pct(v["share_of_value_after_year_ten"], 0),
        "beta": round(r["cost_of_capital"]["beta_used"], 2), "rating": r["cost_of_capital"]["rating"],
        "assumption_source": a["source"], "problems": "; ".join(r["problems"]),
        "margin_of_safety_pct": pct(r["decision"]["margin_of_safety"], 0),
        "risk_markers": ", ".join(r["decision"]["risk_markers"]) or "none",
        "tests_met": r["decision"]["tests_met"],
        "buy_below": money(r["decision"]["buy_below"]) if h["gate"] != "STOP" else None,
        "pessimistic_value": money(r["scenarios"]["pessimistic"]["value_per_share"]),
        "optimistic_value": money(r["scenarios"]["optimistic"]["value_per_share"]),
        "pessimistic_growth_pct": pct(r["scenarios"]["pessimistic"]["growth"]),
        "pessimistic_margin_pct": pct(r["scenarios"]["pessimistic"]["margin"]),
        "optimistic_growth_pct": pct(r["scenarios"]["optimistic"]["growth"]),
        "optimistic_margin_pct": pct(r["scenarios"]["optimistic"]["margin"]),
        "growth_history_pct": pct(a["growth_uncapped"]), "growth_cap_pct": pct(a["growth_cap"], 0),
        "margin_5yr_avg_pct": pct(a["margin_5yr_avg"]), "margin_10yr_avg_pct": pct(a["margin_10yr_avg"]),
        "margin_low_10yr_pct": pct(a["margin_low_10yr"]),
        "run_at": r["run_at"], "engine_version": r["engine_version"],
        # unrounded inputs for the calculator
        "revenue_raw": a["revenue"], "margin_raw": a["margin"], "target_margin_raw": a["target_margin"],
        "rd_uplift_raw": a["rd_uplift"], "growth_raw": a["growth"], "terminal_growth_raw": a["terminal_growth"],
        "sales_to_capital_raw": a["sales_to_capital"], "tax_raw": a["tax_rate"], "wacc_raw": a["wacc"],
        "wacc_mature_raw": a["wacc_mature"], "roic_premium_raw": a["terminal_roic_premium"],
        "cash_raw": a["cash"], "debt_raw": a["debt"], "shares_raw": a["shares"],
        "base_value_raw": v["base_value_per_share"],
    }


def main():
    latest = json.loads((HERE / "runs" / "latest.json").read_text())
    names = {}
    tickers_file = HERE / "edgar_cache" / "company_tickers.json"
    if tickers_file.exists():
        names = {r["ticker"]: r["title"] for r in json.loads(tickers_file.read_text()).values()}

    companies, checks, projection, decision, notes = [], [], [], [], []
    for ticker in sorted(latest):
        r = latest[ticker]
        a, v, s, h, d = r["assumptions"], r["valuation"], r["simulation"], r["health"], r["data"]
        p = s["percentiles"]
        price = d["price"]
        try:
            companies.append(company_row(ticker, r, names, a, v, s, h, d, p, price))
        except (TypeError, KeyError, ZeroDivisionError):
            # a record with missing core numbers (no share count, say): show what is known
            companies.append({"ticker": ticker, "name": names.get(ticker, ticker).title(), "price": money(price),
                              "price_date": d["price_date"], "health": h["gate"], "fails": h["fails"],
                              "watches": h["watches"], "passes": h["passes"], "label": r["verdict"],
                              "verdict_why": r["verdict_why"], "passes_rule": "yes" if r.get("passes_rule") else "no",
                              "quarantine": h.get("quarantine") or "", "dcf_label": r["label"],
                              "good_business": "yes" if r["business"].get("good_business") else "no",
                              "problems": "; ".join(r["problems"]), "run_at": r["run_at"],
                              "engine_version": r["engine_version"]})
        for i, c in enumerate(h["checks"]):
            checks.append({"ticker": ticker, "order": i + 1, "check": c["name"], "status": c["status"], "why": c["why"]})
        if h.get("quarantine"):
            q = next((c for c in h["checks"] if c["name"] in ("value vs price", "market value") and c["status"] == "FAIL"), None)
            decision.append({"ticker": ticker, "order": 0, "test": "Data can be trusted",
                             "result": "NOT MET", "detail": f"{h['quarantine']}: {q['why'] if q else ''}"})
        tests = r["business"].get("tests", [])
        if not tests:
            decision.append({"ticker": ticker, "order": 1, "test": "Good business",
                             "result": "NOT MET", "detail": r["business"].get("why", "")})
        for i, t in enumerate(tests):
            decision.append({"ticker": ticker, "order": i + 1,
                             "test": t["test"].capitalize(),
                             "result": "MET" if t["met"] else "NOT MET", "detail": t["why"]})
        g = r["growth_test"]
        basis = f" (at {pct(g['margin_used'])}% margin, {g.get('margin_basis', '')})" if g.get("margin_used") is not None else ""
        decision.append({"ticker": ticker, "order": 10, "test": "Growth the price needs is realistic",
                         "result": "MET" if g["verdict"] == "plausible" else "NOT MET",
                         "detail": f"{g['verdict']}: {g['why']}{basis}"})
        for i, note in enumerate(a["flags"]):
            notes.append({"ticker": ticker, "order": i + 1, "note": note})
        for y in v["year_by_year"]:
            projection.append({
                "ticker": ticker, "year": y["year"], "growth_pct": pct(y["growth"]),
                "revenue_musd": musd(y["revenue"]), "margin_pct": pct(y["margin"]),
                "after_tax_profit_musd": musd(y["after_tax_profit"]),
                "reinvestment_musd": musd(y["reinvestment"]),
                "free_cash_flow_musd": musd(y["free_cash_flow"]),
                "discount_rate_pct": pct(y["discount_rate"]),
            })

    OUT.mkdir(exist_ok=True)
    code = Path(__file__).read_text()
    meta = [{"label": "make_dashboard_data.py", "value": code, "type": "code", "lang": "python"},
            {"label": "Input", "value": "runs/latest.json, written by run.py"}]
    for name, rows in (("companies", companies), ("checks", checks), ("projection", projection),
                       ("decision", decision), ("notes", notes)):
        (OUT / f"{name}.json").write_text(json.dumps({"rows": rows, "meta": meta}, allow_nan=False))
        print(f"{name}: {len(rows)} rows")


if __name__ == "__main__":
    main()

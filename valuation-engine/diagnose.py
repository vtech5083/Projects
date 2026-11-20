"""
Why does the engine call most healthy companies overpriced?
Change one rule at a time and see how far the typical price-to-value moves.
Run: python3 diagnose.py     (reads runs/latest.json, changes nothing)
"""
import json, statistics, copy
import valuation

d = json.load(open("runs/latest.json"))
mk = json.load(open("market_inputs.json"))
rf, erp = mk["risk_free_rate"], mk["equity_risk_premium"]
healthy = {t: r for t, r in d.items() if r["health"]["gate"] != "STOP"}


def project(a, terminal=None, slow_to=None, premium=None):
    """valuation.project with the after-year-ten rules opened up."""
    a = dict(a)
    if premium is not None:
        a["terminal_roic_premium"] = premium
    if terminal is not None:
        a["terminal_growth"] = terminal
    if slow_to is not None and a["growth"] < a["terminal_growth"]:
        # slow growers normally fade to max(growth, 2%); here they fade to slow_to
        g0 = a["growth"]
        a["terminal_growth"] = slow_to
        a["growth"] = max(g0, -0.05)
        if a["growth"] < slow_to:          # trick project() into fading to slow_to
            v_lo = valuation.project(dict(a, growth=slow_to))[0]
            v = valuation.project(dict(a))[0]
            # project() would use max(g, 2%) -> rebuild exactly below instead
        return exact(a, g0, slow_to)
    return valuation.project(a)[0]


def exact(a, g, g_end):
    """Same arithmetic as valuation.project, with g_end given directly."""
    tax, s2c = a["tax_rate"], a["sales_to_capital"]
    revenue, discount, pv = a["revenue"], 1.0, 0.0
    for year in range(1, 11):
        fade = max(0, year - 5) / 5
        gy = g + (g_end - g) * fade
        m = a["margin"] + (a["target_margin"] - a["margin"]) * min(year, 5) / 5
        w = a["wacc"] + (a["wacc_mature"] - a["wacc"]) * fade
        new = revenue * (1 + gy)
        nopat = new * ((m - a["rd_uplift"]) * (1 - tax) + a["rd_uplift"])
        discount /= 1 + w
        pv += (nopat - (new - revenue) / s2c) * discount
        revenue = new
    roic = a["wacc_mature"] + a["terminal_roic_premium"]
    tv = nopat * (1 + g_end) * (1 - g_end / roic) / (a["wacc_mature"] - g_end)
    return (pv + tv * discount + a["cash"] - a["debt"]) / a["shares"]


# The rule changes, each a function from (assumptions, record) to new assumptions
def lease_fix(a, r):
    # Leases are counted as debt, but their interest is still deducted inside
    # operating profit. Damodaran adds that interest back to operating profit.
    li = r["cost_of_capital"].get("lease_interest_estimate") or 0
    return dict(a, margin=a["margin"] + li / a["revenue"], target_margin=a["target_margin"] + li / a["revenue"])

def mature_wacc_fix(a, r):
    # A mature company's cost of capital should not rise above what it pays
    # today; rf + ERP is a cost of EQUITY for a beta of 1, not a cost of capital.
    return dict(a, wacc_mature=min(a["wacc"], a["wacc_mature"]))

def growth_3yr(a, r):
    g = a["growth_3yr"] if a["growth_3yr"] is not None else a["growth"]
    return dict(a, growth=min(max(g, -0.05), a["growth_cap"]))

CHANGES = [
    ("today's engine", lambda a, r: a, {}),
    ("1. lease interest added back to profit", lease_fix, {}),
    ("2. mature discount rate not raised", mature_wacc_fix, {}),
    ("3. long-run growth = risk-free rate 5.3%", lambda a, r: a, {"terminal": rf}),
    ("4. slow growers fade to 4%, not 2%", lambda a, r: a, {"slow_to": 0.04}),
    ("5. growth = 3-year pace (not lower of 1y/3y)", growth_3yr, {}),
    ("6. lasting advantage +5 pts after year 10", lambda a, r: a, {"premium": 0.05}),
]

rows = {}
for name, fn, kw in CHANGES:
    pv = {}
    for t, r in healthy.items():
        a = fn(dict(r["assumptions"]), r)
        pv[t] = r["data"]["price"] / project(a, **kw)
    rows[name] = pv

base = statistics.median(rows["today's engine"].values())
print(f"{'rule change (one at a time)':48s} median price/value   change")
for name, pv in rows.items():
    m = statistics.median(pv.values())
    print(f"{name:48s} {m:6.2f}            {m / base - 1:+.0%}")

# all the fixes that are corrections of method (1-3) together, then with 4-6
def combo(a, r, which):
    if 1 in which: a = lease_fix(a, r)
    if 2 in which: a = mature_wacc_fix(a, r)
    if 5 in which: a = growth_3yr(a, r)
    kw = {}
    if 3 in which: kw["terminal"] = rf
    if 6 in which: kw["premium"] = 0.05
    if 4 in which and a["growth"] < (kw.get("terminal", a["terminal_growth"])):
        return exact(dict(a, **({"terminal_growth": kw["terminal"]} if "terminal" in kw else {}),
                     **({"terminal_roic_premium": kw["premium"]} if "premium" in kw else {})),
                     a["growth"], 0.04)
    return project(a, **kw)

print()
print(f"{'ticker':6s} {'price':>8s} {'today':>8s} {'fix 1-3':>8s} {'all 1-6':>8s}   p/v today -> fixes -> all   price needs growth  our growth  3y hist")
for t, r in healthy.items():
    a, p = r["assumptions"], r["data"]["price"]
    v0 = project(dict(a))
    v1 = combo(dict(a), r, {1, 2, 3})
    v2 = combo(dict(a), r, {1, 2, 3, 4, 5, 6})
    need = r["valuation"]["growth_price_requires"]
    print(f"{t:6s} {p:8.2f} {v0:8.2f} {v1:8.2f} {v2:8.2f}   {p/v0:5.2f} -> {p/v1:5.2f} -> {p/v2:5.2f}   "
          f"{(need if need is not None else float('nan')):7.1%}            {a['growth']:6.1%}     {(a['growth_3yr'] if a['growth_3yr'] is not None else float('nan')):6.1%}")
m1 = statistics.median(r["data"]["price"] / combo(dict(r["assumptions"]), r, {1, 2, 3}) for r in healthy.values())
m2 = statistics.median(r["data"]["price"] / combo(dict(r["assumptions"]), r, {1, 2, 3, 4, 5, 6}) for r in healthy.values())
print(f"\nmedian price/value: today {base:.2f}, fixes 1-3 {m1:.2f}, all 1-6 {m2:.2f}")


# ---------------------------------------------------------------------------
# The other way round: what discount rate makes our cash flows worth the price?
# (the "implied cost of capital" used in academic work). Our cash flows, one
# flat rate for every year, no other change.
# ---------------------------------------------------------------------------
def implied_rate(a, price):
    lo, hi = 0.045, 0.20
    for _ in range(60):
        mid = (lo + hi) / 2
        b = dict(a, wacc=mid, wacc_mature=mid)
        if mid - b["terminal_growth"] < 0.003:
            lo = mid; continue
        if valuation.project(b)[0] > price: lo = mid
        else: hi = mid
    return (lo + hi) / 2

print(f"\n{'ticker':6s} our rate  rate that matches price  implied equity premium (rate - rf)")
for t, r in healthy.items():
    a = r["assumptions"]
    ir = implied_rate(dict(a), r["data"]["price"])
    print(f"{t:6s} {a['wacc']:6.1%}   {ir:6.1%}                  {ir - rf:+.1%}")

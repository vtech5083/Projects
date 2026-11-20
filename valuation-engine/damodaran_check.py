"""
Check our valuation arithmetic against Damodaran's own NVIDIA spreadsheet
(NVIDIA2023.xlsx, June 2023).  Run:  python3 damodaran_check.py

It does three things:
  1. Rebuilds his numbers from his inputs with his method -> must match his sheet.
  2. Feeds the same inputs to our valuation.project()     -> shows the gap.
  3. Switches his method choices on one at a time         -> explains the gap.
"""
import openpyxl
import valuation

wb = openpyxl.load_workbook("NVIDIA2023.xlsx", data_only=True)
out = wb["Valuation output"]
row = lambda r: [out.cell(r, c).value for c in range(2, 14)]      # base, years 1-10, terminal


def model(rev0, m0, revenues=None, g=0.15, g_end=0.036, target=0.40, s2c=1.15,
          wacc=None, wacc_end=None, roic_end=0.20, tax0=0.10, tax_end=0.25,
          his_margin=True, tax_glide=True, lag=True, margin_y1=0.35):
    """One business line, ten years plus a terminal year. Toggles pick his method or ours."""
    rev, disc, pv, fcfs = [rev0], 1.0, 0.0, []
    for y in range(1, 12):                                       # year 11 = terminal year
        fade = max(0, min(y, 10) - 5) / 5
        gy = g_end if y == 11 else g + (g_end - g) * fade
        rev.append(revenues[y] if revenues else rev[-1] * (1 + gy))
    for y in range(1, 11):
        fade = max(0, y - 5) / 5
        if his_margin:
            margin = margin_y1 if y == 1 else target - (target - margin_y1) / 5 * max(0, 5 - y)
        else:
            margin = m0 + (target - m0) * min(y, 5) / 5
        tax = tax0 + (tax_end - tax0) * fade if tax_glide else tax0
        nopat = rev[y] * margin * (1 - tax)
        reinvest = (rev[y + 1] - rev[y]) / s2c if lag else (rev[y] - rev[y - 1]) / s2c
        disc /= 1 + wacc + (wacc_end - wacc) * fade
        fcfs.append(nopat - reinvest)
        pv += fcfs[-1] * disc
    tax_t = tax_end if tax_glide else tax0
    nopat_t = rev[11] * target * (1 - tax_t) if tax_glide else nopat * (1 + g_end)
    tv = nopat_t * (1 - g_end / roic_end) / (wacc_end - g_end)
    return pv + tv * disc, fcfs


inp = wb["Input sheet"]
wacc, wacc_end = out["C30"].value, out["L30"].value
ok = True

print("1. His method, rebuilt from his inputs")
for name, rev_row, value_cell, fcf_row in [("Rest of business", 3, "B37", 9),
                                           ("AI chips", 14, "B38", 19),
                                           ("Auto chips", 23, "B39", 28)]:
    revs = row(rev_row)
    kw = dict(wacc=wacc, wacc_end=wacc_end)
    if rev_row == 3:
        mine, fcfs = model(revs[0], out["B4"].value, **kw)
    else:
        mine, fcfs = model(revs[0], 0.35, revenues=revs, **kw)
    his = out[value_cell].value
    worst = max(abs(a - b) for a, b in zip(fcfs, row(fcf_row)[1:11]))
    good = abs(mine / his - 1) < 1e-9 and worst < 1e-6
    ok &= good
    print(f"   {name:17s} his {his:12,.2f}   rebuilt {mine:12,.2f}   "
          f"largest yearly cash flow gap {worst:.6f}   {'MATCH' if good else 'DIFFERENT'}")

total = sum(out[c].value for c in ("B37", "B38", "B39"))
per_share = (total - out["B44"].value + out["B46"].value + out["B47"].value) / out["B51"].value
print(f"   Value per share: his {out['B52'].value:.2f}, rebuilt {per_share:.2f}")

print("\n2. R&D converter")
rdc = wb["R& D converter"]
rd = [rdc.cell(r, 2).value for r in range(15, 10, -1)] + [rdc["F7"].value]
asset, amort, _ = valuation.research(rd, 5)
good = abs(asset - rdc["D35"].value) < 1e-6 and abs(amort - rdc["D37"].value) < 1e-6
ok &= good
print(f"   research asset: his {rdc['D35'].value:,.1f}, ours {asset:,.1f};  "
      f"yearly write-off: his {rdc['D37'].value:,.1f}, ours {amort:,.1f}   {'MATCH' if good else 'DIFFERENT'}")

print("\n3. Same inputs through our valuation.project()  (rest of business only)")
a = dict(growth=0.15, target_margin=0.40, terminal_growth=0.036, tax_rate=0.10, sales_to_capital=1.15,
         revenue=out["B3"].value, margin=out["B4"].value, rd_uplift=0.0, wacc=wacc, wacc_mature=wacc_end,
         terminal_roic_premium=0.20 - wacc_end, cash=0.0, debt=0.0, shares=1.0)
ours, _ = valuation.project(a)
kw = dict(wacc=wacc, wacc_end=wacc_end)
twin, _ = model(a["revenue"], a["margin"], his_margin=False, tax_glide=False, lag=False, **kw)
good = abs(ours / twin - 1) < 1e-9
ok &= good
his = out["B37"].value
print(f"   our engine {ours:12,.2f}   independent copy of our method {twin:12,.2f}   {'MATCH' if good else 'DIFFERENT'}")
print(f"   his value  {his:12,.2f}   gap {ours / his - 1:+.1%}")

print("\n4. Where the gap comes from (switching his choices on one at a time)")
steps = [("our method", dict(his_margin=False, tax_glide=False, lag=False)),
         ("+ his margin path (35% in year 1)", dict(his_margin=True, tax_glide=False, lag=False)),
         ("+ his tax rate rising 10% -> 25%", dict(his_margin=True, tax_glide=True, lag=False)),
         ("+ his one-year reinvestment lag", dict(his_margin=True, tax_glide=True, lag=True))]
prev = None
for label, t in steps:
    v, _ = model(a["revenue"], a["margin"], **t, **kw)
    print(f"   {label:36s} {v:12,.2f}" + ("" if prev is None else f"   change {v / prev - 1:+.1%}"))
    prev = v

print("\nRESULT:", "arithmetic agrees" if ok else "ARITHMETIC DIFFERS - look above")

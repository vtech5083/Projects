import math, pandas as pd, numpy as np
d = pd.read_csv("study/business_test_ablation.csv")
B = {c: d[c].fillna(False).astype(bool) for c in d.columns if c.startswith(("m_", "Q", "mac_", "draft_"))}
def stats(mask, label):
    y = [g.ret_1y.mean() for _, g in d[mask].dropna(subset=["ret_1y"]).groupby("date")]
    a = [g.ret_1y.mean() for _, g in d.dropna(subset=["ret_1y"]).groupby("date")]
    t3 = d[mask].ret_3y.mean()
    print(f"{label:62s} {np.mean(y):+6.1%}/yr  $1->{math.prod(1+v for v in y):.2f}  picks {mask.sum():3d}  beat-all {sum(v>w for v,w in zip(y,a))}/12  3y {t3:+.0%}")
mg, dg = B["mac_growth_ok"], B["draft_growth_ok"]
print("== 2x2: business test x growth test (both growth tests at NORMAL margin) ==")
for bn, b in (("Mac business", B["mac_good"]), ("Draft business", B["draft_good"])):
    for gn, g in (("Mac growth", mg), ("Draft growth", dg)):
        stats(b & g, f"{bn} + {gn}")
stats(pd.Series(True, index=d.index), "all companies")
print(f"\ngrowth tests agree on {(mg==dg).mean():.0%} of company-dates; engine vs draft implied growth median gap {(d.g_engine-d.g_draft_normal).abs().median():.3f}")
print(f"business tests agree on {(B['mac_good']==B['draft_good']).mean():.0%}; Mac pass {B['mac_good'].sum()}, draft pass {B['draft_good'].sum()}, both {(B['mac_good']&B['draft_good']).sum()}")
# Mac tests mapped to draft equivalents
m = {k: B["m_" + k] for k in ["generates cash", "stays profitable", "earns more than its cost of capital", "steady margins", "debt it can repay", "does not keep issuing shares"]}
q = {"cash": B["Q1 cash"], "returns": B["Q2 returns"], "profit": B["Q3 profit"], "debt": B["Q4 debt"], "owners": B["Q5 owners"]}
draft_all = q["cash"] & q["returns"] & q["profit"] & q["debt"] & q["owners"]
print("\n== start from DRAFT business test (+ draft growth at normal), change one thing ==")
stats(draft_all & dg, "draft as is")
stats(draft_all & m["steady margins"] & dg, "+ add Mac's steady-margin test as required")
stats(q["cash"] & q["returns"] & q["profit"] & q["debt"] & m["does not keep issuing shares"] & dg, "swap shares test -> Mac (no split fix)")
stats(q["cash"] & m["earns more than its cost of capital"] & q["profit"] & q["debt"] & q["owners"] & dg, "swap returns test -> Mac (cash-rich years count as pass)")
stats(q["cash"] & q["returns"] & m["stays profitable"] & q["debt"] & q["owners"] & dg, "swap profit test -> Mac (90% of years)")
score = sum(q.values()).astype(int) if False else (q["cash"].astype(int)+q["returns"]+q["profit"]+q["debt"]+q["owners"])
stats(q["cash"] & q["profit"] & (score >= 4) & dg, "pass rule -> Mac style (musts + 4 of 5)")
print("\n== start from MAC business test (+ draft growth at normal), change one thing ==")
ms = sum(v.astype(int) for v in m.values())
musts = m["generates cash"] & m["stays profitable"]
stats(musts & (ms >= 5) & dg, "Mac as is (musts + 5 of 6)")
stats(musts & (ms - m["steady margins"].astype(int) >= 4) & dg, "drop steady-margin test (musts + 4 of other 5)")
stats(musts & (ms == 6) & dg, "require all 6")
stats(musts & m["earns more than its cost of capital"] & m["debt it can repay"] & m["does not keep issuing shares"] & dg, "require all except steady margins")
# who differs
only_d = draft_all & ~B["mac_good"] & dg; only_m = B["mac_good"] & ~draft_all & dg
print(f"\npicked only by draft: {only_d.sum()}, avg 1y {d[only_d].ret_1y.mean():+.1%}; only by Mac: {only_m.sum()}, avg 1y {d[only_m].ret_1y.mean():+.1%}")
why = {k: (~v & only_d).sum() for k, v in m.items()}
print("  draft-only picks: Mac test that failed ->", why)
whyq = {k: (~v & only_m).sum() for k, v in q.items()}
print("  Mac-only picks: draft test that failed ->", whyq)
print("\ndraft-only picks by ticker (count, avg 1y):")
print(d[only_d].groupby("ticker").ret_1y.agg(["count","mean"]).sort_values("count", ascending=False).head(15).round(3).to_string())
print("\nMac-only picks by ticker:")
print(d[only_m].groupby("ticker").ret_1y.agg(["count","mean"]).sort_values("count", ascending=False).head(15).round(3).to_string())

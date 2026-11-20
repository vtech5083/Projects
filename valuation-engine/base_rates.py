"""
base_rates.py -- how fast US companies of each size have actually grown.

From the SEC "frames" files (every US company's yearly revenue, 2011 onward)
in edgar_cache/frames/, measure each company's revenue growth over the next
five years, grouped by revenue at the start. Saves base_rates.json with the
1st..99th percentiles for each size group, which quality.py uses to say how
rare the growth a share price needs is.

Note: growth includes acquisitions, and companies that stopped filing within
five years drop out, so these figures if anything flatter fast growth.
Run:  python3 base_rates.py
"""
import collections
import glob
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SIZES = [(1e9, 1e10, "$1-10B"), (1e10, 5e10, "$10-50B"), (5e10, 1e11, "$50-100B"), (1e11, 1e15, "over $100B")]


def main():
    rev = collections.defaultdict(dict)
    for f in glob.glob(str(ROOT / "edgar_cache/frames/*_CY*.json")):
        year = int(f.split("_CY")[1][:4])
        for x in json.loads(Path(f).read_text())["data"]:
            if x.get("loc", "").startswith("US"):
                rev[year][x["cik"]] = max(rev[year].get(x["cik"], 0), x["val"])
    groups = {name: [] for _, _, name in SIZES}
    years = sorted(rev)
    for start in years:
        if start + 5 not in rev:
            continue
        for cik, r0 in rev[start].items():
            r5 = rev[start + 5].get(cik)
            if r5 and r0 >= 1e9 and r5 > 0:
                g = (r5 / r0) ** 0.2 - 1
                for lo, hi, name in SIZES:
                    if lo <= r0 < hi:
                        groups[name].append(g)
    out = {"about": "5-year yearly revenue growth of US companies by revenue at the start; SEC frames "
                    f"{years[0]}-{years[-1]}", "groups": {}}
    for lo, hi, name in SIZES:
        g = sorted(groups[name])
        out["groups"][name] = {"min_revenue": lo, "max_revenue": hi, "companies": len(g),
                               "percentiles": [g[round(q / 100 * (len(g) - 1))] for q in range(1, 100)]}
        print(f"{name:11s} {len(g):5d} companies, median {g[len(g) // 2]:.1%}, 90th {g[int(0.9 * (len(g) - 1))]:.1%}")
    (ROOT / "base_rates.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()

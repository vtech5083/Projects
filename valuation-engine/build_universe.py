"""
build_universe.py -- current statements for every company in study/universe.json
that still files with the SEC, so the live engine can value them all.

Skips companies whose latest SEC filing is more than a year old (taken over,
private, or an old registration of a company that now files under a new
number). Writes <TICKER>_statements.csv and <TICKER>_quarterly.csv here, the
same files fetch_queue.py makes, and the list of usable tickers to
universe_tickers.txt. Re-running skips companies already built today.
Run:  python3 build_universe.py [max companies this run]
"""
import json, subprocess, sys, time
from datetime import date, timedelta
from pathlib import Path

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
universe = json.loads(Path("study/universe.json").read_text())
cutoff = str(date.today() - timedelta(days=365))
today = time.time() - 12 * 3600
usable, skipped, built = [], [], 0
# One registration per ticker: where a company files under two SEC numbers
# (an old and a new parent), use the one with the longer filing history.
best = {}
for cik, u in universe.items():
    t, facts = u.get("price_ticker"), Path(f"edgar_cache/facts_{cik}.json")
    if t and facts.exists() and (t not in best or facts.stat().st_size > best[t][1]):
        best[t] = (cik, facts.stat().st_size)
for cik, u in sorted(universe.items(), key=lambda kv: kv[1].get("price_ticker") or ""):
    t = u.get("price_ticker")
    facts = Path(f"edgar_cache/facts_{cik}.json")
    if not t or not facts.exists():
        continue
    if best[t][0] != cik:
        continue
    f = json.loads(facts.read_text())
    last = max((x.get("filed", "") for ns in f.get("facts", {}).values() for tag in ns.values()
                for arr in tag["units"].values() for x in arr), default="")
    if last < cutoff:
        skipped.append(f"{t} ({u['name']}, last filing {last})")
        continue
    usable.append(t)
    q = Path(f"{t}_quarterly.csv")
    if q.exists() and q.stat().st_mtime > today:
        continue
    if built >= LIMIT:
        continue
    for script in ("edgar_statements.py", "edgar_quarterly.py"):
        subprocess.run([sys.executable, script, t, "--facts-file", str(facts), "--email", "x"],
                       capture_output=True, text=True)
    built += 1
Path("universe_tickers.txt").write_text("\n".join(sorted(set(usable))) + "\n")
print(f"{len(set(usable))} companies usable, {built} built this run; skipped {len(skipped)}:")
for s in skipped:
    print("  " + s)

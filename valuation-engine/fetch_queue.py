#!/usr/bin/env python3
"""
fetch_queue.py  --  A helper that downloads SEC data when asked through a file.

Why it exists
-------------
Claude can read and write files in this folder but cannot reach the SEC
website or type into Terminal. This script runs on your Mac, which can reach
the SEC. It watches fetch_requests.txt. When ticker symbols appear in that
file, it downloads those companies' statements and removes them from the list.

What it will and will not do
----------------------------
It accepts only these kinds of request, one per line:
  * a ticker symbol (letters, dots and dashes, up to 6 characters)
  * the name of a file in KNOWN_FILES below
  * PRICES:TICKER        monthly price history from Yahoo Finance -> prices/TICKER.csv
  * FRAME:Concept:CY2015 one SEC figure for every company that reported it
                         (data.sec.gov "frames") -> edgar_cache/frames/
  * FACTS:0000320193     all SEC figures for one company by its SEC number (CIK),
                         for companies that no longer have a ticker
  * INFO:0000320193      the company's SEC profile (industry code, tickers, names)
  * IBKR:TICKER          monthly prices from your IBKR TWS / IB Gateway (read-only
                         connection on this Mac) -> prices_ibkr/TICKER.csv;
                         later requests fetch only the new months (see ibkr_prices.py)
Anything else is ignored and logged. It never runs a command taken from the
request file, and it downloads from nowhere except sec.gov, finance.yahoo.com and the
fixed addresses in KNOWN_FILES. Each request is checked against a strict
pattern before any address is built from it.

It reads requests from fetch_requests.txt and fetch_requests_more.txt.
When this file itself is updated, the helper restarts itself with the new
version, so it never needs restarting by hand again.

Start it once and leave the Terminal window open:
    cd ~/Projects/valuation-engine && python3 fetch_queue.py --email you@example.com
Stop it with Ctrl+C or by closing the window.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REQUESTS = Path("fetch_requests.txt")
MORE_REQUESTS = Path("fetch_requests_more.txt")
PRICE_REQ = re.compile(r"^PRICES:([A-Z][A-Z.\-]{0,5})$")
FRAME_REQ = re.compile(r"^FRAME:([A-Za-z]{3,120}):(CY\d{4}(?:Q[1-4]I?)?)$")
FACTS_REQ = re.compile(r"^FACTS:(\d{1,10})$")
INFO_REQ = re.compile(r"^INFO:(\d{1,10})$")
IBKR_REQ = re.compile(r"^IBKR:([A-Z][A-Z.\-]{0,5})$")
# Python packages the helper may install into its own Python, and nothing else.
INSTALLABLE = {"ib_async"}
LOG = Path("fetch_log.txt")
TICKER = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")
KNOWN_FILES = {
    "NVIDIA2023.xlsx": "https://pages.stern.nyu.edu/~adamodar/pc/blog/NVIDIA2023.xlsx",
}


def log(message):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def fetch_ticker(ticker, email):
    ok = True
    for script in ("edgar_statements.py", "edgar_quarterly.py"):
        result = subprocess.run([sys.executable, script, ticker, "--email", email],
                                capture_output=True, text=True, timeout=300)
        # edgar_statements.py exits 1 when its checks find a problem; the files
        # are still written, so only a crash counts as a failed download.
        crashed = result.returncode not in (0, 1) or "Traceback" in result.stderr
        if crashed:
            ok = False
            tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no output"]
            log(f"{ticker}: {script} failed: {tail[0][:200]}")
    log(f"{ticker}: {'done' if ok else 'finished with errors'}")


def fetch_file(name):
    try:
        request = urllib.request.Request(KNOWN_FILES[name], headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
        Path(name).write_bytes(data)
        log(f"{name}: downloaded, {len(data):,} bytes")
    except Exception as error:  # report and carry on
        log(f"{name}: download failed: {str(error)[:200]}")


def download(url, headers):
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def fetch_prices(ticker):
    """Monthly prices since listing from Yahoo Finance's chart data (unofficial,
    for personal research): close, close adjusted for splits and dividends,
    and dividends paid. Saved as prices/TICKER.csv."""
    symbol = ticker.replace(".", "-")
    # An explicit start date keeps the bars monthly; "range=max" switches
    # long histories to three-month bars.
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1=946684800&period2={int(time.time())}&interval=1mo"
           "&events=div%2Csplit&includeAdjustedClose=true")
    try:
        data = json.loads(download(url, {"User-Agent": "Mozilla/5.0"}))
        r = data["chart"]["result"][0]
        times = r["timestamp"]
        close = r["indicators"]["quote"][0]["close"]
        adj = r["indicators"]["adjclose"][0]["adjclose"]
        divs = {}
        for d in (r.get("events", {}).get("dividends") or {}).values():
            month = datetime.utcfromtimestamp(d["date"]).strftime("%Y-%m")
            divs[month] = divs.get(month, 0.0) + d["amount"]
        rows = ["Date,Close,AdjClose,Dividend"]
        for t, c, a in zip(times, close, adj):
            if c is None or a is None:
                continue
            month = datetime.utcfromtimestamp(t).strftime("%Y-%m")
            rows.append(f"{month},{c:.6f},{a:.6f},{divs.get(month, 0.0):.6f}")
        Path("prices").mkdir(exist_ok=True)
        Path("prices", f"{ticker}.csv").write_text("\n".join(rows) + "\n")
        log(f"PRICES:{ticker}: {len(rows) - 1} months saved")
    except Exception as error:
        log(f"PRICES:{ticker}: download failed: {str(error)[:200]}")
    time.sleep(1.0)                          # be gentle with a free service


def fetch_frame(concept, period, email):
    try:
        data = download(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/USD/{period}.json",
                        {"User-Agent": f"valuation-engine research {email}"})
        json.loads(data)                     # make sure it is real JSON before saving
        Path("edgar_cache/frames").mkdir(parents=True, exist_ok=True)
        Path("edgar_cache/frames", f"{concept}_{period}.json").write_bytes(data)
        log(f"FRAME:{concept}:{period}: {len(data):,} bytes")
    except Exception as error:
        log(f"FRAME:{concept}:{period}: download failed: {str(error)[:200]}")
    time.sleep(0.2)


def fetch_facts(cik, email):
    try:
        data = download(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json",
                        {"User-Agent": f"valuation-engine research {email}"})
        json.loads(data)
        Path("edgar_cache", f"facts_{int(cik)}.json").write_bytes(data)
        log(f"FACTS:{int(cik)}: {len(data):,} bytes")
    except Exception as error:
        log(f"FACTS:{cik}: download failed: {str(error)[:200]}")
    time.sleep(0.2)


def fetch_info(cik, email):
    try:
        data = download(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                        {"User-Agent": f"valuation-engine research {email}"})
        json.loads(data)
        Path("edgar_cache", f"info_{int(cik)}.json").write_bytes(data)
        log(f"INFO:{int(cik)}: {len(data):,} bytes")
    except Exception as error:
        log(f"INFO:{cik}: download failed: {str(error)[:200]}")
    time.sleep(0.2)


def fetch_ibkr(ticker):
    import importlib
    import ibkr_prices
    kept = getattr(ibkr_prices, "_ib", None)  # keep the open IBKR connection across the reload
    importlib.reload(ibkr_prices)            # pick up changes without restarting
    if kept is not None:
        ibkr_prices._ib = kept
        try:
            kept.errorEvent += ibkr_prices._on_error
        except Exception:
            pass
    try:
        ibkr_prices.update(ticker, log)
    except ModuleNotFoundError as error:
        log(f"IBKR: missing Python package ({error}); this helper runs {sys.executable} "
            f"(Python {sys.version.split()[0]}); install with: {sys.executable} -m pip install ib_async")
    except Exception as error:
        log(f"IBKR:{ticker}: failed: {str(error)[:200]}")


def install(package):
    if package not in INSTALLABLE:
        log(f"refused to install {package!r}: not on the allowed list")
        return
    result = subprocess.run([sys.executable, "-m", "pip", "install", package],
                            capture_output=True, text=True, timeout=600)
    tail = (result.stdout + result.stderr).strip().splitlines()[-2:]
    log(f"INSTALL:{package}: exit {result.returncode}: {' | '.join(tail)[:300]}")


def handle(item, email):
    if item in KNOWN_FILES:
        fetch_file(item)
    elif PRICE_REQ.match(item.upper()):
        fetch_prices(PRICE_REQ.match(item.upper()).group(1))
    elif FRAME_REQ.match(item):
        m = FRAME_REQ.match(item)
        fetch_frame(m.group(1), m.group(2), email)
    elif item.startswith("INSTALL:"):
        install(item.split(":", 1)[1].strip())
    elif IBKR_REQ.match(item.upper()):
        fetch_ibkr(IBKR_REQ.match(item.upper()).group(1))
    elif INFO_REQ.match(item):
        fetch_info(INFO_REQ.match(item).group(1), email)
    elif FACTS_REQ.match(item):
        fetch_facts(FACTS_REQ.match(item).group(1), email)
    elif TICKER.match(item.upper()):
        fetch_ticker(item.upper(), email)
    else:
        log(f"ignored a request it does not recognise: {item[:40]!r}")


def main():
    parser = argparse.ArgumentParser(description="Download SEC data on request.")
    parser.add_argument("--email", required=True, help="contact email the SEC requires")
    parser.add_argument("--every", type=int, default=15, help="seconds between checks")
    args = parser.parse_args()

    REQUESTS.touch()
    MORE_REQUESTS.touch()
    me = Path(__file__).resolve()
    version = me.stat().st_mtime
    log(f"watching {REQUESTS} and {MORE_REQUESTS} every {args.every} seconds; Ctrl+C to stop")
    while True:
        if me.stat().st_mtime != version:     # this file was updated: restart with the new version
            log("helper updated; restarting with the new version")
            os.execv(sys.executable, [sys.executable, str(me)] + sys.argv[1:])
        for queue in (REQUESTS, MORE_REQUESTS):
            lines = [l.strip() for l in queue.read_text().splitlines() if l.strip()]
            if not lines:
                continue
            queue.write_text("")             # take the batch; new requests can arrive meanwhile
            for item in dict.fromkeys(lines):  # keep order, drop repeats
                handle(item, args.email)
            log(f"batch finished ({len(lines)} requests)")
        time.sleep(args.every)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")

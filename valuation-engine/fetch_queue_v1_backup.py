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
It accepts only two kinds of request, one per line:
  * a ticker symbol (letters, dots and dashes, up to 6 characters)
  * the name of a file in KNOWN_FILES below
Anything else is ignored and logged. It never runs a command taken from the
request file, and it downloads from nowhere except sec.gov and the fixed
addresses in KNOWN_FILES.

Start it once and leave the Terminal window open:
    cd ~/Projects/valuation-engine && python3 fetch_queue.py --email you@example.com
Stop it with Ctrl+C or by closing the window.
"""

import argparse
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REQUESTS = Path("fetch_requests.txt")
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


def main():
    parser = argparse.ArgumentParser(description="Download SEC data on request.")
    parser.add_argument("--email", required=True, help="contact email the SEC requires")
    parser.add_argument("--every", type=int, default=15, help="seconds between checks")
    args = parser.parse_args()

    REQUESTS.touch()
    log(f"watching {REQUESTS} every {args.every} seconds; Ctrl+C to stop")
    while True:
        lines = [l.strip() for l in REQUESTS.read_text().splitlines() if l.strip()]
        if lines:
            REQUESTS.write_text("")          # take the batch; new requests can arrive meanwhile
            for item in dict.fromkeys(lines):  # keep order, drop repeats
                if item in KNOWN_FILES:
                    fetch_file(item)
                elif TICKER.match(item.upper()):
                    fetch_ticker(item.upper(), args.email)
                else:
                    log(f"ignored a request that is not a ticker or a known file: {item[:40]!r}")
            log("batch finished")
        time.sleep(args.every)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")

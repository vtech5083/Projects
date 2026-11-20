"""
ibkr_prices.py -- monthly price history from Interactive Brokers, kept up to date.

Used by fetch_queue.py for requests of the form IBKR:TICKER. Needs, on this Mac:
  * TWS or IB Gateway open and logged in, with API access switched on
    (Settings > API > Enable ActiveX and Socket Clients; tick "Read-Only API")
  * the Python package ib_async:  python3 -m pip install ib_async

What it saves, in prices_ibkr/TICKER.csv, one row per month:
  Date      the month (YYYY-MM)
  Close     IBKR's month-end close, adjusted for splits ("TRADES" data)
  AdjClose  the same, also adjusted for dividends ("ADJUSTED_LAST", which IBKR
            serves only as daily bars; the month's last trading day is kept),
            so AdjClose growth = total return

First request for a ticker: 20 years of history. Every later request: only
the months since the last saved one, plus a three-month overlap. Adjusted
prices are restated backwards after every split or dividend, so the overlap
is used to rescale the saved history to IBKR's latest basis instead of
downloading it all again.

Also kept, in prices_ibkr/_latest.json: the exact date and close of each
ticker's latest daily bar, so a valuation can state the day of its price.
IBKR:TNX fetches the 10-year US Treasury yield index (CBOE; quoted as the
yield x 10) into prices_ibkr/_rates.json, for the risk-free rate.

The connection is read-only: this file never places, changes or cancels orders.
"""
import csv
import json
import math
import time
from datetime import date
from pathlib import Path

FOLDER = Path("prices_ibkr")
SETTINGS = Path("ibkr_settings.json")
PORTS = [4001, 7496, 4002, 7497]      # Gateway live, TWS live, Gateway paper, TWS paper
PACE_SECONDS = 11                     # IBKR allows about 60 history requests per 10 minutes

_ib = None
_errors = []
_last_bar = {}
LATEST = FOLDER / "_latest.json"
RATES = FOLDER / "_rates.json"


def _remember(path, key, value):
    data = json.loads(path.read_text()) if path.exists() else {}
    data[key] = value
    path.write_text(json.dumps(data, indent=1, sort_keys=True))


def _on_error(req_id, code, message, *rest):
    _errors.append(f"{code}: {message[:150]}")


def connect(log):
    """One read-only connection, reused for the whole batch."""
    global _ib
    import asyncio
    try:                                  # Python 3.14 no longer makes an event loop by itself
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    from ib_async import IB
    if _ib is not None and _ib.isConnected():
        return _ib
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    host = settings.get("host", "127.0.0.1")
    ports = [settings["port"]] if "port" in settings else PORTS
    errors = []
    for port in ports:
        ib = IB()
        try:
            ib.connect(host, port, clientId=settings.get("client_id", 17), readonly=True, timeout=8)
            log(f"IBKR: connected on port {port} (read-only)")
            ib.errorEvent += _on_error
            SETTINGS.write_text(json.dumps({"host": host, "port": port,
                                            "client_id": settings.get("client_id", 17)}))
            _ib = ib
            return ib
        except Exception as error:
            errors.append(f"{port}: {type(error).__name__} {str(error)[:80]}")
            try:
                ib.disconnect()
            except Exception:
                pass
            continue
    raise RuntimeError("could not reach TWS or IB Gateway -- " + "; ".join(errors))


def read(path):
    if not path.exists():
        return {}
    with path.open() as f:
        return {r["Date"]: (float(r["Close"]), float(r["AdjClose"])) for r in csv.DictReader(f)}


def write(path, rows):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Close", "AdjClose"])
        for m in sorted(rows):
            w.writerow([m, f"{rows[m][0]:.6f}", f"{rows[m][1]:.6f}"])


def bars(ib, contract, what, duration):
    """Month -> month-end close. Adjusted prices come only as daily bars at IBKR,
    so for those the last trading day of each month is kept."""
    _errors.clear()
    daily = what == "ADJUSTED_LAST"
    out = ib.reqHistoricalData(contract, endDateTime="", durationStr=duration,
                               barSizeSetting="1 day" if daily else "1 month",
                               whatToShow=what, useRTH=True, formatDate=1, timeout=180)
    time.sleep(PACE_SECONDS)
    months = {}
    for b in out:                         # bars arrive oldest first, so the last one per month wins
        if b.close and b.close > 0:
            months[str(b.date)[:7]] = b.close
            _last_bar[what] = (str(b.date)[:10], b.close)
    return months


def update_rate(log):
    """The 10-year Treasury yield, from IBKR's TNX index (yield x 10)."""
    from ib_async import Index
    ib = connect(log)
    contract = Index("TNX", "CBOE", "USD")
    if not ib.qualifyContracts(contract):
        log("IBKR:TNX: index not found")
        return
    _errors.clear()
    out = ib.reqHistoricalData(contract, endDateTime="", durationStr="10 D", barSizeSetting="1 day",
                               whatToShow="TRADES", useRTH=True, formatDate=1, timeout=60)
    time.sleep(PACE_SECONDS)
    out = [b for b in out if b.close and b.close > 0]
    if not out:
        log(f"IBKR:TNX: no bars returned {_errors[:2]}")
        return
    FOLDER.mkdir(exist_ok=True)
    b = out[-1]
    _remember(RATES, "TNX", {"date": str(b.date)[:10], "quote": b.close, "yield": b.close / 1000})
    log(f"IBKR:TNX: {b.close} on {str(b.date)[:10]} -> 10-year yield {b.close / 10:.3f}%")


def update(ticker, log):
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    from ib_async import Stock
    if ticker.upper() == "TNX":
        return update_rate(log)
    FOLDER.mkdir(exist_ok=True)
    path = FOLDER / f"{ticker}.csv"
    saved = read(path)
    if saved:
        last = max(saved)
        months = (date.today().year - int(last[:4])) * 12 + date.today().month - int(last[5:7]) + 3
        duration = f"{months} M" if months <= 11 else f"{math.ceil(months / 12)} Y"
    else:
        duration = "20 Y"
    ib = connect(log)
    contract = Stock(ticker.replace(".", " "), "SMART", "USD")
    if not ib.qualifyContracts(contract):
        log(f"IBKR:{ticker}: not found at IBKR (delisted, or a different symbol)")
        return
    close = bars(ib, contract, "TRADES", duration)
    err_close = list(_errors)
    adj = bars(ib, contract, "ADJUSTED_LAST", duration)
    err_adj = list(_errors)
    new = {m: (close[m], adj[m]) for m in close if m in adj}
    if not new:
        log(f"IBKR:{ticker}: no monthly bars returned ({duration}): TRADES {len(close)} bars "
            f"{err_close[:2]}, ADJUSTED_LAST {len(adj)} bars {err_adj[:2]}")
        return
    if saved:
        overlap = [m for m in new if m in saved and m != max(new)]   # the current month is not final yet
        if not overlap:
            log(f"IBKR:{ticker}: no overlap with saved history; downloading it all again")
            path.unlink()
            return update(ticker, log)
        # rescale the saved history to IBKR's latest adjustment basis
        k_close = sum(new[m][0] / saved[m][0] for m in overlap) / len(overlap)
        k_adj = sum(new[m][1] / saved[m][1] for m in overlap) / len(overlap)
        saved = {m: (c * k_close, a * k_adj) for m, (c, a) in saved.items()}
    saved.update(new)
    write(path, saved)
    day = _last_bar.get("ADJUSTED_LAST", (max(new), None))[0]
    _remember(LATEST, ticker, {"date": day, "close": new[max(new)][0]})
    log(f"IBKR:{ticker}: {len(new)} months fetched ({duration}), {len(saved)} months saved, "
        f"{min(saved)} to {max(saved)}")

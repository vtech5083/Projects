"""
update_market.py -- write market_inputs.json from data already on disk; nothing typed by hand.

  prices          latest IBKR close per ticker (prices_ibkr/_latest.json, with its date;
                  else the latest month in prices_ibkr/TICKER.csv)
  monthly closes  the last 61 months of IBKR dividend-adjusted closes, on the same
                  months as SPY, for beta
  risk-free rate  IBKR's TNX (10-year Treasury yield) from prices_ibkr/_rates.json,
                  if it is there; otherwise the rate already in market_inputs.json
  market value    each company's latest reported public float (its 10-K cover page),
                  for health check 21

Which tickers: universe_tickers.txt (the largest US non-financial companies, from
build_universe.py) plus the watchlist in watchlist.txt.
Refresh the inputs first:  IBKR:TICKER and IBKR:TNX requests through fetch_queue.py.
Run:  python3 update_market.py
"""
import json
from datetime import date
from pathlib import Path

import pandas as pd

import health_check as hc

PRICES = Path("prices_ibkr")


def tickers():
    names = []
    for f in ("universe_tickers.txt", "watchlist.txt"):
        if Path(f).exists():
            names += [t.strip() for t in Path(f).read_text().split() if t.strip()]
    return sorted(set(names))


def facts_by_ticker():
    """ticker -> [companyfacts of each SEC registration it has filed under]."""
    out = {}
    u = Path("study/universe.json")
    if u.exists():
        for cik, x in json.loads(u.read_text()).items():
            f = Path(f"edgar_cache/facts_{cik}.json")
            if x.get("price_ticker") and f.exists():
                out.setdefault(x["price_ticker"], []).append(f)
    table = Path("edgar_cache/company_tickers.json")
    if table.exists():
        for r in json.loads(table.read_text()).values():
            f = Path(f"edgar_cache/facts_{r['cik_str']}.json")
            if f.exists() and r["ticker"] not in out:
                out[r["ticker"]] = [f]
    return out


def load(t):
    f = PRICES / f"{t}.csv"
    if not f.exists():
        return None
    return pd.read_csv(f, dtype={"Date": str}).drop_duplicates("Date", keep="last").set_index("Date").sort_index()


def main():
    m = json.loads(Path("market_inputs.json").read_text())
    latest = json.loads((PRICES / "_latest.json").read_text()) if (PRICES / "_latest.json").exists() else {}
    rates = json.loads((PRICES / "_rates.json").read_text()) if (PRICES / "_rates.json").exists() else {}
    today = date.today().isoformat()
    if "TNX" in rates:
        r = rates["TNX"]
        m["risk_free_rate"] = round(r["yield"], 5)
        m["risk_free_source"] = f"US 10-year Treasury yield from IBKR (TNX, CBOE), close of {r['date']}"
        m["rates_as_of"] = r["date"]
    spy = load("SPY")
    months = list(spy.index[-61:])
    m.update({"as_of": today, "prices": {}, "prices_as_of": {}, "monthly_closes": {"SPY": spy.loc[months, "AdjClose"].tolist()},
              "market_value": {}, "index_price": {"SPY": float(spy["Close"].iloc[-1])},
              "prices_source": "Interactive Brokers (IBKR API, read-only), latest daily close; date per ticker in prices_as_of",
              "monthly_closes_source": "Interactive Brokers, month-end closes adjusted for splits and dividends"})
    facts = facts_by_ticker()
    missing = []
    for t in tickers():
        p = load(t)
        if p is None or len(p) == 0:
            missing.append(t)
            continue
        info = latest.get(t)
        m["prices"][t] = float(info["close"]) if info else float(p["Close"].iloc[-1])
        m["prices_as_of"][t] = info["date"] if info else p.index[-1]
        common = [x for x in months if x in p.index]
        m["monthly_closes"][t] = p.loc[common, "AdjClose"].tolist()
        fl = [json.loads(f.read_text()) for f in facts.get(t, [])]
        m["market_value"][t] = hc.market_value_inputs(fl, p, today) if fl else None
    # beta: the market series must cover exactly the stock's months; run.py aligns by count
    m["monthly_closes_by_ticker_months"] = {t: len(v) for t, v in m["monthly_closes"].items()}
    Path("market_inputs.json").write_text(json.dumps(m, indent=1))
    print(f"risk-free {m['risk_free_rate']:.3%} ({m.get('rates_as_of')}); {len(m['prices'])} prices"
          + (f"; no IBKR history for {', '.join(missing)}" if missing else ""))


if __name__ == "__main__":
    main()

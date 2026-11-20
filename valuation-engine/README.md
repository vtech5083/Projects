# Valuation engine

A personal, rule-based fundamental valuation engine for large US non-financial
companies. It reads company filings from the SEC (XBRL company facts), prices and
the 10-year Treasury yield from Interactive Brokers (read-only API), and asks one
question:

> Is this a good business (cash and profit year after year, so a low price is not a
> value trap), at a price that needs only growth companies of its size have
> realistically achieved?

The rule is frozen as **rule v1** (20 November 2026): see [`rule-v1.md`](rule-v1.md).
The engine is in a paper period: verdicts are logged and not acted on. Nothing here
is investment advice.

## Pipeline

| Step | Script |
|---|---|
| Filings to annual and quarterly statements (unit, split and tag fixes) | `edgar_statements.py`, `edgar_quarterly.py`, `build_universe.py` |
| Prices and Treasury yield from IBKR, kept up to date incrementally | `ibkr_prices.py` (through `fetch_queue.py`) |
| Market inputs (nothing typed by hand) | `update_market.py` |
| Cost of capital, DCF valuation, simulation | `cost_of_capital.py`, `valuation.py`, `monte_carlo.py` |
| 22 health checks, including market value vs reported public float and value-vs-price quarantine | `health_check.py` |
| Good-business and realistic-price tests (rule v1) | `quality.py` |
| One run per company, saved records | `run.py` |
| Dashboard tables | `make_dashboard_data.py` |
| Tests | `tests.py` |

`study/` holds the research: backtests on point-in-time SEC data 2013-2024
(`backtest2.py`, `backtest_rule_v1.py`), base rates of revenue growth by company
size (`base_rates.py`), and the reports each produced.

Data (SEC downloads, IBKR prices, statements, run records) is rebuilt locally and
is not in this repository.

# Rule v1: Vivek's buy-list rule, frozen 20 November 2026

Written down on 20 November 2026, before the backtest was rerun on clean data. The rule was chosen on first principles (fixing bugs and edge cases), not on backtest returns. During the paper period it changes only to fix a bug, and every change is dated here. Results from different versions are not mixed.

Code: `quality.py` (`RULE_VERSION = "rule v1 (frozen 2026-11-20)"`), checks 21 and 22 in `health_check.py`, wired in `run.py`. Tests: `tests.py` (41 tests).

## The rule

**Buy-list candidate = data that can be trusted, a good business, and a price that needs only realistic growth.**

### 1. Data gate (health check)

A company is held back from any verdict when:

| Check | Fails when | Verdict given |
|---|---|---|
| 21. Market value | Price x shares is below 0.8x, or above 3x, the public float the company reports on its 10-K cover, moved forward by the share price since that date. The float leaves out insiders' shares, so the market value cannot be below it. 2x to 3x is a WATCH. | Check the data |
| 22. Value vs price | Our base value is more than 5x the price, or no value could be computed (a core figure such as the share count is missing) | Check the data |
| 22. Value vs price | Our base value is zero or negative | Model does not fit |

**Changed on 20 November, before any results:** the first draft also quarantined any price that "even a 30% yearly revenue decline would justify". On the first live run that fired for Ford, Comcast and AES at only 1.3x to 1.8x value to price. For capital-heavy companies, shrinking frees capital, so in the model the value barely falls with growth. Hitting that floor means the price needs no growth, not that the data is wrong, so the growth test treats it as realistic and only the 5x test quarantines.

Any other health-check FAIL gives the verdict "Failed health check".

### 2. Good business: all five, over the last ten fiscal years (at least five)

| Test | Passes when |
|---|---|
| Generates cash | Free cash flow (the engine's: cash from operations minus capital spending) positive in at least 80% of years |
| Stays profitable | Operating profit positive in at least 90% of years |
| Earns its cost of capital | After-tax return on invested capital above the cost of capital in at least 70% of years. A profitable year with invested capital at or below zero (more cash than equity plus debt) counts as a pass. A loss year never counts as a pass. |
| Debt it can repay | Net debt no more than 4 years of free cash flow (latest three years' average), or net cash |
| Does not keep issuing shares | Share count grows no more than 1% a year over five years, leaving out any one-year jump by a whole stock-split ratio |

The old "steady margins" test is dropped. It is not a basic soundness check, and the job it did (not trusting one great year) now sits in the price test.

### 3. Realistic price

The yearly revenue growth for five years that makes our value equal the price is computed at the company's **best-three-year average operating margin** in the last ten years (adjusted for R&D and leases), or at its normal margin if that is higher. The price passes when at least **10%** of US companies of the same revenue size grew that fast (base rates from SEC data, 2011-2024). The growth needed at the normal margin is shown beside it as a stricter second opinion and does not decide the verdict.

### Verdicts

| Verdict | Meaning | Passes the rule |
|---|---|---|
| Candidate | Good business, realistic price, price at or below our base value | Yes |
| Fair | Good business, realistic price, price above our base value | Yes |
| Too expensive | Good business, but the price needs growth fewer than 10% of same-size companies achieved | No |
| Avoid | Fails one or more of the five business tests | No |
| Failed health check | A FAIL on any other health check | No |
| Check the data | Quarantined by check 21 or 22 | No |
| Model does not fit | Our value is zero or negative | No |

## Bugs fixed before freezing (20 November)

| Bug | Effect | Fix |
|---|---|---|
| The backtest guessed splits by comparing old and current share counts | Alphabet's price and shares were on different bases in 11 of 12 years (value 20-30x the price); Cencora 2013 price $0.07 | Splits are found from the filings themselves: a later filing that restates the same period's share count and EPS by a whole split ratio (`split_events` in `edgar_statements.py`; needs at least 4 matching restatements; real splits show 14-36). IBKR prices are adjusted for every split, so the price is multiplied by every split reported after the formation date. |
| A quarterly share count filed in thousands was not converted | ConocoPhillips showed 1.2 million shares instead of 1.2 billion | Each period is checked against net income / diluted EPS, which is always in single shares |
| Loss years with negative capital counted as beating the cost of capital | A negative profit over negative capital looks like a positive return | Only profitable years can pass |
| Live prices were typed in by hand | Manual errors possible | `update_market.py` now reads IBKR's saved prices and TNX yield; nothing is typed |
| A shrinking company "released" capital as cash at the same rate growth costs it (`valuation.py`) | A capital-heavy company (Delta, Lear, Lennar, DXC) was worth more shrinking 30% a year than growing, so the price test said the price "needed no growth" | Reinvestment never goes below zero: plants, stores and aircraft rarely sell back at book value. Value now always falls as assumed growth falls. |
| The verdict ignored failed health checks | Lennar, Kimberly-Clark and Oracle had a health-check FAIL and still showed Candidate or Fair | A failed health check gives the verdict "Failed health check", which does not pass the rule |

## Paper period

Starts with the first run on the full universe (the largest US non-financial companies plus the watchlist, about 180). Every run's record is kept unedited. Review after two quarters of results.

## Backtest after freezing (run once, 20 November; not used to change the rule)

Top-100 US non-financial companies at each year-end 2013-2024 (960 company-dates), IBKR prices, all fixes above. Equal-weighted portfolio of the companies that pass the rule, held one year; a year with no picks would sit in cash.

| | Per year | $1 became | Worst year |
|---|---|---|---|
| Rule v1 (16-30 picks a year) | +13.3% | $4.14 | -7.2% (2021) |
| All companies in the group | +12.6% | $3.92 | -7.7% |
| S&P 500 fund (SPY) | +14.4% | $4.53 | -18.2% (2022) |

The rule beat the group in 8 of 12 years and SPY in 3. Its edge over the group, +0.7 points a year, has t = 0.34: no evidence of an edge in returns either way. Over three years its picks returned +44.9% against +42.9%. What it did do is avoid the worst year: -7.2% against SPY's -18.2%. That matches its purpose (do not overpay, do not buy traps) more than a claim to beat the market.

Quarantine after the fixes: 6 company-dates still had our value over 5x the price. The market-value check flagged 66, mostly Honeywell, GE, RTX, Exelon and AT&T before their spin-offs: IBKR adjusts past prices for spin-offs as well as splits, so those prices are not what traded at the time, and they are now held back instead of valued.

Script and output: `study/backtest_rule_v1.py`, `study/backtest_rule_v1_report.txt`.

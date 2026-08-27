# Quality Dip Scanner

A Telegram bot that scans the S&P 500 once per trading day, finds **strong quality companies that have fallen hard AND show evidence the selling has exhausted**, filters out value traps, and sends an alert.

**Strategy:** Mean reversion on quality stocks. Buy strong companies on sharp dips, sell on recovery. Holding horizon: up to one year. This is a *separate* sleeve from a long-term ETF core, sized as a small, defined slice of the portfolio.

> **Design philosophy:** Do not catch the falling knife. The single biggest edge in a buy-the-dip system is to NOT buy during the fall, but to buy the first sign of stabilization.

---

## Setup

### 1. Create the Telegram bot

1. Open Telegram, search for `@BotFather` (official account, blue check).
2. Send `/newbot`.
3. Give it a name (e.g. `Dip Scanner`).
4. Give it a username ending in `bot` (e.g. `your_dip_scanner_bot`).
5. BotFather returns a token. This is `TELEGRAM_BOT_TOKEN`. Save it.

### 2. Get the chat ID

1. Send any message to your new bot.
2. Open: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find `"chat":{"id":...}` in the JSON. That number is `TELEGRAM_CHAT_ID`.

### 3. Configure GitHub Secrets

In your repo: `Settings > Secrets and variables > Actions > New repository secret`.

Add:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Code reads these via `os.environ` -- never hardcode them.

---

## How to Run

### Manual run (testing)

Go to `Actions > Daily Dip Scan > Run workflow` in GitHub.

### Scheduled run

The workflow runs automatically weekdays at 20:30 UTC (after US market close) via cron.

### Local run

```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=your_token TELEGRAM_CHAT_ID=your_chat_id python src/scanner.py
```

---

## Four-Gate Pipeline

Every stock must pass all four gates:

| Gate | Purpose | Key Metrics |
|------|---------|-------------|
| **Gate 0: Regime** | Is the market healthy? | SPY vs 200-day MA |
| **Gate 1: Quality** | Is this a company worth catching? | ROE > 12%, positive op margin, debt/eq < 150, mkt cap > $10B |
| **Gate 2: Dip + Stabilization** | Has it fallen hard AND stopped falling? | Drawdown >= 25%, vol-adjusted drop, RSI turning up or higher low |
| **Gate 3: Trap** | Is the thesis broken? | Fresh lows, gap-down on volume, earnings blackout, negative growth |

---

## Opportunity Score & Ranking

The gates decide **who qualifies**; the score decides **who is best**. On a
broad sell-off day dozens of stocks can pass all four gates — alerting all of
them buries the good ones. Every passer is scored 0–100 and only the top
`MAX_ALERTS_PER_DAY` (5) are sent, best first, with at most `MAX_PER_SECTOR`
(2) per sector so one crashing industry doesn't fill the whole list.

| Component | Weight | What it measures |
|-----------|--------|------------------|
| Dip depth | 0–25 | How stretched the drop is vs the stock's *own* volatility |
| Timing | 0–30 | Stabilization signals + **fresh bounce** (low was 1–7 days ago) + **volume confirmation** (up close on ≥1.2× average volume — real demand, not quiet drift) |
| Quality | 0–25 | ROE, operating margin, low debt |
| Relative strength | 0–10 | Held up better than SPY over the last 20 sessions — dips that found buyers first recover first |
| Trap penalty | 0 to −30 | Each red flag subtracts (price-based traps hit harder) |

Every alert shows its score and today's rank (e.g. `82/100, rank 1 of 4`).
Candidates crowded out by the caps are *not* marked as alerted, so they can
still alert tomorrow if they keep qualifying. The monthly simulation fills its
free slots by the same score, so paper trading exercises the same ranking.

---

## Backtest — Prove the Timing Before Trusting It

`src/backtest.py` replays the price-based gates over years of history with no
lookahead, then measures what actually happened after each signal:

- Forward returns at 21 / 63 / 126 / 252 trading days
- Win rate and beat-SPY rate per horizon
- Max adverse excursion (how much *more* pain came after entry)
- **Stabilization ON vs OFF** — the falling-knife baseline, so the core design
  claim ("buy the first stabilization, not the fall") is measured, not assumed

```bash
PYTHONPATH=. python -m src.backtest --period 5y --max-tickers 100
```

Or in GitHub: `Actions > Backtest > Run workflow` (choose period, universe
size, and whether to send the report to Telegram).

Honest limitation: Gate 1 (quality) can't be replayed — yfinance has no
point-in-time fundamentals — so backtest results are a lower bound on
selectivity. If the numbers don't beat "buy SPY on the same dates," the system
has no edge; believe the numbers, not the design.

---

## Monthly Paper-Trading Simulation

A one-time, fake-money test of the strategy that reports entirely through the
Telegram bot. It runs for a full month — `SIM_DURATION_DAYS` (30) days from
day one, *not* whatever is left of the calendar month — and is driven by a daily
GitHub Action (`Monthly Simulation` workflow), so there is no always-on server.
(The first run, in June 2026, started on the 8th and under the old
calendar-month-end rule got only 22 days; a mean-reversion strategy needs the
whole month for the reversion to happen.)

**How it trades:**

- **Buys** are the *same strict four-gate signals* the scanner alerts on — no
  relaxation. It opens up to `SIM_MAX_POSITIONS` (10) positions, a notional
  `SIM_CASH_PER_STOCK` ($1,000) each, and fills free slots with fresh dip
  signals as they appear during the month.
- **Sells** use the mean-reversion exit, checked daily:
  - take-profit: recovered ≥ `SIM_TAKE_PROFIT_PCT` from entry,
  - stop-loss: fell ≥ `SIM_STOP_LOSS_PCT` from entry,
  - bounce done: RSI back above `SIM_RSI_EXIT` while in profit,
  - thesis break: a fresh price-based trap (new lows / steep downtrend / gap-down).
- **Every buy and sell is announced** on Telegram with its reason, a plain
  status update goes out every `SIM_UPDATE_INTERVAL_DAYS` (3) days, and a full
  summary is sent at month end.

**Run it:**

```bash
# Local dry run (prints to stdout if Telegram creds are unset)
PYTHONPATH=. python -m src.simulate
```

In GitHub: `Actions > Monthly Simulation > Run workflow` starts it today. The
daily cron then drives buys, sells, updates, and the final summary for the rest
of the month. To start a fresh month later, dispatch the workflow with the
`reset` input checked (deletes `simulation.json`). State lives in
`simulation.json` and is committed back by the workflow. This is a simulation
only — never investment advice.

---

## Replaying a Month After the Fact

`src/simulate.py` can only run forward — one step per cron firing, against
whatever the price feed returns today — so a month takes a month to see. Two
modules answer questions about a month that has already happened.

**`src/replay.py` — re-run the book day by day over a past window.**

```bash
PYTHONPATH=. python -m src.replay 2026-07-28              # a full 30-day month
PYTHONPATH=. python -m src.replay 2026-07-28 2026-08-27   # explicit end date
```

It prints the exact Telegram messages the bot would have sent, then a summary.
How it works, and what is and is not faithful:

- **Prices** come from the offline cache in `data/prices/` (see below),
  truncated to the day being replayed, so no step can see the future.
- **Exits** are the production rule — `simulate.evaluate_exit`, unchanged.
- **Buys** come from the scanner's own recorded alert history
  (`data/alerts.json`, reconstructed from the committed dedup state), because a
  buy signal needs the whole S&P 500 universe *and* point-in-time fundamentals,
  and a price cache can reconstruct neither. Every entry is therefore a signal
  the live bot really did fire, on the day it fired, at that day's close.
- **The one judgement call** the alert log does not record is ordering: when
  more names are flagged than there are free slots, the live scanner ranks by
  composite score. The replay's tie-break is "most oversold first" (lowest RSI),
  which is strategy-consistent and uses only cached data.
- An alert stamped on a non-trading day rolls to the next session; one older
  than `MAX_SIGNAL_AGE_DAYS` (4) is dropped rather than dragged forward.

The summary reports two different returns, because they are easy to confuse:
**return on capital** is P&L over the money actually at risk
(`SIM_CASH_PER_STOCK` x `SIM_MAX_POSITIONS` = $10,000) and is the figure to
compare against a buy-and-hold benchmark; **return on turnover** is P&L over the
sum of every position's cost basis, so a book that recycles the same $10,000
through 26 trades reports $26,000 "invested" and a correspondingly smaller
percentage. The live Telegram summary prints the turnover figure.

**`src/whatif.py` — value a past book at a later date, as if nothing was sold.**

```bash
PYTHONPATH=. python -m src.whatif 2026-06-29              # value that day's book today
PYTHONPATH=. python -m src.whatif 2026-06-29 2026-07-15
```

This is how the exit rules get judged against simply doing nothing. Bars are the
broker's and carry retroactive corporate-action adjustments, so for a ticker
that paid a distribution the cached entry-date close sits below the price the
bot recorded live; the tool reports both the price change against the entry
actually paid and the total return on the adjusted series.

### The offline price cache

`data/prices/` holds daily OHLCV bars snapshotted from the broker feed:

```
data/prices/_dates.json   ["2025-05-06", ...]   shared trading-day index
data/prices/<TICKER>.json {"open": [...], "high": [...], "low": [...],
                           "close": [...], "volume": [...]}
```

A ticker's arrays are right-aligned against `_dates.json`, so a series with N
bars covers the last N dates. `src/pricecache.py` reads them back in the same
`{ticker: DataFrame}` shape `src.data.fetch_prices` returns, which is why every
gate and indicator in the project works unchanged on cached data. The cache
covers the tickers the simulation and the recorded alert stream actually
touched, not the whole index — a replay reports any signal it could not price
rather than silently skipping it.

---

## Tuning the Exit Rules

`src/tune.py` turns the replay into a measuring instrument. It answers two
separate questions, and the order matters.

**1. What does a signal do on its own?** `hold_curve` takes every alert in the
windows, buys at that day's close, and holds for exactly N sessions with no exit
rule at all:

```bash
PYTHONPATH=. python -m src.tune curve
```

If the curve is still climbing at N days, any exit that fires before N is
leaving money behind. This is the honest starting point, because it is measured
before any parameter is chosen. By default it keeps only signals with data out
to the longest horizon, so every row describes the same set of trades — without
that, late signals drop out of the long horizons and part of the curve is a
changing sample rather than a changing holding period.

**2. What would the book have returned under a given set of thresholds?**
`sweep` replays every window under each combination in a grid:

```bash
PYTHONPATH=. python -m src.tune sweep
```

`with_overrides` builds a stand-in for the config module, so the production exit
code runs unmodified against substituted thresholds.

### Reading the results honestly

A grid search over two months will find a winner by construction — there are
hundreds of combinations and about fifty trades. Three guards, none sufficient:

- `sweep` reports **every window separately**, never only the average, and ranks
  on the *worst* window's excess return over SPY rather than the mean.
- `pick` keeps only settings that beat the current configuration in **every**
  window, which throws away the combinations that win big in one month and lose
  in the other.
- The hold curve is computed **independently of the grid**, so agreement between
  the two is weak evidence rather than the same fit counted twice.

Treat any result here as a hypothesis to check against more history — the
five-year `src/backtest.py` replay is the right next test — not as a settled
answer. More cached months make all of this stronger; the cache is the binding
constraint, not the code.

### What the first pass changed, and what it rejected

Measured over eight rolling 30-day windows (return on the $10,000 book; the
full table is in `reports/tuning-exit-rules.txt`):

| | mean | worst window | beats SPY |
|---|---|---|---|
| before | +4.92% | +0.11% | 5/8 |
| `SIM_MIN_HOLD_SESSIONS = 10` | **+6.49%** | +0.11% | **7/8** |

The exit-free hold curve is what motivated it: a signal is worth about 0% one
session after it fires and about +5.6% after 21, because RSI recovers within a
day or two of a bounce while the reversion being bought takes weeks. Without a
floor the bounce-done rule was closing the book almost immediately for one or
two percent. The floor is monotone from 3 to 15 sessions and never makes the
worst window worse, which is why it was adopted over simply raising
`SIM_RSI_EXIT` — that lifts the mean about as much but gives up the worst
window (+0.11% to −0.37%) and effectively deletes the rule instead of deferring
it.

Three changes that looked good on the two headline months were **rejected**
after the rolling windows disagreed:

- `SIM_THESIS_BREAK_MIN_LOSS_PCT = 3` gained a point on both headline months and
  turned the worst rolling window from +0.11% to −1.37%.
- `SIM_TAKE_PROFIT_PCT = 15` doubled one month's return, but that rested on
  about four positions and neighbouring grid cells swung by two points.
- `SIM_STOP_LOSS_PCT` is inert in every window tested — the thesis-break rule
  reaches losers first, so the stop never fires and there is nothing to tune.

That two of the three survived a 720-combination grid over two months and still
failed out of sample is the whole argument for the rolling check.

---

## Threshold Tuning

All thresholds live in `config.py`. Key knobs:

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `MIN_DRAWDOWN` | 25% | Minimum drawdown from 52w high |
| `RSI_OVERSOLD` | 30 | RSI threshold for "oversold" |
| `LOOKBACK` | 5 | Days to look back for RSI turning up |
| `K_ATR` | 2.0 | ATR multiplier for vol-adjusted drop |
| `MIN_ROE` | 12% | Minimum return on equity |
| `MAX_DEBT_EQUITY` | 150 | Maximum debt/equity ratio |
| `MIN_MKT_CAP` | $10B | Minimum market cap |
| `DEDUP_DAYS` | 10 | Don't re-alert same ticker within N days |
| `MAX_ALERTS_PER_DAY` | 5 | Send only the N best-scored candidates per day |
| `MAX_PER_SECTOR` | 2 | Cap alerts per sector (avoid 5 correlated chip stocks) |
| `RS_LOOKBACK` | 20 | Sessions for relative strength vs SPY |
| `VOLUME_CONFIRM_MULT` | 1.2 | Recent volume vs average for volume confirmation |
| `FRESH_BOUNCE_MAX_DAYS` | 7 | Bounce off the low within N sessions scores as fresh |
| `TRAP_BEHAVIOR` | "warn" | "warn" = alert with warning, "suppress" = skip |
| `SUPPRESS_IN_RISK_OFF` | False | Skip all alerts when SPY below 200dma |
| `STABILIZATION_REQUIRED_RISK_OFF` | 2 | Stabilization signals required in RISK_OFF |
| `EARNINGS_BLACKOUT_DAYS` | 5 | Flag if earnings within N days |
| `SIM_DURATION_DAYS` | 30 | Simulation run length — a full month from day one |
| `SIM_MIN_HOLD_SESSIONS` | 10 | Sessions before the bounce-done exit may fire |

---

## Architecture

```
GitHub Actions (daily cron, after US close)
        |
        v
   scanner.py
        |
        +--> universe.py   : S&P 500 list (Wikipedia -> cached copy -> full
        |                    static fallback; partial lists are never trusted)
        +--> data.py       : price history + fundamentals via yfinance
        +--> regime.py     : SPY vs 200dma (RISK_ON / RISK_OFF)
        +--> indicators.py : RSI, ATR, MAs, drawdown, stabilization, volume
        |                    confirmation, relative strength, days-since-low
        +--> gates.py      : four-gate pipeline
        +--> score.py      : opportunity score + best-first ranking, sector caps
        +--> state.py      : dedup store (state.json)
        +--> telegram.py   : send alerts

   backtest.py (on demand): replay the gates historically, measure the edge

   replay.py   (on demand): re-run a past month day by day
        |
        +--> pricecache.py : offline daily bars (data/prices/), as-of truncation
        +--> data/alerts.json : the scanner's own recorded alert history
        +--> simulate.py   : the production exit rule, unchanged

   whatif.py   (on demand): value a past book later, as if nothing was sold
```

No always-on server. Zero cost. Everything runs inside the Action and exits.

---

## Validate Before You Trust It

This is the most important part. A scanner that *looks* smart can still lose money. Prove the edge first.

1. **Backtest the rule.** Measure forward returns at 1, 3, 6, and 12 months for historical signals.
2. **Compare against a dumb baseline.** If it doesn't beat "buy SPY and hold" after costs, it has no edge.
3. **Test the stabilization upgrade specifically.** Run backtests with Gate 2b ON vs OFF.
4. **Walk-forward, don't curve-fit.** Tune thresholds on older data, validate on newer.
5. **Paper-trade first.** Run live alerts for a few weeks without committing money.

---

## Disclaimer

This is a tool for organizing research and thinking, not investment advice. Every signal is a starting point for your own due diligence, never a buy instruction; the bot never tells you to buy. Active single-stock trading underperforms passive broad-index holding for most people, so keep this sleeve a small, defined slice of the portfolio -- money you can afford to be wrong with -- and not at the expense of the ETF core. Consult a licensed professional before significant moves.

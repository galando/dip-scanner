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
  - bounce done: RSI back above `SIM_RSI_EXIT` while in profit, but not before
    `SIM_MIN_HOLD_SESSIONS` have passed — see *Tuning the Exit Rules* below,
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
through 24 trades reports $24,000 of turnover and a correspondingly smaller
percentage.

Capital is the headline everywhere, Telegram included. Turnover is printed
beneath it, labelled as turnover, because it says how hard the capital worked —
but it is not a return: its denominator grows every time the bot trades again,
so the same dollars read differently depending only on how often they moved. The
June 2026 run ended -$730 on a $10,000 book. Reported on turnover that was
-3.0%; on capital it is -7.3%.

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

Fill or deepen it from the live feed (needs network access to the price feed):

```bash
PYTHONPATH=. python -m src.cachebuild --period 5y     # deepen everything cached
PYTHONPATH=. python -m src.cachebuild --check         # compare, write nothing
```

Two things it will not do, because both would quietly invalidate results already
published from the cache. It refuses to write when re-fetched bars **contradict**
what is stored, rather than overwriting them; and because a series is addressed
by position, it refuses a partial re-fetch that would **add sessions at the end**
of the shared calendar and thereby re-date every ticker it is not rewriting. A
ticker whose feed has a hole in the middle is reported and skipped rather than
gap-filled — the store cannot represent a hole, and inventing a bar would put
prices on disk indistinguishable from real ones.

**Depth is the binding constraint on everything downstream.** The five-year
`src/backtest.py` replay reads the cache with `--offline`, but needs more than
`BACKTEST_MIN_HISTORY` (252) sessions per ticker before it can emit a single
signal, and the validation in `src/validate.py` is only as strong as the number
of independent windows the cache spans.

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

Measured over eight rolling 30-day windows (return on the $10,000 book; the full
table, and the command that regenerates it, are in
`reports/tuning-exit-rules.txt`):

| | mean | worst window | beats SPY | trades |
|---|---|---|---|---|
| before | +4.53% | −0.10% | 6/8 | 159 |
| `SIM_MIN_HOLD_SESSIONS = 10` | **+5.55%** | −0.10% | **7/8** | 132 |

The exit-free hold curve is what motivated it: a signal is worth about 0% one
session after it fires and about +5.6% after 21, because RSI recovers within a
day or two of a bounce while the reversion being bought takes weeks. Without a
floor the bounce-done rule was closing the book almost immediately for one or
two percent.

That curve is 38 signals, all from June 2026 — not the whole cache. Measuring a
21-session forward return needs 21 sessions of data after the signal, so the
most recent month of signals cannot appear in it at all, and the report says so
above the table rather than calling it "every cached signal".

**10 is not the argmax, and the table has no argmax to find.** The curve rises
all the way out — 0 → +4.53, 5 → +5.32, 10 → +5.55, 15 → +5.84 — and saturates
at 20, where the row is identical to deleting the bounce-done rule (+6.10%, 124
trades). What the data prefers is "off".

The saturation is the measurement ending, not the strategy topping out: a 30-day
run holds about 21 sessions, so a floor near 20 can barely fire, and every
setting past ~15 collapses onto the same "never sells on RSI" behaviour. So the
gain from 15 and 20 is untested here rather than earned here. 10 is the largest
floor that still fires inside a run — it stays under measurement, and it keeps a
way out of a completed bounce over the holding periods this cache is too short
to see. That is a judgment call about a rule worth keeping, stated as one,
rather than a number the data picked.

For the same reason, raising `SIM_RSI_EXIT` is not a competing option: at 75 it
measures identically to "off" (+6.10%), because RSI never gets that high on a
position this book holds. Raising the threshold and deferring the rule are the
same intervention here; only the untestable part beyond a month tells them
apart.

What the rolling windows **rejected**:

- `SIM_THESIS_BREAK_MIN_LOSS_PCT = 3` buys the best worst-window in the table
  (+1.29%) with the worst mean (+3.91%) and only 4 of 8 windows beating SPY. It
  trades upside for downside rather than adding anything.
- `SIM_TAKE_PROFIT_PCT = 15` and `= 20` are the same row (+4.82%): above 15% the
  rule stops firing inside a month, so there is nothing there to tune.
- `SIM_STOP_LOSS_PCT = 10` moves almost nothing (+4.64% vs +4.53%, 161 vs 159
  trades). Thesis-break reaches most losers first, so the stop rarely gets to
  act.

That a change can survive a 720-combination grid over two months and still fail
out of sample is the whole argument for the rolling check.

### Checking a result away from where it was fitted

`src/validate.py` re-tests a candidate on windows it was not chosen on:

```bash
PYTHONPATH=. python -m src.validate
```

- **Non-overlapping windows.** The rolling windows used for tuning share
  sessions, so eight of them carry nowhere near eight windows of information.
  On the current cache only **two** windows are genuinely independent — that is
  the honest sample size, and the report leads with it.
- **Walk-forward.** Settings are chosen on the earlier windows and scored on the
  later ones, which had no vote in the choice. Test windows that share sessions
  with the training half are purged first — splitting a list of overlapping
  windows down the middle does not separate the data, it only looks like it
  does. On the current cache nothing survives that purge, so the check reports
  that it cannot run rather than a leaked number.
- **Paired bootstrap.** Both settings are replayed on the same windows, then
  whole windows are resampled to put an interval around the difference. The
  resampling unit is the window, so with overlapping windows the interval is
  optimistic — a floor on the uncertainty, never a p-value.

On the current cache the verdict is deliberately unflattering: the change helps
in 4 of the 8 overlapping windows and hurts in 3 — a lean, not a consistency —
the 95% interval on the two independent windows is [+0.00, +2.74] pp and
includes zero, and there is **no out-of-sample check at all**, because the cache
is too short to split into halves that do not share sessions. So the evidence
for `SIM_MIN_HOLD_SESSIONS` is a direction and a mechanism, not a measured size.
Every sentence of that verdict is computed from the numbers printed above it, so
a fix that moves them moves the wording too.
Deepening the cache is what changes that, which is what `src/cachebuild.py` is
for.

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

   tune.py     (on demand): measure the exit rules, sweep their thresholds
   validate.py (on demand): re-test a candidate away from where it was fitted
   cachebuild.py           : fill data/prices from the live feed
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

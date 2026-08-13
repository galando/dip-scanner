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
Telegram bot. It runs for one calendar month and is driven by a daily GitHub
Action (`Monthly Simulation` workflow) — no always-on server.

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

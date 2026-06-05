# Quality Dip Scanner: Implementation Spec

A Telegram bot that scans the S&P 500 once per trading day, finds **strong quality companies that have fallen hard AND show evidence the selling has exhausted**, filters out value traps, and sends an alert.

Strategy: mean reversion on quality stocks. Buy strong companies on sharp dips, sell on recovery. Holding horizon: up to one year. This is a *separate* sleeve from a long-term ETF core, sized as a small, defined slice of the portfolio.

> **Design philosophy:** Do not catch the falling knife. The single biggest edge in a buy-the-dip system is to NOT buy during the fall, but to buy the first sign of stabilization. Every design choice below serves that principle.

---

## Part 0: What changed from a naive design (read this first)

A naive scanner alerts on "fell 25% from high + RSI < 30 + below 200-day MA." That selects for falling knives: oversold can stay oversold, and a stock making fresh lows usually keeps falling. This spec improves on that in six concrete ways. Implement all of them.

1. **Stabilization confirmation, not raw oversold.** Require evidence the decline has paused (RSI turning up from oversold, or a short-term higher low) before alerting. This is the core upgrade.
2. **Volatility-relative drop threshold.** A 25% drop means something different for a utility vs. a semiconductor. Measure the drop relative to the stock's own volatility (ATR / historical vol), not a fixed percent alone.
3. **Market regime filter.** Buying dips behaves very differently in a bull vs. bear tape. Gate everything on whether SPY is above its own 200-day MA, and label the regime in the alert.
4. **Earnings-event guard.** Flag (and optionally suppress) stocks with earnings inside the next N days; a pre-earnings drop is a gamble, not a clean dip.
5. **Honest treatment of trap detection.** Forward-looking fundamentals from yfinance are unreliable. Use what is available, but lean heavily on price-based trap detection and always surface raw data so the human judges.
6. **Dedup with state.** Don't re-alert the same stock every day it qualifies.

---

## Part 1: Create the Telegram bot from scratch

### Create the bot
1. Open Telegram, search for `@BotFather` (official account, blue check).
2. Send `/newbot`.
3. Give it a name (e.g. `Dip Scanner`).
4. Give it a username ending in `bot` (e.g. `gal_dip_scanner_bot`).
5. BotFather returns a token like `123456789:ABCdef...`. This is `TELEGRAM_BOT_TOKEN`. Save it; never share it.

### Get the chat id
1. Send any message to your new bot (at least one, or the bot cannot message you).
2. Open in a browser: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find `"chat":{"id":...}` in the JSON. That number is `TELEGRAM_CHAT_ID`.

Both values go in as GitHub Secrets, never in code.

---

## Part 2: Architecture

```
GitHub Actions (daily cron, after US close)
        |
        v
   scanner.py
        |
        +--> universe.py   : S&P 500 ticker list
        +--> data.py       : price history + fundamentals via yfinance
        +--> regime.py     : is the market in a buy-dips regime? (SPY vs 200dma)
        +--> indicators.py : RSI, ATR, moving averages, drawdown, higher-low detection
        +--> gates.py      : the four gates (regime / quality / dip+stabilization / trap)
        +--> state.py      : dedup store (don't re-alert same ticker within N days)
        +--> telegram.py   : send alert
        |
        v
   Candidates passing all gates --> Telegram --> phone
```

No always-on server. Zero cost. Everything runs inside the Action and exits.

---

## Part 3: Repo layout

```
dip-scanner/
├── .github/workflows/scan.yml
├── src/
│   ├── universe.py
│   ├── data.py
│   ├── indicators.py
│   ├── regime.py
│   ├── gates.py
│   ├── state.py
│   ├── telegram.py
│   └── scanner.py
├── config.py            # all thresholds in one place, easy to tune
├── requirements.txt
└── README.md
```

Put **every threshold in `config.py`**. Tuning is the whole game; thresholds must not be buried in logic.

---

## Part 4: The core logic, four gates

A candidate must pass all four gates, in this order (cheapest/most-rejecting first for efficiency).

### Gate 0: Market regime (checked once per run, not per stock)
Purpose: don't fight a bear market.
- Compute SPY's 200-day MA. Define regime as `RISK_ON` if SPY close > 200dma, else `RISK_OFF`.
- Default behavior: in `RISK_OFF`, still run but **raise the bar** (require deeper stabilization, see Gate 2) and clearly label the regime in every alert. Make this switchable in config: `SUPPRESS_IN_RISK_OFF = False` by default.
- Rationale: most mean-reversion edges are far stronger when the broad market is healthy. The user should always *know* the regime when deciding.

### Gate 1: Quality (is this even a company worth catching?)
Purpose: ensure the drop is noise, not a collapse. All required:
- `ROE` > 12% (configurable)
- Operating margin > 0 (`operatingMargins`)
- Debt/equity < 150 (`debtToEquity`; note this is sector-sensitive, keep configurable)
- Market cap > $10B (filters small, whippy names)

yfinance does not always return every field. Skip a stock gracefully if key fields are missing; never crash the run. Log skips with the reason.

### Gate 2: Hard dip + stabilization (the upgraded entry trigger)
Purpose: the stock has fallen hard AND the selling shows signs of exhausting. This is the heart of the system. Two sub-parts, both required.

**2a. Hard dip (the stock really fell):**
- Drawdown from 52-week high >= `MIN_DRAWDOWN` (default 25%).
- AND the drop is large relative to the stock's own volatility: drawdown / (annualized historical vol) above a threshold, OR price is more than `K` ATRs below a recent reference (e.g. the 50-day MA). This normalizes "hard" across a sleepy utility and a volatile chip stock. Default `K` configurable.
- Price below the 200-day MA.

**2b. Stabilization (the fall is pausing — do NOT skip this):**
Require at least ONE of the following (configurable which are required; default: any one):
- RSI(14) was below `RSI_OVERSOLD` (default 30) within the last `LOOKBACK` days AND is now turning up (today's RSI > yesterday's RSI, and RSI > its own value `LOOKBACK` days ago).
- A short-term higher low: the most recent swing low is higher than the prior swing low (simple version: lowest low of last 5 days > lowest low of the 5 days before that, with today's close above the 5-day low).
- Two consecutive up closes off a 10-day low.

In `RISK_OFF` regime, require a *stronger* stabilization signal (e.g. two of the three, or both up-closes AND RSI turning up). Configurable.

> Why this matters: 2a alone is a falling knife. 2a + 2b is "a great company fell hard and just stopped falling" — a far better risk/reward entry.

### Gate 3: Trap vs. opportunity (is the thesis actually broken?)
Purpose: filter cases where the drop reflects real fundamental breakage. If any red flag fires, either suppress or alert WITH a prominent warning (config: `TRAP_BEHAVIOR = "warn"` or `"suppress"`, default `"warn"` so the human decides).

Red flags:
- **Fundamental (yfinance, use what's available, treat as soft):** `earningsGrowth` sharply negative; `revenueGrowth` negative year-over-year; collapsing forward estimates if obtainable. These fields are unreliable — weight them as warnings, not hard rejects.
- **Price-based trap detection (more reliable, weight these higher):**
  - Free-fall with no floor: price made a fresh `N`-day low within the last 1–2 days (contradicts stabilization; if Gate 2b passed this should rarely fire, but double-check).
  - Persistent steep downtrend: 50-day MA falling steeply AND price far below it with no flattening.
  - Gap-down on huge volume in the last few days (a sudden gap of > X% on volume >> average often signals real news, not noise).
- **Earnings event guard:** if next earnings date is within `EARNINGS_BLACKOUT_DAYS` (default 5), flag prominently. A drop right before earnings is an event bet. Config: optionally suppress.

> **Guiding principle, state it in the README:** No quantitative filter replaces judgment. These gates remove obvious traps, not all of them. Every alert ships with the raw numbers so the human makes the final call. The bot never says "buy."

---

## Part 5: Alert format (Telegram)

Each candidate passing all gates is sent as a clear message. Include enough raw data for the user to judge. Suggested structure:

```
🔔 Dip Opportunity Detected   [Regime: RISK_ON]

NFLX — Netflix Inc.
Price: $XXX
Drawdown from 52w high: -28%
Vol-adjusted drop: 2.1x (hard)
RSI(14): 27 → turning up (was 24 three days ago)
Stabilization: ✅ higher low + 2 up closes
Below 200-day MA: yes

Quality: ROE 22% | Op margin 18% | Debt/Eq 80 | Mkt cap $XXXB

Trap check:
  ⚠️ Revenue growth YoY: -4% (soft flag)
  ✅ No fresh lows, no high-volume gap-down
  ⚠️ Earnings in 6 days

Your call. Check WHY it fell before entering.
```

Always include the regime label, the stabilization evidence, and the trap flags. The alert is a research starting point, not an instruction.

---

## Part 6: requirements.txt

```
yfinance
pandas
numpy
requests
```

(Optionally `pandas-ta` for indicators, or compute RSI/ATR manually.)

---

## Part 7: GitHub Actions workflow

`.github/workflows/scan.yml`. Runs once per weekday after the US close.

```yaml
name: Daily Dip Scan
on:
  schedule:
    # 20:30 UTC ≈ 22:30 Amsterdam (summer) / 21:30 (winter), after US close (16:00 ET)
    - cron: '30 20 * * 1-5'   # weekdays only
  workflow_dispatch:           # manual run for testing
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -r requirements.txt
      - name: Run scanner
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python src/scanner.py
      - name: Commit state file
        # persists the dedup store between runs
        run: |
          git config user.name "dip-bot"
          git config user.email "bot@users.noreply.github.com"
          git add state.json || true
          git commit -m "update dedup state" || true
          git push || true
```

### Secrets
Repo `Settings > Secrets and variables > Actions > New repository secret`. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Code reads them via `os.environ`; never hardcode.

> Note: the cron is UTC and not minute-precise (occasional delays under load). Irrelevant for an end-of-day strategy. The cron does not auto-adjust for DST; accept the one-hour seasonal shift or split into two seasonal cron lines.

---

## Part 8: Implementation notes

1. **Indicators (`indicators.py`):** RSI(14), ATR(14), 50/200-day MAs, 52-week high, annualized historical volatility, swing-low detection. Compute manually or via pandas-ta. Keep these pure functions on a price DataFrame so they are unit-testable.
2. **Error handling:** yfinance fails on individual tickers and returns partial data. Wrap each ticker in try/except, skip gracefully with a logged reason, never let one ticker kill the run.
3. **Rate limiting:** Pulling ~500 tickers at once can get throttled. Batch with `yf.download` for price history (many tickers per call); fetch fundamentals (`.info`) more slowly with a small sleep, or only for tickers that already passed Gates 0–2 (big efficiency win: filter on cheap price data first, fetch expensive fundamentals only for survivors).
4. **Universe (`universe.py`):** Pull S&P 500 from Wikipedia, or keep a static list in the repo. Static is simpler and more reliable to start; refresh occasionally.
5. **State / dedup (`state.py`):** Persist a JSON of `{ticker: last_alerted_date}`. Don't re-alert the same ticker within `DEDUP_DAYS` (default 10) unless it had dropped out of the candidate set and re-qualified. Commit the file back (see workflow) or use Actions cache.
6. **Test before trusting cron:** Use `workflow_dispatch` to run manually and confirm the full path works before relying on the schedule.
7. **config.py:** every threshold lives here. Example keys: `MIN_DRAWDOWN`, `RSI_OVERSOLD`, `LOOKBACK`, `K_ATR`, `MIN_ROE`, `MAX_DEBT_EQUITY`, `MIN_MKT_CAP`, `EARNINGS_BLACKOUT_DAYS`, `DEDUP_DAYS`, `TRAP_BEHAVIOR`, `SUPPRESS_IN_RISK_OFF`.

---

## Part 9: Validate before you trust it (do this before risking real money)

This is the most important part, and it is easy to skip. A scanner that *looks* smart can still lose money. Prove the edge first.

1. **Backtest the rule.** For each historical signal the gates would have fired, measure forward returns at 1, 3, 6, and 12 months. Questions to answer: What fraction recovered? What was the median time-to-recovery? What was the worst drawdown *after* entry (how much more pain before the bounce)? How does it compare to just buying SPY on the same dates?
2. **Compare against a dumb baseline.** If the system does not beat "buy SPY and hold" after costs, it has no edge. Be honest about this.
3. **Test the stabilization upgrade specifically.** Run the backtest with Gate 2b (stabilization) ON vs OFF. If 2b doesn't improve outcomes, the knife-catching critique was wrong for this universe — but it almost certainly will improve them.
4. **Walk-forward, don't curve-fit.** Don't tune thresholds on the same data you test on. Pick thresholds on older data, validate on newer.
5. **Paper-trade first.** Run the live alerts for a few weeks without committing money. See how many alerts come, how they feel, and whether the trap gate is catching the right things.

---

## Part 10: Future extensions (after the base works and backtests well)

- **Exit signal.** The system finds entries; add a "sell" alert when a flagged stock recovers (back above 200-day MA, or RSI > 60, or a target % gain). A buy system without an exit rule is half a system.
- **Position-sizing hint.** Suggest size inversely to volatility so each position risks a similar amount.
- **Better data.** If yfinance fundamentals prove unreliable, move to a paid provider (e.g. Financial Modeling Prep) for cleaner fundamentals and estimate revisions, which would materially strengthen Gate 3.
- **Sector caps.** Avoid five correlated semiconductor alerts at once; cap concurrent candidates per sector.

---

## Disclaimer (put this in the README)

This is a tool for organizing research and thinking, not investment advice. Every signal is a starting point for your own due diligence, never a buy instruction; the bot never tells you to buy. Active single-stock trading underperforms passive broad-index holding for most people, so keep this sleeve a small, defined slice of the portfolio — money you can afford to be wrong with — and not at the expense of the ETF core. Consult a licensed professional before significant moves.

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
        +--> universe.py   : S&P 500 ticker list
        +--> data.py       : price history + fundamentals via yfinance
        +--> regime.py     : SPY vs 200dma (RISK_ON / RISK_OFF)
        +--> indicators.py : RSI, ATR, MAs, drawdown, stabilization detection
        +--> gates.py      : four-gate pipeline
        +--> state.py      : dedup store (state.json)
        +--> telegram.py   : send alerts
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

"""Quality Dip Scanner — all thresholds in one place.

Tuning is the whole game. Every threshold lives here.
"""

# --- Gate 0: Market regime ---
SUPPRESS_IN_RISK_OFF = False  # If True, skip all alerts when RISK_OFF

# --- Gate 1: Quality ---
MIN_ROE = 12.0              # Return on equity minimum (%)
MIN_OP_MARGIN = 0.0         # Operating margin minimum (fraction, 0 = just positive)
MAX_DEBT_EQUITY = 150.0     # Debt/equity maximum
MIN_MKT_CAP = 10_000_000_000  # Market cap minimum ($10B)

# --- Gate 2: Hard dip + stabilization ---
# No S&P 500 component legitimately gaps 50 %+ in a single session; anything
# beyond this is a data-feed artifact (split recorded twice, bad tick, etc.)
# and will produce nonsense drawdown / vol figures for every downstream gate.
MAX_SINGLE_DAY_MOVE_PCT = 50.0  # Reject price series containing a move this large
MIN_DRAWDOWN = 25.0         # Drawdown from 52-week high minimum (%)
RSI_OVERSOLD = 30.0         # RSI threshold for "oversold"
LOOKBACK = 5                # Days to look back for RSI turning up
K_ATR = 2.0                 # ATR multiplier: price must be > K ATRs below 50-day MA
VOL_DROP_THRESHOLD = 1.5    # drawdown / annualized_vol minimum

# --- Gate 2b: Stabilization (RISK_OFF raises the bar) ---
STABILIZATION_REQUIRED_RISK_OFF = 2  # Number of stabilization signals required in RISK_OFF (default 1 for RISK_ON)

# --- Gate 3: Trap detection ---
TRAP_BEHAVIOR = "warn"      # "warn" = alert with warning, "suppress" = skip
EARNINGS_BLACKOUT_DAYS = 5  # Flag if earnings within N days
SUPPRESS_ON_EARNINGS = False  # True = skip a stock whose earnings fall inside the blackout, instead of warning
FRESH_LOW_DAYS = 20         # N-day low window for fresh-low trap
STEEP_DOWNTREND_PCT = 3.0   # 50-day MA declining > X% from 10 days ago
GAP_DOWN_PCT = 5.0          # Gap-down > X% threshold
GAP_VOLUME_MULT = 3.0       # Volume > Xx average for gap-down trap

# --- Ranking & alert quality (src/score.py) ---
# The gates decide WHO qualifies; the score decides WHO IS BEST among them.
# Only the top-scored candidates are alerted, so a busy sell-off day surfaces
# the strongest setups instead of a flood of mediocre ones.
MAX_ALERTS_PER_DAY = 5      # Send only the N best-scored candidates per day
MAX_PER_SECTOR = 2          # Cap alerts per GICS sector (avoid 5 correlated chip stocks)
RS_LOOKBACK = 20            # Sessions for relative strength vs SPY
VOLUME_CONFIRM_DAYS = 2     # Recent sessions checked for volume confirmation
VOLUME_CONFIRM_MULT = 1.2   # Recent volume must exceed X * average volume
VOLUME_CONFIRM_AVG_WINDOW = 20  # Sessions in the average-volume baseline
FRESH_BOUNCE_MAX_DAYS = 7   # Bounce off the low within N sessions scores as "fresh"

# --- SEC EDGAR (src/sec.py) ---
# Everything here needs SEC_USER_AGENT in the environment ("Your Name
# you@example.com"): SEC asks automated tools to identify themselves. Without
# it no SEC request is made and the scanner runs on Yahoo alone, as before.
QUALITY_SOURCE = "sec"      # Gate 1 inputs: "sec" = computed from the filings, the same way
                            # the backtest computes them (Yahoo fills any field SEC lacks);
                            # "yahoo" = Yahoo only, as before SEC support
SEC_8K_HARD_ITEMS = ("1.03", "2.04", "3.01", "4.02")  # Reject: bankruptcy, debt acceleration,
                                                      # delisting notice, past financials unreliable
SEC_8K_SOFT_ITEMS = ("2.06", "5.02")  # Warn + score penalty: impairment, director/officer change
SEC_8K_HARD_LOOKBACK_DAYS = 90  # A hard item filed within N days rejects the stock
SEC_8K_SOFT_LOOKBACK_DAYS = 30  # A soft item filed within N days warns
SEC_MAX_STALENESS_DAYS = 200    # Latest filed quarter older than this = SEC figures treated as missing
EXCLUDED_SIC_RANGES = ((6020, 6036),)  # SEC industry codes Gate 1 rejects: commercial banks and
                                        # savings institutions (debt/equity says nothing about a
                                        # bank's risk). () screens banks like everything else.
SEC_DEBT_INCLUDES_LEASES = True # Count lease liabilities as debt, as Yahoo's figures (and so the
                                # MAX_DEBT_EQUITY limit of 150) always did

# --- State / dedup ---
DEDUP_DAYS = 10             # Don't re-alert same ticker within N days

# --- Data fetching ---
BATCH_SLEEP = 0.5           # Seconds between fundamental fetches
PRICE_HISTORY_PERIOD = "1y" # yfinance download period

# --- Historical backtest (src/backtest.py) ---
# Replays the price-based gates (2a dip, 2b stabilization, price traps) over
# history and measures forward returns vs SPY. yfinance has no point-in-time
# fundamentals, so Gate 1 (quality) and the 8-K trap are replayed only when
# SEC access is configured: they are then rebuilt from what each company had
# filed before every signal date.
BACKTEST_PERIOD = "5y"          # yfinance history period to replay
BACKTEST_STEP_DAYS = 5          # Evaluate gates every N trading days
BACKTEST_MIN_HISTORY = 252      # Sessions of history required before first signal
BACKTEST_HORIZONS = (21, 63, 126, 252)  # Forward-return horizons (trading days)
BACKTEST_MAE_WINDOW = 63        # Window for max-adverse-excursion after entry

# --- Monthly paper-trading simulation (src/simulate.py) ---
# A one-month, fake-money test of the strategy. Buys are the SAME strict
# four-gate signals the scanner alerts on; sells are the mean-reversion exit.
SIM_STATE_PATH = "simulation.json"   # Persisted portfolio + history
SIM_DURATION_DAYS = 30               # Run length: a FULL month from day 1, not
                                     # "whatever is left of the calendar month".
                                     # The June 2026 run started on the 8th and
                                     # had only 22 days to work with; a dip
                                     # strategy needs the whole month to let the
                                     # mean reversion play out.
SIM_CASH_PER_STOCK = 1000.0          # Notional $ allocated per position
SIM_MAX_POSITIONS = 10               # Max concurrent open positions
SIM_UPDATE_INTERVAL_DAYS = 3         # Send a status update every N days
SIM_TAKE_PROFIT_PCT = 12.0           # SELL: recovered >= X% from entry (target hit)
SIM_STOP_LOSS_PCT = 12.0             # SELL: fell >= X% from entry (thesis failed)
SIM_RSI_EXIT = 60.0                  # SELL: RSI recovered above this (bounce done)
SIM_MIN_HOLD_SESSIONS = 10           # ...but not before N sessions have passed.
                                     # RSI recovers a day or two after a bounce,
                                     # while the mean reversion itself takes
                                     # weeks: over the 38 signals deep enough in
                                     # the cache to measure (June 2026 — a signal
                                     # needs 21 sessions after it to have a
                                     # 21-session return, so the latest month
                                     # cannot be measured at all) a signal is
                                     # worth ~0% after one session and ~+5.6%
                                     # after 21, so without this floor the
                                     # bounce-done rule sold the book for one or
                                     # two percent almost immediately. Across
                                     # eight rolling 30-day windows this lifts
                                     # the mean from +4.53% to +5.55% on capital
                                     # and beats SPY in 7 of 8 instead of 6,
                                     # while leaving the worst window unchanged
                                     # (-0.10%).
                                     #
                                     # The measured curve does not have an
                                     # interior peak: it rises all the way out
                                     # (0 -> +4.53, 5 -> +5.32, 10 -> +5.55,
                                     # 15 -> +5.84) and saturates at 20, where it
                                     # equals deleting the bounce-done rule
                                     # outright (+6.10%). That saturation is the
                                     # window running out, not the strategy
                                     # topping out — a 30-day run holds ~21
                                     # sessions, so a floor near 20 simply never
                                     # fires and stops being measured. Anything
                                     # past ~15 is therefore untestable on this
                                     # sample rather than better on it.
                                     #
                                     # So the data says "this exit costs money
                                     # over one month", not "10 is optimal". 10
                                     # is a judgment: high enough to capture most
                                     # of the measured gain, low enough that the
                                     # rule still fires inside a run and stays
                                     # under measurement, and it keeps a way out
                                     # of a completed bounce for the longer
                                     # holding periods this cache cannot see.
                                     # See src/tune.py, src/validate.py, README.
SIM_THESIS_BREAK_MIN_LOSS_PCT = 5.0  # Thesis breaking only triggers if also down >= X% from entry

# Volatility-scaled stop and risk-based sizing. Both are OFF: they are options
# to measure with `python -m src.tune stops`, not settings the data has picked.
# The defaults reproduce the current book exactly (a 12% risk budget on a
# 12% stop sizes every position at the full SIM_CASH_PER_STOCK).
SIM_STOP_MODE = "pct"                # "pct" = SIM_STOP_LOSS_PCT below entry; "atr" = a stop
                                     # SIM_STOP_ATR_MULT x ATR(14) below entry, fixed at entry
SIM_STOP_ATR_MULT = 3.0
SIM_SIZING = "fixed"                 # "fixed" = SIM_CASH_PER_STOCK per position; "risk" = size so the
                                     # loss at the stop is SIM_RISK_PER_TRADE_PCT of the slot, never
                                     # more than the slot's cash
SIM_RISK_PER_TRADE_PCT = 12.0        # Loss at the stop, as % of one slot's cash

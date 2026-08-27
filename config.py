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

# --- State / dedup ---
DEDUP_DAYS = 10             # Don't re-alert same ticker within N days

# --- Data fetching ---
BATCH_SLEEP = 0.5           # Seconds between fundamental fetches
PRICE_HISTORY_PERIOD = "1y" # yfinance download period

# --- Historical backtest (src/backtest.py) ---
# Replays the price-based gates (2a dip, 2b stabilization, price traps) over
# history and measures forward returns vs SPY. Quality (Gate 1) cannot be
# replayed — yfinance has no point-in-time fundamentals — so backtest results
# are a lower bound on selectivity, not an exact replay.
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
SIM_THESIS_BREAK_MIN_LOSS_PCT = 5.0  # Thesis breaking only triggers if also down >= X% from entry

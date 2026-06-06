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

# --- State / dedup ---
DEDUP_DAYS = 10             # Don't re-alert same ticker within N days

# --- Data fetching ---
BATCH_SLEEP = 0.5           # Seconds between fundamental fetches
PRICE_HISTORY_PERIOD = "1y" # yfinance download period

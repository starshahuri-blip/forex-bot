"""
ALL SETTINGS LIVE HERE. Change a number, save, restart the bot. Nothing else to edit.
"""

# ---------- What to trade ----------
PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD"]   # major pairs, tightest spreads
GRANULARITY = "M5"            # "M5" = 5-minute bars, "M10" = 10-minute bars

# ---------- Strategy: trend breakout ----------
EMA_FAST = 50                 # short trend line
EMA_SLOW = 200                # long trend line (only trade in its direction)
BREAKOUT_BARS = 20            # enter when price closes beyond the last 20 bars
ADX_PERIOD = 14
ADX_MIN = 20.0                # only trade when the market is actually trending
ATR_PERIOD = 14
STOP_ATR = 1.5                # stop-loss distance = 1.5 x recent volatility
TARGET_R = 2.0                # take-profit = 2x the amount risked
BREAKEVEN_AT_R = 1.0          # once up 1x risk, move stop to entry (trade can't lose)
MAX_BAR_ATR = 2.5             # skip freak candles (news spikes)
MIN_STOP_PIPS = 5.0           # never use a stop tighter than this
MAX_SPREAD_FRAC = 0.12        # skip if the spread costs more than 12% of the stop

# ---------- Trading hours (UTC) ----------
SESSION_START_UTC = 7         # London open
SESSION_END_UTC = 19          # no new trades after this hour
FLATTEN_UTC = 20              # close everything; never hold overnight
FRIDAY_LAST_ENTRY_UTC = 16    # stop opening trades early on Friday

# ---------- Guardrails (aggressive, but hard limits) ----------
RISK_PER_TRADE = 0.02         # 2% of the account risked per trade
MAX_OPEN_TRADES = 3           # at most 3 trades open at once (max 6% at risk)
MAX_SAME_USD_DIRECTION = 2    # don't stack 3 bets that are all "dollar up" or "dollar down"
MAX_LEVERAGE_PER_TRADE = 10   # cap on position size per trade
MAX_MARGIN_USED = 0.70        # never use more than 70% of the account as margin
MAX_TRADES_PER_DAY = 10
MAX_CONSEC_LOSSES = 4         # 4 losses in a row -> done for the day
DAILY_LOSS_LIMIT = 0.06       # down 6% today -> close everything, stop until tomorrow
MAX_DRAWDOWN = 0.15           # down 15% from the account's high -> close everything, stop for good

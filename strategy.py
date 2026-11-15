"""Indicators and the entry signal. Shared by the live bot and the backtest."""
from datetime import datetime, timezone

GRAN_SECONDS = {"M5": 300, "M10": 600, "M15": 900}


def parse_time(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def ema(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def atr_adx(h, l, c, n):
    """Wilder's ATR and ADX."""
    size = len(c)
    atr = [None] * size
    adx = [None] * size
    if size < 2 * n + 1:
        return atr, adx
    tr = [0.0] * size
    pdm = [0.0] * size
    ndm = [0.0] * size
    for i in range(1, size):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
    a = sum(tr[1:n + 1]) / n
    p = sum(pdm[1:n + 1]) / n
    m = sum(ndm[1:n + 1]) / n
    dxs = []
    adx_val = None
    for i in range(n, size):
        if i > n:
            a = (a * (n - 1) + tr[i]) / n
            p = (p * (n - 1) + pdm[i]) / n
            m = (m * (n - 1) + ndm[i]) / n
        atr[i] = a
        pdi = 100 * p / a if a > 0 else 0.0
        ndi = 100 * m / a if a > 0 else 0.0
        dx = 100 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 0 else 0.0
        if adx_val is None:
            dxs.append(dx)
            if len(dxs) == n:
                adx_val = sum(dxs) / n
                adx[i] = adx_val
        else:
            adx_val = (adx_val * (n - 1) + dx) / n
            adx[i] = adx_val
    return atr, adx


class Indicators:
    def __init__(self, h, l, c, cfg):
        self.h, self.l, self.c = h, l, c
        self.ema_f = ema(c, cfg.EMA_FAST)
        self.ema_s = ema(c, cfg.EMA_SLOW)
        self.atr, self.adx = atr_adx(h, l, c, cfg.ATR_PERIOD)


def signal(ind, i, cfg):
    """Returns (side, atr) for bar i, where side is +1 buy / -1 sell, or None."""
    n = cfg.BREAKOUT_BARS
    if i < n + 1:
        return None
    ef, es, atr, adx = ind.ema_f[i], ind.ema_s[i], ind.atr[i], ind.adx[i]
    if ef is None or es is None or atr is None or adx is None or atr <= 0:
        return None
    if adx < cfg.ADX_MIN:
        return None
    if ind.h[i] - ind.l[i] > cfg.MAX_BAR_ATR * atr:
        return None
    c = ind.c[i]
    if c > max(ind.h[i - n:i]) and ef > es and c > ef:
        return 1, atr
    if c < min(ind.l[i - n:i]) and ef < es and c < ef:
        return -1, atr
    return None


def in_session(dt, cfg):
    """True when new trades are allowed."""
    wd = dt.weekday()
    if wd > 4:
        return False
    if not (cfg.SESSION_START_UTC <= dt.hour < cfg.SESSION_END_UTC):
        return False
    if wd == 4 and dt.hour >= cfg.FRIDAY_LAST_ENTRY_UTC:
        return False
    return True


def must_flatten(dt, cfg):
    """True when nothing should be open (evening, overnight, weekend)."""
    return dt.weekday() > 4 or dt.hour >= cfg.FLATTEN_UTC or dt.hour < cfg.SESSION_START_UTC


def usd_direction(pair, side):
    """+1 if the trade profits when the dollar rises, -1 if it profits when it falls."""
    base, quote = pair.split("_")
    if base == "USD":
        return side
    if quote == "USD":
        return -side
    return 0

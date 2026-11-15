"""
Searches for settings that actually made money on real OANDA prices, and checks them honestly.

How it stays honest: it tunes on the OLDER two-thirds of the data ("training"), then tests the
winners on the NEWEST third, which they have never seen ("exam"). A setting only counts if it
made money on both.

Run:   python3 research.py          (last 730 days; takes 5-10 minutes)
"""
import os
import pickle
import sys
import time
from array import array
from datetime import datetime, timedelta, timezone

import config as cfg
from oanda import Oanda, load_env
from strategy import atr_adx, ema, parse_time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
FIELDS = ["bo", "bh", "bl", "bc", "ao", "ah", "al", "ac"]

# ---- what gets tried (round 2) ----
# Round 1 showed trend-breakouts on 5-15 minute bars lose to the spread, so they are dropped.
# Round 2 tests two ideas with a known rationale, on more pairs and two years of prices:
#   REVERT  = buy dips / sell spikes back toward the average (in the busy day and the quiet Asian night)
#   RANGE   = trade the break of the overnight (00:00-07:00 UTC) range once London opens, one trade a day
PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD",
         "EUR_GBP", "EUR_JPY", "AUD_NZD"]
GRANS = {"M5": 1, "M15": 3}
WINDOWS = {"DAY": (7, 19), "ASIA": (23, 6), "SWING": None}   # UTC hours when new trades are allowed
# SWING = any weekday hour except the 21:00-23:00 UTC rollover; holds overnight, closes Friday 20:00 UTC
MR_SD = [2.0, 2.5, 3.0]
MR_STOP = [2.0, 3.0, 4.0]
MR_ADX = [25, 100]
ORB_END = [10, 13]                              # stop looking for the break at this hour
ORB_STOP = ["mid", "opp"]                       # stop at the middle / far side of the range
ORB_TARGET = [1.0, 2.0, 99.0]                   # 99 = no target, hold until the evening close


def in_win(hr, s, e):
    return s <= hr < e if s < e else (hr >= s or hr < e)


def fetch(api, pair, days, gran="M5", step=300):
    """Bid/ask candles, cached on disk for a day so re-runs are quick."""
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, "%s_%s_%d.pkl" % (pair, gran, days))
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
        with open(path, "rb") as f:
            return pickle.load(f)
    d = {"t": array("q")}
    for k in FIELDS:
        d[k] = array("d")
    start = datetime.now(timezone.utc) - timedelta(days=days)
    while True:
        cs = api.candles(pair, gran, count=5000, frm=start.strftime("%Y-%m-%dT%H:%M:%SZ"), price="BA")
        last = None
        for c in cs:
            if not c.get("complete"):
                continue
            last = parse_time(c["time"])
            d["t"].append(int(last.timestamp()))
            b, a = c["bid"], c["ask"]
            for k, src, f in (("bo", b, "o"), ("bh", b, "h"), ("bl", b, "l"), ("bc", b, "c"),
                              ("ao", a, "o"), ("ah", a, "h"), ("al", a, "l"), ("ac", a, "c")):
                d[k].append(float(src[f]))
        if last is None or len(cs) < 5000:
            break
        start = last + timedelta(seconds=step)
    with open(path, "wb") as f:
        pickle.dump(d, f)
    return d


def aggregate(d, k, base=300):
    """Build bigger bars out of smaller ones (e.g. 15-minute from 5-minute, 4-hour from 1-hour)."""
    if k == 1:
        return d
    out = {"t": array("q")}
    for f in FIELDS:
        out[f] = array("d")
    width = base * k
    cur = None
    for i in range(len(d["t"])):
        g = d["t"][i] // width
        if g != cur:
            cur = g
            out["t"].append(g * width)
            for f in FIELDS:
                out[f].append(d[f][i])
        else:
            for f in ("bh", "ah"):
                if d[f][i] > out[f][-1]:
                    out[f][-1] = d[f][i]
            for f in ("bl", "al"):
                if d[f][i] < out[f][-1]:
                    out[f][-1] = d[f][i]
            out["bc"][-1] = d["bc"][i]
            out["ac"][-1] = d["ac"][i]
    return out


class Bars:
    """Prices plus every indicator, worked out once per pair and bar size."""

    def __init__(self, d):
        self.d = d
        n = self.n = len(d["t"])
        self.h = [(d["bh"][i] + d["ah"][i]) / 2 for i in range(n)]
        self.l = [(d["bl"][i] + d["al"][i]) / 2 for i in range(n)]
        self.c = [(d["bc"][i] + d["ac"][i]) / 2 for i in range(n)]
        self.atr, self.adx = atr_adx(self.h, self.l, self.c, cfg.ATR_PERIOD)
        self.ef = ema(self.c, cfg.EMA_FAST)
        self.es = ema(self.c, cfg.EMA_SLOW)
        # trading-hours flags for each window, from the clock time of each bar
        self.sess = {w: [False] * n for w in WINDOWS}
        self.flat = {w: [False] * n for w in WINDOWS}
        for i in range(n):
            t = d["t"][i]
            wd = (t // 86400 + 3) % 7
            hr = (t % 86400) // 3600
            for w, hours in WINDOWS.items():
                if hours is None:
                    self.flat[w][i] = wd > 4 or (wd == 4 and hr >= 20)
                    self.sess[w][i] = wd <= 4 and hr not in (21, 22) and not (wd == 4 and hr >= cfg.FRIDAY_LAST_ENTRY_UTC)
                    continue
                ws, we = hours
                self.flat[w][i] = not in_win(hr, ws, (we + 1) % 24)
                ok = in_win(hr, ws, we) and not (wd == 4 and hr >= cfg.FRIDAY_LAST_ENTRY_UTC)
                if ws < we and wd > 4:
                    ok = False
                self.sess[w][i] = ok
        # rolling average and spread of the last 20 closes (for the mean-reversion idea)
        self.sma = [None] * n
        self.sd = [None] * n
        s = s2 = 0.0
        for i in range(n):
            s += self.c[i]
            s2 += self.c[i] ** 2
            if i >= 20:
                s -= self.c[i - 20]
                s2 -= self.c[i - 20] ** 2
            if i >= 19:
                m = s / 20
                self.sma[i] = m
                self.sd[i] = max(s2 / 20 - m * m, 0.0) ** 0.5

    def breakout_cands(self, n_bars):
        out = []
        h, l, c, atr, adx, ef, es = self.h, self.l, self.c, self.atr, self.adx, self.ef, self.es
        for i in range(max(cfg.EMA_SLOW + 50, n_bars + 1), self.n - 1):
            a = atr[i]
            if a is None or adx[i] is None or a <= 0 or h[i] - l[i] > cfg.MAX_BAR_ATR * a:
                continue
            if ef[i] > es[i] and c[i] > ef[i] and c[i] > max(h[i - n_bars:i]):
                out.append((i, 1, adx[i], 0.0, None))
            elif ef[i] < es[i] and c[i] < ef[i] and c[i] < min(l[i - n_bars:i]):
                out.append((i, -1, adx[i], 0.0, None))
        return out

    def meanrev_cands(self, k_sd):
        out = []
        for i in range(cfg.EMA_SLOW + 50, self.n - 1):
            a, m, sd = self.atr[i], self.sma[i], self.sd[i]
            if a is None or self.adx[i] is None or a <= 0 or sd is None:
                continue
            if self.c[i] < m - k_sd * sd:
                out.append((i, 1, self.adx[i], m, None))   # stretched down: buy, aim for the average
            elif self.c[i] > m + k_sd * sd:
                out.append((i, -1, self.adx[i], m, None))
        return out


def range_cands(b, k, entry_end):
    """First close outside the overnight range after London opens; at most one per day."""
    out = {"mid": [], "opp": []}
    day = None
    hi = lo = None
    nb = 0
    done = False
    t = b.d["t"]
    for i in range(b.n - 1):
        dday = t[i] // 86400
        hr = (t[i] % 86400) // 3600
        if dday != day:
            day, hi, lo, nb, done = dday, None, None, 0, False
        if (dday + 3) % 7 > 4:
            continue
        if hr < 7:
            hi = b.h[i] if hi is None else max(hi, b.h[i])
            lo = b.l[i] if lo is None else min(lo, b.l[i])
            nb += 1
        elif hr < entry_end and not done and hi is not None and nb * 5 * k >= 180 and b.atr[i] is not None \
                and b.adx[i] is not None and i >= cfg.EMA_SLOW + 50:
            if b.c[i] > hi:
                side = 1
            elif b.c[i] < lo:
                side = -1
            else:
                continue
            done = True
            out["opp"].append((i, side, b.adx[i], 0.0, lo if side > 0 else hi))
            out["mid"].append((i, side, b.adx[i], 0.0, (hi + lo) / 2))
    return out


def simulate(b, cands, pip, win, stop_atr, target_r, be, adx_lo, adx_hi, trail=0.0, fin_pips=0.0):
    """Walk each signal forward to its exit. Buys fill at the ask and close at the bid (and the
    reverse for sells), so the spread is paid on every trade. Returns [(exit_time, R)]."""
    d = b.d
    t, bo, bh, bl, ao, ah, al = d["t"], d["bo"], d["bh"], d["bl"], d["ao"], d["ah"], d["al"]
    atr, sess, flat, n = b.atr, b.sess[win], b.flat[win], b.n
    res = []
    free_from = 0
    min_stop = cfg.MIN_STOP_PIPS * pip
    for i, side, adx, mean, sl_ref in cands:
        if i < free_from or adx < adx_lo or adx > adx_hi:
            continue
        j = i + 1
        if not sess[j]:
            continue
        entry = ao[j] if side > 0 else bo[j]
        if sl_ref is None:
            dist = max(stop_atr * atr[i], min_stop)
        else:
            dist = (entry - sl_ref) * side
            if dist < min_stop:
                continue
        if ao[j] - bo[j] > cfg.MAX_SPREAD_FRAC * dist:
            continue
        sl = entry - side * dist
        if target_r:
            tp = entry + side * target_r * dist
        else:
            tp = mean
            if (tp - entry) * side < 0.5 * dist:
                continue                                  # not enough room to the average
        k = j
        while k < n:
            if side > 0:
                o, hi, lo = bo[k], bh[k], bl[k]
            else:
                o, hi, lo = ao[k], ah[k], al[k]
            px = None
            if k > j and flat[k]:
                px = o
            elif (o - sl) * side <= 0:
                px = o
            elif (side > 0 and lo <= sl) or (side < 0 and hi >= sl):
                px = sl
            elif (side > 0 and hi >= tp) or (side < 0 and lo <= tp):
                px = tp
            if px is not None:
                r = side * (px - entry) / dist
                if fin_pips:
                    r -= fin_pips * pip * ((t[k] - t[j]) / 86400.0) / dist   # cost of holding overnight
                res.append((t[k], r))
                free_from = k
                break
            if be and (sl - entry) * side < 0:
                best = hi if side > 0 else lo
                if (best - entry) * side >= be * dist:
                    sl = entry
            if trail:
                new = (hi if side > 0 else lo) - side * trail * atr[i]
                if (new - sl) * side > 0:
                    sl = new                              # ratchet the stop behind the best price
            k += 1
    return res


class Acc:
    """Running totals for one setting: [training, exam]."""

    def __init__(self):
        self.n = [0, 0]
        self.w = [0, 0]
        self.gw = [0.0, 0.0]
        self.gl = [0.0, 0.0]
        self.days = [{}, {}]
        self.pairs = {}

    def add(self, pair, res, split_t):
        tot = 0.0
        for t, r in res:
            s = 1 if t >= split_t else 0
            self.n[s] += 1
            if r > 0:
                self.w[s] += 1
                self.gw[s] += r
            else:
                self.gl[s] -= r
            day = t // 86400
            self.days[s][day] = self.days[s].get(day, 0.0) + r
            tot += r
        self.pairs[pair] = (len(res), tot)

    def total(self, s):
        return self.gw[s] - self.gl[s]

    def pf(self, s):
        return self.gw[s] / self.gl[s] if self.gl[s] > 0 else (9.99 if self.gw[s] > 0 else 0.0)

    def dd(self, s):
        eq = peak = dd = 0.0
        for day in sorted(self.days[s]):
            eq += self.days[s][day]
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        return dd


def run(datasets, pips, split_t, progress=print):
    accs = {}
    for pair, m5 in datasets():
        t0 = time.time()
        for gname, k in GRANS.items():
            b = Bars(aggregate(m5, k))
            for sdv in MR_SD:
                cands = b.meanrev_cands(sdv)
                for win in ("DAY", "ASIA"):
                    for st in MR_STOP:
                        for ax in MR_ADX:
                            key = "REVERT %-4s %-3s bands=%.1f stop=%.1f adx<=%d" % (win, gname, sdv, st, ax)
                            res = simulate(b, cands, pips[pair], win, st, 0, 0, -1, ax)
                            accs.setdefault(key, Acc()).add(pair, res, split_t)
            for end in ORB_END:
                rc = range_cands(b, k, end)
                for sm in ORB_STOP:
                    for tg in ORB_TARGET:
                        key = "RANGE  %-3s until=%02d:00 stop=%s target=%s" % (
                            gname, end, sm, "hold" if tg > 50 else "%.1f" % tg)
                        res = simulate(b, rc[sm], pips[pair], "DAY", 0, tg, 0, -1, 1e9)
                        accs.setdefault(key, Acc()).add(pair, res, split_t)
        progress("  %s done (%d five-minute bars, %.0fs)" % (pair, len(m5["t"]), time.time() - t0))
    return accs


def passes(a):
    return a.n[1] >= 40 and a.pf(1) >= 1.15 and a.total(1) > 0


def report(accs, min_train=200):
    ok = [(k, a) for k, a in accs.items() if a.n[0] >= min_train]
    made = [x for x in ok if x[1].total(0) > 0]
    print("\nSettings tried: %d. With enough trades to judge: %d. Profitable in training: %d (%.0f%%)."
          % (len(accs), len(ok), len(made), 100.0 * len(made) / max(1, len(ok))))
    good = [x for x in ok if x[1].pf(0) >= 1.1]
    good.sort(key=lambda x: x[1].total(0) / max(x[1].dd(0), 5.0), reverse=True)
    top = good[:10]
    if not top:
        print("\nVERDICT: NOTHING made money even in training. No setting here is worth trading.")
        return None
    print("\nTop %d from training, then how each did on the exam (prices it had never seen):\n" % len(top))
    print("   %-52s | TRAINING: trades  PF  total  drop | EXAM: trades  PF  total  drop" % "setting")
    winner = None
    for r, (k, a) in enumerate(top, 1):
        mark = "PASS" if passes(a) else "fail"
        if mark == "PASS" and winner is None:
            winner = (k, a)
        print("%2d %-52s | %14d %5.2f %+6.1fR %4.1fR | %10d %5.2f %+6.1fR %4.1fR  %s"
              % (r, k, a.n[0], a.pf(0), a.total(0), a.dd(0), a.n[1], a.pf(1), a.total(1), a.dd(1), mark))
    n_pass = sum(1 for _, a in top if passes(a))
    print("\n%d of the top %d passed the exam." % (n_pass, len(top)))
    if winner is None:
        print("\nVERDICT: the best training settings FAILED on unseen prices. That is the signature of")
        print("luck, not a real edge. Do not go live.")
        return None
    k, a = winner
    print("\nBEST PASSING SETTING:\n  " + k)
    print("  by pair (whole period): " + ", ".join("%s %d trades %+.1fR" % (p, n, t)
                                                    for p, (n, t) in sorted(a.pairs.items())))
    if n_pass >= 5:
        print("\nVERDICT: PROMISING. Several of the top settings held up on unseen prices.")
        return k
    print("\nVERDICT: WEAK. Only %d of %d held up. Random prices produce results like this, so treat it" % (n_pass, len(top)))
    print("as luck. Do not go live.")
    return None


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 730
    env = load_env(os.path.join(HERE, ".env"))
    api = Oanda(env["OANDA_TOKEN"], env["OANDA_ACCOUNT"], env.get("OANDA_ENV", "practice"))
    specs = api.instruments(PAIRS)
    pairs = [p for p in PAIRS if p in specs]
    pips = {p: 10 ** specs[p]["pipLocation"] for p in pairs}
    split_t = int(time.time()) - (days // 3) * 86400
    print("Research round 2: %d pairs, last %d days. Training on the first %d, exam on the newest %d."
          % (len(pairs), days, days - days // 3, days // 3))
    print("Downloading prices and testing (about 5-10 minutes; it prints a line per pair)...")

    def datasets():
        from oanda import OandaError
        for p in pairs:
            try:
                data = fetch(api, p, days)
            except OandaError:
                api.summary()                  # if the token itself is dead, this raises and we stop
                print("  %s skipped (OANDA would not send its prices; the token is fine)" % p)
                continue
            yield p, data

    report(run(datasets, pips, split_t), min_train=300)



def friendly_main():
    from oanda import OandaError
    try:
        main()
    except OandaError as e:
        if e.status in (401, 403):
            print("\nOANDA stopped accepting the API token (it was probably replaced by a newer one).")
            print("The trading bot uses the same token, so it is blocked too. To fix, paste:")
            print("    cd ~/forex-bot && rm .env && bash install.sh")
            print("and give it your NEWEST token. Prices already downloaded are kept, so the re-run is quick.")
        else:
            print("\nOANDA had a temporary problem (%s). Wait a minute and run this again." % e.status)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    friendly_main()

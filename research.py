"""
Searches for settings that actually made money on real OANDA prices, and checks them honestly.

How it stays honest: it tunes on the OLDER two-thirds of the data ("training"), then tests the
winners on the NEWEST third, which they have never seen ("exam"). A setting only counts if it
made money on both.

Run:   python3 research.py          (last 365 days; takes a few minutes)
       python3 research.py 540      (longer history)
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

# ---- what gets tried ----
GRANS = {"M5": 1, "M10": 2, "M15": 3}
BRK_N = [20, 40]
BRK_STOP = [2.0, 3.0, 4.0]
BRK_TARGET = [1.0, 1.5, 2.0, 3.0]
BRK_ADX = [0, 20, 30]
BRK_BE = [0, 1]
MR_SD = [2.0, 2.5, 3.0]
MR_STOP = [2.0, 3.0, 4.0]
MR_ADX = [20, 25, 100]


def fetch(api, pair, days):
    """M5 bid/ask candles, cached on disk for a day so re-runs are quick."""
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, "%s_%d.pkl" % (pair, days))
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
        with open(path, "rb") as f:
            return pickle.load(f)
    d = {"t": array("q")}
    for k in FIELDS:
        d[k] = array("d")
    start = datetime.now(timezone.utc) - timedelta(days=days)
    while True:
        cs = api.candles(pair, "M5", count=5000, frm=start.strftime("%Y-%m-%dT%H:%M:%SZ"), price="BA")
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
        start = last + timedelta(seconds=300)
    with open(path, "wb") as f:
        pickle.dump(d, f)
    return d


def aggregate(d, k):
    """Build 10- or 15-minute bars out of 5-minute bars."""
    if k == 1:
        return d
    out = {"t": array("q")}
    for f in FIELDS:
        out[f] = array("d")
    width = 300 * k
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
        # trading-hours flags, from the clock time of each bar
        self.sess = [False] * n
        self.flat = [False] * n
        for i in range(n):
            t = d["t"][i]
            wd = (t // 86400 + 3) % 7
            hr = (t % 86400) // 3600
            self.flat[i] = wd > 4 or hr >= cfg.FLATTEN_UTC or hr < cfg.SESSION_START_UTC
            self.sess[i] = (wd <= 4 and cfg.SESSION_START_UTC <= hr < cfg.SESSION_END_UTC
                            and not (wd == 4 and hr >= cfg.FRIDAY_LAST_ENTRY_UTC))
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
                out.append((i, 1, adx[i], 0.0))
            elif ef[i] < es[i] and c[i] < ef[i] and c[i] < min(l[i - n_bars:i]):
                out.append((i, -1, adx[i], 0.0))
        return out

    def meanrev_cands(self, k_sd):
        out = []
        for i in range(cfg.EMA_SLOW + 50, self.n - 1):
            a, m, sd = self.atr[i], self.sma[i], self.sd[i]
            if a is None or self.adx[i] is None or a <= 0 or sd is None:
                continue
            if self.c[i] < m - k_sd * sd:
                out.append((i, 1, self.adx[i], m))        # stretched down: buy, aim for the average
            elif self.c[i] > m + k_sd * sd:
                out.append((i, -1, self.adx[i], m))
        return out


def simulate(b, cands, pip, stop_atr, target_r, be, adx_lo, adx_hi):
    """Walk each signal forward to its exit. Buys fill at the ask and close at the bid (and the
    reverse for sells), so the spread is paid on every trade. Returns [(exit_time, R)]."""
    d = b.d
    t, bo, bh, bl, ao, ah, al = d["t"], d["bo"], d["bh"], d["bl"], d["ao"], d["ah"], d["al"]
    atr, sess, flat, n = b.atr, b.sess, b.flat, b.n
    res = []
    free_from = 0
    min_stop = cfg.MIN_STOP_PIPS * pip
    for i, side, adx, mean in cands:
        if i < free_from or adx < adx_lo or adx > adx_hi:
            continue
        j = i + 1
        if not sess[j]:
            continue
        dist = max(stop_atr * atr[i], min_stop)
        if ao[j] - bo[j] > cfg.MAX_SPREAD_FRAC * dist:
            continue
        entry = ao[j] if side > 0 else bo[j]
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
                res.append((t[k], side * (px - entry) / dist))
                free_from = k
                break
            if be and (sl - entry) * side < 0:
                best = hi if side > 0 else lo
                if (best - entry) * side >= be * dist:
                    sl = entry
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
            for nb in BRK_N:
                cands = b.breakout_cands(nb)
                for st in BRK_STOP:
                    for tg in BRK_TARGET:
                        for ax in BRK_ADX:
                            for be in BRK_BE:
                                key = "BREAKOUT %-3s bars=%d stop=%.1f target=%.1f adx>=%d breakeven=%s" % (
                                    gname, nb, st, tg, ax, "on" if be else "off")
                                res = simulate(b, cands, pips[pair], st, tg, be, ax, 1e9)
                                accs.setdefault(key, Acc()).add(pair, res, split_t)
            for sdv in MR_SD:
                cands = b.meanrev_cands(sdv)
                for st in MR_STOP:
                    for ax in MR_ADX:
                        key = "REVERT   %-3s bands=%.1f stop=%.1f adx<=%d" % (gname, sdv, st, ax)
                        res = simulate(b, cands, pips[pair], st, 0, 0, -1, ax)
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
    print("   %-62s | TRAINING: trades  PF  total  drop | EXAM: trades  PF  total  drop" % "setting")
    winner = None
    for r, (k, a) in enumerate(top, 1):
        mark = "PASS" if passes(a) else "fail"
        if mark == "PASS" and winner is None:
            winner = (k, a)
        print("%2d %-62s | %14d %5.2f %+6.1fR %4.1fR | %10d %5.2f %+6.1fR %4.1fR  %s"
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
    else:
        print("\nVERDICT: WEAK. Only %d of %d held up, so this may still be luck. Practice-test only." % (n_pass, len(top)))
    return k


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    env = load_env(os.path.join(HERE, ".env"))
    api = Oanda(env["OANDA_TOKEN"], env["OANDA_ACCOUNT"], env.get("OANDA_ENV", "practice"))
    specs = api.instruments(cfg.PAIRS)
    pips = {p: 10 ** specs[p]["pipLocation"] for p in cfg.PAIRS}
    split_t = int(time.time()) - (days // 3) * 86400
    print("Research: last %d days. Training on the first %d, exam on the newest %d."
          % (days, days - days // 3, days // 3))
    print("Downloading prices and testing (a few minutes)...")

    def datasets():
        for p in cfg.PAIRS:
            yield p, fetch(api, p, days)

    report(run(datasets, pips, split_t))


if __name__ == "__main__":
    main()

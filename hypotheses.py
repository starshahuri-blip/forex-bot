"""
Research round 4: structurally different ideas, not indicator tuning.

PRE-REGISTERED. Every rule and number below was fixed before seeing any result, from the
published paper where one exists. Nothing is tuned. There are 15 tests, so a result only
counts with a t-statistic of 3 or more (the usual bar when many ideas are tried at once),
must be positive in the most recent 12 months, and must be positive in at least 70% of years.

Costs: real OANDA bid/ask on every entry and exit, plus 0.2 pip extra slippage per side.
Trades held overnight pay a financing charge.

Run it so it keeps going if your phone locks:
    nohup python3 hypotheses.py > hypotheses.txt 2>&1 &
Read the results (when it says FINISHED):
    cat hypotheses.txt
"""
import calendar
import math
import os
import pickle
import sys
import time
from datetime import datetime, timezone

from oanda import Oanda, OandaError, load_env
from research import Bars, aggregate, fetch, simulate

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DAY = 86400
SLIP_PIPS = 0.2
T_HURDLE = 3.0
YEARS_M5 = 1825                               # 5 years of 5-minute prices (Nov 2021 onward)
USD_PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD",
             "USD_CHF", "NZD_USD", "USD_SEK", "USD_NOK"]          # the paper's top-9 currencies vs USD
SWING_PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD",
               "EUR_GBP", "EUR_JPY", "AUD_NZD"]

# H1. Fix reversals (Krohn, Mueller & Whelan 2024, J. Finance): USD rises into the fixes, falls after.
#     (name, fix, start hours, end hours, +1 = buy USD / -1 = sell USD)
FIX_TESTS = [
    ("FIX-1 London fix: buy USD 2h before", "LON", -2, 0, 1),
    ("FIX-2 London fix: sell USD 1h after", "LON", 0, 1, -1),
    ("FIX-3 London fix: sell USD 2h after", "LON", 0, 2, -1),
    ("FIX-4 Tokyo fix: buy USD 2h before", "TKY", -2, 0, 1),
    ("FIX-5 Tokyo fix: sell USD 1h after", "TKY", 0, 1, -1),
    ("FIX-6 Tokyo fix: sell USD 2h after", "TKY", 0, 2, -1),
]
# H4. Announcement continuation. No free historical calendar exists, so this uses the US release
#     slots themselves: if the 5-minute bar at the slot moves more than 3x the average 5-minute range
#     of the previous 4 hours, follow that direction.   (name, NY hour, NY minute, hold hours)
NEWS_TESTS = [
    ("NEWS-1 08:30 New York shock: follow 1h", 8, 30, 1),
    ("NEWS-2 08:30 New York shock: follow 2h", 8, 30, 2),
    ("NEWS-3 14:00 New York shock: follow 1h", 14, 0, 1),
    ("NEWS-4 14:00 New York shock: follow 2h", 14, 0, 2),
]
NEWS_MULT = 3.0
# H2. Cross-sectional momentum (Menkhoff, Sarno, Schmeling & Schrimpf, BIS WP 366): each month buy
#     the 3 strongest G10 currencies vs USD and short the 3 weakest. Data from 2012, after publication.
MOM_FORMATION = [1, 3, 12]
MOM_FEE_PER_LEG_YEAR = 0.015                  # financing mark-up charged on both legs
# H3. Volatility compression: a trend-aligned 20-bar breakout, only when the previous bar's
#     Bollinger width was in its lowest 10% of the last 500 bars. Exit: 2x ATR trailing stop.
VOL_TESTS = [("VOL-1 compression breakout, 1-hour bars", 1), ("VOL-2 compression breakout, 4-hour bars", 4)]
FIN_PIPS_PER_DAY = 0.5
# H5. Cross-market: needs non-FX prices, so first check what this OANDA account can trade.
CROSS_ASSETS = ["XAU_USD", "XAG_USD", "SPX500_USD", "NAS100_USD", "BCO_USD", "WTICO_USD", "USB10Y_USD"]


# ---------------- clocks (daylight saving worked out by rule, no extra packages) ----------------
def sunday(y, m, n):
    """Midnight UTC of the n-th Sunday of a month (n = -1 for the last one)."""
    if n > 0:
        d = 1 + (6 - datetime(y, m, 1).weekday()) % 7 + 7 * (n - 1)
    else:
        last = calendar.monthrange(y, m)[1]
        d = last - (datetime(y, m, last).weekday() - 6) % 7
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def london_off(ts):
    y = datetime.fromtimestamp(ts, timezone.utc).year
    return 1 if sunday(y, 3, -1) + 3600 <= ts < sunday(y, 10, -1) + 3600 else 0


def ny_off(ts):
    y = datetime.fromtimestamp(ts, timezone.utc).year
    return -4 if sunday(y, 3, 2) + 7 * 3600 <= ts < sunday(y, 11, 1) + 6 * 3600 else -5


def local_to_utc(day, hh, mm, off_fn):
    return day + (hh - off_fn(day + 12 * 3600)) * 3600 + mm * 60


def weekday(ts):
    return (ts // DAY + 3) % 7


def foreign_side(pair):
    """Pair direction that profits when the other currency rises against USD."""
    return 1 if pair.endswith("_USD") else -1


# ---------------- trade maths ----------------
def ret_bps(side, b0, a0, b1, a1, slip):
    """Net (bid/ask + slippage) and gross (mid) return of one trade, in basis points."""
    if side > 0:
        net = math.log((b1 - slip) / (a0 + slip))
    else:
        net = math.log((b0 - slip) / (a1 + slip))
    gross = side * math.log((b1 + a1) / (b0 + a0))
    return net * 1e4, gross * 1e4


def stats(points, per_year, now):
    """points = [(timestamp, value)]. Returns a dict, or None if too few points."""
    if len(points) < 20:
        return None
    vals = [v for _, v in points]
    n = len(vals)
    mean = sum(vals) / n
    sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5
    by_year = {}
    for t, v in points:
        y = datetime.fromtimestamp(t, timezone.utc).year
        by_year[y] = by_year.get(y, 0.0) + v
    eq = peak = dd = 0.0
    for v in vals:
        eq += v
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": n, "mean": mean, "t": mean / (sd / n ** 0.5) if sd > 0 else 0.0,
            "sharpe": mean / sd * per_year ** 0.5 if sd > 0 else 0.0, "ann": mean * per_year,
            "years_pos": sum(1 for v in by_year.values() if v > 0), "years": len(by_year),
            "last12": sum(v for t, v in points if t >= now - 365 * DAY), "dd": dd}


def survived(s):
    return (s is not None and s["t"] >= T_HURDLE and s["last12"] > 0
            and s["years_pos"] >= 0.7 * s["years"])


def verdict(net, gross):
    if survived(net):
        return "SURVIVED. Worth a forward practice test."
    if net and net["t"] >= 2.0 and net["mean"] > 0:
        return "HINT ONLY: positive but below the bar. Not tradable yet."
    if gross and gross["t"] >= T_HURDLE and gross["mean"] > 0:
        return "REAL EFFECT, BUT OANDA COSTS EAT IT."
    return "NOT FOUND."


def show(name, unit, net, gross=None):
    print("\n" + name)
    if net is None:
        print("   too few trades to judge")
        return
    if gross is not None:
        print("   before costs: %+.2f %s, t=%.1f" % (gross["mean"], unit, gross["t"]))
    print("   after costs:  %+.2f %s, t=%.1f, %d trades" % (net["mean"], unit, net["t"], net["n"]))
    print("   yearly: %.1f%% at 1x, Sharpe %.2f, worst drop %.0f %s"
          % (net["ann"] / 100 if unit.startswith("bps") else net["ann"], net["sharpe"], net["dd"],
             unit.split("/")[0]))
    print("   positive years %d of %d, last 12 months %+.0f %s"
          % (net["years_pos"], net["years"], net["last12"], unit.split("/")[0]))
    print("   VERDICT: " + verdict(net, gross))


# ---------------- H1 + H4: intraday clock effects on 5-minute prices ----------------
def clock_tests(pair, d, pip, acc):
    """Adds this pair's trades to acc[test]["net"/"gross"][day] lists."""
    t = d["t"]
    idx = {ts: i for i, ts in enumerate(t)}
    bo, ao, bc, ac, bh, bl, ah, al = d["bo"], d["ao"], d["bc"], d["ac"], d["bh"], d["bl"], d["ah"], d["al"]
    slip = SLIP_PIPS * pip
    fs = foreign_side(pair)
    pref = [0.0]
    for i in range(len(t)):
        pref.append(pref[-1] + ((bh[i] + ah[i]) - (bl[i] + al[i])) / 2)
    day0 = t[0] // DAY * DAY + DAY
    for day in range(day0, t[-1] - DAY, DAY):
        if weekday(day) > 4:
            continue
        fixes = {"LON": local_to_utc(day, 16, 0, london_off), "TKY": day + 55 * 60}
        for name, fix, h0, h1, usd in FIX_TESTS:
            e, x = idx.get(fixes[fix] + h0 * 3600), idx.get(fixes[fix] + h1 * 3600)
            if e is None or x is None:
                continue
            net, gross = ret_bps(-usd * fs, bo[e], ao[e], bo[x], ao[x], slip)
            acc[name]["net"].setdefault(day, []).append(net)
            acc[name]["gross"].setdefault(day, []).append(gross)
        for name, hh, mm, hold in NEWS_TESTS:
            ts = local_to_utc(day, hh, mm, ny_off)
            e = idx.get(ts)
            if e is None or e < 48:
                continue
            avg = (pref[e] - pref[e - 48]) / 48
            move = (bc[e] + ac[e]) / 2 - (bo[e] + ao[e]) / 2
            if avg <= 0 or abs(move) <= NEWS_MULT * avg:
                continue
            i0, x = idx.get(ts + 300), idx.get(ts + 300 + hold * 3600)
            if i0 is None or x is None:
                continue
            net, gross = ret_bps(1 if move > 0 else -1, bo[i0], ao[i0], bo[x], ao[x], slip)
            acc[name]["net"].setdefault(day, []).append(net)
            acc[name]["gross"].setdefault(day, []).append(gross)


def day_points(by_day):
    return [(day, sum(v) / len(v)) for day, v in sorted(by_day.items())]


# ---------------- H2: cross-sectional momentum on month-end prices ----------------
def fetch_daily(api, pair):
    """Daily bid/ask candles from 2012, closing 10:00 New York (liquid hours, not the 5pm spread spike)."""
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, "%s_D10_2012.pkl" % pair)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < DAY:
        with open(path, "rb") as f:
            return pickle.load(f)
    out = []
    frm = "2012-01-01T00:00:00Z"
    while True:
        cs = api._req("GET", "/v3/instruments/%s/candles" % pair, {
            "granularity": "D", "price": "BA", "from": frm, "count": 5000,
            "dailyAlignment": 10, "alignmentTimezone": "America/New_York"})["candles"]
        cs = [c for c in cs if c.get("complete")]
        for c in cs:
            ts = int(datetime.strptime(c["time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
            out.append((ts + DAY, float(c["bid"]["c"]), float(c["ask"]["c"])))
        if len(cs) < 4000:
            break
        frm = datetime.fromtimestamp(out[-1][0], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(path, "wb") as f:
        pickle.dump(out, f)
    return out


def momentum(series, pips, now):
    """series[pair] = [(close_ts, bid, ask)]. Returns {F: (net_points, gross_points)}."""
    this_month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
    month_end = {}
    for p, rows in series.items():
        m = {}
        for ts, b, a in rows:
            key = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")
            if key != this_month:
                m[key] = (ts, b, a)
        month_end[p] = m
    months = sorted(set.intersection(*(set(m) for m in month_end.values())))
    pairs = sorted(series)
    k = len(pairs) // 3
    fee = 2 * MOM_FEE_PER_LEG_YEAR / 12 * 1e4
    out = {}
    for F in MOM_FORMATION:
        net_pts, gross_pts = [], []
        for i in range(F, len(months) - 1):
            m0, mf, m1 = months[i], months[i - F], months[i + 1]
            score = {}
            for p in pairs:
                _, b0, a0 = month_end[p][m0]
                _, bf, af = month_end[p][mf]
                score[p] = foreign_side(p) * math.log((b0 + a0) / (bf + af))
            ranked = sorted(pairs, key=lambda p: score[p])
            legs = [(p, 1) for p in ranked[-k:]] + [(p, -1) for p in ranked[:k]]
            n_sum = g_sum = 0.0
            for p, want in legs:
                _, b0, a0 = month_end[p][m0]
                _, b1, a1 = month_end[p][m1]
                net, gross = ret_bps(want * foreign_side(p), b0, a0, b1, a1, SLIP_PIPS * pips[p])
                n_sum += net
                g_sum += gross
            ts = month_end[pairs[0]][m1][0]
            net_pts.append((ts, n_sum / k - fee))
            gross_pts.append((ts, g_sum / k))
        out[F] = (net_pts, gross_pts)
    return out


# ---------------- H3: volatility compression breakout ----------------
def compression_cands(b):
    w = [None if b.sma[i] in (None, 0) or b.sd[i] is None else b.sd[i] / b.sma[i] for i in range(b.n)]
    thresh = None
    ok = [False] * b.n
    for i in range(520, b.n):
        if thresh is None or i % 24 == 0:
            window = sorted(x for x in w[i - 500:i] if x is not None)
            thresh = window[len(window) // 10] if window else None
        ok[i] = thresh is not None and w[i] is not None and w[i] <= thresh
    return [c for c in b.breakout_cands(20) if ok[c[0] - 1]]


# ---------------- main ----------------
def main():
    env = load_env(os.path.join(HERE, ".env"))
    api = Oanda(env["OANDA_TOKEN"], env["OANDA_ACCOUNT"], env.get("OANDA_ENV", "practice"))
    now = int(time.time())
    allinst = {i["name"]: i for i in api._req("GET", api._acct("/instruments"))["instruments"]}
    pip = {n: 10 ** i["pipLocation"] for n, i in allinst.items()}
    print("Research round 4: 15 pre-registered tests. Bar to pass: t >= %.1f, positive in the last"
          " 12 months, and in 70%% of years. Started %s UTC." % (T_HURDLE, time.strftime("%H:%M", time.gmtime())))

    print("\n=== H5 cross-market: what this account can trade besides currencies ===")
    have = [a for a in CROSS_ASSETS if a in allinst]
    kinds = sorted(set(i.get("type", "?") for i in allinst.values()))
    print("Instrument types on this account: %s" % ", ".join(kinds))
    print("Available: %s" % (", ".join(have) if have else "none of " + ", ".join(CROSS_ASSETS)))

    print("\n=== Downloading 5 years of 5-minute prices (the slow part, ~10-20 min) ===")
    acc = {name: {"net": {}, "gross": {}} for name, *_ in FIX_TESTS + NEWS_TESTS}
    for p in USD_PAIRS:
        if p not in allinst:
            print("  %s not offered on this account, skipped" % p)
            continue
        t0 = time.time()
        d = None
        for attempt in range(4):                 # OANDA sometimes refuses a download for a moment
            try:
                d = fetch(api, p, YEARS_M5)
                break
            except OandaError:
                api.summary()                    # raises if the token itself is dead
                time.sleep(15 * (attempt + 1))
        if d is None:
            print("  %s skipped (OANDA refused its prices 4 times)" % p)
            continue
        clock_tests(p, d, pip[p], acc)
        print("  %s done (%d bars, %.0fs)" % (p, len(d["t"]), time.time() - t0))
        del d

    print("\n=== H1 FIX REVERSALS (Krohn, Mueller & Whelan 2024) ===")
    print("Each day's result = average over the currencies; unit = basis points (1 bp = 0.01%).")
    for name, *_ in FIX_TESTS:
        show(name, "bps/day", stats(day_points(acc[name]["net"]), 252, now),
             stats(day_points(acc[name]["gross"]), 252, now))

    print("\n=== H4 ANNOUNCEMENT CONTINUATION (US release times) ===")
    for name, *_ in NEWS_TESTS:
        pts = day_points(acc[name]["net"])
        per_year = len(pts) / 5.0 if pts else 1
        show(name, "bps/event", stats(pts, per_year, now), stats(day_points(acc[name]["gross"]), per_year, now))

    print("\n=== H2 CROSS-SECTIONAL MOMENTUM (BIS WP 366), monthly, 2012-now ===")
    series = {}
    for p in USD_PAIRS:
        if p in allinst:
            series[p] = fetch_daily(api, p)
    print("Universe: %d G10 currencies vs USD; long the top %d, short the bottom %d each month."
          % (len(series), len(series) // 3, len(series) // 3))
    print("Costs: bid/ask + slippage + %.1f%% a year financing per leg. Interest-rate carry is"
          " NOT modelled (OANDA does not publish its history)." % (MOM_FEE_PER_LEG_YEAR * 100))
    for F, (net_pts, gross_pts) in momentum(series, pip, now).items():
        show("MOM-%d ranked on the last %d month%s" % (MOM_FORMATION.index(F) + 1, F, "s" if F > 1 else ""),
             "bps/month", stats(net_pts, 12, now), stats(gross_pts, 12, now))

    print("\n=== H3 VOLATILITY COMPRESSION BREAKOUT (5 years, 10 pairs) ===")
    print("Unit = R (1R = amount risked). Overnight financing charged.")
    for name, k in VOL_TESTS:
        pts = []
        for p in SWING_PAIRS:
            if p not in allinst:
                continue
            b = Bars(aggregate(fetch(api, p, YEARS_M5, "H1", 3600), k, 3600))
            pts += simulate(b, compression_cands(b), pip[p], "SWING", 2.0, 99.0, 0, 0, 1e9,
                            trail=2.0, fin_pips=FIN_PIPS_PER_DAY)
        pts.sort()
        show(name, "R/trade", stats(pts, max(len(pts) / 5.0, 1), now))

    print("\n=== H6 PORTFOLIO RULES ===")
    print("Not tested on its own: portfolio rules change risk, not the average result. They only")
    print("matter once at least one test above SURVIVES.")
    print("\nFINISHED %s UTC" % time.strftime("%H:%M", time.gmtime()))


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)   # show progress live, even when saved to a file
    try:
        main()
    except OandaError as e:
        if e.status in (401, 403):
            print("\nOANDA stopped accepting the API token. Fix: cd ~/forex-bot && rm .env && bash install.sh")
        else:
            print("\nOANDA had a temporary problem (%s). Run it again; downloaded prices are kept." % e.status)
        sys.exit(1)

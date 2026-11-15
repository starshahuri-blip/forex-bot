"""
Research round 5. Implements PREREG-ROUND5.md exactly (registered at commit ba87644,
before any Round 5 data was downloaded). Read that file for the rules.

Run (keeps going if your phone locks):
    nohup python3 round5.py > round5.txt 2>&1 &
Read the results when the last line says FINISHED:
    cat round5.txt
"""
import math
import os
import pickle
import sys
import time
import urllib.request
from datetime import datetime, timezone

from hypotheses import DAY, USD_PAIRS, fetch_daily, foreign_side, ret_bps, stats
from oanda import Oanda, OandaError, load_env

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SLIP_PIPS = 0.5
MARKUP = {"BASE": 0.015, "ADVERSE": 0.025}
T_HURDLE = 3.0
CC = {"EUR_USD": "EZ", "GBP_USD": "GB", "USD_JPY": "JP", "AUD_USD": "AU", "USD_CAD": "CA",
      "USD_CHF": "CH", "NZD_USD": "NZ", "USD_SEK": "SE", "USD_NOK": "NO"}
MAX_STALE_MONTHS = 4


# ---------------- free data from FRED ----------------
def fred(series):
    """{date 'YYYY-MM-DD': value}. Cached for a day. Missing values ('.') are left out, never filled."""
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, "fred_%s.pkl" % series)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < DAY:
        with open(path, "rb") as f:
            return pickle.load(f)
    req = urllib.request.Request("https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + series,
                                 headers={"User-Agent": "Mozilla/5.0 (research script)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = parse_fred(r.read().decode())
    with open(path, "wb") as f:
        pickle.dump(out, f)
    return out


def parse_fred(text):
    out = {}
    for line in text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 2 and parts[1].strip() not in ("", "."):
            try:
                out[parts[0].strip()] = float(parts[1])
            except ValueError:
                pass
    return out


def month_key(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")


def prev_month(key, n=1):
    y, m = int(key[:4]), int(key[5:])
    m -= n
    while m <= 0:
        m += 12
        y -= 1
    return "%04d-%02d" % (y, m)


def latest_on_or_before(series, date_str):
    """Most recent value dated on or before date_str (series keys are 'YYYY-MM-DD')."""
    best = None
    for d in series:
        if d <= date_str and (best is None or d > best):
            best = d
    return series[best] if best else None


class Rates:
    """Monthly 3-month rates, in percent, keyed 'YYYY-MM'."""

    def __init__(self, raw):
        self.m = {cc: {d[:7]: v for d, v in s.items()} for cc, s in raw.items()}

    def known_at(self, cc, key):
        """Value usable for a decision at the end of month `key` (previous month, else up to 2 earlier)."""
        for back in (1, 2, 3):
            v = self.m[cc].get(prev_month(key, back))
            if v is not None:
                return v
        return None

    def accrual(self, cc, key):
        """Rate actually in force during month `key` (latest published at or before it)."""
        for back in (0, 1, 2, 3):
            v = self.m[cc].get(prev_month(key, back))
            if v is not None:
                return v
        return None


# ---------------- trade maths ----------------
def leg(pair, want, row0, row1, pip, rates, cc_hold_key, markup):
    """One position for one holding period. want=+1 long the foreign currency, -1 short it.
    Returns (net bps, gross bps). Net = spot at bid/ask with slippage + carry - mark-up."""
    t0, b0, a0 = row0
    t1, b1, a1 = row1
    net, gross = ret_bps(want * foreign_side(pair), b0, a0, b1, a1, SLIP_PIPS * pip)
    years = (t1 - t0) / (365.0 * DAY)
    i_f, i_us = rates.accrual(CC[pair], cc_hold_key), rates.accrual("US", cc_hold_key)
    carry = want * (i_f - i_us) / 100.0 * years * 1e4 if i_f is not None and i_us is not None else 0.0
    return net + carry - markup * years * 1e4, gross + carry


def period_ends(rows, key_fn):
    """Index of the last daily row of each period, in order: [(key, idx)]."""
    last = {}
    order = []
    for i, (ts, _, _) in enumerate(rows):
        k = key_fn(ts)
        if k not in last:
            order.append(k)
        last[k] = i
    return [(k, last[k]) for k in order]


def week_key(ts):
    y, w, _ = datetime.fromtimestamp(ts, timezone.utc).isocalendar()
    return "%04d-W%02d" % (y, w)


def nw_t(vals):
    n = len(vals)
    mean = sum(vals) / n
    e = [v - mean for v in vals]
    lags = int(4 * (n / 100.0) ** (2.0 / 9.0))
    var = sum(x * x for x in e) / n
    for L in range(1, lags + 1):
        w = 1 - L / (lags + 1.0)
        var += 2 * w * sum(e[i] * e[i - L] for i in range(L, n)) / n
    return mean / math.sqrt(var / n) if var > 0 else 0.0


def judge(points, per_year, now):
    s = stats(points, per_year, now)
    if s is None:
        return None
    s["t_nw"] = nw_t([v for _, v in points])
    s["t_use"] = min(s["t"], s["t_nw"])
    s["pass"] = s["t_use"] >= T_HURDLE and s["last12"] > 0 and s["years_pos"] >= 0.7 * s["years"]
    return s


def show(name, unit, adverse, base, gross, exploratory=False):
    print("\n" + name + ("   [EXPLORATORY]" if exploratory else ""))
    if adverse is None:
        print("   too few periods to judge")
        return
    if gross:
        print("   before costs:        %+.1f %s, t=%.1f" % (gross["mean"], unit, gross["t"]))
    if base:
        print("   after BASE costs:    %+.1f %s, t=%.1f" % (base["mean"], unit, base["t"]))
    print("   after ADVERSE costs: %+.1f %s, t=%.1f (Newey-West %.1f), %d periods"
          % (adverse["mean"], unit, adverse["t"], adverse["t_nw"], adverse["n"]))
    print("   yearly %.1f%%, Sharpe %.2f, positive years %d of %d, last 12 months %+.0f bps"
          % (adverse["ann"] / 100, adverse["sharpe"], adverse["years_pos"], adverse["years"], adverse["last12"]))
    if adverse["pass"]:
        print("   VERDICT: " + ("PASSED, but exploratory: register a new test on future data."
                               if exploratory else "SURVIVED. Next step: forward practice test."))
    elif gross and gross["t"] >= T_HURDLE and gross["mean"] > 0:
        print("   VERDICT: REAL BEFORE COSTS, BUT COSTS EAT IT.")
    else:
        print("   VERDICT: NOT FOUND.")


# ---------------- the tests ----------------
def monthly_tests(px, pips, rates, now):
    """px[pair] = daily rows. Returns {test: {"ADVERSE": pts, "BASE": pts, "GROSS": pts}}."""
    pairs = sorted(px)
    ends = {p: dict(period_ends(px[p], month_key)) for p in pairs}
    this_month = month_key(now)
    months = sorted(k for k in set.intersection(*(set(e) for e in ends.values())) if k != this_month)
    k3 = len(pairs) // 3
    out = {t: {"ADVERSE": [], "BASE": [], "GROSS": []} for t in ("R5-1", "R5-3", "R5-4")}
    skipped = 0
    for i in range(4, len(months) - 1):
        m, m_next = months[i], months[i + 1]
        diff = {}
        for p in pairs:
            a, b = rates.known_at(CC[p], m), rates.known_at("US", m)
            diff[p] = None if a is None or b is None else a - b
        if any(v is None for v in diff.values()):
            skipped += 1
            continue
        old = {}
        for p in pairs:
            a, b = rates.known_at(CC[p], months[i - 3]), rates.known_at("US", months[i - 3])
            old[p] = None if a is None or b is None else a - b
        rows = {}
        for p in pairs:
            e0, e1 = ends[p][m] + 1, ends[p][m_next] + 1        # enter the day AFTER the signal
            if e1 >= len(px[p]):
                rows = None
                break
            rows[p] = (px[p][e0], px[p][e1])
        if rows is None:
            continue
        hold_key = m_next
        ts = rows[pairs[0]][1][0]
        books = {"R5-1": None, "R5-3": None, "R5-4": None}
        by_carry = sorted(pairs, key=lambda p: diff[p])
        books["R5-1"] = [(p, 1) for p in by_carry[-k3:]] + [(p, -1) for p in by_carry[:k3]]
        avg_gap = sum(diff.values()) / len(diff)
        books["R5-3"] = [(p, 1 if avg_gap > 0 else -1) for p in pairs]
        if all(v is not None for v in old.values()):
            by_change = sorted(pairs, key=lambda p: diff[p] - old[p])
            books["R5-4"] = [(p, 1) for p in by_change[-k3:]] + [(p, -1) for p in by_change[:k3]]
        for name, book in books.items():
            if not book:
                continue
            res = {}
            for band in ("ADVERSE", "BASE"):
                legs = [leg(p, w, rows[p][0], rows[p][1], pips[p], rates, hold_key, MARKUP[band]) for p, w in book]
                res[band] = legs
            longs = sum(1 for _, w in book if w > 0)
            shorts = len(book) - longs
            scale = (lambda tot: tot / k3) if name != "R5-3" else (lambda tot: tot / len(book))
            out[name]["ADVERSE"].append((ts, scale(sum(n for n, _ in res["ADVERSE"]))))
            out[name]["BASE"].append((ts, scale(sum(n for n, _ in res["BASE"]))))
            out[name]["GROSS"].append((ts, scale(sum(g for _, g in res["ADVERSE"]))))
    # R5-2: vol-managed carry, weights from R5-1's own trailing 12 months (known in advance)
    out["R5-2"] = {"ADVERSE": [], "BASE": [], "GROSS": []}
    for band in ("ADVERSE", "BASE", "GROSS"):
        src = out["R5-1"][band]
        ref = out["R5-1"]["ADVERSE"]
        for j in range(12, len(src)):
            past = [v for _, v in ref[j - 12:j]]
            mu = sum(past) / 12
            vol = (sum((v - mu) ** 2 for v in past) / 11) ** 0.5 * math.sqrt(12) / 1e4
            w = min(2.0, 0.08 / vol) if vol > 0 else 1.0
            out["R5-2"][band].append((src[j][0], w * src[j][1]))
    return out, skipped


def weekly_tests(px, pips, rates, oil, vix, now):
    pairs = sorted(px)
    ends = {p: dict(period_ends(px[p], week_key)) for p in pairs}
    weeks = sorted(set.intersection(*(set(e) for e in ends.values())))
    k3 = len(pairs) // 3
    out = {t: {"ADVERSE": [], "BASE": [], "GROSS": []} for t in ("R5-5", "R5-6", "R5-7")}
    for i in range(1, len(weeks) - 1):
        w_prev, w, w_next = weeks[i - 1], weeks[i], weeks[i + 1]
        rows, perf = {}, {}
        for p in pairs:
            s0 = px[p][ends[p][w_prev]]
            s1 = px[p][ends[p][w]]
            perf[p] = foreign_side(p) * math.log((s1[1] + s1[2]) / (s0[1] + s0[2]))
            e0, e1 = ends[p][w] + 1, ends[p][w_next] + 1
            if e1 >= len(px[p]):
                rows = None
                break
            rows[p] = (px[p][e0], px[p][e1])
        if rows is None:
            continue
        sig_ts = px[pairs[0]][ends[pairs[0]][w]][0]
        d1 = datetime.fromtimestamp(sig_ts - DAY, timezone.utc).strftime("%Y-%m-%d")
        d8 = datetime.fromtimestamp(sig_ts - 8 * DAY, timezone.utc).strftime("%Y-%m-%d")
        hold_key = month_key(rows[pairs[0]][0][0])
        ts = rows[pairs[0]][1][0]
        ranked = sorted(pairs, key=lambda p: perf[p])
        books = {"R5-5": [(p, 1) for p in ranked[:k3]] + [(p, -1) for p in ranked[-k3:]]}
        for name, series, members in (("R5-6", oil, ("USD_CAD", "USD_NOK")), ("R5-7", vix, ("USD_JPY", "USD_CHF"))):
            a, b = latest_on_or_before(series, d1), latest_on_or_before(series, d8)
            if a is not None and b is not None and a != b and all(m in rows for m in members):
                books[name] = [(m, 1 if a > b else -1) for m in members]
        for name, book in books.items():
            for band in ("ADVERSE", "BASE"):
                tot = sum(leg(p, wt, rows[p][0], rows[p][1], pips[p], rates, hold_key, MARKUP[band])[0] for p, wt in book)
                out[name][band].append((ts, tot / (k3 if name == "R5-5" else len(book))))
            g = sum(leg(p, wt, rows[p][0], rows[p][1], pips[p], rates, hold_key, 0.0)[1] for p, wt in book)
            out[name]["GROSS"].append((ts, g / (k3 if name == "R5-5" else len(book))))
    return out


def main():
    env = load_env(os.path.join(HERE, ".env"))
    api = Oanda(env["OANDA_TOKEN"], env["OANDA_ACCOUNT"], env.get("OANDA_ENV", "practice"))
    now = int(time.time())
    print("Research round 5, implementing PREREG-ROUND5.md (commit ba87644). Started %s UTC."
          % time.strftime("%H:%M", time.gmtime()))
    print("Bar: smaller of plain/Newey-West t >= 3 under ADVERSE costs, positive last 12 months, 70% of years.")

    print("\n=== Free interest-rate data (FRED / OECD 3-month rates) ===")
    raw = {}
    for cc in ["US"] + [CC[p] for p in USD_PAIRS]:
        sid = "IR3TIB01%sM156N" % cc
        try:
            s = fred(sid)
        except Exception as e:
            print("  %s: could not download (%s)" % (sid, str(e)[:60]))
            s = {}
        if s:
            print("  %-4s %s  %s to %s" % (cc, sid, min(s)[:7], max(s)[:7]))
        raw[cc] = s
    if not raw["US"]:
        print("\nNo US rate data, so the rate tests cannot run. Nothing has been invented. STOPPED.")
        return
    cutoff = prev_month(month_key(now), MAX_STALE_MONTHS)
    pairs = []
    for p in USD_PAIRS:
        s = raw[CC[p]]
        if not s or max(s)[:7] < cutoff:
            print("  %s EXCLUDED: rate series missing or ends before %s" % (p, cutoff))
        else:
            pairs.append(p)
    rates = Rates(raw)
    print("Universe: %d currencies vs USD." % len(pairs))

    print("\n=== Prices (OANDA daily, 10:00 New York) ===")
    allinst = {i["name"]: i for i in api._req("GET", api._acct("/instruments"))["instruments"]}
    pips = {p: 10 ** allinst[p]["pipLocation"] for p in pairs}
    px = {p: fetch_daily(api, p) for p in pairs}
    print("  %d pairs, %d to %d daily closes each" % (len(px), min(len(v) for v in px.values()), max(len(v) for v in px.values())))

    print("\n=== Monthly tests ===")
    monthly, skipped = monthly_tests(px, pips, rates, now)
    if skipped:
        print("(%d months skipped because a rate value was not yet published)" % skipped)
    for name, label in (("R5-1", "R5-1 CARRY: long 3 highest-rate, short 3 lowest"),
                        ("R5-2", "R5-2 VOL-MANAGED CARRY"),
                        ("R5-3", "R5-3 DOLLAR CARRY: all 9 with or against USD"),
                        ("R5-4", "R5-4 POLICY DIVERGENCE: widening vs narrowing rate gaps")):
        d = monthly[name]
        show(label, "bps/month", judge(d["ADVERSE"], 12, now), judge(d["BASE"], 12, now), judge(d["GROSS"], 12, now))

    print("\n=== Weekly tests ===")
    oil, vix = {}, {}
    for sid, store in (("DCOILWTICO", oil), ("VIXCLS", vix)):
        try:
            store.update(fred(sid))
        except Exception as e:
            print("  %s: could not download (%s); its test will be skipped" % (sid, str(e)[:60]))
    weekly = weekly_tests(px, pips, rates, oil, vix, now)
    for name, label, expl in (("R5-5", "R5-5 WEEKLY REVERSAL: buy last week's losers", False),
                              ("R5-6", "R5-6 OIL -> CAD and NOK", True),
                              ("R5-7", "R5-7 VIX -> JPY and CHF", True)):
        d = weekly[name]
        show(label, "bps/week", judge(d["ADVERSE"], 52, now), judge(d["BASE"], 52, now), judge(d["GROSS"], 52, now), expl)

    print("\nTrial counter: 696 before this round, 705 after (exploratory tests counted double).")
    print("FINISHED %s UTC" % time.strftime("%H:%M", time.gmtime()))


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    try:
        main()
    except OandaError as e:
        print("\nOANDA problem (%s). Run it again; downloaded data is kept." % e.status)
        sys.exit(1)

"""
Tests the strategy on real past OANDA prices (real bid/ask, so spread costs are included).

Run:   python3 backtest.py          (last 60 days)
       python3 backtest.py 180      (last 180 days)

Results are in "R": 1R = the amount risked on one trade. +2R = a full winner, -1R = a full loser.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import config as cfg
from oanda import Oanda, load_env
from strategy import GRAN_SECONDS, Indicators, in_session, must_flatten, parse_time, signal

HERE = os.path.dirname(os.path.abspath(__file__))


def fetch(api, pair, days):
    g = GRAN_SECONDS[cfg.GRANULARITY]
    start = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    while True:
        cs = api.candles(pair, cfg.GRANULARITY, count=5000,
                         frm=start.strftime("%Y-%m-%dT%H:%M:%SZ"), price="BA")
        cs = [c for c in cs if c.get("complete")]
        if not cs:
            break
        for c in cs:
            b, a = c["bid"], c["ask"]
            out.append({
                "t": parse_time(c["time"]),
                "bo": float(b["o"]), "bh": float(b["h"]), "bl": float(b["l"]), "bc": float(b["c"]),
                "ao": float(a["o"]), "ah": float(a["h"]), "al": float(a["l"]), "ac": float(a["c"]),
            })
        start = out[-1]["t"] + timedelta(seconds=g)
        if len(cs) < 4000:
            break
    return out


def simulate(bars, pip):
    """Returns a list of (close_time, R) for one pair."""
    h = [(b["bh"] + b["ah"]) / 2 for b in bars]
    l = [(b["bl"] + b["al"]) / 2 for b in bars]
    c = [(b["bc"] + b["ac"]) / 2 for b in bars]
    ind = Indicators(h, l, c, cfg)
    results = []
    pos = None
    for i in range(cfg.EMA_SLOW + 50, len(bars) - 1):
        nb = bars[i + 1]
        if pos is None:
            sig = signal(ind, i, cfg)
            if not sig or not in_session(nb["t"], cfg):
                continue
            side, atr = sig
            d = max(cfg.STOP_ATR * atr, cfg.MIN_STOP_PIPS * pip)
            if nb["ao"] - nb["bo"] > cfg.MAX_SPREAD_FRAC * d:
                continue
            entry = nb["ao"] if side > 0 else nb["bo"]
            pos = {"side": side, "entry": entry, "d": d, "sl": entry - side * d,
                   "tp": entry + side * cfg.TARGET_R * d, "from": i + 1}
        if pos is None or i + 1 < pos["from"]:
            continue
        side, entry, d = pos["side"], pos["entry"], pos["d"]
        # A buy is closed at the bid; a sell is closed at the ask.
        o, hi, lo = (nb["bo"], nb["bh"], nb["bl"]) if side > 0 else (nb["ao"], nb["ah"], nb["al"])
        exit_px = None
        if i + 1 > pos["from"] and must_flatten(nb["t"], cfg):
            exit_px = o
        elif (side > 0 and o <= pos["sl"]) or (side < 0 and o >= pos["sl"]):
            exit_px = o                                   # gapped through the stop
        elif (side > 0 and lo <= pos["sl"]) or (side < 0 and hi >= pos["sl"]):
            exit_px = pos["sl"]                           # if stop and target both hit, assume stop
        elif (side > 0 and hi >= pos["tp"]) or (side < 0 and lo <= pos["tp"]):
            exit_px = pos["tp"]
        if exit_px is not None:
            results.append((nb["t"], side * (exit_px - entry) / d))
            pos = None
            continue
        best = hi if side > 0 else lo
        if (best - entry) * side >= cfg.BREAKEVEN_AT_R * d:
            pos["sl"] = entry if (pos["sl"] - entry) * side < 0 else pos["sl"]
    return results


def stats(rs):
    if not rs:
        return None
    wins = [r for r in rs if r > 0]
    gross_w = sum(wins)
    gross_l = -sum(r for r in rs if r < 0)
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": len(rs), "win": 100.0 * len(wins) / len(rs), "total": sum(rs),
            "pf": (gross_w / gross_l) if gross_l > 0 else float("inf"), "dd": dd}


def report(name, s):
    if not s:
        print("%-9s no trades" % name)
        return
    print("%-9s trades %4d | win %5.1f%% | total %+7.1fR | profit factor %5.2f | worst drop %5.1fR"
          % (name, s["n"], s["win"], s["total"], s["pf"], s["dd"]))


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    env = load_env(os.path.join(HERE, ".env"))
    api = Oanda(env["OANDA_TOKEN"], env["OANDA_ACCOUNT"], env.get("OANDA_ENV", "practice"))
    specs = api.instruments(cfg.PAIRS)
    print("Backtest: last %d days, %s bars, real bid/ask prices\n" % (days, cfg.GRANULARITY))
    everything = []
    for pair in cfg.PAIRS:
        bars = fetch(api, pair, days)
        res = simulate(bars, 10 ** specs[pair]["pipLocation"])
        everything += res
        report(pair, stats([r for _, r in res]))
    everything.sort(key=lambda x: x[0])
    s = stats([r for _, r in everything])
    print("-" * 86)
    report("ALL", s)
    if s:
        pct = s["total"] * cfg.RISK_PER_TRADE * 100
        print("\nAt %.1f%% risk per trade that is roughly %+.1f%% on the account (not compounded),"
              % (cfg.RISK_PER_TRADE * 100, pct))
        print("with a worst drop of about %.1f%%." % (s["dd"] * cfg.RISK_PER_TRADE * 100))
        print("Note: the daily limits (max trades, loss cap, max 3 open) are not applied here.")
        if s["pf"] < 1.0:
            print("\nVERDICT: these settings LOST money over this period. Do not go live with them.")
        elif s["pf"] < 1.2 or s["n"] < 100:
            print("\nVERDICT: thin edge or too few trades to trust. Test a longer period (e.g. 365).")
        else:
            print("\nVERDICT: profitable over this period. Past results do not guarantee future ones.")


if __name__ == "__main__":
    main()

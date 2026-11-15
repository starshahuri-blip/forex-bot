"""
Research round 3: SLOWER trading (1-hour and 4-hour bars, trades held for hours to days).

Why: on 5-15 minute bars the spread ate every idea. On bigger bars each trade aims for a much
larger move, so the same spread is a far smaller slice of it.

Same honest method: tune on the older two-thirds of five years, exam on the newest third.
Includes a charge for holding trades overnight.

Run:   python3 swing.py          (takes about 5 minutes)
"""
import os
import sys
import time

from oanda import Oanda, load_env
from research import PAIRS, Acc, Bars, aggregate, fetch, report, simulate

HERE = os.path.dirname(os.path.abspath(__file__))
GRANS = {"H1": 1, "H4": 4}
FIN_PIPS_PER_DAY = 0.5          # overnight holding cost, charged against every trade
TREND_N = [20, 55]
TREND_STOP = [2.0, 3.0]
TREND_EXIT = ["2.0", "3.0", "trail"]
TREND_ADX = [0, 25]
MR_SD = [2.0, 2.5, 3.0]
MR_STOP = [2.0, 3.0]
MR_ADX = [25, 100]


def run(datasets, pips, split_t, progress=print):
    accs = {}
    for pair, h1 in datasets():
        t0 = time.time()
        for gname, k in GRANS.items():
            b = Bars(aggregate(h1, k, 3600))
            for nb in TREND_N:
                cands = b.breakout_cands(nb)
                for st in TREND_STOP:
                    for ex in TREND_EXIT:
                        for ax in TREND_ADX:
                            key = "TREND  %s bars=%d stop=%.1f exit=%s adx>=%d" % (gname, nb, st, ex, ax)
                            trail = st if ex == "trail" else 0.0
                            tg = 99.0 if ex == "trail" else float(ex)
                            res = simulate(b, cands, pips[pair], "SWING", st, tg, 0, ax, 1e9,
                                           trail=trail, fin_pips=FIN_PIPS_PER_DAY)
                            accs.setdefault(key, Acc()).add(pair, res, split_t)
            for sdv in MR_SD:
                cands = b.meanrev_cands(sdv)
                for st in MR_STOP:
                    for ax in MR_ADX:
                        key = "REVERT %s bands=%.1f stop=%.1f adx<=%d" % (gname, sdv, st, ax)
                        res = simulate(b, cands, pips[pair], "SWING", st, 0, 0, -1, ax,
                                       fin_pips=FIN_PIPS_PER_DAY)
                        accs.setdefault(key, Acc()).add(pair, res, split_t)
        progress("  %s done (%d one-hour bars, %.0fs)" % (pair, len(h1["t"]), time.time() - t0))
    return accs


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1825
    env = load_env(os.path.join(HERE, ".env"))
    api = Oanda(env["OANDA_TOKEN"], env["OANDA_ACCOUNT"], env.get("OANDA_ENV", "practice"))
    specs = api.instruments(PAIRS)
    pairs = [p for p in PAIRS if p in specs]
    pips = {p: 10 ** specs[p]["pipLocation"] for p in pairs}
    split_t = int(time.time()) - (days // 3) * 86400
    print("Research round 3 (slower trading): %d pairs, last %.0f years. Training on the first %d days, exam on the newest %d."
          % (len(pairs), days / 365.0, days - days // 3, days // 3))
    print("Downloading prices and testing (about 5 minutes; it prints a line per pair)...")

    def datasets():
        for p in pairs:
            yield p, fetch(api, p, days, "H1", 3600)

    report(run(datasets, pips, split_t), min_train=300)


if __name__ == "__main__":
    main()

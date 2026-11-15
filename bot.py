"""
The live trading bot. Wakes up once per bar, checks the guardrails, manages open
trades, and opens new ones when the strategy fires. Every trade is sent to OANDA
with its stop-loss and take-profit attached, so trades stay protected even if this
program or the server goes down.

Run:   python3 bot.py           (normal)
       python3 bot.py --check   (just test the OANDA connection and exit)
"""
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import config as cfg
from oanda import Oanda, OandaError, load_env
from strategy import (GRAN_SECONDS, Indicators, in_session, must_flatten,
                      parse_time, signal, usd_direction)

HERE = os.path.dirname(os.path.abspath(__file__))
STOP_FILE = os.path.join(HERE, "STOP")     # create this file = close everything and pause
HALT_FILE = os.path.join(HERE, "HALT")     # written by the bot when max drawdown is hit
STATE_FILE = os.path.join(HERE, "state.json")

log = logging.getLogger("bot")


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    logging.Formatter.converter = time.gmtime
    log.setLevel(logging.INFO)
    fh = RotatingFileHandler(os.path.join(HERE, "bot.log"), maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


class Bot:
    def __init__(self, api, env):
        self.api = api
        self.tg_token = env.get("TG_TOKEN")
        self.tg_chat = env.get("TG_CHAT")
        self.g = GRAN_SECONDS[cfg.GRANULARITY]
        self.specs = api.instruments(cfg.PAIRS)
        self.last_bar = {}
        self.idle_reason = None
        self.state = {}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    self.state = json.load(f)
            except (OSError, ValueError):
                self.state = {}

    # ---------- helpers ----------
    def save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f)

    def alert(self, msg):
        log.info(msg)
        if self.tg_token and self.tg_chat:
            try:
                data = urllib.parse.urlencode({"chat_id": self.tg_chat, "text": "FX bot: " + msg}).encode()
                urllib.request.urlopen(
                    "https://api.telegram.org/bot%s/sendMessage" % self.tg_token, data=data, timeout=10)
            except Exception:
                pass

    def fmt(self, pair, price):
        return "%.*f" % (self.specs[pair]["displayPrecision"], price)

    def flatten(self, trades, why):
        for t in trades:
            try:
                self.api.close_trade(t["id"])
                self.alert("CLOSED %s trade %s (%s)" % (t["instrument"], t["id"], why))
            except Exception as e:
                log.info("Could not close trade %s: %s", t["id"], e)

    def idle(self, reason, trades):
        """Not trading right now: make sure nothing is open, and say why (once)."""
        if trades:
            self.flatten(trades, reason)
        if reason != self.idle_reason:
            log.info("Paused: %s", reason)
            self.idle_reason = reason

    def bars(self, pair, now_ts):
        out = []
        for c in self.api.candles(pair, cfg.GRANULARITY, count=500, price="M"):
            t = parse_time(c["time"]).timestamp()
            if c.get("complete") or t + self.g <= now_ts:
                m = c["mid"]
                out.append({"t": t, "h": float(m["h"]), "l": float(m["l"]), "c": float(m["c"])})
        return out

    # ---------- one wake-up ----------
    def cycle(self):
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        acct = self.api.summary()
        nav = float(acct["NAV"])
        balance = float(acct["balance"])
        margin_used = float(acct["marginUsed"])
        home = acct["currency"]
        trades = self.api.open_trades()

        # --- kill switches ---
        if os.path.exists(STOP_FILE):
            return self.idle("STOP file found (manual pause)", trades)
        if os.path.exists(HALT_FILE):
            return self.idle("HALTED after max drawdown. Delete the HALT file to resume", trades)

        peaks = self.state.setdefault("peak", {})
        peak = max(peaks.get(self.api.account, nav), nav)
        if peak != peaks.get(self.api.account):
            peaks[self.api.account] = peak
            self.save_state()
        if nav <= 0 or (peak - nav) / peak >= cfg.MAX_DRAWDOWN:
            with open(HALT_FILE, "w") as f:
                f.write("Halted %s: account %.2f, peak %.2f\n" % (now.isoformat(), nav, peak))
            self.alert("MAX DRAWDOWN HIT (%.1f%% below peak). Closing everything and halting."
                       % (100 * (peak - nav) / peak if peak else 100))
            return self.idle("HALTED after max drawdown. Delete the HALT file to resume", trades)

        # --- today's results, rebuilt from OANDA's own records ---
        closed = [t for t in self.api.closed_trades(100) if t.get("closeTime", "")[:10] == today]
        closed.sort(key=lambda t: t["closeTime"], reverse=True)
        pls = [float(t.get("realizedPL", 0)) + float(t.get("financing", 0)) for t in closed]
        day_start = balance - sum(pls)
        day_pl = (nav - day_start) / day_start if day_start > 0 else 0.0
        trades_today = len(closed) + sum(1 for t in trades if t["openTime"][:10] == today)
        consec = 0
        for pl in pls:
            if pl < -0.002 * day_start:
                consec += 1
            else:
                break

        halts = self.state.setdefault("day_halt", {})
        if day_pl <= -cfg.DAILY_LOSS_LIMIT and halts.get(self.api.account) != today:
            halts[self.api.account] = today
            self.save_state()
            self.alert("DAILY LOSS LIMIT HIT (%.2f%%). Closing everything until tomorrow." % (100 * day_pl))
        if halts.get(self.api.account) == today:
            return self.idle("Daily loss limit hit; back tomorrow", trades)

        if must_flatten(now, cfg):
            return self.idle("Outside trading hours", trades)

        self.idle_reason = None
        log.info("Account %.2f | today %+.2f%% | open trades %d | trades today %d",
                 nav, 100 * day_pl, len(trades), trades_today)

        can_enter = (in_session(now, cfg) and trades_today < cfg.MAX_TRADES_PER_DAY
                     and consec < cfg.MAX_CONSEC_LOSSES)
        open_by_pair = {t["instrument"]: t for t in trades}
        open_count = len(trades)
        usd_dirs = [usd_direction(t["instrument"], 1 if float(t["currentUnits"]) > 0 else -1)
                    for t in trades]

        for pair in cfg.PAIRS:
            t = open_by_pair.get(pair)
            if not t and (not can_enter or open_count >= cfg.MAX_OPEN_TRADES):
                continue
            bars = self.bars(pair, now.timestamp())
            if t:
                self.manage(t, bars)
                continue
            if len(bars) < cfg.EMA_SLOW + 50:
                continue
            bt = bars[-1]["t"]
            if self.last_bar.get(pair) == bt:
                continue                          # already looked at this bar
            self.last_bar[pair] = bt
            if now.timestamp() - (bt + self.g) > 120:
                continue                          # bar is old (quiet market or we started mid-bar)
            ind = Indicators([b["h"] for b in bars], [b["l"] for b in bars], [b["c"] for b in bars], cfg)
            sig = signal(ind, len(bars) - 1, cfg)
            if not sig:
                continue
            side, atr = sig
            d_usd = usd_direction(pair, side)
            if d_usd and usd_dirs.count(d_usd) >= cfg.MAX_SAME_USD_DIRECTION:
                log.info("%s signal skipped: too many trades leaning the same way on the dollar", pair)
                continue
            margin = self.enter(pair, side, atr, nav, margin_used, home)
            if margin is not None:
                open_count += 1
                trades_today += 1
                margin_used += margin
                usd_dirs.append(d_usd)
                if trades_today >= cfg.MAX_TRADES_PER_DAY:
                    can_enter = False

    def enter(self, pair, side, atr, nav, margin_used, home):
        """Size and send one trade. Returns the margin it uses, or None if not opened."""
        spec = self.specs[pair]
        pip = 10 ** spec["pipLocation"]
        bid, ask, tradeable = self.api.pricing(pair)
        if not tradeable:
            return None
        d = max(cfg.STOP_ATR * atr, cfg.MIN_STOP_PIPS * pip)
        if ask - bid > cfg.MAX_SPREAD_FRAC * d:
            log.info("%s signal skipped: spread too wide (%.1f pips)", pair, (ask - bid) / pip)
            return None
        entry = ask if side > 0 else bid
        sl = entry - side * d
        tp = entry + side * cfg.TARGET_R * d
        base, quote = pair.split("_")
        if quote == home:
            qh = 1.0                    # profit comes in the account currency already
        elif base == home:
            qh = 1.0 / entry
        else:
            log.info("%s skipped: account currency %s not supported for this pair", pair, home)
            return None
        units = cfg.RISK_PER_TRADE * nav / (d * qh)
        units = int(min(units, cfg.MAX_LEVERAGE_PER_TRADE * nav / (entry * qh)))
        if units < max(1, float(spec.get("minimumTradeSize", "1"))):
            return None
        margin = units * entry * qh * float(spec.get("marginRate", "0.05"))
        if (margin_used + margin) / nav > cfg.MAX_MARGIN_USED:
            log.info("%s signal skipped: would use too much margin", pair)
            return None
        try:
            resp = self.api.market_order(pair, side * units, self.fmt(pair, sl), self.fmt(pair, tp))
        except OandaError as e:
            self.alert("%s order rejected: %s" % (pair, e.body[:200]))
            return None
        fill = resp.get("orderFillTransaction")
        if not fill:
            reason = resp.get("orderCancelTransaction", {}).get("reason", "unknown")
            self.alert("%s order cancelled by OANDA: %s" % (pair, reason))
            return None
        risk_amt = units * d * qh
        self.alert("OPENED %s %s %d units @ %s | stop %s | target %s | risking %.2f %s"
                   % ("BUY" if side > 0 else "SELL", pair, units, fill.get("price", self.fmt(pair, entry)),
                      self.fmt(pair, sl), self.fmt(pair, tp), risk_amt, home))
        return margin

    def manage(self, t, bars):
        """Move the stop to entry once the trade is up 1x its risk."""
        slo, tpo = t.get("stopLossOrder"), t.get("takeProfitOrder")
        if not slo or not tpo:
            log.info("WARNING: %s trade %s has no stop/target info; leaving it alone", t["instrument"], t["id"])
            return
        entry = float(t["price"])
        side = 1 if float(t["currentUnits"]) > 0 else -1
        sl, tp = float(slo["price"]), float(tpo["price"])
        if (sl - entry) * side >= 0:
            return                                 # already at breakeven or better
        d = abs(tp - entry) / cfg.TARGET_R
        opened = parse_time(t["openTime"]).timestamp()
        trigger = entry + side * cfg.BREAKEVEN_AT_R * d
        for b in bars:
            if b["t"] + self.g <= opened:
                continue
            if (side > 0 and b["h"] >= trigger) or (side < 0 and b["l"] <= trigger):
                try:
                    self.api.set_stop(t["id"], self.fmt(t["instrument"], entry))
                    self.alert("%s trade %s: stop moved to entry (risk-free now)" % (t["instrument"], t["id"]))
                except OandaError as e:
                    log.info("Could not move stop on %s: %s", t["id"], e.body[:200])
                return


def main():
    setup_logging()
    env = load_env(os.path.join(HERE, ".env"))
    if not env.get("OANDA_TOKEN") or not env.get("OANDA_ACCOUNT"):
        print("Missing OANDA_TOKEN / OANDA_ACCOUNT. Run:  bash install.sh")
        sys.exit(1)
    mode = env.get("OANDA_ENV", "practice")
    api = Oanda(env["OANDA_TOKEN"], env["OANDA_ACCOUNT"], mode)

    if "--check" in sys.argv:
        try:
            a = api.summary()
        except Exception as e:
            print("CONNECTION FAILED: %s" % e)
            sys.exit(1)
        print("CONNECTED to OANDA (%s). Balance: %s %s" % (mode.upper(), a["balance"], a["currency"]))
        return

    bot = Bot(api, env)
    bot.alert("Bot started in %s mode on %s, %s bars" % (mode.upper(), ", ".join(cfg.PAIRS), cfg.GRANULARITY))
    errors = 0
    while True:
        nxt = (int(time.time()) // bot.g + 1) * bot.g + 5     # 5 seconds after each bar closes
        time.sleep(max(1, nxt - time.time()))
        try:
            bot.cycle()
            errors = 0
        except Exception as e:
            errors += 1
            log.exception("Cycle failed: %s", e)
            if errors == 3:
                bot.alert("3 failed cycles in a row; still retrying. Last error: %s" % str(e)[:200])


if __name__ == "__main__":
    main()

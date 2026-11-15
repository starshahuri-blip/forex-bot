"""Tiny OANDA v20 client. Uses only Python's standard library (nothing to install)."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}


class OandaError(Exception):
    def __init__(self, status, body):
        super().__init__("OANDA error %s: %s" % (status, body))
        self.status = status
        self.body = body


def load_env(path):
    """Read KEY=VALUE lines from the .env file into a dict (real env vars win)."""
    env = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    for k in ("OANDA_TOKEN", "OANDA_ACCOUNT", "OANDA_ENV", "TG_TOKEN", "TG_CHAT"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


class Oanda:
    def __init__(self, token, account, env="practice"):
        if env not in HOSTS:
            raise ValueError("OANDA_ENV must be 'practice' or 'live'")
        self.base = HOSTS[env]
        self.env = env
        self.account = account
        self.headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339",
        }

    def _req(self, method, path, params=None, body=None, tries=3):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        last = None
        for attempt in range(tries):
            req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                if e.code == 429 or e.code >= 500:
                    last = OandaError(e.code, text)       # temporary: retry
                else:
                    raise OandaError(e.code, text)        # our mistake: don't retry
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
            # Never blindly re-send an order: it may have gone through.
            if method != "GET":
                break
            time.sleep(2 * (attempt + 1))
        raise last

    def _acct(self, suffix=""):
        return "/v3/accounts/%s%s" % (self.account, suffix)

    # ----- reads -----
    def summary(self):
        return self._req("GET", self._acct("/summary"))["account"]

    def instruments(self, pairs):
        r = self._req("GET", self._acct("/instruments"), {"instruments": ",".join(pairs)})
        return {i["name"]: i for i in r["instruments"]}

    def candles(self, pair, gran, count=None, frm=None, price="M"):
        params = {"granularity": gran, "price": price}
        if count:
            params["count"] = count
        if frm:
            params["from"] = frm
        return self._req("GET", "/v3/instruments/%s/candles" % pair, params)["candles"]

    def pricing(self, pair):
        r = self._req("GET", self._acct("/pricing"), {"instruments": pair})
        p = r["prices"][0]
        return float(p["bids"][0]["price"]), float(p["asks"][0]["price"]), p.get("tradeable", True)

    def open_trades(self):
        return self._req("GET", self._acct("/openTrades"))["trades"]

    def closed_trades(self, count=100):
        return self._req("GET", self._acct("/trades"), {"state": "CLOSED", "count": count})["trades"]

    # ----- writes -----
    def market_order(self, pair, units, sl, tp):
        body = {"order": {
            "type": "MARKET",
            "instrument": pair,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": sl, "timeInForce": "GTC"},
            "takeProfitOnFill": {"price": tp, "timeInForce": "GTC"},
        }}
        return self._req("POST", self._acct("/orders"), body=body)

    def set_stop(self, trade_id, price):
        body = {"stopLoss": {"price": price, "timeInForce": "GTC"}}
        return self._req("PUT", self._acct("/trades/%s/orders" % trade_id), body=body)

    def close_trade(self, trade_id):
        return self._req("PUT", self._acct("/trades/%s/close" % trade_id), body={"units": "ALL"})

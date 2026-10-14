# Live TopstepX streams (SignalR over a websocket): every price as it trades, and order updates the moment they happen.
# If a stream drops, it reconnects by itself; the bot keeps working off its once-a-second checks in the meantime.
import json
import threading
import time
import websocket

RS = "\x1e"                                                 # SignalR message separator


class Stream(threading.Thread):
    def __init__(self, url, token_fn, subscriptions, handlers, log, name):
        super().__init__(daemon=True)
        self.url, self.token_fn, self.subs, self.handlers, self.log, self.label = url, token_fn, subscriptions, handlers, log, name
        self.connected, self.stop_flag, self.ws, self.last_msg, self.drops = False, False, None, 0.0, 0

    def run(self):
        wait = 1
        while not self.stop_flag:
            try:
                self._session()
                wait = 1
            except Exception as e:
                if not self.stop_flag:
                    self.log(f"{self.label} stream dropped ({e}) - reconnecting in {wait} s")
            self.connected = False
            if self.stop_flag:
                break
            self.drops += 1
            time.sleep(wait)
            wait = min(wait * 2, 30)

    def _session(self):
        ws = self.ws = websocket.create_connection(f"{self.url}?access_token={self.token_fn()}", timeout=30)
        ws.send(json.dumps({"protocol": "json", "version": 1}) + RS)
        hello = ws.recv()
        if json.loads(hello.split(RS)[0] or "{}").get("error"):
            raise RuntimeError(f"handshake refused: {hello[:120]}")
        for target, args in self.subs:
            ws.send(json.dumps({"type": 1, "target": target, "arguments": args}) + RS)
        self.connected, ping = True, time.time()
        self.log(f"{self.label} stream connected")
        ws.settimeout(5)
        while not self.stop_flag:
            if time.time() - ping > 15:                     # keep-alive
                ws.send(json.dumps({"type": 6}) + RS)
                ping = time.time()
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise RuntimeError("closed by server")
            self.last_msg = time.time()
            for part in raw.split(RS):
                if not part:
                    continue
                m = json.loads(part)
                if m.get("type") == 1 and m.get("target") in self.handlers:
                    try:
                        self.handlers[m["target"]](*m.get("arguments", []))
                    except Exception as e:
                        self.log(f"{self.label} stream message skipped: {e}")
                elif m.get("type") == 7:
                    raise RuntimeError(m.get("error") or "closed by server")

    def stop(self):
        self.stop_flag = True
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass


class LiveFeed:
    """Market stream for one contract (latest price) + user stream for one account (order updates)."""

    def __init__(self, api, contract_id, account_id, log, orders=True):
        self.price, self.price_at, self.order_event = None, 0.0, threading.Event()
        rtc = "wss://rtc.topstepx.com/hubs"
        token = lambda: (api.token or api.login() or api.token)
        self.market = Stream(f"{rtc}/market", token,
                             [("SubscribeContractQuotes", [contract_id]), ("SubscribeContractTrades", [contract_id])],
                             {"GatewayQuote": self._quote, "GatewayTrade": self._trade}, log, "price")
        self.user = Stream(f"{rtc}/user", token, [("SubscribeOrders", [account_id])],
                           {"GatewayUserOrder": lambda *a: self.order_event.set()}, log, "orders") if orders else None

    def _quote(self, cid, data):
        px = (data or {}).get("lastPrice")
        if px:
            self.price, self.price_at = float(px), time.time()

    def _trade(self, cid, data):
        rows = data if isinstance(data, list) else [data]
        if rows and rows[-1] and rows[-1].get("price"):
            self.price, self.price_at = float(rows[-1]["price"]), time.time()

    def start(self):
        self.market.start()
        if self.user:
            self.user.start()

    def stop(self):
        self.market.stop()
        if self.user:
            self.user.stop()

    def fresh_price(self, max_age=5):
        return self.price if self.price is not None and time.time() - self.price_at < max_age else None

    def status(self):
        s = "live" if self.market.connected else "reconnecting"
        if self.user:
            s += ", orders live" if self.user.connected else ", orders reconnecting"
        return s

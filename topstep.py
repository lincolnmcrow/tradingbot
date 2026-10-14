# TopstepX (ProjectX Gateway API) client and a broker that sends real orders from this Mac.
# Endpoint and field names follow the ProjectX API docs; check_connection() in the app tests them without trading.
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace as NS
import requests
from AlgorithmImports import Ticket, OrderStatus

API = "https://api.topstepx.com"
ET = ZoneInfo("America/New_York")
TYPE = {"lmt": 1, "mkt": 2, "stop": 4}
STATUS_FILLED, STATUS_DEAD = 2, (3, 4, 5)        # 2 filled; 3 cancelled, 4 expired, 5 rejected


class TopstepAPI:
    def __init__(self, user, key):
        self.user, self.key, self.token, self.token_at = user, key, None, 0

    def call(self, path, body, auth=True):
        if auth and (not self.token or time.time() - self.token_at > 20 * 3600):
            self.login()
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            h["Authorization"] = f"Bearer {self.token}"
        r = requests.post(API + path, json=body, headers=h, timeout=15)
        try:
            data = r.json() if r.text else {}
        except ValueError:
            data = {"success": False, "errorMessage": r.text[:300]}
        if r.status_code >= 400 or data.get("success") is False:
            raise RuntimeError(data.get("errorMessage") or f"TopstepX {path} HTTP {r.status_code}")
        return data

    def login(self):
        d = self.call("/api/Auth/loginKey", {"userName": self.user, "apiKey": self.key}, auth=False)
        if not d.get("token"):
            raise RuntimeError("TopstepX did not return a token (check username and API key)")
        self.token, self.token_at = d["token"], time.time()

    def accounts(self):
        return [a for a in self.call("/api/Account/search", {"onlyActiveAccounts": True}).get("accounts", []) if a.get("canTrade")]

    def front_contract(self, symbol):
        cs = self.call("/api/Contract/search", {"live": False, "searchText": symbol}).get("contracts", [])
        cs = [c for c in cs if str(c.get("name", "")).upper().startswith(symbol)] or cs
        active = [c for c in cs if c.get("activeContract")] or cs
        if not active:
            raise RuntimeError(f"No TopstepX contract found for {symbol}")
        return active[0]

    def bars(self, contract_id, start_utc, end_utc, limit=20000, partial=False):
        """1-minute bars, oldest first, as bar objects with ET (New York) naive times. partial=True also returns the
        minute still in progress (its close = the latest price)."""
        d = self.call("/api/History/retrieveBars", {
            "contractId": contract_id, "live": False, "startTime": start_utc.isoformat(), "endTime": end_utc.isoformat(),
            "unit": 2, "unitNumber": 1, "limit": limit, "includePartialBar": partial})
        out = []
        for r in d.get("bars", []):
            t = datetime.fromisoformat(str(r["t"]).replace("Z", "+00:00")).astimezone(ET).replace(tzinfo=None)
            out.append(NS(time=t, end_time=t + timedelta(minutes=1), open=float(r["o"]), high=float(r["h"]),
                          low=float(r["l"]), close=float(r["c"]), volume=int(r.get("v") or 0)))
        out.sort(key=lambda b: b.time)
        return out

    def history(self, contract_id, days):
        """Paged 1-minute history for the warm-up."""
        end, out = datetime.now(timezone.utc), []
        start = end - timedelta(days=days)
        while start < end:
            stop = min(start + timedelta(days=10), end)
            out += self.bars(contract_id, start, stop)
            start = stop
        seen, uniq = set(), []
        for b in out:
            if b.time not in seen:
                seen.add(b.time)
                uniq.append(b)
        return uniq


class TopstepBroker:
    """Same interface as PaperBroker, but every order goes to the TopstepX account."""

    def __init__(self, api, account_id, contract_id, log):
        self.api, self.acc, self.contract, self.log = api, account_id, contract_id, log
        self.tickets, self.queue, self.fills = {}, [], []
        self.pos, self.cash_pts, self.last, self.now = 0, 0.0, None, None
        self.since = datetime.now(timezone.utc) - timedelta(minutes=5)

    def account_position(self):
        ps = self.api.call("/api/Position/searchOpen", {"accountId": self.acc}).get("positions", [])
        return sum((p.get("size", 0) if p.get("type") == 1 else -p.get("size", 0)) for p in ps if p.get("contractId") == self.contract)

    def place(self, qty, kind, price, tag):
        body = {"accountId": self.acc, "contractId": self.contract, "type": TYPE[kind], "side": 0 if qty > 0 else 1,
                "size": abs(qty), "limitPrice": price if kind == "lmt" else None,
                "stopPrice": price if kind == "stop" else None, "trailPrice": None, "customTag": None}
        try:
            oid = self.api.call("/api/Order/place", body)["orderId"]
        except Exception as e:
            self.log(f"ORDER REJECTED {tag} {qty} {kind} {price}: {e}")
            t = Ticket(self, -1, qty, kind, price, tag)
            t.status = OrderStatus.CANCELED
            return t
        t = Ticket(self, oid, qty, kind, price, tag)
        self.tickets[oid] = t
        self.log(f"ORDER {tag}: {'BUY' if qty > 0 else 'SELL'} {abs(qty)} {kind} {price or ''} (#{oid})")
        if kind == "mkt":                                   # wait for the fill, like QuantConnect does
            for _ in range(20):
                time.sleep(0.5)
                self._sync()
                if t.status != OrderStatus.SUBMITTED:
                    break
            if t.status == OrderStatus.SUBMITTED:
                self.log(f"market order #{oid} not filled after 10 s, cancelling")
                self.cancel(t)
                time.sleep(1)
                self._sync()
        return t

    def cancel(self, t):
        try:
            self.api.call("/api/Order/cancel", {"accountId": self.acc, "orderId": t.order_id})
            t.status = OrderStatus.CANCELED
        except Exception as e:                              # usually: it already filled; the next sync sees it
            self.log(f"cancel #{t.order_id} failed: {e}")

    def modify(self, t, p):
        try:
            self.api.call("/api/Order/modify", {"accountId": self.acc, "orderId": t.order_id, "size": abs(t.qty),
                                                "limitPrice": None, "stopPrice": p, "trailPrice": None})
            t.price = p
            self.log(f"trail {t.tag} -> {p}")
        except Exception as e:
            self.log(f"modify #{t.order_id} failed: {e}")

    def _sync(self):
        orders = self.api.call("/api/Order/search", {"accountId": self.acc, "startTimestamp": self.since.isoformat()}).get("orders", [])
        for o in orders:
            t = self.tickets.get(o.get("id"))
            if t is None or t.status == OrderStatus.FILLED:
                continue
            st = o.get("status")
            if st == STATUS_FILLED:
                px = float(o.get("filledPrice") or t.price or self.last)
                t.status, t.average_fill_price = OrderStatus.FILLED, px
                self.pos += t.qty
                self.cash_pts -= t.qty * px
                self.fills.append((datetime.now(), t.qty, px, t.tag))
                self.log(f"FILL {t.tag}: {'BUY' if t.qty > 0 else 'SELL'} {abs(t.qty)} @ {px}")
                if t.kind != "mkt":                          # resting order: the algorithm hears about it
                    self.queue.append(NS(status=OrderStatus.FILLED, order_id=t.order_id, fill_price=px, symbol=self.contract))
            elif st in STATUS_DEAD and t.status == OrderStatus.SUBMITTED:
                t.status = OrderStatus.CANCELED

    def on_bar(self, bar):
        pass                                               # the exchange fills orders; poll() reports them

    def poll(self):
        self._sync()
        q, self.queue = self.queue, []
        return q

    def open_orders(self):
        return [t for t in self.tickets.values() if t.status == OrderStatus.SUBMITTED]

    def liquidate(self, tag):
        for t in self.open_orders():
            self.cancel(t)
        self._sync()
        if self.pos:
            self.place(-self.pos, "mkt", None, tag)

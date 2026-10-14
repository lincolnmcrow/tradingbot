# A small stand-in for QuantConnect's API, so champion_plus.py (the exact algorithm tested on QuantConnect)
# runs unchanged on this Mac. Orders go to a broker object: PaperBroker (simulated fills) or
# TopstepBroker (topstep.py, real orders through the TopstepX API).
from datetime import datetime, timedelta, date
from types import SimpleNamespace as NS
from dateutil.easter import easter
from pandas.tseries.holiday import USFederalHolidayCalendar


class _Names:
    def __getattr__(self, k):
        return k


Resolution = DataNormalizationMode = DataMappingMode = _Names()
Futures = NS(Indices=NS(NASDAQ_100_E_MINI="NQ", MICRO_NASDAQ_100_E_MINI="MNQ"))


class OrderStatus:
    SUBMITTED, FILLED, CANCELED = "submitted", "filled", "canceled"


EXIT_WORDS = (" stop", " target", " time exit", "undo", "safety", "Roll", "Halt")


def is_exit(tag):
    """Exit orders are always allowed; entries can be refused by the risk guard."""
    return any(w in tag for w in EXIT_WORDS)


class Ticket:
    def __init__(self, broker, oid, qty, kind, price, tag):
        self.broker, self.order_id, self.qty, self.kind, self.price, self.tag = broker, oid, qty, kind, price, tag
        self.status, self.average_fill_price = OrderStatus.SUBMITTED, 0.0

    def cancel(self):
        if self.status == OrderStatus.SUBMITTED:
            self.broker.cancel(self)

    def update_stop_price(self, p):
        if self.status == OrderStatus.SUBMITTED:
            self.broker.modify(self, p)


def refused(tag):
    t = Ticket(None, -1, 0, "none", None, tag)
    t.status = OrderStatus.CANCELED
    return t


# ---------------------------------------------------------------- exchange calendar (CME equity index, ET)
_cache = {}


def _year(y):
    if y not in _cache:
        h = USFederalHolidayCalendar().holidays(f"{y}-01-01", f"{y}-12-31")
        # Columbus and Veterans Day trade normally; Good Friday is closed
        days = {d.date() for d in h if not (d.month == 10 and d.weekday() == 0 and 8 <= d.day <= 14)
                and not (d.month == 11 and d.day == 11)}
        days.add(easter(y) - timedelta(days=2))
        thanks = max(d for d in days if d.month == 11)
        # ponytail: holidays with a shortened Globex session are treated as closed (no new trades)
        early = {thanks + timedelta(days=1), date(y, 12, 24)}
        _cache[y] = (days, early)
    return _cache[y]


class Hours:
    def is_date_open(self, dt, ext=False):
        d = dt.date() if isinstance(dt, datetime) else dt
        return d.weekday() < 5 and d not in _year(d.year)[0]

    def get_next_market_close(self, dt, ext=False):
        d = dt.date()
        if not self.is_date_open(d):
            return datetime(d.year, d.month, d.day, 8, 0)      # closed: day ends before the open
        if d in _year(d.year)[1]:
            return datetime(d.year, d.month, d.day, 13, 15)    # early close
        return datetime(d.year, d.month, d.day, 17, 0)


# ---------------------------------------------------------------- simulated broker
class PaperBroker:
    """Fills resting orders against each new 1-minute bar (stop: at the stop, or the open if it gapped;
    limit: at the limit). Fill events are queued and handed over after the bar, like QuantConnect does."""

    def __init__(self, contract="SIM"):
        self.contract, self.tickets, self.queue, self.fills = contract, [], [], []
        self.pos, self.cash_pts, self.last, self.now, self.next_id = 0, 0.0, None, None, 0

    def place(self, qty, kind, price, tag):
        self.next_id += 1                                  # unique order number, never reused
        t = Ticket(self, self.next_id, qty, kind, price, tag)
        self.tickets.append(t)
        if kind == "mkt":
            self._fill(t, self.last)
        return t

    def cancel(self, t):
        t.status = OrderStatus.CANCELED

    def modify(self, t, p):
        t.price = p

    def _fill(self, t, px):
        t.status, t.average_fill_price = OrderStatus.FILLED, px
        self.pos += t.qty
        self.cash_pts -= t.qty * px
        self.fills.append((self.now, t.qty, px, t.tag))

    def on_bar(self, bar):
        self.tickets = [t for t in self.tickets if t.status == OrderStatus.SUBMITTED]
        for t in list(self.tickets):
            if t.status != OrderStatus.SUBMITTED or t.kind == "mkt":
                continue
            buy = t.qty > 0
            if t.kind == "stop" and ((buy and bar.high >= t.price) or (not buy and bar.low <= t.price)):
                px = max(t.price, bar.open) if buy else min(t.price, bar.open)
            elif t.kind == "lmt" and ((buy and bar.low <= t.price) or (not buy and bar.high >= t.price)):
                px = t.price
            else:
                continue
            self._fill(t, px)
            self.queue.append(NS(status=OrderStatus.FILLED, order_id=t.order_id, fill_price=px, symbol=self.contract))

    def poll(self):
        q, self.queue = self.queue, []
        return q

    def open_orders(self):
        return [t for t in self.tickets if t.status == OrderStatus.SUBMITTED]

    def liquidate(self, tag):
        for t in self.open_orders():
            t.cancel()
        if self.pos:
            self.place(-self.pos, "mkt", None, tag)


# ---------------------------------------------------------------- the QCAlgorithm subset champion_plus.py uses
class QCAlgorithm:
    broker = None          # set by engine.Runner
    guard = None
    hours = None
    contract = "SIM"
    logger = print
    is_warming_up = False

    def __init__(self):
        self.time = None
        self.schedule = NS(on=lambda *a, **k: None)
        self.date_rules = NS(every_day=lambda *a, **k: None)
        self.time_rules = NS(at=lambda *a, **k: None)

    def set_start_date(self, *a): pass
    def set_end_date(self, *a): pass
    def set_cash(self, *a): pass
    def set_benchmark(self, *a): pass
    def set_warm_up(self, *a): pass
    def plot(self, *a): pass

    def debug(self, msg):
        self.logger(msg)

    def add_future(self, product, *a, **k):
        self.fut = NS(symbol="CONT", mapped=self.contract, exchange=NS(hours=self.hours))
        return self.fut

    @property
    def portfolio(self):
        return NS(invested=self.broker.pos != 0)

    def _order(self, qty, kind, price, tag):
        if self.is_warming_up:
            return refused(tag)
        if not is_exit(tag):
            why = self.guard.refuse_entry(qty, self.session) if self.guard else None
            if why:
                self.logger(f"refused {tag}: {why}")
                return refused(tag)
        return self.broker.place(qty, kind, price, tag)

    def market_order(self, sym, qty, tag=""):
        return self._order(qty, "mkt", None, tag)

    def stop_market_order(self, sym, qty, price, tag=""):
        return self._order(qty, "stop", price, tag)

    def limit_order(self, sym, qty, price, tag=""):
        return self._order(qty, "lmt", price, tag)

    def liquidate(self, *a, tag=""):
        self.broker.liquidate(tag or "Halt")

# Page extras: trade history + stats (saved between restarts), Topstep account rule tracker, today's plan.
# None of this changes what the strategy trades.
import csv
import json
import os
from datetime import datetime, date
import numpy as np

POINT_VALUE = {"MNQ": 2.0, "NQ": 20.0}
SUFFIXES = (" entry", " stop", " target", " time exit")
GENERIC_EXITS = ("Halt", "End of day safety", "undo extra entry", "undo double exit", "Roll")


# ---------------------------------------------------------------- trade history
class TradeBook:
    """Every fill is appended to logs/fills.csv; trades are rebuilt from it by matching each exit to the oldest
    open entry of the same piece (or any piece for a risk-stop / safety close)."""
    FIELDS = ["time", "mode", "symbol", "qty", "price", "tag"]

    def __init__(self, path):
        self.path = path

    def record(self, when, mode, symbol, qty, price, tag):
        new = not os.path.exists(self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(self.FIELDS)
            w.writerow([when.strftime("%Y-%m-%d %H:%M:%S"), mode, symbol, qty, price, tag])

    def fills(self, mode=None):
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            rows = [r for r in csv.DictReader(f) if mode is None or r["mode"] == mode]
        for r in rows:
            r["qty"], r["price"] = int(r["qty"]), float(r["price"])
        return rows

    @staticmethod
    def piece(tag):
        for s in SUFFIXES:
            if tag.endswith(s):
                return tag[: -len(s)]
        return tag

    def trades(self, mode=None):
        return self.trades_from(self.fills(mode))

    @staticmethod
    def trades_from(rows):
        lots, out = [], []                                  # open lots: dict(piece, side, qty, px, time, symbol)
        for f in rows:
            tag, qty, px = f["tag"], f["qty"], f["price"]
            generic = any(tag.startswith(g) for g in GENERIC_EXITS)
            is_exit = generic or any(tag.endswith(s) for s in SUFFIXES[1:])
            name = None if generic else TradeBook.piece(tag)
            left = abs(qty)
            if is_exit:
                for lot in [l for l in lots if (name is None or l["piece"] == name) and l["side"] * qty < 0]:
                    if not left:
                        break
                    n = min(left, lot["qty"])
                    pv = POINT_VALUE.get(lot["symbol"], 2.0)
                    pts = lot["side"] * (px - lot["px"])
                    reason = "risk / safety close" if generic else tag[len(lot["piece"]):].strip()
                    out.append(dict(piece=lot["piece"], side="long" if lot["side"] > 0 else "short", qty=n,
                                    entry_time=lot["time"], entry=lot["px"], exit_time=f["time"], exit=px,
                                    reason=reason, pts=round(pts, 2), usd=round(pts * pv * n, 2)))
                    lot["qty"] -= n
                    left -= n
                lots = [l for l in lots if l["qty"] > 0]
            if left:                                        # an entry (or an exit with nothing to close)
                lots.append(dict(piece=TradeBook.piece(tag), side=1 if qty > 0 else -1, qty=left, px=px, time=f["time"],
                                 symbol=f["symbol"]))
        return out

    def summary(self, mode):
        tr = self.trades(mode)
        today = date.today().strftime("%Y-%m-%d")

        def stats(ts):
            usd = [t["usd"] for t in ts]
            wins, losses = [u for u in usd if u > 0], [u for u in usd if u <= 0]
            return dict(trades=len(ts), win_rate=round(100 * len(wins) / len(ts), 1) if ts else None,
                        net=round(sum(usd), 2), avg_win=round(np.mean(wins), 2) if wins else None,
                        avg_loss=round(np.mean(losses), 2) if losses else None,
                        profit_factor=round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
                        best=max(usd) if usd else None, worst=min(usd) if usd else None)
        eq, run = [], 0.0
        for t in tr:
            run += t["usd"]
            eq.append(dict(time=t["exit_time"], value=round(run, 2)))
        notes = Notes(os.path.join(os.path.dirname(self.path), "notes.json")).load()
        snaps = os.path.join(os.path.dirname(self.path), "snapshots")
        recent = list(reversed(tr))[:100]
        for t in recent:
            t["key"] = trade_key(t)
            t["note"] = notes.get(t["key"], "")
            t["snapshot"] = os.path.exists(os.path.join(snaps, safe_name(t["key"]) + ".png"))
        daily = {}
        for t in tr:
            daily[t["exit_time"][:10]] = round(daily.get(t["exit_time"][:10], 0.0) + t["usd"], 2)
        return dict(today=stats([t for t in tr if t["exit_time"].startswith(today)]), all=stats(tr),
                    trades=recent, equity=eq, pieces=by_piece(tr), daily=daily)


# ---------------------------------------------------------------- Topstep account rules
class AccountTracker:
    """Balance against the account's rules. Max loss trails the best end-of-day balance (as Topstep's Maximum Loss
    Limit does) and stops trailing at the starting balance. Rule numbers come from the page; check them on Topstep."""

    def __init__(self, path, account_id):
        self.path, self.key = path, str(account_id)
        self.balance, self.day_open, self.day = None, None, None
        st = self._load().get(self.key, {})
        self.peak_eod, self.eod_day = st.get("peak_eod"), st.get("eod_day")

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        st = self._load()
        st[self.key] = dict(peak_eod=self.peak_eod, eod_day=self.eod_day)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(st, f)

    def update(self, balance, now_et, session):
        if balance is None:
            return
        self.balance = balance
        if session != self.day:                             # first reading of a new session = the day's open
            self.day, self.day_open = session, balance
        today = now_et.strftime("%Y-%m-%d")
        if now_et.hour >= 16 and self.eod_day != today:     # end-of-day balance (16:00 ET onwards, once a day)
            self.peak_eod = balance if self.peak_eod is None else max(self.peak_eod, balance)
            self.eod_day = today
            self._save()

    def snapshot(self, cfg):
        start, target, mll, dll = (float(cfg.get(k) or 0) for k in ("acct_start", "profit_target", "max_loss", "daily_limit"))
        if self.balance is None or not start:
            return None
        peak = max(self.peak_eod or start, start)
        floor = min(peak - mll, start) if mll else None
        day = self.balance - (self.day_open if self.day_open is not None else self.balance)
        warn = []
        room = None if floor is None else self.balance - floor
        if room is not None and room < 0.25 * mll:
            warn.append(f"Only ${room:,.0f} left before the max loss limit (${floor:,.0f})")
        if dll and day <= -0.75 * dll:
            warn.append(f"Down ${-day:,.0f} today - daily loss limit is ${dll:,.0f}")
        return dict(balance=round(self.balance, 2), start=start, target=target, profit=round(self.balance - start, 2),
                    target_pct=round(100 * (self.balance - start) / target, 1) if target else None,
                    floor=floor, room=None if room is None else round(room, 2), day=round(day, 2),
                    daily_limit=dll or None, warnings=warn)


# ---------------------------------------------------------------- today's plan
def plan(a, extras, off=()):
    """Which pieces trade today and why, from the strategy's own state (same tests as champion_plus.open_930)."""
    d, t = a.session, a.time
    if d is None or t is None:
        return []
    hm = t.hour * 100 + t.minute if t.date() == d else 0     # evening before the session counts as early
    rows = []

    def add(piece, status, why):
        rows.append(dict(piece=piece, status=status, why=why))

    if not a.hours.is_date_open(d):
        add("All pieces", "no", "exchange holiday or shortened session")
        return rows
    if a.news:
        add("All pieces", "no", "news day: 08:30 volume was 3x its 20-day average")
        return rows
    news_wait = hm < 835
    up5 = len(a.closes) >= 6 and a.closes[-1] > a.closes[0]
    pm = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
    prior_up = pm in a.m_open and pm in a.m_close and a.m_close[pm] > a.m_open[pm]
    mon = d.weekday() == 0 and up5
    entry = "09:30" if hm < 930 else "traded 09:30"

    if d.weekday() == 0:
        add("Monday long", "yes" if up5 else "no", f"last 5 sessions {'up' if up5 else 'down'}" + (f" - buy {entry}" if up5 else ""))
    else:
        add("Monday long", "no", "not a Monday")
    if a.day_in_month <= 3:
        if mon and prior_up and "Monday long" not in off:
            add("Turn of Month", "no", "same trade as the Monday long today - one unit only")
        else:
            add("Turn of Month", "yes" if prior_up else "no",
                f"month day {a.day_in_month}, last month {'up' if prior_up else 'down'}" + (f" - buy {entry}" if prior_up else ""))
    else:
        add("Turn of Month", "no", f"month day {a.day_in_month} (needs day 1-3)")
    left = a.sessions_left(d)
    mk = (d.year, d.month)
    if left < 2:
        up_month = mk in a.m_open and bool(a.closes) and a.closes[-1] > a.m_open[mk]
        add("Month-end short", "yes" if up_month else "no",
            f"last {2 - left} of 2 month-end days, month so far {'up' if up_month else 'down'}" + (f" - short {entry}" if up_month else ""))
    else:
        add("Month-end short", "no", f"{left} sessions left in the month (needs the last 2)")
    if a.day_in_month > 3:
        add("Small Dip", "no", f"month day {a.day_in_month} (needs day 1-3)")
    elif hm < 945:
        add("Small Dip", "wait", "decided at 09:45 (dip from the 09:30 open vs half its 30-day spread)")
    else:
        add("Small Dip", "yes" if a.dip_ok else "no", "small dip - buy 09:50" if a.dip_ok else "dip too big")
    if len(a.ranges):
        trig = 0.35 * float(np.mean(a.ranges))
        move = (a.c959 - a.o900) if a.o900 is not None and a.c959 is not None else None
        if hm < 1000:
            so_far = f", move so far {move:+.1f}" if move is not None else ""
            add("Opening Drive", "wait", f"decided at 10:00: needs a 09:00-10:00 move over {trig:.1f} pts{so_far}")
        elif move is not None and abs(move) > trig:
            add("Opening Drive", "yes", f"move {move:+.1f} vs {trig:.1f} - {'buy' if move > 0 else 'sell'} 10:00")
        else:
            add("Opening Drive", "no", f"move {move:+.1f} vs {trig:.1f} needed" if move is not None else "no 09:00-10:00 data")
    if extras != "none":
        add("Thursday short", "yes" if d.weekday() == 3 else "no", f"short {entry}" if d.weekday() == 3 else "not a Thursday")
    if extras == "all":
        on = [n for n in ("ORB retest", "First FVG", "SD fade", "AMD AM", "AMD PM") if n not in off]
        if on:
            add(" / ".join(on), "watch", "limit orders placed during the day when their setups appear")
    for r in rows:
        if r["piece"] in off:
            r["status"], r["why"] = "off", "turned off in settings"
    if news_wait:
        for r in rows:
            if r["status"] == "yes":
                r["why"] += " (unless 08:35 news check says skip)"
    return rows


# ---------------------------------------------------------------- news calendar
_news = {"at": 0.0, "rows": None, "error": None}
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"   # free weekly economic calendar (Forex Factory)
NEWS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "news_cache.json")


def news(now_et):
    """This week's US (USD) medium / high impact releases in New York time. The feed is weekly and rate-limited,
    so it is fetched at most every 3 hours (1 hour after a refusal) and saved; the saved copy is shown meanwhile."""
    import time as _t
    import requests
    if _news["rows"] is None:                               # first call: start from the saved copy
        try:
            with open(NEWS_CACHE) as f:
                c = json.load(f)
            _news.update(rows=c["rows"], at=c["at"])
        except (OSError, ValueError, KeyError):
            _news["rows"] = []
    if _t.time() - _news["at"] > 3 * 3600:
        try:
            r = requests.get(NEWS_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0 (champion-bot)"})
            if r.status_code == 429:
                raise RuntimeError("the calendar site is rate-limiting; showing the saved copy, retrying in 1 hour")
            r.raise_for_status()
            _news.update(rows=r.json(), error=None, at=_t.time())
            os.makedirs(os.path.dirname(NEWS_CACHE), exist_ok=True)
            with open(NEWS_CACHE, "w") as f:
                json.dump(dict(at=_news["at"], rows=_news["rows"]), f)
        except Exception as e:
            _news.update(error=str(e) if "rate-limit" in str(e) else f"calendar not available right now ({e.__class__.__name__}); retrying in 1 hour",
                         at=_t.time() - 2 * 3600)
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    out = []
    for r in _news["rows"]:
        if r.get("country") != "USD" or r.get("impact") not in ("High", "Medium"):
            continue
        try:
            t = datetime.fromisoformat(r["date"]).astimezone(et).replace(tzinfo=None)
        except (KeyError, ValueError):
            continue
        out.append(dict(time=t.strftime("%a %b %d %H:%M"), day=t.strftime("%Y-%m-%d"), title=r.get("title", ""),
                        impact=r["impact"], forecast=r.get("forecast", ""), previous=r.get("previous", ""),
                        minutes=round((t - now_et).total_seconds() / 60)))
    out.sort(key=lambda x: x["minutes"])
    soon = [x for x in out if x["impact"] == "High" and 0 <= x["minutes"] <= 30]
    return dict(rows=out, warning=(f"High-impact news in {soon[0]['minutes']} min: {soon[0]['title']}" if soon else None),
                error=_news["error"], today=now_et.strftime("%Y-%m-%d"))


# ---------------------------------------------------------------- CSV export
def to_csv(rows, fields):
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


# ---------------------------------------------------------------- results by piece vs the backtest
# Backtest per trade, 2023-10-09 .. 2026-10-07, after 0.75 pt cost (QuantConnect-logic replay, 1 contract per piece).
BACKTEST = {
    "Opening Drive": dict(trades=168, win=68, pts=43.9), "Monday long": dict(trades=83, win=63, pts=41.9),
    "Turn of Month": dict(trades=55, win=69, pts=61.1), "Month-end short": dict(trades=49, win=65, pts=49.3),
    "Small Dip": dict(trades=35, win=66, pts=47.4), "Thursday short": dict(trades=139, win=None, pts=30.9),
    "First FVG": dict(trades=700, win=None, pts=3.0), "SD fade": dict(trades=224, win=None, pts=4.2),
    "AMD AM": dict(trades=189, win=None, pts=2.7), "AMD PM": dict(trades=86, win=None, pts=-1.1),
    "ORB retest": dict(trades=224, win=51, pts=12.9),
}


def by_piece(trades):
    out = {}
    for t in trades:
        p = out.setdefault(t["piece"], dict(piece=t["piece"], trades=0, wins=0, usd=0.0, pts=0.0))
        p["trades"] += 1
        p["wins"] += t["usd"] > 0
        p["usd"] += t["usd"]
        p["pts"] += t["pts"]
    rows = []
    for name, p in out.items():
        bt = BACKTEST.get(name)
        rows.append(dict(piece=name, trades=p["trades"], win=round(100 * p["wins"] / p["trades"]), usd=round(p["usd"], 2),
                         pts=round(p["pts"] / p["trades"], 1), bt_pts=bt["pts"] if bt else None,
                         bt_win=bt["win"] if bt else None))
    return sorted(rows, key=lambda r: -r["trades"])


# ---------------------------------------------------------------- notes and chart snapshots
def trade_key(t):
    return f"{t['entry_time']}|{t['piece']}|{t['entry']}"


def safe_name(key):
    return "".join(c if c.isalnum() else "_" for c in key)[:120]


class Notes:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def set(self, key, text):
        d = self.load()
        if text.strip():
            d[key] = text.strip()[:2000]
        else:
            d.pop(key, None)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(d, f, indent=1)


# ---------------------------------------------------------------- daily report
def daily_report(day, mode, trades, account, plan_rows, news_rows):
    """A plain-text (Markdown) summary of one trading day."""
    tt = [t for t in trades if t["exit_time"].startswith(day)]
    usd = sum(t["usd"] for t in tt)
    wins = sum(t["usd"] > 0 for t in tt)
    lines = [f"# Champion bot - {day} ({mode})", ""]
    lines.append(f"Trades: {len(tt)}   Wins: {wins}   Net: ${usd:,.2f}" + (f"   Win rate: {100 * wins / len(tt):.0f}%" if tt else ""))
    if account:
        lines.append(f"Balance: ${account['balance']:,.2f}   Profit vs start: ${account['profit']:,.2f}"
                     + (f" ({account['target_pct']}% of the ${account['target']:,.0f} target)" if account.get("target") else "")
                     + (f"   Room to max loss: ${account['room']:,.0f}" if account.get("room") is not None else ""))
    lines += ["", "## Trades", ""]
    if tt:
        lines += ["| Closed | Piece | Side | Qty | Entry | Exit | How | Pts | $ |", "|---|---|---|---|---|---|---|---|---|"]
        lines += [f"| {t['exit_time'][11:16]} | {t['piece']} | {t['side']} | {t['qty']} | {t['entry']} | {t['exit']} | {t['reason']} | {t['pts']} | {t['usd']:,.2f} |" for t in tt]
    else:
        lines.append("No trades closed today.")
    bp = by_piece(tt)
    if bp:
        lines += ["", "## By piece", "", "| Piece | Trades | Win % | $ | Pts / trade | Backtest pts / trade |", "|---|---|---|---|---|---|"]
        lines += [f"| {p['piece']} | {p['trades']} | {p['win']} | {p['usd']:,.2f} | {p['pts']} | {p['bt_pts'] if p['bt_pts'] is not None else '-'} |" for p in bp]
    if plan_rows:
        lines += ["", "## Today's plan", ""] + [f"- {r['piece']}: {r['status']} - {r['why']}" for r in plan_rows]
    nd = [n for n in news_rows if n["day"] == day]
    if nd:
        lines += ["", "## News today", ""] + [f"- {n['time'][-5:]} {n['title']} ({n['impact']})" for n in nd]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- session levels for the chart
def session_levels(a):
    """Overnight high / low, 09:30 open, 09:30-09:45 opening range, prior day high / low (prices as the bot sees them)."""
    out = []
    if a.prev_days and a.prev_days[-1]:
        p = a.prev_days[-1]
        out += [dict(name="PDH", price=max(b[3] for b in p)), dict(name="PDL", price=min(b[4] for b in p))]
    tb = a.tb
    on = [b for b in tb if b[1] >= 1800 or b[1] < 930]
    if on:
        out += [dict(name="ON high", price=max(b[3] for b in on)), dict(name="ON low", price=min(b[4] for b in on))]
    o930 = next((b for b in tb if b[1] == 930), None)
    if o930:
        out.append(dict(name="09:30 open", price=o930[2]))
    orb = [b for b in tb if 930 <= b[1] < 945]
    if len(orb) >= 15:
        out += [dict(name="OR high", price=max(b[3] for b in orb)), dict(name="OR low", price=min(b[4] for b in orb))]
    return out

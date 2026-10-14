# Runs champion_plus.py bar by bar: feeds 1-minute bars, hands fill events to the algorithm, and applies
# the risk guard. Used by the local web app (live / paper) and by replay.py (historical check).
import time
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
import AlgorithmImports as AI
import champion_plus as CP
import extras as EX

POINT_VALUE = {"MNQ": 2.0, "NQ": 20.0}


def EX_ET():
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/New_York")


class Guard:
    """Stops new entries (exits always allowed) when the day loses too much or the account draws down
    too far from its peak, and caps the number of contracts held at once."""

    def __init__(self, broker, cfg, log):
        self.b, self.log = broker, log
        self.pv = POINT_VALUE[cfg["contract"]]
        self.daily_stop, self.dd_stop, self.max_contracts = float(cfg["daily_loss_stop"]), float(cfg["max_drawdown_stop"]), int(cfg["max_contracts"])
        self.day, self.day_start, self.peak, self.halted, self.killed, self.pnl = None, 0.0, 0.0, False, False, 0.0
        self.paused = None                                  # reason text while new trades are paused for news

    def refuse_entry(self, qty, session):
        if self.killed:
            return "drawdown stop hit - restart the bot to trade again"
        if self.halted:
            return "daily loss stop hit - no new trades today"
        if self.paused:
            return f"paused for news ({self.paused})"
        if session and not AI.Hours().is_date_open(session):
            return "exchange holiday or shortened session"
        if abs(self.b.pos) + abs(qty) > self.max_contracts:
            return f"would hold more than {self.max_contracts} contracts"
        return None

    def update(self, price, session, algo):
        self.pnl = (self.b.cash_pts + self.b.pos * price) * self.pv
        if session != self.day:
            self.day, self.day_start, self.halted = session, self.pnl, False
        self.peak = max(self.peak, self.pnl)
        hit = None
        if not self.halted and self.pnl - self.day_start <= -self.daily_stop:
            self.halted, hit = True, f"daily loss stop: ${self.pnl - self.day_start:,.0f} today"
        if not self.killed and self.peak - self.pnl >= self.dd_stop:
            self.killed, hit = True, f"drawdown stop: ${self.peak - self.pnl:,.0f} below the peak"
        if hit:
            self.log(f"RISK STOP - {hit} - closing everything")
            algo.flatten_addons()                       # cancels pending entries and closes every leg
            if self.b.pos:
                self.b.liquidate("Halt")

    def snapshot(self):
        return dict(pnl=round(self.pnl, 2), day_pnl=round(self.pnl - self.day_start, 2), peak=round(self.peak, 2),
                    halted=self.halted, killed=self.killed, paused=self.paused)


class Runner:
    def __init__(self, cfg, broker, contract_id, log):
        CP.EXTRAS, CP.MICRO, CP.QTY = cfg["extras"], cfg["contract"] == "MNQ", int(cfg["qty"])
        CP.OFF = set(cfg.get("off") or [])
        self.cfg, self.b, self.log = cfg, broker, log
        self.guard = Guard(broker, cfg, log)
        a = CP.ChampionPlus()
        a.broker, a.guard, a.hours, a.contract, a.logger = broker, self.guard, AI.Hours(), contract_id, log
        a.initialize()
        self.algo, self.last_bar, self.live = a, None, None     # live = (time, price) of the minute in progress
        self.recent, self.live_bar = deque(maxlen=900), None    # chart: last 900 finished 1-minute bars + the forming one
        self.book = EX.TradeBook(cfg["book_path"]) if cfg.get("book_path") else None
        self.mode = "paper" if isinstance(broker, AI.PaperBroker) else "live"
        self.saved, self.account = len(broker.fills), None
        self.manual_count, self.manual_n = {}, 0            # manual entries per session (daily limit), group counter

    def manual_refusal(self):
        """Daily manual-trade limit and cool-down after a losing manual trade (settings on the page; 0 = off)."""
        cap, cool = int(self.cfg.get("manual_max_per_day") or 0), float(self.cfg.get("manual_cooldown_min") or 0)
        if cap and self.manual_count.get(self.algo.session, 0) >= cap:
            return f"daily manual trade limit reached ({cap})"
        if cool and self.book:
            now = datetime.now(EX_ET()).replace(tzinfo=None)
            losses = [t for t in self.book.trades(self.mode) if t["piece"] == "Manual" and t["usd"] < 0]
            if losses:
                last = datetime.strptime(losses[-1]["exit_time"], "%Y-%m-%d %H:%M:%S")
                left = cool - (now - last).total_seconds() / 60
                if left > 0:
                    return f"cool-down after a losing manual trade: {left:.0f} more minute(s)"
        return None

    def manual(self, side, stop_pts, tgt_pts, override=False, qty=None, trail=0, arm=0, tgt2=0, kind="mkt", price=None):
        """A Buy / Sell from the page or the chart. kind = mkt (now), lmt or stop (a resting order at `price`).
        Stop / targets are points from the fill; tgt2 with 2+ contracts closes half at each target; trail moves the
        stop `trail` points behind the best price once the trade is `arm` points up. Same risk guard as the bot."""
        a, t = self.algo, self.algo.time
        hm = t.hour * 100 + t.minute if t else 0
        if not override and (not (800 <= hm < 1550) or a.shadow_done):
            return self.log("manual trade refused: only between 08:00 and 15:50 ET - tick Override to trade outside those hours")
        if not stop_pts or stop_pts <= 0:
            return self.log("manual trade refused: set a stop in points")
        why = self.manual_refusal()
        if why:
            return self.log(f"manual trade refused: {why}")
        n = int(qty or CP.QTY)
        split = [(n // 2, tgt_pts), (n - n // 2, tgt2)] if tgt2 and n >= 2 else None
        trail_pts = float(trail or 0) or None
        arm_pts = (float(arm or 0) or trail_pts) if trail_pts else None
        exit_hm = 9999 if override else a.exit_hm
        paper = " - PAPER: simulated, nothing sent to Topstep" if isinstance(self.b, AI.PaperBroker) else ""
        what = (f"{n} {'BUY' if side > 0 else 'SELL'}, stop {stop_pts} pts, target {tgt_pts or 'none'}"
                + (f" / {tgt2} on half" if split else "") + (f", trail {trail_pts} after +{arm_pts}" if trail_pts else "")
                + (" (override: stays open past 15:55)" if override else ""))
        sym = a.fut.mapped
        if kind in ("lmt", "stop"):
            self.manual_n += 1
            a.place(f"Manual-{self.manual_n}", "Manual", side, CP.rnd(float(price)), None, None, 10 ** 9,
                    9999 if override else 1550, exit_hm, kind=kind, qty=n,
                    extra=dict(offsets=True, stop_pts=stop_pts, tgt_pts=tgt_pts, split=split, trail=trail_pts, arm=arm_pts,
                               keep=override))
            if not a.pend or a.pend[-1]["tk"].status != AI.OrderStatus.SUBMITTED:
                if a.pend and a.pend[-1]["name"] == "Manual":
                    a.pend.pop()
                return self.log("manual order not placed (see the line above for why)")
            self.manual_count[a.session] = self.manual_count.get(a.session, 0) + 1
            return self.log(f"MANUAL {'BUY' if side > 0 else 'SELL'} {'LIMIT' if kind == 'lmt' else 'STOP'} @ {CP.rnd(float(price))}: {what}{paper}")
        tk = a.market_order(sym, side * n, tag="Manual")
        if tk.status != AI.OrderStatus.FILLED:
            return self.log("manual trade not filled (see the line above for why)")
        px = tk.average_fill_price
        for q, tp in (split or [(n, tgt_pts)]):
            leg = a.add_leg("Manual", side, sym, px, CP.rnd(px - side * stop_pts), CP.rnd(px + side * tp) if tp else None,
                            trail_pts, arm_pts, exit_hm, qty=q)
            leg["keep"] = bool(override)                    # override: not closed at 15:55
        self.manual_count[a.session] = self.manual_count.get(a.session, 0) + 1
        self.log(f"MANUAL @ {px}: {what}{paper}")

    def breakeven(self):
        """Move the stop of every manual trade that is in profit to its entry price."""
        price = self.live[1] if self.live else (self.last_bar.close if self.last_bar else None)
        moved = 0
        for g in self.algo.legs:
            if g["name"] != "Manual" or g["stop"] is None or price is None:
                continue
            s = g["side"]
            if s * (price - g["px"]) < 2 * CP.TICK:
                self.log(f"break-even skipped: manual {'long' if s > 0 else 'short'} @ {g['px']} is not in profit yet")
                continue
            if s * (g["px"] - g["cur"]) > 0:
                g["cur"] = CP.rnd(g["px"])
                g["stop"].update_stop_price(g["cur"])
                moved += 1
        self.log(f"moved {moved} manual stop(s) to break even")

    def persist(self):
        """Append new fills to the trade history file."""
        fills = self.b.fills
        if self.book:
            now = datetime.now(EX_ET()).replace(tzinfo=None)
            for t, q, px, tag in fills[self.saved:]:
                when = t if (self.mode == "paper" and isinstance(t, datetime)) else now
                self.book.record(when, self.mode, self.cfg["contract"], q, px, tag)
        self.saved = len(fills)

    def report(self, day=None):
        """Write the daily report (logs/reports/<day>-<mode>.md) and return its file name."""
        import os
        if not self.book:
            return None
        now = datetime.now(EX_ET()).replace(tzinfo=None)
        day = day or now.strftime("%Y-%m-%d")
        acct = self.account.snapshot(self.cfg) if self.account else None
        text = EX.daily_report(day, self.mode, self.book.trades(self.mode), acct, EX.plan(self.algo, CP.EXTRAS, CP.OFF),
                               EX.news(now)["rows"])
        folder = os.path.join(os.path.dirname(self.book.path), "reports")
        os.makedirs(folder, exist_ok=True)
        name = f"{day}-{self.mode}.md"
        with open(os.path.join(folder, name), "w") as f:
            f.write(text)
        self.log(f"daily report saved: logs/reports/{name}")
        return name

    def close_manual(self):
        legs = [g for g in self.algo.legs if g["name"] == "Manual" and g["stop"] is not None]
        for g in legs:
            self.algo.close_leg(g)
        waiting = [p for p in self.algo.pend if p["name"] == "Manual"]
        for p in waiting:                                   # and cancel manual orders still waiting to fill
            p["tk"].cancel()
            p["live"] = False
        self.algo.pend = [p for p in self.algo.pend if p not in waiting]
        self.log(f"closed {len(legs)} manual trade(s), cancelled {len(waiting)} waiting manual order(s)")

    def chart(self):
        """Candles for the page's chart. Times are New York clock times written as if UTC, so the chart shows ET."""
        rows = list(self.recent) + ([self.live_bar] if self.live_bar else [])
        out = [dict(time=int((b.time - datetime(1970, 1, 1)).total_seconds()), open=b.open, high=b.high, low=b.low,
                    close=b.close, volume=b.volume) for b in rows]
        lines = [dict(name=g["name"], side=g["side"], entry=g["px"], stop=g["cur"],
                      target=(g["tgt"].price if g.get("tgt") is not None else None))
                 for g in self.algo.legs if g["stop"] is not None]
        return dict(bars=out, lines=lines, levels=EX.session_levels(self.algo))

    def deliver(self):
        for ev in self.b.poll():
            self.algo.on_order_event(ev)

    def feed(self, bar, warm=False):
        a = self.algo
        a.is_warming_up, a.time = warm, bar.end_time
        if not warm:
            self.b.now, self.b.last = bar.time, bar.open
            self.b.on_bar(bar)
            self.deliver()
        self.b.last = bar.close
        a.on_data(NS(bars={"CONT": bar}))
        if not warm:
            self.guard.update(bar.close, a.session, a)
        self.last_bar = bar
        self.recent.append(bar)

    def state(self):
        a = self.algo
        legs = [dict(name=g["name"], side="long" if g["side"] > 0 else "short", entry=g["px"],
                     stop=g["cur"], target=(g["tgt"].price if g.get("tgt") is not None else None))
                for g in a.legs if g["stop"] is not None]                 # closed legs stay in a.legs until 15:55
        pend = [dict(name=p["name"], side="long" if p["side"] > 0 else "short", price=p["tk"].price) for p in a.pend]
        lb, lv = self.last_bar, self.live
        price = lv[1] if lv else (lb.close if lb else None)
        return dict(session=str(a.session), position=self.b.pos, legs=legs, pending=pend,
                    price=price, price_time=(lv[0].strftime("%H:%M:%S") if lv else None),
                    open_pnl=(round((self.b.cash_pts + self.b.pos * price) * self.guard.pv - self.guard.day_start, 2)
                              if price is not None else None),
                    last_bar=(lb.time.strftime("%Y-%m-%d %H:%M") if lb else None), last_price=(lb.close if lb else None),
                    shadow_5d=round(sum(a.shadow), 1), guard=self.guard.snapshot(),
                    plan=EX.plan(a, CP.EXTRAS, CP.OFF),
                    account=(self.account.snapshot(self.cfg) if self.account else None))


class LiveLoop(threading.Thread):
    """Warm-up from TopstepX history, then every few seconds: report fills, feed each new closed 1-minute bar,
    and switch to the next contract at the quarterly roll (checked once a day while the market is shut)."""

    def __init__(self, cfg, api, account_id, log, paper):
        super().__init__(daemon=True)
        self.cfg, self.api, self.acc, self.log, self.paper = cfg, api, account_id, log, paper
        self.stop_flag, self.flatten_flag, self.runner, self.error, self.status = False, False, None, None, "starting"
        self.feed = None                                    # live TopstepX streams (stream.py), when available
        self.started, self.api_errors, self.sleeps, self.last_sleep, self.check = time.time(), 0, 0, None, None
        self.requests = []                                  # Buy / Sell clicks, run by this thread between bars

    def run(self):
        try:
            self._run()
        except Exception as e:                              # anything unexpected: close out, then stop
            self.error = str(e)
            self.log(f"STOPPED ON ERROR: {e}")
            self._flatten()
        self.status = "stopped"

    def _run(self):
        from topstep import TopstepBroker
        sym = self.cfg["contract"]
        c = self.api.front_contract(sym)
        self.contract = c["id"]
        self.log(f"contract {c.get('name')} ({self.contract}), account {self.acc}, {'PAPER' if self.paper else 'LIVE'} mode")
        if self.paper:
            broker = AI.PaperBroker(self.contract)
        else:
            broker = TopstepBroker(self.api, self.acc, self.contract, self.log)
            held = broker.account_position()
            if held:
                raise RuntimeError(f"the account already holds {held} {sym} - flatten it in TopstepX before starting")
        self.runner = r = Runner(self.cfg, broker, self.contract, self.log)
        r.account = EX.AccountTracker(self.cfg["account_path"], self.acc) if self.cfg.get("account_path") else None
        self.status = "warming up"
        hist = self.api.history(self.contract, 50)
        for b in hist:
            r.feed(b, warm=True)
        r.algo.is_warming_up = False                        # trading allowed from now on, even before the next bar
        self.log(f"warm-up done: {len(hist)} bars, last {hist[-1].time if hist else '-'}")
        last_t = hist[-1].time if hist else datetime.now() - timedelta(minutes=5)
        import topstep
        if r.account:
            self._balance(r)
        if hasattr(self.api, "token"):                       # real TopstepX: start the live streams
            try:
                from stream import LiveFeed
                self.feed = LiveFeed(self.api, self.contract, self.acc, self.log, orders=not self.paper)
                self.feed.start()
            except Exception as e:
                self.log(f"live stream not started ({e}) - using once-a-second checks")
        self.status, roll_day, fails, tick = "running", None, 0, 0
        next_slow, report_day, last_loop = 0.0, None, time.time()
        while not self.stop_flag:
            gap = time.time() - last_loop
            last_loop = time.time()
            if gap > 30:                                      # the Mac slept (or froze): note it; missed bars are fetched below
                self.sleeps += 1
                self.last_sleep = f"{datetime.now(EX_ET()):%H:%M} ET, {gap / 60:.0f} min"
                self.log(f"WARNING: the bot was paused for {gap / 60:.1f} min (Mac asleep?) - catching up on missed bars")
            if self.feed and self.feed.fresh_price():         # every streamed price, for the page
                r.live = (datetime.now(EX_ET()).replace(tzinfo=None), self.feed.price)
            if self.feed and self.feed.order_event.is_set() and not self.paper:
                self.feed.order_event.clear()                 # an order changed at Topstep: check fills now
                try:
                    r.deliver()
                except Exception as e:
                    self.log(f"fill check failed: {e}")
            if time.time() < next_slow and not self.requests and not self.flatten_flag:
                time.sleep(0.1)
                continue
            next_slow = time.time() + 1
            try:
                if self.flatten_flag:
                    self._flatten()
                    self.flatten_flag = False
                while self.requests:
                    kind, args = self.requests.pop(0)
                    getattr(r, kind)(*args)
                if r.account and tick % 30 == 0:              # account balance: every 30 s
                    self._balance(r)
                now_et = datetime.now(EX_ET()).replace(tzinfo=None)
                r.guard.paused = self._news_pause(now_et)
                if now_et.weekday() < 5 and now_et.hour * 100 + now_et.minute >= 1605 and report_day != now_et.date():
                    report_day = now_et.date()                 # daily report + live-vs-backtest check after 16:05 ET
                    r.report()
                    self.start_check(now_et.date())
                tick += 1
                if not self.paper and tick % 3 == 0:          # order fills: every 3 s
                    r.deliver()
                now_utc = datetime.now(timezone.utc)
                minute = datetime.now(topstep.ET).replace(second=0, microsecond=0, tzinfo=None)
                behind = (datetime.now(topstep.ET).replace(tzinfo=None) - last_t).total_seconds() / 60
                window = timedelta(minutes=min(max(15, behind + 5), 24 * 60))   # wider after a sleep, max one day
                bars = self.api.bars(self.contract, now_utc - window, now_utc, partial=True)
                streamed = self.feed.fresh_price() if self.feed else None
                for b in bars:
                    if b.time >= minute:                       # the minute in progress: only its latest price
                        if streamed:
                            b.close, b.high, b.low = streamed, max(b.high, streamed), min(b.low, streamed)
                        r.live = (datetime.now(topstep.ET).replace(tzinfo=None), b.close)
                        r.live_bar = b
                    elif b.time > last_t:                      # a finished bar: the strategy sees it
                        r.feed(b)
                        last_t = b.time
                if bars and bars[-1].time < minute and not streamed:   # quiet market: no trade yet this minute
                    r.live = (datetime.now(topstep.ET).replace(tzinfo=None), bars[-1].close)
                    r.live_bar = None
                et = r.algo.time or datetime.now()
                if 17 <= et.hour < 18 and roll_day != et.date():  # market shut 17:00-18:00 ET: check the roll
                    roll_day = et.date()
                    self._roll(sym, r)
                r.persist()
                fails, self.status = 0, "running"
            except Exception as e:                          # a network blip: retry; give up after ~1 minute
                fails += 1
                self.api_errors += 1
                self.status = f"connection problem ({fails})"
                if fails % 5 == 1:
                    self.log(f"TopstepX request failed ({fails}/60): {e}")
                if fails >= 60:
                    raise
        if self.feed:
            self.feed.stop()
        self._flatten()

    def _news_pause(self, now_et):
        """Reason text while inside the pause window around a high-impact US release, else None."""
        if not self.cfg.get("news_pause"):
            return None
        before, after = float(self.cfg.get("news_before_min") or 5), float(self.cfg.get("news_after_min") or 10)
        for n in EX.news(now_et)["rows"]:
            if n["impact"] == "High" and -after <= n["minutes"] <= before:
                return f"{n['title']} at {n['time'][-5:]}"
        return None

    def health(self):
        r = self.runner
        lb = r.last_bar if r else None
        now = datetime.now(EX_ET()).replace(tzinfo=None)
        bar_age = (now - lb.end_time).total_seconds() if lb else None
        wd, hr = now.weekday(), now.hour                     # CME Globex: Sun 18:00 - Fri 17:00 ET, daily break 17-18
        open_now = not (wd == 5 or (wd == 6 and hr < 18) or (wd == 4 and hr >= 17) or 17 <= hr < 18)
        price_age = (time.time() - self.feed.price_at) if self.feed and self.feed.price_at else None
        warn = []
        if open_now and bar_age is not None and bar_age > 180:
            warn.append(f"no new 1-minute bar for {bar_age / 60:.0f} min")
        if self.feed and not self.feed.market.connected:
            warn.append("price stream reconnecting")
        return dict(uptime_min=round((time.time() - self.started) / 60), bar_age_s=None if bar_age is None else round(bar_age),
                    price_age_s=None if price_age is None else round(price_age, 1), api_errors=self.api_errors,
                    stream_drops=(self.feed.market.drops + (self.feed.user.drops if self.feed.user else 0)) if self.feed else None,
                    sleeps=self.sleeps, last_sleep=self.last_sleep, warnings=warn)

    def start_check(self, day):
        """Replay `day` through the backtest engine (separate thread) and compare with what the bot traded."""
        def run():
            try:
                self.check = dict(day=str(day), status="running")
                self.check = backtest_day(self.api, self.contract, self.cfg, day, self.runner, self.log)
            except Exception as e:
                self.check = dict(day=str(day), status="failed", error=str(e))
                self.log(f"live-vs-backtest check failed: {e}")
        threading.Thread(target=run, daemon=True).start()

    def _balance(self, r):
        now = datetime.now(EX_ET()).replace(tzinfo=None)
        if self.paper:                                      # paper: starting balance + the bot's own result
            bal = float(self.cfg.get("acct_start") or 0) + r.guard.pnl
        else:
            accs = self.api.call("/api/Account/search", {"onlyActiveAccounts": True}).get("accounts", [])
            bal = next((float(x["balance"]) for x in accs if x.get("id") == self.acc and x.get("balance") is not None), None)
        r.account.update(bal, now, r.algo.session)

    def _roll(self, sym, r):
        c = self.api.front_contract(sym)
        if c["id"] == self.contract:
            return
        now = datetime.now(timezone.utc)
        old = self.api.bars(self.contract, now - timedelta(days=3), now)
        new = self.api.bars(c["id"], now - timedelta(days=3), now)
        if old and new:
            r.algo.adjust(new[-1].close - old[-1].close)
        self.log(f"ROLL {self.contract} -> {c['id']}")
        self.contract = c["id"]
        r.algo.contract = r.algo.fut.mapped = r.b.contract = c["id"]

    def _flatten(self):
        if self.runner:
            try:
                self.runner.algo.flatten_addons()
                if self.runner.b.pos:
                    self.runner.b.liquidate("Halt")
                self.log("flattened")
                self.runner.persist()
            except Exception as e:
                self.log(f"FLATTEN FAILED: {e} - close the position in TopstepX yourself")


def backtest_day(api, contract, cfg, day, live_runner, log):
    """What the backtest engine trades on `day` (same code, simulated fills, 50 days of history before it) vs the
    bot's own trades that day. Manual trades are left out. Saved to logs/checks/<day>-<mode>.json."""
    import json, os
    hist = api.history(contract, 55)
    sess = lambda b: (b.time + timedelta(hours=6)).date()
    c = dict(cfg, book_path=None, account_path=None, daily_loss_stop=1e12, max_drawdown_stop=1e12, max_contracts=99)
    r = Runner(c, AI.PaperBroker(contract), contract, lambda m: None)
    for b in hist:
        s = sess(b)
        if s > day:
            break
        r.feed(b, warm=s < day)
        if s == day:
            r.algo.is_warming_up = False
    rows = [dict(time=t.strftime("%Y-%m-%d %H:%M:%S"), qty=q, price=px, tag=tag, symbol=cfg["contract"])
            for t, q, px, tag in r.b.fills]
    bt = [t for t in EX.TradeBook.trades_from(rows) if t["piece"] != "Manual"]
    live = [t for t in (live_runner.book.trades(live_runner.mode) if live_runner and live_runner.book else [])
            if t["piece"] != "Manual" and t["entry_time"].startswith(str(day))]
    out, pieces = [], sorted({t["piece"] for t in bt + live})
    for p in pieces:
        a = [t for t in bt if t["piece"] == p]
        b = [t for t in live if t["piece"] == p]
        for i in range(max(len(a), len(b))):
            x, y = (a[i] if i < len(a) else None), (b[i] if i < len(b) else None)
            slip = None
            if x and y:
                s = 1 if x["side"] == "long" else -1
                slip = round(s * (y["entry"] - x["entry"]) + s * (x["exit"] - y["exit"]), 2)   # points lost to fills
            out.append(dict(piece=p, backtest=x, live=y, match=bool(x and y and x["side"] == y["side"]), slip_pts=slip))
    res = dict(day=str(day), status="done", mode=live_runner.mode if live_runner else None, rows=out,
               bt_pts=round(sum(t["pts"] for t in bt), 2), live_pts=round(sum(t["pts"] for t in live), 2),
               matched=sum(o["match"] for o in out), total=len(out))
    if live_runner and live_runner.book:
        folder = os.path.join(os.path.dirname(live_runner.book.path), "checks")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, f"{day}-{res['mode']}.json"), "w") as f:
            json.dump(res, f, indent=1)
    log(f"live-vs-backtest {day}: {res['matched']}/{res['total']} trades matched, backtest {res['bt_pts']} pts, bot {res['live_pts']} pts")
    return res

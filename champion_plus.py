# Research-loop CHAMPION (iteration 35) + add-ons, for QuantConnect. NO Opening Drive filter.
# OD2: Opening Drive upgrades from the 2026-10-13 tests (2015-2023 + 2023-2026), each with its own switch below.
# NQ E-mini, 1 contract per piece, $100,000, last 3 years, minute data.
#
# EXTRAS = "none" -> Champion only (5 pieces)
# EXTRAS = "thu" -> Champion + Thursday short
# EXTRAS = "all" -> Champion + Thursday short + AMD tight-Asia + SD fade Asia 2.5 + First FVG 09:30
EXTRAS = "all"
MICRO = False        # True = trade MNQ micro contracts ($2/pt) instead of NQ ($20/pt) - use this to start live
OFF = set()          # pieces to leave out, by name, e.g. {"Thursday short", "AMD PM"}
#
# Opening Drive upgrades (set to None / False to switch one off; all off = the old Opening Drive):
OD_ON_TRIGGER = 0.2  # no drive signal, but the 09:59 close is past the overnight (18:00-09:29) high / low by 0.2 x range
                     # -> trade that way at 10:00, same stop / trail
OD_STOP_CAP = 0.6    # stop no further than 0.6 x range from the fill (also used for the re-entry)
OD_REENTRY = True    # once per day: after the Opening Drive trade is stopped / trailed out before 15:30, buy (sell) a
                     # break of the day's high (low) since 09:00; stop past the low (high) since the exit, same trail
# v5 rules (found in the 2015-2026 research; each one can be switched off from the page / settings.json "v5"):
PDL_SKIP = True      # Thursday short / Month-end short: skip when the overnight range (18:00-09:29) traded below the prior RTH low
FVG_BIAS = 0.35      # First FVG: only gaps in the direction of the 09:00 move when it is >= 0.35 x range (None = every gap)
ORB_R = 3.0          # ORB retest target in R (2.0 was the old value)
DIP_ANY = True       # Small Dip on any day with a 1.5% stop and no target (False = old: first 3 sessions, -0.25% / +0.5%)
DIP_STOP = 0.015
BIGDIP = 1.5         # Big Dip Short: short 09:50 when the 09:30-09:44 dip is more than 1.5 x its 30-day spread (None = off)
BIGDIP_STOP = 0.01
MIDTRIG = 0.5        # Midday Follow: 11:00-11:30, first time the move since 09:00 is over 0.5 x range, trade that way (None = off)
MID_T0, MID_T1 = 1100, 1130
OD_VIX_SKIP = None   # e.g. 0.30 = no Opening Drive if VIX rose > 30% over the last 5 days (CBOE data; optional)
#
# Champion (same as champion.py):
#  0. News skip: no trades if 08:30-08:34 volume >= 3 x its 20-day average (applies to every piece).
#  1. Monday long 09:30 -> 15:55, 1.5% stop, only if the last 5 sessions closed up.
#  2. Turn of Month long (first 3 sessions) 09:30 -> 15:55, 1% stop, only if last month closed up (not with Monday).
#  3. Small Dip (first 3 sessions): dip = 09:30 open - lowest low 09:30-09:44 <= 0.5 x std of the previous 30 dips
#     -> buy 09:50, target +0.5%, stop -0.25%.
#  4. Opening Drive: |09:00-10:00 move| > 0.35 x 20-day avg 09:00-16:00 range -> trade that way at 10:00, stop 1 tick
#     past the first-hour extreme, trail 0.4 x range once +0.5 x range. Always taken (the old 5-session shadow
#     filter is removed: 2023-2026 replay +2,212 pts more without it, max drawdown slightly deeper).
#  5. Month-end short: last 2 sessions of a month that is up so far, short 09:30, 1% stop, out 15:55.
# Add-ons (not part of the shadow):
#  6. Thursday short: every Thursday, short 09:30 -> 15:55, 1% stop.
#  7. SD fade Asia 2.5: Asia range = 18:00-23:59 (>= 30 bars). 08:30-15:30 fade the first touch of high + 2.5 x size
#     (short) / low - 2.5 x size (long) that was not reached 00:00-08:29. Limit at the level, stop at 4 x size,
#     target the range edge, out 15:55.
#  8. First FVG 09:30-10:30: every 1-minute FVG gets a limit at its midpoint for 30 bars; the first fill is the day's
#     trade. Stop 1 tick past the FVG's first candle, target 2R, out 15:55.
#  9. AMD tight-Asia: only when the Asia range <= 0.8 x its 20-day average. Windows AM 08:30-11:00 (out 15:00) and
#     PM 13:30-15:30 (out 15:55), one trade each. Manipulation = price runs an untaken session level (prior day,
#     Asia, London, + NY AM for PM) or a fresh 15m / 1h FVG; then a 1-minute reversal FVG within 20 bars; limit at its
#     midpoint for 30 bars, stop 1 tick past the extreme, target the nearest untaken opposite level >= 1R (or the
#     midnight open).
# 10. ORB retest (longs only): 09:30-09:59 range; the first 10-minute close outside it by 12:00 must be ABOVE it.
#     Then a limit buy at the 2-minute 20 EMA (last finished 2-min bar) until 13:00. Only when the prior close is above
#     its 20-day average. Stop 0.25 x 20-day average 09:30-16:00 range below the fill, target 2R, out 15:55.
from AlgorithmImports import *
from collections import deque
import numpy as np

TICK, COST, QTY = 0.25, 0.75, 1
EPOCH = datetime(1970, 1, 1)
O, H, L, C = 2, 3, 4, 5                     # positions in a stored bar (lm, hm, o, h, l, c)


def rnd(p):
    return round(p / TICK) * TICK


def hm_minus(hm, m):
    t = hm // 100 * 60 + hm % 100 - m
    return t // 60 * 100 + t % 60


def htf_zones(bars, ws_lm, width):
    """Fresh 15m / 1h FVGs before the window opens: list of (side, tag price). side -1 = bearish zone above."""
    if len(bars) < 3 * width:
        return []
    lm = np.array([b[0] for b in bars]); h = np.array([b[H] for b in bars]); l = np.array([b[L] for b in bars])
    c = np.array([b[C] for b in bars])
    bk = lm // width
    st = np.flatnonzero(np.r_[True, bk[1:] != bk[:-1]]); en = np.r_[st[1:], len(bk)] - 1
    B, Hb, Lb, Cb = bk[st], np.maximum.reduceat(h, st), np.minimum.reduceat(l, st), c[en]
    sufmax = np.maximum.accumulate(h[::-1])[::-1]; sufmin = np.minimum.accumulate(l[::-1])[::-1]
    out = []
    for i in range(2, len(B)):
        if (B[i] + 1) * width > ws_lm or B[i - 1] != B[i] - 1 or B[i - 2] != B[i] - 2:
            continue
        e = en[i] + 1
        if Hb[i] < Lb[i - 2] and Lb[i - 2] - Hb[i] >= 0.0003 * Cb[i]:
            if e >= len(h) or sufmax[e] < Hb[i]:
                out.append((-1, Hb[i]))
        elif Lb[i] > Hb[i - 2] and Lb[i] - Hb[i - 2] >= 0.0003 * Cb[i]:
            if e >= len(l) or sufmin[e] > Lb[i]:
                out.append((1, Lb[i]))
    return out


class ChampionPlus(QCAlgorithm):

    def initialize(self):
        self.set_start_date(2023, 10, 9)
        self.set_end_date(2026, 10, 9)
        self.set_cash(100000)
        self.fut = self.add_future(
            Futures.Indices.MICRO_NASDAQ_100_E_MINI if MICRO else Futures.Indices.NASDAQ_100_E_MINI, Resolution.MINUTE,
            extended_market_hours=True,
            data_normalization_mode=DataNormalizationMode.RAW,
            data_mapping_mode=DataMappingMode.OPEN_INTEREST,
            contract_depth_offset=0)
        self.hours = self.fut.exchange.hours
        self.set_warm_up(timedelta(days=50))   # 30 sessions of history before the first trade (needed live)

        self.set_benchmark("QQQ")
        self.bench_start = None
        self.schedule.on(self.date_rules.every_day(), self.time_rules.at(16, 5), self.plot_bench)

        self.closes = deque(maxlen=6)
        self.m_open, self.m_close = {}, {}
        self.v830 = deque(maxlen=20)
        self.dips = deque(maxlen=30)
        self.ranges = deque(maxlen=20)
        self.asia_ranges = deque(maxlen=20)
        self.rth_ranges, self.c20 = deque(maxlen=20), deque(maxlen=20)   # ORB retest: ATR and daily trend
        self.shadow = deque(maxlen=5)
        self.prev_days = deque(maxlen=7)       # bars of the previous sessions (roll-adjusted)
        self.session, self.day_in_month = None, 0
        self.tb, self.legs, self.vlegs, self.pend = [], [], [], []
        self.entries = {}                      # every add-on entry order ever placed: order id -> details
        self.a_h, self.a_l, self.a_n = -1e18, 1e18, 0  # Asia range (set here too: a roll can come before day 1)
        self.rth_h = self.rth_l = None
        self.orb = dict(h=-1e18, l=1e18, side=0, te=None, ema=None, bk=None, last=None, atr=None)
        self.on_h, self.on_l = -1e18, 1e18
        self.od_re = None
        self.pd_low = None                     # prior session RTH low (PDL_SKIP)
        self.vix, self.vix_hist = None, deque(maxlen=6)
        if OD_VIX_SKIP is not None:
            self.vix = self.add_data(CBOE, "VIX", Resolution.DAILY).symbol

    # ---------- helpers
    @staticmethod
    def hm(t):
        return t.hour * 100 + t.minute

    def sessions_left(self, d):
        n, x = 0, d + timedelta(days=1)
        while x.month == d.month:
            if x.weekday() < 5 and self.hours.is_date_open(datetime(x.year, x.month, x.day), False):
                n += 1
            x += timedelta(days=1)
        return n

    def new_session(self, d):
        if self.session is not None:
            if self.rng_h is not None:
                self.ranges.append(self.rng_h - self.rng_l)
            if self.a_n:
                self.asia_ranges.append(self.a_h - self.a_l)
            if self.rth_h is not None:
                self.rth_ranges.append(self.rth_h - self.rth_l)
                self.pd_low = self.rth_l
            if not self.shadow_done:
                self.finish_day(None)
            self.prev_days.append(self.tb)
        self.day_in_month = self.day_in_month + 1 if self.session and self.session.month == d.month else 1
        self.session = d
        close = self.hours.get_next_market_close(datetime(d.year, d.month, d.day, 8, 0), False)
        self.exit_hm = 1555
        if close.date() == d:
            self.exit_hm = min(1555, self.hm(close - timedelta(minutes=5)))
        self.news, self.dip_ok, self.shadow_done = False, False, False
        self.big_dip, self.mid_done = False, False
        self.o900 = self.o930 = self.c959 = None
        self.fh_h, self.fh_l, self.fh_n = -1e18, 1e18, 0
        self.d_low, self.d_n, self.vol830 = 1e18, 0, 0
        self.rng_h = self.rng_l = None
        self.rth_h = self.rth_l = None
        ok = len(self.c20) == 20 and self.c20[-1] > np.mean(self.c20) and len(self.rth_ranges) == 20
        self.orb = dict(h=-1e18, l=1e18, side=0, te=None, ema=None, bk=None, last=None,
                        atr=float(np.mean(self.rth_ranges)) if ok else None)
        kept = [g for g in self.legs if g.get("keep") and g["stop"] is not None]   # override trades carry over
        kept_p = [p for p in self.pend if p.get("keep") and p["tk"].status == OrderStatus.SUBMITTED]
        self.tb, self.legs, self.vlegs, self.pend = [], kept, [], kept_p
        self.done = set()                      # add-on groups that already traded today
        self.a_h, self.a_l, self.a_n = -1e18, 1e18, 0
        self.mopen, self.sd_lv, self.fvg_ws, self.amd = None, None, None, {}
        self.on_h, self.on_l = -1e18, 1e18
        self.od_re = None                      # Opening Drive re-entry state for today

    # ---------- data
    def on_data(self, slice):
        if self.vix is not None and slice.contains_key(self.vix):
            self.vix_hist.append(float(slice[self.vix].close))
        bar = slice.bars.get(self.fut.symbol)
        if bar is None:
            return
        d = (bar.time + timedelta(hours=6)).date()
        if d != self.session:
            self.new_session(d)
        h = self.hm(bar.time)
        mkey = (d.year, d.month)
        self.tb.append((int((bar.time - EPOCH).total_seconds() // 60), h, bar.open, bar.high, bar.low, bar.close))
        j = len(self.tb) - 1

        if h >= 1800:
            self.a_h, self.a_l, self.a_n = max(self.a_h, bar.high), min(self.a_l, bar.low), self.a_n + 1
        elif self.mopen is None:
            self.mopen = bar.open
        if h >= 1800 or h < 930:
            self.on_h, self.on_l = max(self.on_h, bar.high), min(self.on_l, bar.low)
        if 830 <= h < 835:
            self.vol830 += bar.volume
        if 900 <= h < 1600:
            self.rng_h = bar.high if self.rng_h is None else max(self.rng_h, bar.high)
            self.rng_l = bar.low if self.rng_l is None else min(self.rng_l, bar.low)
        if 930 <= h < 1600:
            self.rth_h = bar.high if self.rth_h is None else max(self.rth_h, bar.high)
            self.rth_l = bar.low if self.rth_l is None else min(self.rth_l, bar.low)
        if 900 <= h < 1000:
            self.o900 = bar.open if self.o900 is None else self.o900
            self.fh_h, self.fh_l, self.fh_n, self.c959 = max(self.fh_h, bar.high), min(self.fh_l, bar.low), self.fh_n + 1, bar.close
        if h == 930:
            self.o930 = bar.open
            self.m_open.setdefault(mkey, bar.open)
            self.fvg_ws = j
        if 930 <= h < 945:
            self.d_low, self.d_n = min(self.d_low, bar.low), self.d_n + 1

        self.sim_bar(bar, h)
        self.trail_real(bar)
        self.od_reentry_bar(j, h)
        if EXTRAS == "all":
            self.addons_bar(j, h)

        if h == 834:
            hist = list(self.v830)
            self.news = len(hist) >= 10 and self.vol830 >= 3 * np.mean(hist)
            self.v830.append(self.vol830)
            if self.news:                                   # add-on orders from 08:30 are cancelled / closed
                self.flatten_addons()
        if h == 944:
            if self.d_n >= 14 and self.o930 is not None:
                dip = self.o930 - self.d_low
                sd = np.std(self.dips, ddof=1) if len(self.dips) >= 2 else np.nan
                self.dip_ok = bool(np.isfinite(sd) and dip <= 0.5 * sd and (self.day_in_month <= 3 or DIP_ANY))
                self.big_dip = bool(BIGDIP is not None and np.isfinite(sd) and dip > BIGDIP * sd)
                self.dips.append(dip)
        if h == 1559:
            self.closes.append(bar.close)
            self.c20.append(bar.close)
            self.m_close[mkey] = bar.close

        nxt = self.hm(bar.end_time)
        if self.news:
            pass
        elif nxt == 930:
            self.open_930(d)
        elif nxt == 950 and self.dip_ok:
            if DIP_ANY:
                self.enter("Small Dip", 1, 950, stop_pct=DIP_STOP)
            else:
                self.enter("Small Dip", 1, 950, stop_pct=0.0025, tgt_pct=0.005)
        elif nxt == 950 and self.big_dip:
            self.enter("Big Dip Short", -1, 950, stop_pct=BIGDIP_STOP)
        elif MIDTRIG is not None and not self.mid_done and MID_T0 < nxt <= MID_T1 and self.o900 is not None and len(self.ranges) >= 1 \
                and abs(bar.close - self.o900) > MIDTRIG * float(np.mean(self.ranges)):
            self.mid_done = True
            self.enter("Midday Follow", 1 if bar.close > self.o900 else -1, nxt, stop_pct=0.01)
        elif nxt == 1000 and self.fh_n >= 55 and len(self.ranges) >= 1:
            atr = float(np.mean(self.ranges))
            move = self.c959 - self.o900
            side = 0
            if abs(move) > 0.35 * atr:
                side = 1 if move > 0 else -1
            elif OD_ON_TRIGGER is not None:
                if self.c959 > self.on_h + OD_ON_TRIGGER * atr:
                    side = 1
                elif self.c959 < self.on_l - OD_ON_TRIGGER * atr:
                    side = -1
            if side and self.vix_spike():
                self.debug(f"{self.session} Opening Drive skipped: VIX up over {OD_VIX_SKIP:.0%} in 5 days")
                side = 0
            if side:
                stop = self.fh_l - TICK if side > 0 else self.fh_h + TICK
                cap = OD_STOP_CAP * atr if OD_STOP_CAP is not None else None
                self.enter("Opening Drive", side, 1000, stop=stop, trail=0.4 * atr, arm=0.5 * atr, cap=cap)
                if OD_REENTRY:
                    self.od_re = dict(side=side, atr=atr, state="armed")
        if EXTRAS == "all" and not self.news:
            if nxt == 830:
                self.setup_sd()
                self.setup_amd("AM", 1100, 1500, ["PD", "Asia", "London"])
            elif nxt == 1330:
                self.setup_amd("PM", 1530, 1555, ["PD", "Asia", "London", "NYAM"])
        self.expire(j, nxt)
        for g in list(self.legs):                           # AMD AM leaves at 15:00
            if g["exit"] < self.exit_hm and nxt == g["exit"]:
                self.close_leg(g)
        if nxt == self.exit_hm:
            self.finish_day(bar)

    def pdl_swept(self):
        return bool(PDL_SKIP and self.pd_low is not None and self.on_l < self.pd_low)

    def open_930(self, d):
        up5 = len(self.closes) >= 6 and self.closes[-1] > self.closes[0]
        pm = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
        # ponytail: daily/month up-down uses 15:59 closes and the 09:30 open, not settlement / 18:00 open (tiny difference)
        prior_up = pm in self.m_open and pm in self.m_close and self.m_close[pm] > self.m_open[pm]
        mon = d.weekday() == 0 and up5
        if mon:
            self.enter("Monday long", 1, 930, stop_pct=0.015)
        if self.day_in_month <= 3 and prior_up and not (mon and "Monday long" not in OFF):
            self.enter("Turn of Month", 1, 930, stop_pct=0.01)
        mk = (d.year, d.month)
        if self.sessions_left(d) < 2 and mk in self.m_open and self.closes and self.closes[-1] > self.m_open[mk]:
            if not self.pdl_swept():
                self.enter("Month-end short", -1, 930, stop_pct=0.01)
        if EXTRAS != "none" and d.weekday() == 3:
            if not self.pdl_swept():
                self.enter("Thursday short", -1, 930, stop_pct=0.01, shadow=False)

    # ---------- add-ons: SD fade, First FVG, AMD
    def setup_sd(self):
        if self.a_n < 30 or self.a_h <= self.a_l:
            return
        hi, lo = self.a_h, self.a_l
        size = hi - lo
        night = [b for b in self.tb if b[1] < 1800]         # 00:00-08:29
        for side, lvl, edge in ((-1, hi + 2.5 * size, hi), (1, lo - 2.5 * size, lo)):
            before = any((b[H] >= lvl) if side < 0 else (b[L] <= lvl) for b in night)
            if not before:
                self.place("SD", "SD fade", side, rnd(lvl), rnd(edge - side * 4 * size), rnd(edge), 10 ** 9, 1530, 1555)

    def setup_amd(self, w, end, ex, names):
        if len(self.asia_ranges) < 20 or not self.a_n or (self.a_h - self.a_l) > 0.8 * np.mean(self.asia_ranges):
            return
        tb, ws_lm = self.tb, self.tb[-1][0] + 1
        sess = {}
        if self.prev_days and len(self.prev_days[-1]) >= 5:
            p = self.prev_days[-1]
            sess["PD"] = (max(b[H] for b in p), min(b[L] for b in p), p[-1][0])
        masks = {"Asia": lambda b: b[1] >= 1800, "London": lambda b: b[1] < 500, "NYAM": lambda b: 930 <= b[1] < 1200}
        for n in names:
            if n in masks:
                bs = [b for b in tb if masks[n](b)]
                if len(bs) >= 5:
                    sess[n] = (max(b[H] for b in bs), min(b[L] for b in bs), bs[-1][0])
        levels, watch = [], []
        for n in names:
            if n not in sess:
                continue
            Hh, Ll, end_lm = sess[n]
            after = [b for b in tb if b[0] > end_lm]
            lv = dict(H=Hh, L=Ll, end=end_lm, hi_t=any(b[H] >= Hh for b in after), lo_t=any(b[L] <= Ll for b in after))
            levels.append(lv)
            if not lv["hi_t"]:
                watch.append((-1, Hh, True))
            if not lv["lo_t"]:
                watch.append((1, Ll, True))
        hist15 = [b for day in list(self.prev_days)[-2:] for b in day] + tb
        hist60 = [b for day in list(self.prev_days)[-6:] for b in day] + tb
        for side, p in htf_zones(hist15, ws_lm, 15) + htf_zones(hist60, ws_lm, 60):
            watch.append((side, p, False))
        self.amd[w] = dict(end=end, ex=ex, levels=levels, watch=watch, events={})

    def addons_bar(self, j, h):
        b = self.tb[j]
        # First FVG 09:30-10:30
        if self.fvg_ws is not None and j >= self.fvg_ws + 2 and h < 1030 and "FVG" not in self.done and not self.news:
            b0 = self.tb[j - 2]
            side = 1 if b[L] > b0[H] else -1 if b[H] < b0[L] else 0
            if side:
                zlo, zhi = (b0[H], b[L]) if side > 0 else (b[H], b0[L])
                px = rnd((zlo + zhi) / 2)
                stop = b0[L] - TICK if side > 0 else b0[H] + TICK
                risk = abs(px - stop)
                # ponytail: FVGs with < 1 pt risk are skipped (research lets them fill and then takes no trade)
                bias_ok = FVG_BIAS is None or bool(self.o900 is not None and len(self.ranges) >= 1 and
                                                   side * (b[C] - self.o900) > FVG_BIAS * float(np.mean(self.ranges)))
                if risk >= 4 * TICK and bias_ok:
                    self.place("FVG", "First FVG", side, px, stop, px + side * 2 * risk, j + 30, 1555, 1555)
        self.orb_bar(b, h)
        # AMD windows
        for w, s in self.amd.items():
            for lv in s["levels"]:                          # untaken-level bookkeeping for targets
                if b[0] > lv["end"]:
                    lv["hi_t"] |= b[H] >= lv["H"]
                    lv["lo_t"] |= b[L] <= lv["L"]
            if h < s["end"]:
                for k, (side, p, strict) in enumerate(s["watch"]):
                    if p is None:
                        continue
                    hit = (b[H] > p if strict else b[H] >= p) if side < 0 else (b[L] < p if strict else b[L] <= p)
                    if hit:
                        s["watch"][k] = (side, None, strict)
                        s["events"].setdefault((j, side), dict(s=j, side=side, ext=b[H] if side < 0 else b[L], done=False))
            for ev in s["events"].values():
                i = j
                if ev["done"] or i < ev["s"] + 2:
                    continue
                if i > ev["s"] + 20 or h >= s["end"]:
                    ev["done"] = True
                    continue
                side = ev["side"]
                ev["ext"] = max(ev["ext"], b[H]) if side < 0 else min(ev["ext"], b[L])
                b0 = self.tb[i - 2]
                if side < 0 and b0[L] > b[H]:
                    zlo, zhi = b[H], b0[L]
                elif side > 0 and b0[H] < b[L]:
                    zlo, zhi = b0[H], b[L]
                else:
                    continue
                ev["done"] = True
                px = rnd((zlo + zhi) / 2)
                stop = ev["ext"] + TICK if side < 0 else ev["ext"] - TICK
                risk = abs(stop - px)
                if risk < 4 * TICK:
                    continue
                # ponytail: target picked when the order is placed (research: at the fill bar, a few bars later)
                opp = [lv["L"] for lv in s["levels"] if side < 0 and not lv["lo_t"] and lv["L"] < px - risk] + \
                      [lv["H"] for lv in s["levels"] if side > 0 and not lv["hi_t"] and lv["H"] > px + risk]
                mo = self.mopen
                if mo is not None and ((side < 0 and mo < px - risk) or (side > 0 and mo > px + risk)):
                    opp.append(mo)
                if opp:
                    self.place("AMD-" + w, "AMD " + w, side, px, stop, max(opp) if side < 0 else min(opp),
                               i + 30, hm_minus(s["ex"], 1), s["ex"])

    def orb_bar(self, b, h):
        o = self.orb
        if 930 <= h < 1000:
            o["h"], o["l"] = max(o["h"], b[H]), min(o["l"], b[L])
        bk = b[0] // 2                                      # 2-minute bars start on even minutes
        if o["bk"] is not None and bk != o["bk"] and o["last"] is not None:   # previous 2-min bar missed its 2nd minute
            o["ema"] = o["last"] if o["ema"] is None else o["ema"] + (o["last"] - o["ema"]) * 2 / 21
            o["last"] = None
        o["bk"], o["last"] = bk, b[C]
        if b[0] % 2 == 0:
            return
        o["ema"] = b[C] if o["ema"] is None else o["ema"] + (b[C] - o["ema"]) * 2 / 21
        o["last"] = None
        nx = hm_minus(h, -1)
        if o["side"] == 0 and 1000 <= h < 1200 and h % 10 == 9 and o["l"] < 1e17:
            o["side"] = -1 if b[C] < o["l"] else 1 if b[C] > o["h"] else 0   # first breakout down: no trade today
            if o["side"] > 0:
                o["te"] = nx
        if o["side"] < 1 or o["atr"] is None or self.news or "ORB" in self.done or nx >= 1300:
            return
        px = rnd(o["ema"])
        cur = [p for p in self.pend if p["group"] == "ORB"]
        if cur and cur[0]["extra"].get("px") == px:
            return
        for p in cur:                                       # move the limit to the new EMA
            if p["tk"].status != OrderStatus.FILLED:
                p["tk"].cancel()
                p["live"] = False
                self.pend.remove(p)
        if any(p["group"] == "ORB" for p in self.pend):
            return
        self.place("ORB", "ORB retest", 1, px, None, None, 10 ** 9, 1300, 1555,
                   extra=dict(offsets=True, px=px, stop_pts=0.25 * o["atr"], tgt_pts=ORB_R * 0.25 * o["atr"]))

    def place(self, group, name, side, px, stop, tgt, last_j, cancel_hm, exit_hm, kind="lmt", qty=None, extra=None):
        if group in self.done or self.is_warming_up or name in OFF:
            return
        q = qty or QTY                                      # qty / kind / extra are only used by manual orders
        order = self.stop_market_order if kind == "stop" else self.limit_order
        tk = order(self.fut.mapped, side * q, px, tag=name + " entry")
        p = dict(group=group, name=name, side=side, stop=stop, tgt=tgt, last_j=last_j,
                 cancel=cancel_hm, exit=min(exit_hm, self.exit_hm), tk=tk, live=True, qty=q, extra=extra or {},
                 keep=bool((extra or {}).get("keep")))
        self.pend.append(p)
        self.entries[tk.order_id] = p

    def expire(self, j, nxt):
        keep = []
        for p in self.pend:
            if p["tk"].status == OrderStatus.FILLED:        # filled, its event is still on the way
                keep.append(p)
            elif j >= p["last_j"] or nxt >= p["cancel"] or (nxt >= self.exit_hm and not p.get("keep")) or p["group"] in self.done:
                p["tk"].cancel()
                p["live"] = False
            else:
                keep.append(p)
        self.pend = keep

    def flatten_addons(self):
        for p in self.pend:
            p["tk"].cancel()
            p["live"] = False
        self.pend = []
        for g in list(self.legs):
            self.close_leg(g)

    # ---------- Opening Drive upgrades
    def vix_spike(self):
        v = self.vix_hist
        return OD_VIX_SKIP is not None and len(v) == v.maxlen and v[-1] / v[0] - 1 > OD_VIX_SKIP

    def od_reentry_bar(self, j, h):
        r = self.od_re
        if r is None or r["state"] not in ("exited", "waiting"):
            return
        b = self.tb[j]
        if r["state"] == "exited":                          # this bar is the exit bar
            if h >= 1530 or self.news:
                r["state"] = "done"
                return
            day = [x for x in self.tb[:j + 1] if 900 <= x[1] < 1600]
            s = r["side"]
            dext = max(x[H] for x in day) if s > 0 else min(x[L] for x in day)
            r.update(state="waiting", lo=b[L], hi=b[H])
            self.place("OD-RE", "Opening Drive re-entry", s, rnd(dext + s * TICK), None, None, 10 ** 9, 1530, 1555,
                       kind="stop", extra={"od_re": True})
        else:                                               # waiting: track the low / high since the exit
            r["lo"], r["hi"] = min(r["lo"], b[L]), max(r["hi"], b[H])

    # ---------- trades
    def enter(self, name, side, t, stop=None, stop_pct=None, tgt_pct=None, trail=None, arm=None, real=True, shadow=True,
              cap=None):
        if shadow:
            self.vlegs.append(dict(name=name, side=side, t=t, px=None, stop=stop, stop_pct=stop_pct, tgt_pct=tgt_pct,
                                   tgt=None, trail=trail, arm=arm, i=0, pnl=None))
        if self.is_warming_up or name in OFF:
            return
        if not real:
            self.debug(f"{self.session} {name} skipped by eval filter (shadow {sum(self.shadow):+.1f})")
            return
        sym = self.fut.mapped
        tk = self.market_order(sym, side * QTY, tag=name)
        if tk.status != OrderStatus.FILLED:                 # refused or not filled: no leg to manage
            return
        px = tk.average_fill_price
        st = rnd(stop) if stop is not None else rnd(px * (1 - side * stop_pct))
        if cap is not None and side * (px - st) > cap:      # stop cap, measured from the fill
            st = rnd(px - side * cap)
        self.add_leg(name, side, sym, px, st, rnd(px * (1 + side * tgt_pct)) if tgt_pct else None, trail, arm, self.exit_hm)

    def add_leg(self, name, side, sym, px, st, tgt, trail, arm, exit_hm, qty=None):
        q = qty or QTY
        leg = dict(name=name, side=side, sym=sym, px=px, best=px, trail=trail, arm=arm, cur=st, exit=exit_hm, qty=q,
                   stop=self.stop_market_order(sym, -side * q, st, tag=name + " stop"), tgt=None)
        if tgt is not None:
            leg["tgt"] = self.limit_order(sym, -side * q, tgt, tag=name + " target")
        self.legs.append(leg)
        return leg

    def close_leg(self, g):
        done = [t for t in (g["stop"], g["tgt"]) if t is not None and t.status == OrderStatus.FILLED]
        if done:                                            # stop / target already filled this bar
            g["stop"] = g["tgt"] = None
        if g["stop"] is not None:
            g["stop"].cancel()
            if g["tgt"] is not None:
                g["tgt"].cancel()
            g["stop"] = g["tgt"] = None
            self.market_order(g["sym"], -g["side"] * g.get("qty", QTY), tag=g["name"] + " time exit")
        if g in self.legs:
            self.legs.remove(g)

    def trail_real(self, bar):
        for g in self.legs:
            if g["trail"] is None or g["stop"] is None:
                continue
            s = g["side"]
            g["best"] = max(g["best"], bar.high) if s > 0 else min(g["best"], bar.low)
            if s * (g["best"] - g["px"]) >= g["arm"]:
                ts = rnd(g["best"] - s * g["trail"])
                if s * (ts - g["cur"]) > 0:
                    g["cur"] = ts
                    g["stop"].update_stop_price(ts)

    def on_order_event(self, e):
        if e.status != OrderStatus.FILLED:
            return
        p = self.entries.pop(e.order_id, None)
        if p is not None:                                   # add-on limit entry filled
            if p in self.pend:
                self.pend.remove(p)
            sym = e.symbol
            if p["group"] in self.done or not p["live"]:    # a second order of the group, or one already withdrawn
                self.market_order(sym, -p["side"] * p.get("qty", QTY), tag="undo extra entry")
                return
            self.done.add(p["group"])
            for q in self.pend:
                if q["group"] == p["group"] and q["tk"].status != OrderStatus.FILLED:
                    q["tk"].cancel()
                    q["live"] = False
            self.pend = [q for q in self.pend if q["group"] != p["group"] or q["tk"].status == OrderStatus.FILLED]
            x = p.get("extra") or {}
            r = self.od_re
            if x.get("od_re") and r is not None:            # Opening Drive re-entry filled
                s, atr, px = p["side"], r["atr"], e.fill_price
                st = r["lo"] - TICK if s > 0 else r["hi"] + TICK
                cap = (OD_STOP_CAP if OD_STOP_CAP is not None else 0.6) * atr
                if s * (px - st) > cap or s * (px - st) <= 0:
                    st = px - s * cap
                self.add_leg(p["name"], s, sym, px, rnd(st), None, 0.4 * atr, 0.5 * atr, p["exit"])
                r["state"] = "done"
                return
            if x.get("offsets"):                            # manual order: stop / targets measured from the fill price
                px, s = e.fill_price, p["side"]
                parts = x.get("split") or [(p.get("qty", QTY), x.get("tgt_pts"))]
                for n, tp in parts:
                    leg = self.add_leg(p["name"], s, sym, px, rnd(px - s * x["stop_pts"]), rnd(px + s * tp) if tp else None,
                                       x.get("trail"), x.get("arm"), p["exit"], qty=n)
                    leg["keep"] = bool(x.get("keep"))
            else:
                self.add_leg(p["name"], p["side"], sym, e.fill_price, p["stop"], p["tgt"], None, None, p["exit"])
            return
        for g in self.legs:
            for mine, other in (("stop", "tgt"), ("tgt", "stop")):
                tk = g[mine]
                if tk is not None and tk.order_id == e.order_id:
                    o = g[other]
                    if o is not None and o.status == OrderStatus.FILLED:
                        self.market_order(g["sym"], g["side"] * g.get("qty", QTY), tag="undo double exit")
                    elif o is not None:
                        o.cancel()
                    g["stop"] = g["tgt"] = None
                    if mine == "stop" and g["name"] == "Opening Drive" and self.od_re and self.od_re["state"] == "armed":
                        self.od_re["state"] = "exited"      # stopped / trailed out: re-entry is set up on this bar
                    return

    def finish_day(self, bar):
        for p in self.pend:
            if not p.get("keep"):                           # manual override orders stay working
                p["tk"].cancel()
                p["live"] = False
        self.pend = [p for p in self.pend if p.get("keep")]
        kept = [g for g in self.legs if g.get("keep") and g["stop"] is not None]   # manual override trades stay open
        for g in list(self.legs):
            if g not in kept:
                self.close_leg(g)
        self.legs = kept
        if self.portfolio.invested and not kept:            # safety net: never hold anything overnight
            self.debug(f"{self.session} not flat at the close -> liquidating")
            self.liquidate(tag="End of day safety")
        pts = 0.0
        for v in self.vlegs:
            if v["px"] is None:
                continue
            if v["pnl"] is None:
                v["pnl"] = v["side"] * (v["last"] - v["px"]) - COST
            pts += v["pnl"]
        self.shadow.append(pts)
        self.shadow_done = True
        self.vlegs = []

    def sim_bar(self, bar, h):
        """Shadow trades, same as champion_signals.simulate: stop first, target from the next bar, trail after."""
        if h >= self.exit_hm:
            return
        for v in self.vlegs:
            if v["pnl"] is not None or h < v["t"]:
                continue
            s = v["side"]
            if v["px"] is None:
                v["px"] = v["best"] = bar.open
                if v["stop"] is None:
                    v["stop"] = rnd(bar.open * (1 - s * v["stop_pct"]))
                if v["tgt_pct"]:
                    v["tgt"] = rnd(bar.open * (1 + s * v["tgt_pct"]))
            if (s > 0 and bar.low <= v["stop"]) or (s < 0 and bar.high >= v["stop"]):
                v["pnl"] = s * (v["stop"] - v["px"]) - COST
                continue
            if v["i"] > 0 and v["tgt"] is not None and ((s > 0 and bar.high >= v["tgt"]) or (s < 0 and bar.low <= v["tgt"])):
                v["pnl"] = s * (v["tgt"] - v["px"]) - COST
                continue
            v["best"] = max(v["best"], bar.high) if s > 0 else min(v["best"], bar.low)
            if v["trail"] is not None and s * (v["best"] - v["px"]) >= v["arm"]:
                ts = v["best"] - s * v["trail"]
                v["stop"] = max(v["stop"], ts) if s > 0 else min(v["stop"], ts)
            v["i"] += 1
            v["last"] = bar.close

    # ---------- roll / chart
    def adjust(self, gap):
        """Back-adjust stored price levels after a contract roll (Panama)."""
        self.closes = deque((x + gap for x in self.closes), maxlen=6)
        self.c20 = deque((x + gap for x in self.c20), maxlen=20)
        o = self.orb
        o["h"], o["l"] = o["h"] + gap, o["l"] + gap
        o["ema"] = None if o["ema"] is None else o["ema"] + gap
        o["last"] = None if o["last"] is None else o["last"] + gap
        self.m_open = {k: x + gap for k, x in self.m_open.items()}
        self.m_close = {k: x + gap for k, x in self.m_close.items()}
        sh = lambda bs: [(b[0], b[1], b[O] + gap, b[H] + gap, b[L] + gap, b[C] + gap) for b in bs]
        self.prev_days = deque((sh(day) for day in self.prev_days), maxlen=7)
        self.tb = sh(self.tb)
        if self.a_n:
            self.a_h, self.a_l = self.a_h + gap, self.a_l + gap
        if self.on_h > -1e17:
            self.on_h, self.on_l = self.on_h + gap, self.on_l + gap
        if self.pd_low is not None:
            self.pd_low += gap
        if self.od_re and "lo" in self.od_re:
            self.od_re["lo"] += gap
            self.od_re["hi"] += gap

    def on_symbol_changed_events(self, changes):
        for c in changes.values():
            old, new = self.securities[c.old_symbol].price, self.securities[c.new_symbol].price
            if old and new:
                self.adjust(new - old)
            if self.portfolio[c.old_symbol].invested:
                self.liquidate(c.old_symbol, tag="Roll")

    def plot_bench(self):
        px = self.benchmark.evaluate(self.time)
        if not px:
            return
        self.bench_start = self.bench_start or px
        self.plot("Strategy Equity", "QQQ buy & hold", 100000 * px / self.bench_start)

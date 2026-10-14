#!/usr/bin/env python3
# Historical check: runs the bot's own engine (same files the live bot uses) over 1-minute NQ data with
# simulated fills, and prints the result per piece. Compare with the research numbers before trading.
#   python3 replay.py ~/Downloads/nq_ssmt_backtest/nq_1m_cont_ext.parquet --extras none --start 2023-10-09
import argparse
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import AlgorithmImports as AI   # noqa: E402
from engine import Runner       # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("data")
p.add_argument("--extras", default="none", choices=["none", "thu", "all"])
p.add_argument("--start", default="2023-10-09")
p.add_argument("--off", default="", help="pieces to leave out, comma separated")
p.add_argument("--end", default="2026-10-09")
p.add_argument("--warm-days", type=int, default=50)
a = p.parse_args()

d = pd.read_parquet(a.data)
start = pd.Timestamp(a.start)
d = d[(d.tday >= start - pd.Timedelta(days=a.warm_days)) & (d.tday <= a.end)]
cfg = dict(contract="NQ", qty=1, extras=a.extras, off=[x for x in a.off.split(",") if x], daily_loss_stop=1e12, max_drawdown_stop=1e12, max_contracts=99)
broker = AI.PaperBroker()
r = Runner(cfg, broker, "SIM", lambda m: None)
t0 = datetime(1970, 1, 1)
lm, td, o, h, l, c, v, off = (d[k].values for k in ("lm", "tday", "o", "h", "l", "c", "v", "off"))
prev = off[0]
for i in range(len(d)):
    if off[i] != prev:                                   # contract roll in the data: back-adjust stored levels
        r.algo.adjust(-(off[i] - prev))
        prev = off[i]
    t = t0 + timedelta(minutes=int(lm[i]))
    bar = NS(time=t, end_time=t + timedelta(minutes=1), open=float(o[i]), high=float(h[i]), low=float(l[i]),
             close=float(c[i]), volume=int(v[i]))
    r.feed(bar, warm=pd.Timestamp(td[i]) < start)

f = pd.DataFrame(broker.fills, columns=["t", "q", "px", "tag"])
f["day"] = (f.t + pd.Timedelta(hours=6)).dt.normalize()
f["piece"] = f.tag.str.replace(r" (entry|stop|target|time exit)$", "", regex=True)
f["cash"] = -f.q * f.px
flat_bad = int((f.groupby("day").q.sum() != 0).sum())
g = f.groupby("piece").agg(fills=("q", "size"), pts=("cash", "sum"))
g["trades"] = g.fills // 2
g["net_pts"] = g.pts - 0.75 * g.trades
print(g[["trades", "net_pts"]].round(0).to_string())
net = f.cash.sum() - 0.75 * len(f) / 2
print(f"\nversion {a.extras}: {len(f) // 2} trades, net {net:,.0f} pts = ${net * 20:,.0f} on 1 NQ (${net * 2:,.0f} on 1 MNQ)")
print(f"days not flat at the close: {flat_bad}, open position at the end: {broker.pos}")

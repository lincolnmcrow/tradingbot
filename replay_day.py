#!/usr/bin/env python3
# Replays ONE day with the bot's own engine (same files the live bot uses) and prints the fills as JSON.
# Used by the page's "backtest" Pine Script. Run as a separate process so it never touches the running bot.
#   python3 replay_day.py 2026-10-16 /path/nq_1m_cont_ext.parquet "First FVG,SD fade" '{"pdl_skip":true,...}'
import json
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import AlgorithmImports as AI   # noqa: E402
from engine import Runner       # noqa: E402

day, path = sys.argv[1], sys.argv[2]
off = [x for x in (sys.argv[3] if len(sys.argv) > 3 else "").split(",") if x]
v5 = json.loads(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None
start = pd.Timestamp(day)
d = pd.read_parquet(path)
d = d[(d.tday >= start - pd.Timedelta(days=60)) & (d.tday <= start)]
if not (d.tday == start).any():
    print(json.dumps({"error": f"no data for {day} in {os.path.basename(path)}"}))
    sys.exit(0)
cfg = dict(contract="NQ", qty=1, extras="all", off=off, v5=v5, daily_loss_stop=1e12, max_drawdown_stop=1e12, max_contracts=99)
broker = AI.PaperBroker()
r = Runner(cfg, broker, "SIM", lambda m: None)
t0 = datetime(1970, 1, 1)
lm, td, o, h, l, c, v, offv = (d[k].values for k in ("lm", "tday", "o", "h", "l", "c", "v", "off"))
prev = offv[0]
for i in range(len(d)):
    if offv[i] != prev:
        r.algo.adjust(-(offv[i] - prev))
        prev = offv[i]
    t = t0 + timedelta(minutes=int(lm[i]))
    bar = NS(time=t, end_time=t + timedelta(minutes=1), open=float(o[i]), high=float(h[i]), low=float(l[i]), close=float(c[i]), volume=int(v[i]))
    r.feed(bar, warm=pd.Timestamp(td[i]) < start)
rows = [dict(time=f[0].strftime("%Y-%m-%d %H:%M:%S"), mode="backtest", symbol="NQ", qty=int(f[1]), price=float(f[2]), tag=f[3]) for f in broker.fills]
print(json.dumps({"fills": rows}))

#!/usr/bin/env python3
# Champion bot - local control page. Run:  python3 app.py   then open http://localhost:8787
# Everything runs on this Mac: the page, the strategy, and the orders to TopstepX. The page only listens on
# 127.0.0.1, so nothing outside this computer can reach it.
import json
import os
import sys
import threading
import time
from zoneinfo import ZoneInfo
from collections import deque
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from topstep import TopstepAPI          # noqa: E402
from engine import LiveLoop             # noqa: E402
from extras import BACKTEST, TradeBook, news, to_csv, Notes, AccountTracker, daily_report, safe_name, pine_script, nq_csv_bytes   # noqa: E402
import glob                             # noqa: E402
import subprocess                       # noqa: E402
import shutil                           # noqa: E402
import base64                           # noqa: E402
from datetime import timedelta          # noqa: E402

SETTINGS = os.path.join(HERE, "settings.json")
SECRETS = os.path.join(HERE, ".secrets.json")
DEFAULTS = dict(account_id=None, account_name="", contract="MNQ", qty=1, extras="none", off=[], mode="paper",
                daily_loss_stop=900, max_drawdown_stop=1800, max_contracts=5,
                acct_start=50000, profit_target=3000, max_loss=2000, daily_limit=1000,
                manual_max_per_day=0, manual_cooldown_min=0, news_pause=False, news_before_min=5, news_after_min=10,
                v5=dict(pdl_skip=True, fvg_bias=True, orb_3r=True, dip_any=True))
RULES = ("acct_start", "profit_target", "max_loss", "daily_limit")   # editable while the bot runs
PREFS = ("manual_max_per_day", "manual_cooldown_min", "news_pause", "news_before_min", "news_after_min")
BOOK = TradeBook(os.path.join(HERE, "logs", "fills.csv"))
NOTES = Notes(os.path.join(HERE, "logs", "notes.json"))
SNAPS, REPORTS = os.path.join(HERE, "logs", "snapshots"), os.path.join(HERE, "logs", "reports")
ET = ZoneInfo("America/New_York")
# NQ data files offered for download on the page (the folder with the research data; override with CHAMPION_DATA_DIR)
DATA_DIR = os.environ.get("CHAMPION_DATA_DIR") or os.path.join(os.path.dirname(HERE), "nq_ssmt_backtest")


def data_files():
    def newest(pattern):
        found = sorted(glob.glob(os.path.join(DATA_DIR, pattern)), key=os.path.getmtime)
        return found[-1] if found else None
    csv_p, pq_p = newest("NQ_1m_20*_to_*.csv"), newest("nq_1m_cont_ext*.parquet")
    return csv_p, pq_p


def watch_accounts():
    """Every 60 s: balance of every tradable account, and its rule status when rules were saved for it."""
    trackers = {}
    while True:
        try:
            cfg = load(SETTINGS, DEFAULTS)
            now = datetime.now(ET).replace(tzinfo=None)
            rows = []
            for x in api().accounts():
                tr = trackers.setdefault(x["id"], AccountTracker(os.path.join(HERE, "logs", "account_state.json"), x["id"]))
                tr.update(float(x["balance"]) if x.get("balance") is not None else None, now, (now + timedelta(hours=6)).date())
                rules = (cfg.get("rules") or {}).get(str(x["id"]))
                rows.append(dict(id=x["id"], name=x.get("name"), balance=x.get("balance"), simulated=x.get("simulated"),
                                 day=round(tr.balance - tr.day_open, 2) if tr.balance is not None and tr.day_open is not None else None,
                                 status=tr.snapshot(rules) if rules else None, bot=x["id"] == cfg.get("account_id")))
            STATE["accounts_live"], STATE["accounts_at"] = rows, now.strftime("%H:%M:%S")
        except Exception as e:
            STATE["accounts_error"] = str(e)
        time.sleep(60)


def start_watcher():
    if not STATE.get("watcher"):
        STATE["watcher"] = threading.Thread(target=watch_accounts, daemon=True)
        STATE["watcher"].start()


def write_report_now():
    lp = STATE["loop"]
    if lp and lp.runner:
        lp.requests.append(("report", ()))
        return
    mode = load(SETTINGS, DEFAULTS)["mode"]
    now = datetime.now(ET).replace(tzinfo=None)
    day = now.strftime("%Y-%m-%d")
    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, f"{day}-{mode}.md"), "w") as f:
        f.write(daily_report(day, mode, BOOK.trades(mode), None, [], news(now)["rows"]))
    log(f"daily report saved: logs/reports/{day}-{mode}.md")
LOGS = deque(maxlen=300)
LOCK = threading.Lock()
STATE = {"loop": None, "accounts": []}


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    LOGS.append(line)
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    with open(os.path.join(HERE, "logs", datetime.now().strftime("%Y-%m-%d") + ".log"), "a") as f:
        f.write(line + "\n")


def load(path, default):
    try:
        with open(path) as f:
            return {**default, **json.load(f)}
    except (OSError, ValueError):
        return dict(default)


def save(path, data, private=False):
    with open(path, "w") as f:
        json.dump(data, f, indent=1)
    if private:
        os.chmod(path, 0o600)                     # only your Mac user can read the API key file


def api():
    s = load(SECRETS, {"username": "", "api_key": ""})
    if not s["username"] or not s["api_key"]:
        raise RuntimeError("Enter your Topstep username and API key first")
    key = (s["username"], s["api_key"])                    # one login, reused (fewer sign-ins at Topstep)
    if STATE.get("api_key") != key:
        STATE["api"], STATE["api_key"] = TopstepAPI(*key), key
    return STATE["api"]


def running():
    lp = STATE["loop"]
    return lp is not None and lp.is_alive()


def status():
    lp, cfg, sec = STATE["loop"], load(SETTINGS, DEFAULTS), load(SECRETS, {"username": "", "api_key": ""})
    out = dict(settings=cfg, username=sec["username"], has_key=bool(sec["api_key"]), accounts=STATE["accounts"],
               running=running(), status=(lp.status if lp else "stopped"), error=(lp.error if lp else None),
               mode=(("paper" if lp.paper else "live") if lp else cfg["mode"]), logs=list(LOGS)[-80:])
    if lp and lp.runner:
        out["bot"] = lp.runner.state()
    out["backtest"] = BACKTEST
    out["stream"] = lp.feed.status() if lp and getattr(lp, "feed", None) else ("off" if running() else None)
    out["health"] = lp.health() if lp and lp.runner and hasattr(lp, "health") else None
    out["check"] = lp.check if lp and hasattr(lp, "check") else None
    if not out["check"] and os.path.isdir(os.path.join(HERE, "logs", "checks")):
        files = sorted(os.listdir(os.path.join(HERE, "logs", "checks")))
        if files:
            try:
                with open(os.path.join(HERE, "logs", "checks", files[-1])) as f:
                    out["check"] = json.load(f)
            except (OSError, ValueError):
                pass
    return out


def handle(path, body):
    cfg = load(SETTINGS, DEFAULTS)
    if path == "/api/credentials":
        sec = load(SECRETS, {"username": "", "api_key": ""})
        sec["username"] = body.get("username", sec["username"]).strip()
        if body.get("api_key"):
            sec["api_key"] = body["api_key"].strip()
        save(SECRETS, sec, private=True)
        log("credentials saved")
    elif path == "/api/rules":
        for k in RULES:
            if k in body:
                cfg[k] = float(body[k] or 0)
        if cfg.get("account_id"):                           # remembered per account for the accounts card
            cfg.setdefault("rules", {})[str(cfg["account_id"])] = {k: cfg[k] for k in RULES}
        save(SETTINGS, cfg)
        if STATE["loop"]:
            STATE["loop"].cfg.update({k: cfg[k] for k in RULES})
        log("account rules saved")
    elif path == "/api/settings":
        if running():
            raise RuntimeError("Stop the bot before changing settings")
        for k in DEFAULTS:
            if k in body:
                cfg[k] = body[k]
        save(SETTINGS, cfg)
        log("settings saved")
    elif path == "/api/check":
        a = api()
        if not a.token:
            a.login()
        STATE["accounts"] = [dict(id=x["id"], name=x.get("name"), balance=x.get("balance"), simulated=x.get("simulated"))
                             for x in a.accounts()]
        c = a.front_contract(cfg["contract"])
        log(f"connection OK: {len(STATE['accounts'])} tradable account(s), contract {c.get('name')}")
        start_watcher()
    elif path == "/api/start":
        if running():
            raise RuntimeError("Already running")
        if not cfg["account_id"]:
            raise RuntimeError("Test the connection and pick an account first")
        paper = cfg["mode"] != "live"
        if not paper and body.get("confirm") != "LIVE":
            raise RuntimeError("Live mode sends real orders: type LIVE in the box to confirm")
        cfg = dict(cfg, book_path=BOOK.path, account_path=os.path.join(HERE, "logs", "account_state.json"))
        lp = LiveLoop(cfg, api(), int(cfg["account_id"]), log, paper)
        STATE["loop"] = lp
        lp.start()
        log(f"bot started ({'paper' if paper else 'LIVE'}): {cfg['contract']} x{cfg['qty']}, version {cfg['extras']}" + (f", off: {', '.join(cfg['off'])}" if cfg.get('off') else ""))
    elif path == "/api/prefs":                            # manual limits + news pause: editable while running
        for k in PREFS:
            if k in body:
                cfg[k] = bool(body[k]) if k == "news_pause" else float(body[k] or 0)
        save(SETTINGS, cfg)
        if STATE["loop"]:
            STATE["loop"].cfg.update({k: cfg[k] for k in PREFS})
        log("trading limits / news pause saved")
    elif path == "/api/check_day":
        lp = STATE["loop"]
        if not running() or not lp.runner:
            raise RuntimeError("Start the bot first")
        lp.start_check(datetime.now(ET).date())
        log("live-vs-backtest check started for today (takes about 30 s)")
    elif path == "/api/note":
        NOTES.set(str(body.get("key", "")), str(body.get("text", "")))
    elif path == "/api/snapshot":
        png = str(body.get("png", ""))
        if not png.startswith("data:image/png;base64,"):
            raise RuntimeError("not a PNG")
        os.makedirs(SNAPS, exist_ok=True)
        with open(os.path.join(SNAPS, safe_name(str(body.get("key", ""))) + ".png"), "wb") as f:
            f.write(base64.b64decode(png.split(",", 1)[1]))
    elif path == "/api/report_now":
        write_report_now()
    elif path == "/api/use_account":
        if running():
            raise RuntimeError("Stop the bot before switching accounts")
        cfg["account_id"], cfg["account_name"] = int(body["id"]), str(body.get("name", ""))
        saved = (cfg.get("rules") or {}).get(str(cfg["account_id"]))
        if saved:
            cfg.update(saved)
        save(SETTINGS, cfg)
        log(f"bot account set to {cfg['account_name']}" + (" (its saved rules loaded)" if saved else " - enter its rules in Topstep account"))
    elif path == "/api/stop":
        if STATE["loop"]:
            STATE["loop"].stop_flag = True
            log("stop requested - closing positions")
    elif path in ("/api/manual", "/api/close_manual", "/api/breakeven"):
        lp = STATE["loop"]
        if not running() or not lp.runner or not lp.status.startswith(("running", "connection")):
            raise RuntimeError("Start the bot first (and wait until it says running)")
        if path == "/api/close_manual":
            lp.requests.append(("close_manual", ()))
        elif path == "/api/breakeven":
            lp.requests.append(("breakeven", ()))
        else:
            side = 1 if body.get("side") == "buy" else -1
            stop, tgt = float(body.get("stop") or 0), float(body.get("target") or 0)
            ovr = bool(body.get("override"))
            kind = body.get("kind") if body.get("kind") in ("lmt", "stop") else "mkt"
            price = float(body["price"]) if kind != "mkt" else None
            qty = int(body.get("qty") or 0) or None
            if qty and qty > 50:
                raise RuntimeError("too many contracts")
            lp.requests.append(("manual", (side, stop, tgt, ovr, qty, float(body.get("trail") or 0), float(body.get("arm") or 0),
                                           float(body.get("target2") or 0), kind, price)))
            log(f"manual {'BUY' if side > 0 else 'SELL'} {kind.upper()}{f' @ {price}' if price else ''} requested"
                f" (stop {stop} pts, target {tgt or 'none'}{', OVERRIDE' if ovr else ''})")
    elif path == "/api/flatten":
        if STATE["loop"]:
            STATE["loop"].flatten_flag = True
            log("flatten requested")
    else:
        raise RuntimeError("unknown action")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, data, ctype="application/json"):
        raw = data if isinstance(data, bytes) else json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/chart":
            lp = STATE["loop"]
            d = lp.runner.chart() if lp and lp.runner else {"bars": [], "lines": [], "prior_day": None}
            mode = lp.runner.mode if lp and lp.runner else load(SETTINGS, DEFAULTS)["mode"]
            d["fills"] = BOOK.fills(mode)[-300:]
            return self.send(200, d)
        if self.path == "/api/research":                    # research & statistics shown on the page (static file, no secrets)
            try:
                with open(os.path.join(HERE, "research.json"), "rb") as f:
                    return self.send(200, f.read())
            except OSError:
                return self.send(404, {"error": "research.json missing"})
        if self.path == "/api/data":                        # NQ data downloads
            csv_p, pq_p = data_files()
            files = []
            if csv_p:
                files.append(dict(name=os.path.basename(csv_p), url="/data/full.csv", size=os.path.getsize(csv_p),
                                  desc="All 1-minute bars, one CSV (too big for Excel; opens in Python / R / a database)"))
            if pq_p:
                files.append(dict(name=os.path.basename(pq_p), url="/data/nq.parquet", size=os.path.getsize(pq_p),
                                  desc="Same data as Parquet (what the bot's replay reads; open with pandas.read_parquet)"))
            years, span = [], None
            if pq_p:
                import pandas as pd
                dd = pd.read_parquet(pq_p, columns=["tday"])
                years = sorted(int(y) for y in dd.tday.dt.year.unique())
                span = f"{dd.tday.min():%Y-%m-%d} to {dd.tday.max():%Y-%m-%d}"
            return self.send(200, dict(files=files, years=years, span=span, folder=DATA_DIR))
        if self.path.startswith("/data/"):
            name = self.path.split("?")[0][len("/data/"):]
            csv_p, pq_p = data_files()
            if name in ("full.csv", "nq.parquet"):
                p = csv_p if name == "full.csv" else pq_p
                if not p or not os.path.isfile(p):
                    return self.send(404, {"error": "data file not found in " + DATA_DIR})
                self.send_response(200)
                self.send_header("Content-Type", "text/csv" if name == "full.csv" else "application/octet-stream")
                self.send_header("Content-Length", str(os.path.getsize(p)))
                self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(p)}"')
                self.end_headers()
                try:
                    with open(p, "rb") as f:
                        shutil.copyfileobj(f, self.wfile, 1 << 20)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if pq_p and (name == "last30.csv" or (name.startswith("year/") and name.endswith(".csv") and name[5:-4].isdigit())):
                body = nq_csv_bytes(pq_p, last_days=30) if name == "last30.csv" else nq_csv_bytes(pq_p, year=int(name[5:-4]))
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", f'attachment; filename="NQ_1m_{"last30days" if name == "last30.csv" else name[5:-4]}.csv"')
                self.end_headers()
                return self.wfile.write(body)
            return self.send(404, {"error": "unknown data file"})
        if self.path.startswith("/api/pine"):               # TradingView Pine Script of one day's trades
            lp = STATE["loop"]
            mode = lp.runner.mode if lp and lp.runner else load(SETTINGS, DEFAULTS)["mode"]
            q = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&") if "=" in p) if "?" in self.path else {}
            day = q.get("day") or datetime.now(ET).strftime("%Y-%m-%d")
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                return self.send(400, {"error": "bad date"})
            if q.get("source") == "backtest":                # what the bot's rules would have traded that day (separate process)
                cfg = load(SETTINGS, DEFAULTS)
                pq_p = data_files()[1]
                if not pq_p:
                    return self.send(400, {"error": "no NQ data file found to replay"})
                try:
                    res = subprocess.run([sys.executable, "-W", "ignore", os.path.join(HERE, "replay_day.py"), day, pq_p,
                                          ",".join(cfg.get("off") or []), json.dumps(cfg.get("v5") or {})],
                                         capture_output=True, text=True, timeout=180, cwd=HERE)
                    out = json.loads(res.stdout.strip().splitlines()[-1])
                except Exception as e:
                    return self.send(500, {"error": f"replay failed: {e}"})
                if out.get("error"):
                    return self.send(400, {"error": out["error"]})
                tr = TradeBook.trades_from(out["fills"])
                mode = "backtest replay"
            else:
                tr = BOOK.trades(mode)
            script = pine_script(tr, day, mode)
            n = len([t for t in tr if t["entry_time"][:10] == day or t["exit_time"][:10] == day])
            return self.send(200, dict(script=script, day=day, mode=mode, trades=n))
        if self.path == "/api/accounts":
            return self.send(200, dict(rows=STATE.get("accounts_live", []), at=STATE.get("accounts_at"),
                                       error=STATE.get("accounts_error")))
        if self.path == "/api/reports":
            files = sorted(os.listdir(REPORTS), reverse=True)[:60] if os.path.isdir(REPORTS) else []
            return self.send(200, dict(files=[f for f in files if f.endswith(".md")]))
        if self.path.startswith("/reports/") or self.path.startswith("/snapshots/"):
            folder = REPORTS if self.path.startswith("/reports/") else SNAPS
            name = os.path.basename(self.path.split("?")[0])
            p = os.path.join(folder, name)
            if not os.path.isfile(p) or name.startswith("."):
                return self.send(404, {"error": "not found"})
            with open(p, "rb") as f:
                return self.send(200, f.read(), "image/png" if name.endswith(".png") else "text/plain; charset=utf-8")
        if self.path == "/api/news":
            return self.send(200, news(datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)))
        if self.path.startswith("/api/export"):
            lp = STATE["loop"]
            mode = lp.runner.mode if lp and lp.runner else load(SETTINGS, DEFAULTS)["mode"]
            what = "fills" if "fills" in self.path else "trades"
            if what == "fills":
                body = to_csv(BOOK.fills(mode), TradeBook.FIELDS)
            else:
                body = to_csv(BOOK.trades(mode), ["exit_time", "piece", "side", "qty", "entry_time", "entry", "exit",
                                                  "reason", "pts", "usd"])
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", f'attachment; filename="champion_{mode}_{what}_{datetime.now():%Y-%m-%d}.csv"')
            self.end_headers()
            return self.wfile.write(body)
        if self.path == "/api/stream":                      # live price pushed to the page (Server-Sent Events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = None
            try:
                while True:
                    lp = STATE["loop"]
                    lv = lp.runner.live if lp and lp.runner else None
                    if lv and lv != last:
                        last = lv
                        t = int((lv[0] - datetime(1970, 1, 1)).total_seconds())
                        self.wfile.write(f"data: {json.dumps(dict(price=lv[1], t=t, clock=lv[0].strftime('%H:%M:%S')))}\n\n".encode())
                        self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                return
        if self.path == "/api/history":
            lp = STATE["loop"]
            mode = lp.runner.mode if lp and lp.runner else load(SETTINGS, DEFAULTS)["mode"]
            return self.send(200, dict(mode=mode, **BOOK.summary(mode)))
        if self.path == "/api/status":
            return self.send(200, status())
        self.send(404, {"error": "not found"})

    def do_POST(self):
        # only this page may post (blocks other websites from driving the bot through your browser)
        origin = self.headers.get("Origin", "")
        if origin and not origin.startswith(("http://localhost:", "http://127.0.0.1:")):
            return self.send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            with LOCK:
                handle(self.path, body)
            self.send(200, {"ok": True})
        except Exception as e:
            log(f"error: {e}")
            self.send(400, {"ok": False, "error": str(e)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8787))
    log(f"Champion bot page: http://127.0.0.1:{port}  (leave this window open)")
    if load(SECRETS, {"username": "", "api_key": ""})["api_key"]:
        start_watcher()
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

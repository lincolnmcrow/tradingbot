# Champion Bot

Runs the Champion NQ strategy on your computer (Windows or Mac) and trades a TopstepX account through the TopstepX API.
The control page is local only (http://127.0.0.1:8787); orders go from this computer to Topstep.

## Start

Double-click `start.bat` on Windows / `start.command` on Mac (or run `python -X utf8 app.py`), then in the page:

1. **Topstep login** - username + API key (TopstepX -> Settings -> API Access), Save login, Test connection, pick the account.
2. **Settings** - contract (MNQ recommended), contracts per piece, strategy version, mode, risk stops. Save settings.
3. **Start**. Paper mode uses live Topstep prices with simulated fills and sends no orders.
   Live mode sends real orders and asks you to type LIVE first.

Keep the computer awake and online 08:25-16:00 ET (start.command does this on Mac; on Windows set Settings > Power > Sleep to Never), no VPN.
Closing the window stops the bot; use **Stop & flatten** first so positions are closed.

## What it does

- `champion_plus.py` - the strategy, the same file tested on QuantConnect (`EXTRAS` = none / thu / all).
- `AlgorithmImports.py` - stand-in for the QuantConnect functions the strategy uses; simulated broker; exchange calendar.
- `topstep.py` - TopstepX API: login, accounts, front contract, 1-minute bars, orders (market / limit / stop), fills.
- `engine.py` - feeds bars, reports fills, risk guard, quarterly contract roll, warm-up from 50 days of history.
- `app.py` + `index.html` - the local page.
- `replay.py` - historical check of this exact engine.

Safety: refuses to start live if the account already holds the contract; daily loss stop and drawdown stop close
everything and block new trades; never holds overnight (anything open after 15:55 is liquidated); stops and targets
rest at the exchange; your API key is stored only in `.secrets.json` (readable by your user only).

## Checked

`python replay.py <nq_1m.parquet> --extras none` (needs your own NQ 1-minute data; not in this repo) (2023-10-09 .. 2026-10-07):

| Version | Trades | Net points | 1 MNQ | 1 NQ |
| --- | --- | --- | --- | --- |
| Champion only | 332 | 17,262 | $34,524 | $345,240 |
| Champion + all four add-ons | 1,769 | 26,192 | $52,384 | $523,835 |

Every day flat at the close. The research and QuantConnect results were 16,955-17,191 and 25,922 points.

Paper mode and Live on a Practice account have been run against TopstepX on Mac. The Windows port is untested by its author.
Run paper mode first, then live on a Practice account, and watch the log during the first sessions.

## Topstep rules to confirm yourself

Bots are allowed through the TopstepX API on Combine / Express accounts, must run on your own device (no VPS / VPN),
no high-frequency trading, and the API is not available on Live Funded accounts. The daily loss and drawdown stops
here are set just inside a 50K account's limits - check your account's numbers and lower them if needed.

## Install on Windows (for a friend rebuilding this with Claude)

1. Install Python 3.10+ from python.org (tick **Add python.exe to PATH**) and Git.
2. `git clone https://github.com/lincolnmcrow/tradingbot.git` then `cd tradingbot`
3. `pip install -r requirements.txt`
4. Double-click `start.bat`. The page opens at http://127.0.0.1:8787 (use 127.0.0.1, not localhost).
5. Setup card: enter your own TopstepX username + API key (TopstepX > Settings > API Access). These go to `.secrets.json`, which is git-ignored. Never commit it.
6. Start in **Paper** mode. Go Live only on a Practice account first (you must type LIVE).

Or open the folder in Claude Code and say: "Read CLAUDE.md and get this bot running on my Windows PC."

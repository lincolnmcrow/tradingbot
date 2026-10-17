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

- `champion_plus.py` - the strategy (`EXTRAS` = none / thu / all; the v5 rule switches are at the top and are set from `settings.json` -> `v5`). `nq_ssmt_backtest/quantconnect/champion_plus_v5.py` is the cleaned QuantConnect version.
- `AlgorithmImports.py` - stand-in for the QuantConnect functions the strategy uses; simulated broker; exchange calendar.
- `topstep.py` - TopstepX API: login, accounts, front contract, 1-minute bars, orders (market / limit / stop), fills.
- `engine.py` - feeds bars, reports fills, risk guard, quarterly contract roll, warm-up from 50 days of history.
- `app.py` + `index.html` - the local page (+ `research.json`, the research and statistics shown on it).
- `replay.py` - historical check of this exact engine. `replay_day.py` replays one day (used by the page's backtest Pine Script).
- The page also has a **Data** section: download the NQ 1-minute history (CSV / Parquet / by year, read from `../nq_ssmt_backtest` or `CHAMPION_DATA_DIR`) and make a TradingView Pine Script of a day's trades.

Safety: refuses to start live if the account already holds the contract; daily loss stop and drawdown stop close
everything and block new trades; never holds overnight (anything open after 15:55 is liquidated); stops and targets
rest at the exchange; your API key is stored only in `.secrets.json` (readable by your user only).

## Checked

`python replay.py <nq_1m.parquet> --extras all --off "AMD AM,AMD PM,SD fade" --start 2023-10-09 --end 2026-10-09` (needs your own NQ 1-minute data; not in this repo).
Ten pieces, 1 contract each, after 0.75 pt cost per trade:

| Version | 2015-10 .. 2023-10 | 2023-10 .. 2026-10 | QuantConnect (1 NQ, 2023-26) |
| --- | --- | --- | --- |
| Original 8 pieces | 7,358 pts | 29,760 pts | about 25,900 pts |
| v5 (current, all rules on) | 27,135 pts | 46,311 pts (about $92,600 on 1 MNQ) | $821,474 net (about 41,400 pts) |

Every day flat at the close. v5 = the original plus: skip Thursday / Month-end short when the overnight range already traded
below the prior-day low; First FVG only with the 09:00 move; ORB retest 3R target; Small Dip on any calm open (1.5% stop);
two new pieces, Big Dip Short and Midday Follow. Each rule can be switched back to the old one under Bot settings
(all switched off reproduces the original 29,760 pts exactly).

The page has a **Research** section with every statistic behind this: per-piece results in two periods, walk-forward and
random-shuffle tests, Monte Carlo, slippage, Topstep Combine pass rates, and the ideas that were tested and rejected
(data in `research.json`). Everything was tuned on the same history, so expect less than the backtest; paper trade first.

Paper mode and Live on a Practice account have been run against TopstepX on Mac. The Windows port is untested by its author.
Run paper mode first, then live on a Practice account, and watch the log during the first sessions.

## Topstep rules to confirm yourself

Bots are allowed through the TopstepX API on Combine / Express accounts, must run on your own device (no VPS / VPN),
no high-frequency trading, and the API is not available on Live Funded accounts. The daily loss and drawdown stops
here are set just inside a 50K account's limits - check your account's numbers and lower them if needed.

## Install on Windows (for a friend rebuilding this with Claude)

1. Download the repo (green **Code** button > **Download ZIP**, then unzip) or `git clone https://github.com/lincolnmcrow/tradingbot.git`.
2. Double-click `start.bat`. First run it installs Python (via winget, built into Windows 10/11) and asks you to close the window and double-click again; second run installs the packages and opens the page at http://127.0.0.1:8787 (use 127.0.0.1, not localhost).
3. Setup card: enter your own TopstepX username + API key (TopstepX > Settings > API Access). These go to `.secrets.json`, which is git-ignored. Never commit it.
4. Start in **Paper** mode. Go Live only on a Practice account first (you must type LIVE).

If winget is missing, install Python 3.10+ from python.org (tick **Add python.exe to PATH**) and run `start.bat` again.

Or open the folder in Claude Code and say: "Read CLAUDE.md and get this bot running on my Windows PC."

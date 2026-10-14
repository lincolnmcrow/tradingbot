# Champion Bot - notes for Claude

Local Python bot (stdlib HTTP server + single-page UI) that trades a rule-based NQ/MNQ strategy on TopstepX via the ProjectX REST API
and SignalR websocket. Runs on the user's own PC only (Topstep forbids VPS/VPN). Orders only go out in Live mode after typing LIVE.

## Files
- `champion_plus.py` strategy (pieces: Opening Drive, Thursday short, Turn of Month, Monday long, ORB retest, Month-end short, First FVG, Small Dip). `OFF` set disables pieces.
- `AlgorithmImports.py` stand-in for the QuantConnect API the strategy uses + PaperBroker + CME holiday calendar.
- `topstep.py` REST client; `stream.py` minimal SignalR client (prices, order updates).
- `engine.py` Runner / risk Guard / LiveLoop (50-day warm-up, 1 s loop, roll check 17-18 ET).
- `extras.py` trade book, account tracker, daily report, news, backtest stats. `app.py` server + `index.html` UI. `replay.py` historical check.

## Setting up on Windows
`pip install -r requirements.txt` (tzdata is required: zoneinfo has no timezone database on Windows), run with `python -X utf8 app.py` or `start.bat`.
Runtime files are git-ignored and created on first run: `.secrets.json` (API key), `settings.json`, `logs/`.
Never ask for, print or commit the user's API key. Times in the strategy are US Eastern wall-clock.

## Rules when changing things
Default to Paper mode. Don't change strategy logic without a `replay.py` comparison. Keep every day flat by 15:55 ET.

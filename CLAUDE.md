# Champion Bot - notes for Claude

Local Python bot (stdlib HTTP server + single-page UI) that trades a rule-based NQ/MNQ strategy on TopstepX via the ProjectX REST API
and SignalR websocket. Runs on the user's own PC only (Topstep forbids VPS/VPN). Orders only go out in Live mode after typing LIVE.

## Files
- `champion_plus.py` strategy (pieces: Opening Drive (+re-entry), Monday long, Turn of Month, Small Dip, Month-end short, Thursday short, ORB retest, First FVG, Big Dip Short, Midday Follow). `OFF` set disables pieces. v5 rule switches (PDL_SKIP, FVG_BIAS, ORB_R, DIP_ANY, BIGDIP, MIDTRIG) sit at the top; engine.py sets four of them from `settings.json` -> `v5`.
- `AlgorithmImports.py` stand-in for the QuantConnect API the strategy uses + PaperBroker + CME holiday calendar.
- `topstep.py` REST client; `stream.py` minimal SignalR client (prices, order updates).
- `engine.py` Runner / risk Guard / LiveLoop (50-day warm-up, 1 s loop, roll check 17-18 ET).
- `extras.py` trade book, account tracker, daily report, news, today's plan, backtest stats. `app.py` server + `index.html` UI (Research section reads `research.json` via /api/research). `replay.py` historical check.

## Setting up on Windows
`pip install -r requirements.txt` (tzdata is required: zoneinfo has no timezone database on Windows), run with `python -X utf8 app.py` or `start.bat`.
Runtime files are git-ignored and created on first run: `.secrets.json` (API key), `settings.json`, `logs/`.
Never ask for, print or commit the user's API key. Times in the strategy are US Eastern wall-clock.

## Rules when changing things
Default to Paper mode. Don't change strategy logic without a `replay.py` comparison. Keep every day flat by 15:55 ET.

v5 reference numbers (replay, all pieces, retired off): 2015-10..2023-10 = 27,135 pts, 2023-10..2026-10 = 46,311 pts; with every v5 switch off and Big Dip Short / Midday Follow off = 29,760 pts. Re-run replay.py after any strategy change and compare.

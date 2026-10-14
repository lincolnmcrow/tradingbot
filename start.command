#!/bin/bash
# Double-click to start the Champion bot page. Works from any folder (the bot lives in ~/Downloads/champion_bot).
# Keeps the Mac awake while it runs; close this window to stop the bot.
if curl -s -o /dev/null http://127.0.0.1:8787/; then
  echo "The bot is already running - opening the page."
  open http://127.0.0.1:8787
  exit 0
fi
cd "$HOME/Downloads/champion_bot" || { echo "Can't find ~/Downloads/champion_bot"; read -n 1; exit 1; }
(sleep 2 && open http://127.0.0.1:8787) &
caffeinate -dimsu python3 -W ignore app.py

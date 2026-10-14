@echo off
REM Double-click to start the Champion bot. Close this window to stop it (press Stop & flatten on the page first).
cd /d "%~dp0"
curl -s -o nul http://127.0.0.1:8787/ && (echo Bot already running - opening page. & start http://127.0.0.1:8787 & exit /b)
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8787"
python -X utf8 -W ignore app.py
pause

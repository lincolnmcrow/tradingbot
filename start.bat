@echo off
REM Double-click to start the Champion bot. Installs Python and the packages on first run.
REM Close this window to stop it (press Stop & flatten on the page first).
cd /d "%~dp0"
curl -s -o nul http://127.0.0.1:8787/ && (echo Bot already running - opening page. & start http://127.0.0.1:8787 & exit /b)
where python >nul 2>&1 || (
  echo Python not found - installing it with winget...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  echo.
  echo Python installed. CLOSE this window and double-click start.bat again.
  pause & exit /b
)
python -c "import requests,websocket,pandas,numpy,tzdata" >nul 2>&1 || python -m pip install -r requirements.txt
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8787"
python -X utf8 -W ignore app.py
pause

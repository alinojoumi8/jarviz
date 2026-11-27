@echo off
rem Double-click to start Jarviz and open it in the browser.
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py --open
) else (
  py -3 server.py --open
)
pause

@echo off
rem =============================
rem  TJU Daily Bot - Start TUI
rem  Double-click to open Textual TUI
rem =============================
setlocal
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" -c "import textual, requests, yaml" >nul 2>&1
if errorlevel 1 (
    echo [WARN] Dependencies missing, installing...
    "%PY%" -m pip install -r requirements.txt
)

echo ==================================================================
echo   Starting TJU Daily Bot TUI ...
echo   Note: search will auto git pull to sync history reports
echo ==================================================================
echo.

"%PY%" -m tui.app

echo.
echo [TUI exited]
pause
endlocal

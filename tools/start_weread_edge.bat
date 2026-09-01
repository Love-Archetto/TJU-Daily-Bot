@echo off
REM 一键拉起调试 Edge(本地公众号抓取用). 等价执行 tools/start_weread_edge.py
setlocal
cd /d "%~dp0\.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "tools\start_weread_edge.py"
) else (
    python "tools\start_weread_edge.py"
)
endlocal

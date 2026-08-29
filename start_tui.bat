@echo off
REM ============================================================
REM  TJU Daily Bot - 启动 TUI 交互界面
REM  双击本文件即可打开 Textual TUI
REM ============================================================
setlocal
cd /d "%~dp0"

REM 优先使用虚拟环境里的 Python(.venv)，否则退回系统 Python
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

REM 首次运行检查依赖
"%PY%" -c "import textual, requests, yaml" >nul 2>&1
if errorlevel 1 (
    echo [提示] 检测到依赖未安装，安装中...
    "%PY%" -m pip install -r requirements.txt
)

echo ============================================
echo  启动 TJU Daily Bot TUI ...
echo  提示: 搜索前会自动 git pull 同步历史报告
echo ============================================
echo.

"%PY%" -m tui.app

echo.
echo [TUI 已退出]
pause
endlocal

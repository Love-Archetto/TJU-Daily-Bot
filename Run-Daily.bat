@echo off
REM ============================================================
REM  TJU Daily Bot - 一键全流程（抓取 + AI 报告 + 生成 + Git Push）
REM
REM  用法：双击本文件，或在命令行运行  Run-Daily.bat
REM  前置：
REM    1) 已配置 .env（至少一个 AI API Key：DEEPSEEK_API_KEY / TJU_API_KEY 等）
REM    2) 已用调试 Edge 登录微信读书（详见 README / tools/start_weread_edge.py）
REM       - 若未启动，本脚本会自动拉起调试 Edge 并提示你扫码
REM       - 公众号抓取复用真实 Edge 会话；若 Edge 未登录会跳过公众号（不阻塞网站源）
REM    3) 本机 git 已配置对 origin/main 的 push 凭据（HTTPS 需已认证 / SSH 已配）
REM ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo [TJU Daily Bot] 开始一键全流程...
echo.

REM ---- 0. 定位可用的 Python（优先项目 .venv）----
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [WARN] 未找到项目虚拟环境 .venv\Scripts\python.exe，尝试用系统 python...
    set "PY=python"
)

REM ---- 1. 确认 .env 存在 ----
if not exist ".env" (
    echo [错误] 缺少 .env 配置文件。
    echo        请复制 .env.example 为 .env，并填入至少一个 AI API Key。
    goto :fail
)

REM ---- 2. 确保依赖已装 ----
if not exist "%CD%\.venv\Scripts\python.exe" (
    echo [WARN] 未安装依赖。请先执行：
    echo        python -m venv .venv
    echo        .venv\Scripts\pip install -r requirements.txt
    goto :fail
)

REM ---- 3. 确保调试 Edge 可用（公众号抓取需要）----
"%PY%" -c "import urllib.request as u; u.urlopen('http://127.0.0.1:9333/json/version', timeout=2)" >nul 2>&1
if errorlevel 1 (
    echo [INFO] 未检测到调试 Edge（端口 9333），正在拉起并提示你登录微信读书...
    "%PY%" "tools\start_weread_edge.py"
    echo.
    echo [INFO] 请在弹出的 Edge 窗口中完成微信读书登录，并打开一个公众号阅读器页后，
    echo        回到本窗口按任意键继续（或直接关闭窗口）。
    pause
) else (
    echo [OK] 调试 Edge 已就绪（端口 9333）。
)
echo.

REM ---- 4. 执行主流程：抓取 + AI + 报告 + Git Push ----
REM     CI=true  -> 走 main.py 的 commit_and_push（data: 前缀 + git push origin main）
REM     FORCE=1  -> 绕过"一天一次"调度闸门，保证每次真的抓
REM     PYTHONIOENCODING=utf-8 -> 避免 Windows 控制台 GBK 撞 emoji 崩溃
echo [INFO] 正在抓取并生成报告（CI=true 将自动 git 提交并推送）...
set "CI=true"
set "FORCE=1"
set "PYTHONIOENCODING=utf-8"
"%PY%" src\main.py
set "RC=%ERRORLEVEL%"

if "%RC%"=="0" (
    echo.
    echo ============================================
    echo   全流程完成！报告已生成，并已自动 Git Push。
    echo   最新报告见：output\ 目录
    echo ============================================
    goto :ok
) else (
    echo.
    echo [错误] 主流程返回错误码 %RC%。
    echo        检查上方日志。常见原因：未配 API Key / 调试 Edge 未登录 / 网络 / git 凭据。
    goto :fail
)

:ok
endlocal
exit /b 0

:fail
echo.
echo [TJU Daily Bot] 执行失败，见上方日志。
endlocal
exit /b 1

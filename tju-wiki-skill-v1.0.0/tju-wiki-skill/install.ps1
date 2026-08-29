# ============================================================
# 北洋维基查询 Skill — 一键安装脚本 (Windows PowerShell)
# 用法: powershell -ExecutionPolicy Bypass -File install.ps1 [-Target <目录>]
# 默认安装到 %USERPROFILE%\.codebuddy\skills\tju-wiki
# ============================================================
param(
    [string]$Target = ""
)
$ErrorActionPreference = "Stop"

Write-Host "📦 北洋维基查询 Skill 安装程序" -ForegroundColor Cyan
$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Target) {
    $Target = Join-Path $env:USERPROFILE ".codebuddy\skills\tju-wiki"
}
Write-Host "   目标目录: $Target"
Write-Host ""

# 1. 检查 python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "❌ 错误: 未找到 python，请先安装 Python 3 (https://www.python.org/downloads/)" -ForegroundColor Red
    exit 1
}
Write-Host "✔ python: $(& python --version)"

# 2. 检查/安装依赖
python -c "import requests, bs4" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "✔ 依赖已满足 (requests, beautifulsoup4)"
} else {
    Write-Host "⬇ 正在安装依赖 requests + beautifulsoup4 ..." -ForegroundColor Yellow
    python -m pip install --quiet requests beautifulsoup4
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ 依赖安装失败，请手动执行: python -m pip install requests beautifulsoup4" -ForegroundColor Red
        exit 1
    }
    Write-Host "✔ 依赖安装完成"
}

# 3. 复制文件
New-Item -ItemType Directory -Force -Path $Target | Out-Null
Copy-Item "$Src\SKILL.md" "$Src\tju_wiki.py" $Target -Force

# 4. 验证
& python "$Target\tju_wiki.py" latest 1 *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "✔ 安装验证通过 (已能访问北洋维基)"
} else {
    Write-Host "⚠ 文件已复制，但网络验证未通过（可能网络受限，不影响本机调用）" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "✅ 安装完成!" -ForegroundColor Green
Write-Host "   手动测试: python `"$Target\tju_wiki.py`" search 校历"
Write-Host "   在 agent 中直接提问天大相关问题时将自动调用本 skill。"

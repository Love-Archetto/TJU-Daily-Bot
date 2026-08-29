#!/usr/bin/env bash
# ============================================================
# 北洋维基查询 Skill — 一键安装脚本 (Linux / macOS)
# 用法: bash install.sh [目标目录]
# 默认安装到 $HOME/.codebuddy/skills/tju-wiki
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$HOME/.codebuddy/skills/tju-wiki}"

echo "📦 北洋维基查询 Skill 安装程序"
echo "   目标目录: $TARGET"
echo

# 1. 检查 python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ 错误: 未找到 python3，请先安装 Python 3 (https://www.python.org/downloads/)"
  exit 1
fi
echo "✔ python3: $(python3 --version)"

# 2. 检查/安装依赖
if python3 -c "import requests, bs4" >/dev/null 2>&1; then
  echo "✔ 依赖已满足 (requests, beautifulsoup4)"
else
  echo "⬇ 正在安装依赖 requests + beautifulsoup4 ..."
  python3 -m pip install --quiet requests beautifulsoup4
  python3 -c "import requests, bs4" >/dev/null 2>&1 || {
    echo "❌ 依赖安装失败，请手动执行: python3 -m pip install requests beautifulsoup4"
    exit 1
  }
  echo "✔ 依赖安装完成"
fi

# 3. 复制文件
mkdir -p "$TARGET"
cp "$SCRIPT_DIR/SKILL.md" "$SCRIPT_DIR/tju_wiki.py" "$TARGET/"
chmod +x "$TARGET/tju_wiki.py"

# 4. 验证
if python3 "$TARGET/tju_wiki.py" latest 1 >/dev/null 2>&1; then
  echo "✔ 安装验证通过 (已能访问北洋维基)"
else
  echo "⚠ 文件已复制，但网络验证未通过（可能网络受限，不影响本机调用）"
fi

echo
echo "✅ 安装完成!"
echo "   手动测试: python3 $TARGET/tju_wiki.py search 校历"
echo "   在 agent 中直接提问天大相关问题时将自动调用本 skill。"

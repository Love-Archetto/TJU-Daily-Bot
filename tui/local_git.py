"""Git 操作封装 — 提供安全的 Git 操作接口。

实现：
- commit_only(message): git add -A && git commit
- commit_and_push(message): commit_only + git push
- pull_latest(): git pull --rebase
- get_output_files(): 返回 output/ 下文件列表
- check_conflicts(): 检测冲突文件
"""

import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _run_git(args: list[str], capture: bool = True) -> tuple[int, str, str]:
    """运行 git 命令.

    Returns:
        (returncode, stdout, stderr)
    """
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=PROJECT_ROOT,
            capture_output=capture,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Git command timed out"
    except FileNotFoundError:
        return -1, "", "Git not found"


def commit_only(message: str) -> dict[str, Any]:
    """仅提交不推送.

    Args:
        message: 提交信息（自动加 data: 前缀）

    Returns:
        {"success": bool, "message": str}
    """
    full_message = f"data: {message}"
    rc, out, err = _run_git(["add", "-A"])
    if rc != 0:
        return {"success": False, "message": f"git add failed: {err}"}

    rc, out, err = _run_git(["commit", "-m", full_message])
    if rc == 0:
        logger.info("Committed: %s", full_message)
        return {"success": True, "message": f"Committed: {full_message}"}
    elif "nothing to commit" in err.lower() or "nothing to commit" in out.lower():
        return {"success": True, "message": "Nothing to commit"}
    else:
        return {"success": False, "message": f"Commit failed: {err}"}


def commit_and_push(message: str) -> dict[str, Any]:
    """提交并推送.

    Args:
        message: 提交信息（自动加 data: 前缀）

    Returns:
        {"success": bool, "message": str}
    """
    result = commit_only(message)
    if not result["success"]:
        return result

    rc, out, err = _run_git(["push", "origin", "main"])
    if rc == 0:
        logger.info("Pushed successfully")
        return {"success": True, "message": "Committed and pushed"}
    else:
        return {"success": False, "message": f"Push failed: {err}"}


def pull_latest() -> dict[str, Any]:
    """拉取最新代码.

    Returns:
        {"success": bool, "message": str}
    """
    rc, out, err = _run_git(["pull", "--rebase", "origin", "main"])
    if rc == 0:
        return {"success": True, "message": out or "Pulled successfully"}
    else:
        return {"success": False, "message": f"Pull failed: {err}"}


def get_output_files() -> list[str]:
    """返回 output/ 目录下 .md 文件列表."""
    output_dir = os.path.join(PROJECT_ROOT, "output")
    if not os.path.isdir(output_dir):
        return []
    files = sorted(
        [f for f in os.listdir(output_dir) if f.endswith(".md")],
        reverse=True,
    )
    return files


def check_conflicts() -> list[str]:
    """检测冲突文件列表.

    Returns:
        冲突文件路径列表
    """
    rc, out, err = _run_git(["diff", "--name-only", "--diff-filter=U"])
    if rc != 0:
        return []
    return [line for line in out.split("\n") if line]
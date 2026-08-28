"""工具层 — 提供 TUI Agent 可调用的工具函数。

严格遵循路径白名单：
- read: config/, output/, state.json
- write: 仅 config/
- 禁止写入 state.json 或 history/
"""

import logging
import os
import platform
import subprocess
from typing import Any

import yaml

from .local_git import commit_and_push, commit_only, get_output_files
from .search_handler import SearchHandler

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 路径白名单
READ_ALLOWED_PREFIXES = [
    os.path.join(PROJECT_ROOT, "config"),
    os.path.join(PROJECT_ROOT, "output"),
    os.path.join(PROJECT_ROOT, "state.json"),
]
WRITE_ALLOWED_PREFIX = os.path.join(PROJECT_ROOT, "config")


def _resolve_path(path: str) -> str:
    """解析并验证路径."""
    if not os.path.isabs(path):
        path = os.path.join(PROJECT_ROOT, path)
    return os.path.normpath(path)


def read_file(path: str) -> dict[str, Any]:
    """读取文件内容.

    Args:
        path: 文件路径（相对于项目根目录）

    Returns:
        {"success": bool, "content": str, "message": str}
    """
    resolved = _resolve_path(path)
    # 检查白名单
    allowed = any(resolved.startswith(p) for p in READ_ALLOWED_PREFIXES)
    if not allowed:
        return {"success": False, "content": "", "message": f"Permission denied: {path}"}

    if not os.path.exists(resolved):
        return {"success": False, "content": "", "message": f"File not found: {path}"}

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
        return {"success": True, "content": content, "message": ""}
    except Exception as e:
        return {"success": False, "content": "", "message": str(e)}


def write_file(path: str, content: str) -> dict[str, Any]:
    """写入文件（仅限 config/ 目录）.

    Args:
        path: 文件路径（相对于项目根目录）
        content: 文件内容

    Returns:
        {"success": bool, "message": str}
    """
    resolved = _resolve_path(path)
    # 严格检查白名单
    if not resolved.startswith(WRITE_ALLOWED_PREFIX):
        raise PermissionError(f"Write denied: {path}. Only config/ is writable.")

    try:
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
        return {"success": True, "message": f"Written: {path}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def append_keyword(word: str) -> dict[str, Any]:
    """追加关键词到 keywords.txt.

    Args:
        word: 要添加的关键词

    Returns:
        {"success": bool, "message": str}
    """
    keywords_path = os.path.join(PROJECT_ROOT, "config", "keywords.txt")
    try:
        # 读出现有关键词
        existing = []
        if os.path.exists(keywords_path):
            with open(keywords_path, "r", encoding="utf-8") as f:
                existing = [line.strip() for line in f if line.strip()]

        if word.strip() in existing:
            return {"success": True, "message": f"Keyword already exists: {word}"}

        with open(keywords_path, "a", encoding="utf-8") as f:
            f.write(f"\n{word.strip()}")
        return {"success": True, "message": f"Keyword added: {word}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def update_profile(field: str, value: str) -> dict[str, Any]:
    """更新用户画像.

    Args:
        field: 字段名（degree/college/major）
        value: 值

    Returns:
        {"success": bool, "message": str}
    """
    if field not in ("degree", "college", "major"):
        return {"success": False, "message": f"Invalid field: {field}"}

    profile_path = os.path.join(PROJECT_ROOT, "config", "user_profile.yaml")
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
        profile[field] = value
        with open(profile_path, "w", encoding="utf-8") as f:
            yaml.dump(profile, f, allow_unicode=True, default_flow_style=False)
        return {"success": True, "message": f"Profile updated: {field} = {value}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def list_outputs() -> dict[str, Any]:
    """列出所有输出报告.

    Returns:
        {"success": bool, "files": [...], "message": str}
    """
    try:
        files = get_output_files()
        return {"success": True, "files": files, "message": f"{len(files)} reports found"}
    except Exception as e:
        return {"success": False, "files": [], "message": str(e)}


def open_report(filename: str) -> dict[str, Any]:
    """用系统编辑器打开报告.

    Args:
        filename: 报告文件名

    Returns:
        {"success": bool, "message": str}
    """
    filepath = _resolve_path(os.path.join("output", filename))
    if not os.path.exists(filepath):
        return {"success": False, "message": f"Report not found: {filename}"}

    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(filepath)
        elif system == "Darwin":
            subprocess.run(["open", filepath], check=True)
        else:
            subprocess.run(["xdg-open", filepath], check=True)
        return {"success": True, "message": f"Opened: {filename}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def search(query: str, source: str | None = None) -> dict[str, Any]:
    """搜索已抓取内容.

    Args:
        query: 搜索关键词
        source: 限定信源名称（可选）

    Returns:
        {"success": bool, "results": [...], "message": str}
    """
    try:
        handler = SearchHandler()
        results = handler.search(query, source)
        if not results:
            # 降级提示
            return {
                "success": True,
                "results": [],
                "message": f"No results found for '{query}'",
            }
        return {"success": True, "results": results, "message": f"{len(results)} results"}
    except Exception as e:
        return {"success": False, "results": [], "message": str(e)}


def git_commit_only(message: str) -> dict[str, Any]:
    """仅提交不推送."""
    return commit_only(message)


def git_commit_push(message: str) -> dict[str, Any]:
    """提交并推送."""
    return commit_and_push(message)
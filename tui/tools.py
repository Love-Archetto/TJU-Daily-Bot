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

from .local_git import commit_and_push, commit_only, get_output_files, pull_latest

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
    """在历史 output 报告中全文搜索.

    策略：
    1. 搜索前自动 git pull，把云端累积的历史报告同步到本地,保证 output/ 是完整的。
    2. 遍历 output/*.md，用关键词做全文匹配（大小写不敏感）。
    3. 命中则返回报告文件名、匹配行及上下文。

    Args:
        query: 搜索关键词（支持空格分隔多词,需全部命中）
        source: 保留参数（兼容），当前忽略按信源过滤，改为全文搜

    Returns:
        {"success": bool, "results": [...], "message": str}
    """
    try:
        # 1. 先同步历史报告
        pull_result = pull_latest()
        if not pull_result.get("success"):
            logger.warning("搜索前 git pull 失败（继续搜本地现有报告）: %s", pull_result.get("message"))

        # 2. 全文搜 output/
        output_dir = os.path.join(PROJECT_ROOT, "output")
        if not os.path.isdir(output_dir):
            return {"success": True, "results": [], "message": "output/ 目录不存在,暂无历史报告可搜索"}

        keywords = [k.lower() for k in query.split() if k.strip()]
        hits = []
        for fname in sorted(os.listdir(output_dir), reverse=True):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(output_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except Exception as e:
                logger.warning("读取报告失败 %s: %s", fname, e)
                continue
            # 逐行匹配
            matched = []
            for i, line in enumerate(lines):
                low = line.lower()
                if all(kw in low for kw in keywords):
                    matched.append({
                        "line": i + 1,
                        "text": line.strip()[:120],
                    })
            if matched:
                hits.append({
                    "file": fname,
                    "matches": matched[:5],   # 每文件最多保留 5 个命中行,避免过载
                    "total": len(matched),
                })

        if not hits:
            return {"success": True, "results": [], "message": f"在历史报告中未找到 '{query}'"}
        return {"success": True, "results": hits, "message": f"在 {len(hits)} 份历史报告中找到相关内容"}

    except Exception as e:
        return {"success": False, "results": [], "message": str(e)}


def get_tju_wiki_response(cmd: str, args: str = "") -> dict[str, Any]:
    """调用北洋维基查询工具 (tju_wiki.py), 返回查询文本.

    Args:
        cmd: search / cat / cats / read / latest / home 之一
        args: 命令参数(如关键词、分类名、词条URL)

    Returns:
        {"success": bool, "reply": str, "message": str}
    """
    wiki_path = os.path.join(PROJECT_ROOT, "src", "tju_wiki.py")
    if not os.path.exists(wiki_path):
        return {"success": False, "reply": "", "message": "北洋维基脚本缺失 (src/tju_wiki.py)"}

    argv = ["python", wiki_path, cmd]
    if args:
        # 拆成多参数(支持空格分隔, 如 read url / search 关键词 / cat 分类)
        argv.extend(args.split())
    # 强制子进程 UTF-8 输出，避免 emoji 在 GBK 终端/管道下报 UnicodeEncodeError
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                              encoding="utf-8", errors="replace", env=child_env)
        text = proc.stdout.strip()
        if not text:
            text = proc.stderr.strip() or "(无输出)"
        return {"success": proc.returncode == 0, "reply": text, "message": text[:80]}
    except subprocess.TimeoutExpired:
        return {"success": False, "reply": "", "message": "北洋维基查询超时"}
    except Exception as e:
        return {"success": False, "reply": "", "message": f"查询失败: {e}"}


def git_commit_only(message: str) -> dict[str, Any]:
    """仅提交不推送."""
    return commit_only(message)


def git_commit_push(message: str) -> dict[str, Any]:
    """提交并推送."""
    return commit_and_push(message)
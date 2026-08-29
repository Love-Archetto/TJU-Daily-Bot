"""诊断 we-mp-rss /rss/fresh 返回结构，排查日报标题/链接解析异常。

现象：生成的日报里 title 显示为公众号名、link 显示为 rss/MP_WXS_xxx（相对路径），
与预期（文章标题+完整链接）不符。本脚本 dump 原始 RSS XML + feedparser 解析结果，
用于定位是「we-mp-rss 返回的 title/link 就是这样」还是「我的解析拿错了字段」。

用法：
    python tools/diagnose_wemp_rss.py [--base http://localhost:8001]
"""

import argparse
import os
import sys
from pathlib import Path

import feedparser
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 复用项目的 RSS 客户端以保持地址一致
from src.crawler.wemp_rss_crawler import get_base_url  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=None)
    parser.add_argument("--max-entries", type=int, default=8, help="打印前 N 条")
    args = parser.parse_args()

    base = (args.base or get_base_url()).rstrip("/")
    url = f"{base}/rss/fresh"
    print(f"请求: {url}")
    try:
        resp = requests.get(url, timeout=40)
        print(f"HTTP {resp.status_code}")
    except requests.RequestException as e:
        print(f"请求失败: {e}")
        return

    text = resp.text
    out = []
    out.append("=" * 60)
    out.append("原始 XML 前 2000 字符:")
    out.append(text[:2000])
    out.append("=" * 60)

    feed = feedparser.parse(text)
    out.append(f"feedparser entries 数量: {len(feed.entries)}")
    out.append("")
    for i, e in enumerate(feed.entries[: args.max_entries]):
        out.append(f"--- entry #{i} ---")
        out.append(f"  id        : {getattr(e, 'id', '')!r}")
        out.append(f"  title     : {getattr(e, 'title', '')!r}")
        out.append(f"  link      : {getattr(e, 'link', '')!r}")
        out.append(f"  author    : {getattr(e, 'author', '')!r}")
        src = getattr(e, "source", None)
        out.append(f"  source    : {getattr(src, 'title', '') if src else ''!r}")
        out.append(f"  published : {getattr(e, 'published', '')!r}")
        # 所有含 title/link 的额外字段（诊断用）
        extra = {k: v for k, v in e.items() if "title" in k or "link" in k or "url" in k}
        out.append(f"  相关字段  : { {k: (str(v)[:60]) for k, v in extra.items()} }")
        out.append("")

    result = "\n".join(out)
    print(result)
    # 写文件便于查看（避免终端中文截断）
    dump_file = PROJECT_ROOT / "tools" / "_wemp_rss_dump.txt"
    dump_file.write_text(result, encoding="utf-8")
    print(f"诊断详情已写入 {dump_file}")


if __name__ == "__main__":
    main()

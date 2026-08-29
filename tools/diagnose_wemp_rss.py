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

API_PREFIX = "/api/v1/wx"


def _login(base: str, user: str = "admin", pwd: str = "admin@123") -> str:
    """登录 we-mp-rss 拿 token."""
    r = requests.post(f"{base}{API_PREFIX}/auth/login",
                      data={"username": user, "password": pwd}, timeout=30)
    r.raise_for_status()
    return (r.json().get("access_token") or (r.json().get("data") or {}).get("access_token") or "")


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
        out.append(f"--- 订阅项 #{i} ---")
        out.append(f"  id        : {getattr(e, 'id', '')!r}")
        out.append(f"  title     : {getattr(e, 'title', '')!r}")
        out.append(f"  link      : {getattr(e, 'link', '')!r}")
        out.append("")

    # === 关键：测试 /feed/{feed_id}.xml 是否返回真文章标题/链接 ===
    out.append("=" * 60)
    out.append("测试 /feed/{feed_id}.xml（应返回文章标题+完整链接）")
    out.append("=" * 60)
    # 用 sources.yaml 的 fakeid 构造正确 feed_id（避免 /rss/fresh 的 rss/ 前缀 id）
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.crawler.wemp_rss_crawler import _discover_feed_ids
    subs = _discover_feed_ids(base)
    if not subs:
        out.append("  (sources.yaml 无订阅可测)")
    else:
        for fid, name in subs[:3]:
            feed_url = f"{base}/feed/{fid}.xml?is_update=true&limit=5"
            out.append(f"  订阅: {name} -> {fid}")
            try:
                r2 = requests.get(feed_url, timeout=60)
                f2 = feedparser.parse(r2.text)
                out.append(f"    HTTP {r2.status_code} 文章数: {len(f2.entries)}")
                for j, en in enumerate(f2.entries[:3]):
                    out.append(f"      - title: {getattr(en, 'title', '')!r}")
                    out.append(f"        link : {getattr(en, 'link', '')!r}")
            except requests.RequestException as e:
                out.append(f"    请求异常: {e}")

    out.append("=" * 60)
    out.append("登录并测试微信读书公众号采集连接（定位 0 文章根因）")
    out.append("=" * 60)
    try:
        token = _login(base)
        if not token:
            out.append("  登录失败（拿不到 token）")
        else:
            out.append(f"  登录成功")
            hdr = {"Authorization": f"Bearer {token}"}
            # 微信读书采集状态
            try:
                rs = requests.get(f"{base}{API_PREFIX}/weread", headers=hdr, timeout=30)
                out.append(f"  微信读书状态 HTTP {rs.status_code}: {rs.text[:300]}")
            except requests.RequestException as e:
                out.append(f"  /weread 状态请求异常: {e}")
            # 对一个订阅测 mp 采集连接
            if subs:
                fid = subs[0][0]
                try:
                    rt = requests.post(
                        f"{base}{API_PREFIX}/weread/mp/test",
                        headers=hdr, json={"book_id": fid.replace("MP_WXS_", "")},
                        timeout=60,
                    )
                    out.append(f"  mp/test HTTP {rt.status_code}: {rt.text[:500]}")
                except requests.RequestException as e:
                    out.append(f"  mp/test 异常: {e}")
    except Exception as e:
        out.append(f"  诊断异常: {e}")

    result = "\n".join(out)
    print(result)
    # 写文件便于查看（避免终端中文截断）
    dump_file = PROJECT_ROOT / "tools" / "_wemp_rss_dump.txt"
    dump_file.write_text(result, encoding="utf-8")
    print(f"诊断详情已写入 {dump_file}")


if __name__ == "__main__":
    main()

"""公众号文章增强 — 发布时间兜底.

正文与封面现在由 weread_subscribe 在抓取时一并取回(/web/mp/content 的 og:url/og:image/
#js_content), 本模块不再自己发请求去 mp.weixin —— 那条路已被微信风控封死(数据中心 IP /
无头客户端一律拦), 且 UA 伪装方案(fetch_body_ua)已删除。这里只剩一件事: 万一某篇的
publish_time 仍为空, 用轻量 requests 试着补一次(仅 mp.weixin 原文页的 createTime)。
失败即跳过, 不阻塞。

策略:
  只对增量过滤后的"公众号新文章"处理(数量通常很少)。
  每篇独立 try/except, 失败跳过不阻塞。
"""

import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

# 每轮最多补时间的篇数(防某轮突然大量新文章拖垮任务; 可通过 WECHAT_SUMMARY_MAX 调)
MAX_ARTICLES = int(os.environ.get("WECHAT_SUMMARY_MAX", "10"))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _is_mp_article(article: dict) -> bool:
    """判断是否为公众号文章(有 mp.weixin.qq.com 原文链接)."""
    link = article.get("link", "")
    return "mp.weixin.qq.com" in link


def _fetch_publish_time(link: str, timeout: int = 12) -> str:
    """纯 requests 抓原文页 createTime(轻量). 失败返回空."""
    try:
        r = requests.get(link, headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code != 200:
            return ""
        html = r.content.decode("utf-8", errors="replace")
        m = re.search(r"createTime\s*=\s*'([^']*)'", html)
        if m:
            return m.group(1).strip()
        m2 = re.search(r'var\s+createTime\s*=\s*"([^"]*)"', html)
        if m2:
            return m2.group(1).strip()
    except Exception:
        pass
    return ""


def enhance_wechat_articles(articles: list[dict], cookie: str = "") -> list[dict]:
    """对公众号新文章补 publish_time(仅当缺失时).

    正文/封面已由 weread_subscribe 抓取时写入(content/image), 此处不动; 网页文章不处理。

    Args:
        articles: 增量过滤后的文章列表(改写原地, 返回同一列表)
        cookie: 保留兼容签名, 未使用

    Returns:
        增强后的同一列表
    """
    mp = [a for a in articles if _is_mp_article(a)]
    if not mp:
        return articles

    missing = [a for a in mp if not a.get("publish_time")][:MAX_ARTICLES]
    if missing:
        logger.info("公众号补时间: %d 篇缺失(处理上限 %d)", len(missing), MAX_ARTICLES)
    for a in missing:
        a["publish_time"] = _fetch_publish_time(a["link"])
        if a["publish_time"]:
            logger.info("  公众号补时间[%s]: %s", a.get("source", "?"), a["publish_time"])
    return articles

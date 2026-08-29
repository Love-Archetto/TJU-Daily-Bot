"""公众号 RSS 爬虫 — 从 we-mp-rss 服务拉取公众号文章聚合 RSS。

背景：
    we-mp-rss 是独立服务（本地 Docker 或 GitHub Actions service 容器），
    它负责扫码授权微信读书、批量抓取公众号、维护订阅与去重、生成 RSS。
    本模块只负责「拉取 we-mp-rss 生成的 RSS 并解析成统一文章结构」，
    不再自己调微信 API（避免触发账号级风控 freq control）。

接口（we-mp-rss）：
    GET /feed/{feed_id}  单个公众号 RSS
    GET /rss/fresh       更新所有订阅并返回聚合 RSS（本模块使用）
    服务地址默认 http://localhost:8001，可用环境变量 WE_MP_RSS_BASE 覆盖
"""

import logging
import os
from datetime import datetime
from typing import Any

import feedparser
import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
# we-mp-rss 服务地址（本地默认 8001，Actions 里由 service 提供同地址）
DEFAULT_BASE = os.environ.get("WE_MP_RSS_BASE", "http://localhost:8001")


def get_base_url() -> str:
    """返回 we-mp-rss 服务根地址（去尾部 /）."""
    return (os.environ.get("WE_MP_RSS_BASE", DEFAULT_BASE) or DEFAULT_BASE).rstrip("/")


def healthy() -> bool:
    """检测 we-mp-rss 服务是否可访问."""
    try:
        resp = requests.get(f"{get_base_url()}/", timeout=REQUEST_TIMEOUT)
        return resp.status_code < 500
    except requests.RequestException:
        return False


def _parse_entry(entry) -> dict[str, Any]:
    """把单个 feedparser entry 转成统一文章结构."""
    publish_time = ""
    for attr in ("published_parsed", "updated_parsed"):
        ts = getattr(entry, attr, None)
        if ts:
            try:
                publish_time = datetime(*ts[:6]).strftime("%Y-%m-%d %H:%M:%S")
                break
            except Exception:
                pass
    return {
        "title": getattr(entry, "title", ""),
        "link": getattr(entry, "link", ""),
        "publish_time": publish_time,
        "summary": getattr(entry, "summary", ""),
    }


def fetch_all_articles(base: str | None = None) -> list[dict[str, Any]]:
    """从 we-mp-rss 拉取聚合 RSS（触发一次更新 + 返回所有文章）.

    对每个公众号，feedparser 解析出的 entry 里的 source/author 视 we-mp-rss 而定，
    无法区分时统一标为“公众号”；若 entry 有 source 字段则用之。

    Args:
        base: we-mp-rss 服务根地址，默认取环境变量 WE_MP_RSS_BASE 或 localhost:8001

    Returns:
        [{"title","link","publish_time","summary","source"}, ...]
    """
    base = (base or get_base_url()).rstrip("/")
    url = f"{base}/rss/fresh"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.encoding = "utf-8"
        feed = feedparser.parse(resp.text)
    except requests.RequestException as e:
        logger.error("we-mp-rss feed request failed: %s", e)
        return []

    if feed.bozo and not feed.entries:
        logger.warning("we-mp-rss feed empty/failed (%s): %s", url, feed.bozo_exception)
        return []

    articles = []
    for entry in feed.entries:
        arts = _parse_entry(entry)
        # we-mp-rss 聚合 RSS 中作者/标签若可解析则作 source
        source = getattr(entry, "author", "") or getattr(entry, "source", {}).get("title", "")
        arts["source"] = source or "公众号"
        articles.append(arts)

    logger.info("we-mp-rss: fetched %d articles", len(articles))
    return articles

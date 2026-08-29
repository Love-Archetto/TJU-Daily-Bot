"""公众号 RSS 爬虫 — 从 we-mp-rss 拉取公众号文章。

背景：
    we-mp-rss 是独立服务（GitHub Actions service 容器），负责扫码授权微信读书、
    批量抓取公众号、维护订阅与去重、生成 RSS 与文章接口。
    本模块从 we-mp-rss 拉取「真正的文章」，不自己调微信 API。

接口（we-mp-rss，均为裸路径，无需登录）：
    GET /rss/fresh          更新订阅并返回订阅源列表（item 的 <id> 为 feed_id，即 MP_WXS_xxx）
    GET /feed/{feed_id}.xml 单个公众号的文章 RSS（title=文章标题, link=完整链接）
    服务地址默认 http://localhost:8001，可用环境变量 WE_MP_RSS_BASE 覆盖

注意：不要用 /rss/fresh 当文章源——它返回的是订阅源列表（title=公众号名, link=相对路径），
      不含真正文章。必须遍历 /feed/{feed_id}.xml 拿文章。
"""

import base64
import logging
import os
import time
from datetime import datetime
from typing import Any

import feedparser
import requests
import yaml

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 40
FEED_FETCH_INTERVAL = 3  # 每个 feed 之间的间隔秒数，避免 we-mp-rss 抓取过频
PER_FEED_LIMIT = 20      # 每个公众号最多取多少篇文章
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
        "title": getattr(entry, "title", "") or "",
        "link": getattr(entry, "link", "") or "",
        "publish_time": publish_time,
        "summary": getattr(entry, "summary", "") or "",
    }


def _fakeid_to_feed_id(fakeid: str) -> str:
    """把 base64 fakeid 转成 we-mp-rss 的 feed_id.

    we-mp-rss 添加订阅时用 base64 fakeid(如 MjM5NzkwNzU0Mg==)，内部解码为数字 id
    并生成 feed；其 RSS 地址为 /feed/MP_WXS_{解码id}.xml。
    """
    try:
        decoded = base64.b64decode(fakeid).decode("utf-8")
    except Exception:
        decoded = fakeid
    return f"MP_WXS_{decoded}"


def _discover_feed_ids(base: str) -> list[tuple[str, str]]:
    """从 config/sources.yaml 直接构造订阅的 (feed_id, 公众号名)，不依赖 /rss/fresh.

    注意：/rss/fresh 的 <id> 常带 rss/ 前缀且与真实 feed_id 不一致，改用 sources.yaml
    的 fakeid 构造更可靠（与 we-mp-rss 添加订阅时生成的 feed id 一致）。
    """
    sources_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "sources.yaml")
    try:
        with open(sources_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        logger.error("读取 sources.yaml 失败: %s", e)
        return []

    items = []
    for s in data.get("sources", []):
        if s.get("type") != "wechat_rss":
            continue
        fakeid = s.get("fakeid", "") or ""
        if not fakeid:
            continue
        fid = _fakeid_to_feed_id(fakeid)
        items.append((fid, s.get("name", fid)))
    logger.info("从 sources.yaml 构造 %d 个订阅", len(items))
    return items


def _fetch_feed_articles(base: str, feed_id: str) -> list[dict[str, Any]]:
    """请求单个公众号的文章 RSS（is_update=True 触发抓取）."""
    url = f"{base}/feed/{feed_id}.xml?is_update=true&limit={PER_FEED_LIMIT}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.encoding = "utf-8"
        feed = feedparser.parse(resp.text)
    except requests.RequestException as e:
        logger.warning("抓取 feed %s 失败: %s", feed_id, e)
        return []

    if feed.bozo and not feed.entries:
        logger.warning("feed %s 空/失败: %s", feed_id, feed.bozo_exception)
        return []

    articles = []
    for entry in feed.entries:
        arts = _parse_entry(entry)
        # title 应为文章标题；若为空则退回公众号名（feed id 前缀）
        if not arts["title"]:
            arts["title"] = feed_id
        articles.append(arts)
    return articles


def fetch_all_articles(base: str | None = None) -> list[dict[str, Any]]:
    """从 we-mp-rss 拉取所有已订阅公众号的文章.

    Args:
        base: we-mp-rss 服务根地址，默认取环境变量 WE_MP_RSS_BASE 或 localhost:8001

    Returns:
        [{"title","link","publish_time","summary","source"}, ...]
    """
    base = (base or get_base_url()).rstrip("/")

    # 1. 找所有已订阅公众号的 feed_id
    sub_sources = _discover_feed_ids(base)
    if not sub_sources:
        logger.warning("未发现任何已订阅公众号（先运行订阅初始化）")
        return []

    # 2. 逐个 feed 拉文章
    articles = []
    for idx, (feed_id, name) in enumerate(sub_sources):
        items = _fetch_feed_articles(base, feed_id)
        for a in items:
            a["source"] = name or feed_id
            articles.append(a)
        if idx < len(sub_sources) - 1:
            time.sleep(FEED_FETCH_INTERVAL)

    logger.info("we-mp-rss: fetched %d articles from %d feeds", len(articles), len(sub_sources))
    return articles

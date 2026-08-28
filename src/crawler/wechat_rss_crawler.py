"""公众号 RSS 爬虫 — 通过 we-mp-rss 服务获取公众号文章。

实现：
- check_service_health(): 检测 we-mp-rss 服务是否可访问
- fetch_from_rss(rss_url): 通过 RSS 获取文章
- fetch_from_api(api_url, gzh_name): 通过 REST API 获取文章
- fetch_articles_from_gzh(gzh_name): 主函数，从 sources.yaml 读取配置
- CI 环境跳过：检测 CI=true 时返回空列表
"""

import logging
import os
from typing import Any

import feedparser
import requests
import yaml

logger = logging.getLogger(__name__)

# we-mp-rss 服务地址
DEFAULT_SERVICE_URL = "http://localhost:4000"
HEALTH_ENDPOINT = "/health"
REQUEST_TIMEOUT = 10

# 降级状态
class ServiceStatus:
    OK = "ok"
    UNAVAILABLE = "unavailable"
    MANUAL_REQUIRED = "manual_required"


def _is_ci_environment() -> bool:
    """检测是否在 GitHub Actions 环境."""
    return os.environ.get("CI", "").lower() == "true"


def _load_sources() -> list[dict[str, Any]]:
    """加载 sources.yaml 中的公众号信源配置."""
    config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "sources.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return [s for s in data.get("sources", []) if s.get("type") == "wechat_rss"]
    except Exception as e:
        logger.error("Failed to load sources.yaml: %s", e)
        return []


def check_service_health(service_url: str = DEFAULT_SERVICE_URL) -> bool:
    """检测 we-mp-rss 服务是否可访问.

    Args:
        service_url: we-mp-rss 服务地址

    Returns:
        True 如果服务健康可访问
    """
    try:
        resp = requests.get(f"{service_url}{HEALTH_ENDPOINT}", timeout=REQUEST_TIMEOUT)
        return resp.status_code == 200
    except requests.RequestException:
        # 也尝试请求根路径
        try:
            resp = requests.get(service_url, timeout=REQUEST_TIMEOUT)
            return resp.status_code == 200
        except requests.RequestException:
            return False


def fetch_from_rss(rss_url: str) -> list[dict[str, Any]]:
    """通过 RSS 源获取文章列表.

    Args:
        rss_url: RSS 订阅地址

    Returns:
        [{"title":..., "link":..., "publish_time":..., "summary":...}, ...]
    """
    try:
        feed = feedparser.parse(rss_url)
    except Exception as e:
        logger.error("Failed to parse RSS feed %s: %s", rss_url, e)
        return []

    if feed.bozo and not feed.entries:
        logger.warning("RSS feed %s is malformed: %s", rss_url, feed.bozo_exception)
        return []

    articles = []
    for entry in feed.entries:
        articles.append({
            "title": getattr(entry, "title", ""),
            "link": getattr(entry, "link", ""),
            "publish_time": getattr(entry, "published", ""),
            "summary": getattr(entry, "summary", ""),
        })

    logger.info("Fetched %d articles from RSS: %s", len(articles), rss_url)
    return articles


def fetch_from_api(api_url: str, gzh_name: str) -> list[dict[str, Any]]:
    """通过 REST API 获取文章列表.

    Args:
        api_url: API 地址
        gzh_name: 公众号名称

    Returns:
        [{"title":..., "link":..., "publish_time":..., "summary":...}, ...]
    """
    try:
        resp = requests.get(api_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error("Failed to fetch from API %s: %s", api_url, e)
        return []
    except ValueError as e:
        logger.error("Invalid JSON from API %s: %s", api_url, e)
        return []

    articles = []
    # 适配常见 API 响应格式
    items = data if isinstance(data, list) else data.get("articles", data.get("items", []))
    for item in items:
        articles.append({
            "title": item.get("title", ""),
            "link": item.get("link", item.get("url", "")),
            "publish_time": item.get("publish_time", item.get("published", item.get("date", ""))),
            "summary": item.get("summary", item.get("description", "")),
        })

    logger.info("Fetched %d articles from API for %s", len(articles), gzh_name)
    return articles


def fetch_articles_from_gzh(gzh_name: str) -> tuple[list[dict[str, Any]], str]:
    """从公众号获取文章列表（主入口）.

    Args:
        gzh_name: 公众号名称（在 sources.yaml 中定义）

    Returns:
        (articles, status) — articles 是文章列表，status 是 ServiceStatus 值
    """
    # CI 环境跳过
    if _is_ci_environment():
        logger.info("Skipping wechat RSS in Actions environment")
        return [], ServiceStatus.OK

    # 获取该公众号的配置
    sources = _load_sources()
    gzh_config = next((s for s in sources if s.get("name") == gzh_name), None)
    if gzh_config is None:
        logger.warning("No config found for gzh: %s", gzh_name)
        return [], ServiceStatus.UNAVAILABLE

    # 检查服务健康
    if not check_service_health():
        logger.warning("we-mp-rss service is unavailable")
        return [], ServiceStatus.MANUAL_REQUIRED

    # 优先使用 RSS
    rss_url = gzh_config.get("rss_url", "")
    if rss_url:
        articles = fetch_from_rss(rss_url)
        if articles:
            return articles, ServiceStatus.OK

    # 回退到 API
    api_url = gzh_config.get("api_url", "")
    if api_url:
        articles = fetch_from_api(api_url, gzh_name)
        return articles, ServiceStatus.OK

    logger.warning("No RSS URL or API URL configured for %s", gzh_name)
    return [], ServiceStatus.MANUAL_REQUIRED
"""TJU 网站爬虫 — 根据 sources.yaml 配置抓取列表页文章。

实现：
- fetch_articles_from_list_page(url, selectors): 返回 [{"title":..., "link":..., "publish_time":...}]
- User-Agent 轮换，最多重试 3 次
- 错误时返回空列表并记录日志
"""

import logging
import random
import time
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

MAX_RETRIES = 3
REQUEST_TIMEOUT = 15


def _make_request(url: str) -> requests.Response | None:
    """带重试和 UA 轮换的 HTTP 请求."""
    for attempt in range(1, MAX_RETRIES + 1):
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.encoding = resp.apparent_encoding or "utf-8"
            if resp.status_code == 200:
                return resp
            else:
                logger.warning("Attempt %d: HTTP %d for %s", attempt, resp.status_code, url)
        except requests.RequestException as e:
            logger.warning("Attempt %d: request failed for %s: %s", attempt, url, e)
        if attempt < MAX_RETRIES:
            time.sleep(1 * attempt)
    return None


def fetch_articles_from_list_page(url: str, selectors: dict[str, str]) -> list[dict[str, Any]]:
    """从列表页抓取文章信息。

    Args:
        url: 列表页 URL
        selectors: 支持两种模式
          - 通用 CMS 模式(天大各部门官网常用): {"item_container": "li.list-item"} 或
            {"item_container": "li"}, 从每个 li 的 <a href*='/info/'> 提取标题+链接,
            从 li 文本提取日期。
          - 传统模式: {"title": "h2 a", "link": "h2 a", "time": ".date"}

    Returns:
        [{"title":..., "link":..., "publish_time":...}, ...]
    """
    resp = _make_request(url)
    if resp is None:
        logger.error("Failed to fetch list page: %s", url)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    articles: list[dict[str, Any]] = []

    # 通用 CMS 模式优先
    item_container = selectors.get("item_container")
    if item_container:
        items = soup.select(item_container)
        for it in items:
            # 优先 /info/ 的文章链接
            a_el = it.select_one('a[href*="/info/"]') or it.select_one("a")
            if a_el is None:
                continue
            title = a_el.get_text(strip=True)
            if not title:
                continue
            link = a_el.get("href", "")
            if link and not link.startswith("http"):
                link = urljoin(url, link)
            publish_time = _extract_date(it.get_text(" ")) or _extract_date(title)
            articles.append({
                "title": title,
                "link": link,
                "publish_time": publish_time,
            })
        logger.info("Fetched %d articles (CMS-mode) from %s", len(articles), url)
        return articles

    # 传统 title/link/time 模式
    title_sel = selectors.get("title", "a")
    link_sel = selectors.get("link", "a")
    time_sel = selectors.get("time", "")

    title_elements = soup.select(title_sel)
    for el in title_elements:
        title = el.get_text(strip=True)
        if not title:
            continue
        link = el.get("href", "")
        if link and not link.startswith("http"):
            link = urljoin(url, link)

        publish_time = ""
        if time_sel:
            time_el = el.find_parent("li") or el.find_parent("div")
            if time_el:
                time_found = time_el.select_one(time_sel)
                if time_found:
                    publish_time = time_found.get_text(strip=True)
            if not publish_time:
                time_found = soup.select_one(time_sel)
                if time_found:
                    publish_time = time_found.get_text(strip=True)

        articles.append({
            "title": title,
            "link": link,
            "publish_time": publish_time,
        })

    logger.info("Fetched %d articles from %s", len(articles), url)
    return articles


def _extract_date(text: str) -> str:
    """从文本中提取第一个日期(YYYY-MM-DD 或 YYYY-M-D 等)."""
    import re
    m = re.search(r"(\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2})", text)
    if m:
        return m.group(1).replace("/", "-").replace(".", "-")
    return ""
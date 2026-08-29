"""搜狗微信搜索公众号抓取 — 纯云端零人工拿公众号多篇。

背景/动机：
    - wechat2rss / RSSHub 等公共聚合服务未收录用户需要的天大公众号，用不了。
    - we-mp-rss web 模式需要扫码登录，且云端 IP 连不上微信公众平台扫码接口。
    - we-mp-rss 微信读书模式只能在云端抓每号最新 1 篇（单篇限制）。
    - 用户要求「纯云端全自动零人工 + 公众号多篇」。
    搜狗微信搜索（weixin.sogou.com）是公开入口，无需登录/收录名单，可搜到公众号多篇文章。

本模块：
    1. 对每个公众号名发搜狗微信搜索（type=2 搜文章），URL 编码中文 query。
    2. 解析搜索结果：文章标题 + 跳转链接 /link?url=...
    3. 跟进跳转，解析中间页 JS 的 `url += '...'` 拼接片段，还原真实微信文章 URL。
    4. 返回 [{"title","link","publish_time","source"}...]。

局限：
    - 搜狗按关键词返回，可能有少量同名/相关号文章混入（含关键词但非目标号）。
    - 无时间筛选，会含历史旧文；由上层 state.json 增量去重过滤已处理内容。
"""

import logging
import re
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

SEARCH_URL = "https://weixin.sogou.com/weixin"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

REQUEST_TIMEOUT = 20
# 每个公众号搜索后的间隔秒数，降低被搜狗限流的风险
QUERY_INTERVAL = 3.0
# 每个公众号最多解析多少篇
MAX_PER_ACCOUNT = 10


class SogouWechatCrawler:
    """搜狗微信搜索结果抓取."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def _search(self, keyword: str) -> str | None:
        """发一次搜狗微信搜索，返回 HTML；失败/反爬返回 None."""
        params = {"type": "2", "query": keyword, "ie": "utf8"}
        try:
            resp = self.session.get(SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
            resp.encoding = resp.apparent_encoding or "utf-8"
            html = resp.text
            if "验证码" in html or "antispider" in html or "安全验证" in html:
                logger.warning("搜狗搜索触发反爬: %s", keyword)
                return None
            return html
        except requests.RequestException as e:
            logger.warning("搜狗搜索请求失败 %s: %s", keyword, e)
            return None

    def _parse_results(self, html: str) -> list[dict[str, Any]]:
        """从搜狗搜索结果 HTML 提取 (标题, 跳转链接)."""
        items = []
        # 每个结果块是 <li ...> ... <h3><a href="/link?url=...">标题</a></h3>
        # 简化：匹配 h3>a 标题 + 它的 href
        blocks = re.findall(
            r'<h3>\s*<a[^>]*href="(/link\?url=[^"]*)"[^>]*>(.*?)</a>\s*</h3>',
            html, re.S)
        for href, title_html in blocks:
            title = re.sub(r"<[^>]+>", "", title_html).strip()
            if title:
                items.append({"title": title, "jump": href.replace("&amp;", "&")})
        return items

    def _reconstruct_url(self, jump_path: str) -> str | None:
        """跟进 /link?url= 跳转中间页，从 JS url 拼接还原真实微信文章链接."""
        link = "https://weixin.sogou.com" + jump_path
        try:
            resp = self.session.get(link, timeout=REQUEST_TIMEOUT, allow_redirects=False)
            if resp.status_code == 302:
                loc = resp.headers.get("Location", "")
                return loc if loc else None
            # 200 中间页：提取 url += '...' 片段拼接
            if resp.status_code == 200:
                parts = re.findall(r"url \+= '([^']*)'", resp.text)
                if parts:
                    return "".join(parts)
            return None
        except requests.RequestException as e:
            logger.warning("跳转还原失败 %s: %s", jump_path[:40], e)
            return None

    def fetch_account(self, name: str) -> list[dict[str, Any]]:
        """抓取单个公众号名的近期文章."""
        html = self._search(name)
        if not html:
            return []
        raw_items = self._parse_results(html)[:MAX_PER_ACCOUNT]

        articles = []
        for it in raw_items:
            real_url = self._reconstruct_url(it["jump"])
            if not real_url:
                continue
            articles.append({
                "title": it["title"],
                "link": real_url,
                "publish_time": "",
                "source": name,
            })
            time.sleep(0.8)  # 跳转间小间隔
        logger.info("搜狗[%s] 解析 %d 篇", name, len(articles))
        return articles

    def fetch_all(self, account_names: list[str]) -> list[dict[str, Any]]:
        """抓取多个公众号名的文章，汇总."""
        all_articles = []
        for i, name in enumerate(account_names):
            all_articles.extend(self.fetch_account(name))
            if i < len(account_names) - 1:
                time.sleep(QUERY_INTERVAL)
        logger.info("搜狗抓取共 %d 篇来自 %d 个公众号", len(all_articles), len(account_names))
        return all_articles


def fetch_wechat_articles(account_names: list[str] | None = None) -> list[dict[str, Any]]:
    """便捷入口：抓取公众号多篇.

    若 account_names 为空，自动从 config/sources.yaml 读取 type=wechat_rss 的公众号名。
    """
    if account_names is None:
        import os
        import yaml
        sp = os.path.join(os.path.dirname(__file__), "..", "..", "config", "sources.yaml")
        try:
            with open(sp, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            account_names = [s["name"] for s in data.get("sources", [])
                             if s.get("type") == "wechat_rss"]
        except Exception:
            account_names = []

    if not account_names:
        logger.warning("无公众号名可搜索")
        return []

    crawler = SogouWechatCrawler()
    return crawler.fetch_all(account_names)

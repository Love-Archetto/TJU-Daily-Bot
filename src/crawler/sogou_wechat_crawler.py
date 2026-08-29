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
# 每个公众号搜索后的间隔秒数(搜狗反爬经验约 3s; 我们取 3~5 随机, 降低规律性)
QUERY_INTERVAL_MIN = 3.0
QUERY_INTERVAL_MAX = 5.0
# 触发反爬时的等待重试间隔与最大重试次数
RATE_LIMIT_WAIT = 15.0
RATE_LIMIT_RETRIES = 2
# 每个公众号最多解析多少篇
MAX_PER_ACCOUNT = 10


class SogouWechatCrawler:
    """搜狗微信搜索结果抓取.

    反爬应对(参考 scrapy 反爬经验: 每次请求前从搜狗子域拿新 cookies + 随机 UA,
    避免长期用同一 Session 的同一组 cookies 被识别)。
    """

    def __init__(self):
        self.session = requests.Session()
        self._seed_session_cookies()

    def _seed_session_cookies(self) -> None:
        """先从搜狗子域(v.sogou.com)拿一组初始 cookies, 降低首搜被拦概率."""
        try:
            r = self.session.get(
                "https://v.sogou.com/v?ie=utf8&query=&p=40030600",
                headers={"User-Agent": UA, "Referer": "https://www.sogou.com/"},
                allow_redirects=False, timeout=15,
            )
            # 只保留 set-cookie; 不强制, 失败也继续
        except requests.RequestException as e:
            logger.info("获取搜狗初始 cookies 失败(继续): %s", e)

    def _new_session(self) -> requests.Session:
        """每次搜索新建一个承载新 cookies 的请求(带随机 UA 用固定 UA 亦可)."""
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        # 从搜狗子域预热 cookies(每步换, 让搜狗认为是不同会话)
        try:
            s.get("https://v.sogou.com/v?ie=utf8&query=&p=40030600",
                  headers={"User-Agent": UA}, allow_redirects=False, timeout=15)
        except requests.RequestException:
            pass
        return s

    def _search(self, keyword: str) -> str | None:
        """发一次搜狗微信搜索(每次新会话), 返回 HTML；失败/反爬返回 None."""
        session = self._new_session()
        params = {"type": "2", "query": keyword, "ie": "utf8"}
        try:
            resp = session.get(SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
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
        """抓取单个公众号名的近期文章，触发反爬时等待后重试."""
        html = None
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            html = self._search(name)
            if html is not None:
                break
            if attempt < RATE_LIMIT_RETRIES:
                logger.warning("搜狗[%s] 触发反爬，等待 %.0fs 后重试 (%d/%d)",
                               name, RATE_LIMIT_WAIT, attempt + 1, RATE_LIMIT_RETRIES)
                time.sleep(RATE_LIMIT_WAIT)
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
        import random
        all_articles = []
        for i, name in enumerate(account_names):
            all_articles.extend(self.fetch_account(name))
            if i < len(account_names) - 1:
                time.sleep(random.uniform(QUERY_INTERVAL_MIN, QUERY_INTERVAL_MAX))
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

    # 测试样本控制: 设 WECHAT_SAMPLE=N 则只抓前 N 个公众号(避免长时间跑)
    try:
        sample = int(os.environ.get("WECHAT_SAMPLE", "0") or 0)
        if sample > 0:
            account_names = account_names[:sample]
            logger.info("WECHAT_SAMPLE=%s, 本次仅抓前 %d 个公众号", sample, len(account_names))
    except ValueError:
        pass

    if not account_names:
        logger.warning("无公众号名可搜索")
        return []

    crawler = SogouWechatCrawler()
    return crawler.fetch_all(account_names)

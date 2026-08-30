"""公众号文章增强 — 补发布时间 + 正文(供 LLM 生成摘要).

背景:
  weread cover 接口只返回标题/封面/链接, 无摘要无时间。
  公众号原文页 (mp.weixin.qq.com/s/xxx) 里:
    - 时间: JS 变量 createTime(纯 requests 可拿)
    - 正文: Playwright 渲染 #js_content 拿纯文本(纯 requests 拿不到, JS 动态渲染)
  原理已验证(本地 probe 通过)。

策略:
  只对增量过滤后的"公众号新文章"处理(数量通常很少)。
  每篇独立 try/except, 失败跳过不阻塞; 设数量上限防整轮拖垮。
  复用同一 Playwright browser 循环处理, 省时间省连接。

依赖:
  playwright(云端 daily 需 pip install + playwright install chromium)
"""

import logging
import os
import re
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# 每轮最多渲染数(防某轮突然大量新文章拖垮任务; 可通过 WECHAT_SUMMARY_MAX 调)
MAX_ARTICLES = int(os.environ.get("WECHAT_SUMMARY_MAX", "10"))

# 正文传给 LLM 的最大长度
CONTENT_CAP = 1500

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


def _fetch_body_text(link: str, cookie: str) -> str:
    """Playwright 渲染 #js_content 拿正文纯文本. 失败返回 ""."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        logger.warning("playwright 未安装, 跳过公众号正文渲染: %s", link)
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                                        args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent=UA, locale="zh-CN")
            # 注入 wr_* cookie(有则带, 提升可用性)
            for pair in (cookie or "").split(";"):
                if "=" in pair:
                    k, v = pair.strip().split("=", 1)
                    try:
                        ctx.add_cookies([{"name": k, "value": v,
                                          "domain": ".weixin.qq.com", "path": "/"}])
                    except Exception:
                        pass
            page = ctx.new_page()
            page.goto(link, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(4000)  # 等 JS 渲染正文
            try:
                txt = page.eval_on_selector("#js_content", "el => el.innerText")
            except Exception:
                txt = ""
            if not txt or not txt.strip():
                try:
                    txt = page.locator("body").inner_text()
                except Exception:
                    txt = ""
            browser.close()
        text = re.sub(r"[ \t　]+", " ", (txt or "").strip())
        text = re.sub(r"\n{2,}", "\n", text).strip()
        if not text:
            return ""
        return text[:CONTENT_CAP]
    except Exception as e:
        logger.warning("渲染公众号正文失败 %s: %s", link, str(e)[:150])
        return ""


def enhance_wechat_articles(articles: list[dict], cookie: str = "") -> list[dict]:
    """对公众号新文章补 publish_time + content(正文).

    封面 image 由 weread_mp_crawler 抓取时已写入, 此处不动。
    网页文章不处理, 原样保留。

    Args:
        articles: 增量过滤后的文章列表(改写原地, 返回同一列表)
        cookie: 微信读书 wr_* cookie(原文页渲染可选带)

    Returns:
        增强后的同一列表
    """
    mp = [a for a in articles if _is_mp_article(a)]
    if not mp:
        return articles

    todo = mp[:MAX_ARTICLES]
    logger.info("公众号增强: %d 篇(处理 %d, 上限 %d)",
                len(mp), len(todo), MAX_ARTICLES)

    # 1. 纯 requests 补时间(轻量、快)
    for a in todo:
        if not a.get("publish_time"):
            a["publish_time"] = _fetch_publish_time(a["link"])
            if a["publish_time"]:
                logger.info("  公众号补时间[%s]: %s", a.get("source", "?"), a["publish_time"])

    # 2. Playwright 渲染补正文
    for a in todo:
        if a.get("content"):
            continue
        logger.info("  渲染公众号正文[%s]: %s", a.get("source", "?"), a["title"][:25])
        a["content"] = _fetch_body_text(a["link"], cookie)
        if not a["content"]:
            logger.info("    未取到公众号正文(%s), 跳过", a["link"])
        time.sleep(2)  # 间隔(加倍), 降限流

    done = sum(1 for a in todo if a.get("content") or a.get("publish_time"))
    logger.info("公众号增强完成: %d/%d 拿到数据", done, len(todo))
    return articles

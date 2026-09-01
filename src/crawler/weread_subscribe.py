"""微信读书新接口公众号抓取 — 书架订阅 + /web/mp/articles + UA 伪装正文.

取代被限流的 cover(/api/mp/cover) 方案。三条已验证的能力:
  1. /web/shelf/sync 书架接口 → 动态发现"已订阅"的公众号(MP_WXS_开头)
  2. /web/mp/articles?bookId=&offset= → 拿该号文章列表(多篇), 含 mp.weixin 原文直链
  3. UA 伪装(MicroMessenger UA + requests) → 抓 mp.weixin 正文(绕滑块, 替代 Playwright 渲染)
实测: articles/articles 需在 Playwright 页面上下文 fetch(纯 requests 返回 -2041)。

依赖: playwright(项目已装)。cookie 从 env/.env 的 WEREAD_COOKIE 读。
默认间隔: 所有请求(书架/articles/UA正文)之间 3min±60s(180±60s), 防微信读书限流。
"""

import logging
import os
import random
import re
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# 请求间隔(秒): 3min ± 60s
REQUEST_INTERVAL_MEAN = 180.0
REQUEST_INTERVAL_SPREAD = 60.0

# UA 伪装(weflow-cli 实测有效): 微信内置浏览器
WECHAT_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
             "MicroMessenger/8.0.34(0x16082222) NetType/WIFI Language/zh_CN")

CYAN = "\033[36m"
EOM = "\033[0m"


def _sleep_interval():
    """请求间隔 3min±60s. FAST_TEST=1 时跳过(仅用于调试)."""
    if os.environ.get("FAST_TEST", "") in ("1", "true"):
        return
    wait = random.uniform(REQUEST_INTERVAL_MEAN - REQUEST_INTERVAL_SPREAD,
                          REQUEST_INTERVAL_MEAN + REQUEST_INTERVAL_SPREAD)
    logger.info("请求间隔 %.0fs", wait)
    time.sleep(wait)


def _get_cookie() -> str:
    """取微信读书 cookie(env/.env WEREAD_COOKIE)."""
    c = os.environ.get("WEREAD_COOKIE", "").strip()
    if not c:
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"))
            c = os.environ.get("WEREAD_COOKIE", "").strip()
        except Exception:
            pass
    return c


def _run_page_js(cookie: str, js: str, tag: str) -> dict:
    """在 weread 页面上下文执行 JS(书架/articles). 用 sync_playwright(Windows 稳定).

    Playwright async 在 Windows 上会因 proactor 事件循环 + 浏览器子进程崩(I/O closed pipe),
    故用 sync_api。
    """
    import json
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
                locale="zh-CN",
            )
            for pair in cookie.split(";"):
                if "=" in pair:
                    k, v = pair.strip().split("=", 1)
                    try:
                        ctx.add_cookies([{"name": k, "value": v, "domain": ".qq.com", "path": "/"}])
                    except Exception:
                        pass
            page = ctx.new_page()
            page.goto("https://weread.qq.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            raw = page.evaluate(js)
            return json.loads(raw) if isinstance(raw, str) else raw
        finally:
            browser.close()


def list_subscribed(cookie: str) -> list[dict]:
    """书架接口发现已订阅的公众号. 返回 [{"name","bookId","v"}...]."""
    # 与 tools/test_weread_reader.py 验证过的 SHELF 脚本一致(逐字符)
    js = ("(()=>fetch('/web/shelf/sync?synckey=0&teenmode=0&album=1',{credentials:'include'})"
          ".then(r=>r.json()).then(o=>JSON.stringify({errCode:o.errCode,books:(o.books||[])"
          ".filter(b=>String(b.bookId||'').indexOf('MP_WXS_')===0)"
          ".map(b=>({name:b.title,bookId:b.bookId,"
          "v:(b.deepLink||'').match(/[?&]v=([^&]+)/)?.[1]||null}))}))"
          ".catch(e=>JSON.stringify({err:String(e)})))()")
    d = _run_page_js(cookie, js, "shelf")
    if d.get("errCode"):
        logger.warning("书架接口 errCode=%s", d.get("errCode"))
        return []
    subs = d.get("books", [])
    logger.info("书架发现公众号 %d 个: %s", len(subs),
                ", ".join(s.get("name", "") for s in subs))
    return subs


def fetch_articles(cookie: str, book_id: str, offset: int = 0) -> list[dict]:
    """/web/mp/articles 拿某订阅号文章列表.

    offset 是"已跳过的群发条数"(非文章篇数)。返回 [{"title","url","createTime"}...]。
    """
    url = f"/web/mp/articles?bookId={book_id}&offset={offset}"
    js_safe = url.replace("\\", "\\\\").replace("'", "\\'")
    js = ("(()=>fetch('" + js_safe + "',{credentials:'include'})"
          ".then(r=>r.json()).then(o=>JSON.stringify({errCode:o.errCode,"
          "items:(o.reviews||[]).map(g=>(g.subReviews||[]).map(s=>{var mi=(s.review&&s.review.mpInfo)||{};"
          "return {t:mi.title,url:mi.originalId?('https://mp.weixin.qq.com/s/'+mi.originalId.replace(/~/g,'_')):null,"
          "ct:s.review?Number(s.review.createTime||0):0,rid:(s.review&&s.review.reviewId)||''}})).flat(),"
          "n:(o.reviews||[]).length}))"
          ".catch(e=>JSON.stringify({err:String(e)})))()")
    d = _run_page_js(cookie, js, "articles")
    if d.get("errCode"):
        logger.warning("articles errCode=%s bookId=%s", d.get("errCode"), book_id)
        return []
    items = [it for it in d.get("items", []) if it.get("url")]
    # createTime 时间戳 → 北京日期
    for it in items:
        if it.get("ct"):
            try:
                it["date"] = datetime.fromtimestamp(it["ct"], timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
            except Exception:
                it["date"] = ""
        else:
            it["date"] = ""
    logger.info("%s 返回文章 %d 篇", book_id, len(items))
    return items


def fetch_body_ua(url: str) -> dict:
    """UA 伪装抓 mp.weixin 正文. 返回 {"content": 纯文本, "create_time": 字符串}."""
    try:
        import requests as _requests
    except Exception:
        return {"content": "", "create_time": ""}
    headers = {
        "User-Agent": WECHAT_UA,
        "Referer": "https://mp.weixin.qq.com/",
        "Origin": "https://mp.weixin.qq.com",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        r = _requests.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            return {"content": "", "create_time": ""}
        html = r.content.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("UA抓正文异常 %s: %s", url, str(e)[:80])
        return {"content": "", "create_time": ""}
    # 正文
    body = ""
    m = re.search(r'id="js_content"[^>]*>(.*?)</div>', html, re.S)
    if m:
        body = re.sub(r"<[^>]+>", "", m.group(1))
        body = re.sub(r"\s+", " ", body).strip()
    # createTime
    ct = ""
    m2 = re.search(r"createTime\s*=\s*'([^']*)'", html)
    if m2:
        ct = m2.group(1).strip()
    # 截断
    if len(body) > 1500:
        body = body[:1500]
    return {"content": body, "create_time": ct}


def fetch_subscribed_articles(cookie: str = "") -> tuple[list[dict], list[dict]]:
    """主入口: 书架订阅号 → articles列表 → UA正文. 返回 (articles, inactive).

    articles: [{"title","url","publish_time","summary","source","createTime"}...]
    inactive: 1年未更新订阅号记录(仅用于日志, 无 fakeid 概念)
    """
    cookie = cookie or _get_cookie()
    if not cookie:
        logger.warning("无微信读书 cookie(设置 WEREAD_COOKIE), 跳过公众号")
        return [], []

    subs = list_subscribed(cookie)
    if not subs:
        logger.info("书架无订阅公众号, 跳过")
        return [], []
    _sleep_interval()

    articles = []
    inactive = []
    for sub in subs:
        book_id = sub.get("bookId", "")
        if not book_id:
            continue
        logger.info("== 抓取订阅号[%s] %s ==", sub.get("name"), book_id)
        item_list = fetch_articles(cookie, book_id)
        _sleep_interval()
        if not item_list:
            continue
        # 判定 1 年未更新: 用最新一篇 createTime
        latest_ct = item_list[0].get("date", "")
        if latest_ct and _is_one_year_stale(latest_ct):
            inactive.append({"name": sub.get("name"), "bookId": book_id,
                             "last_update": latest_ct})
            logger.warning("订阅号[%s] 1年未更新(%s), 记失效", sub.get("name"), latest_ct)
        # 每篇文章 UA 抓正文
        for it in item_list:
            art = {
                "title": it.get("t", ""),
                "link": it.get("url", ""),
                "publish_time": it.get("date", ""),
                "createTime": it.get("date", ""),
                "source": sub.get("name"),
                "summary": "",
            }
            if it.get("url"):
                body_info = fetch_body_ua(it["url"])
                _sleep_interval()
                art["content"] = body_info.get("content", "")
                if body_info.get("create_time"):
                    art["publish_time"] = art["publish_time"] or body_info["create_time"]
            articles.append(art)
    logger.info("订阅公众号抓取: %d 篇来自 %d 个订阅号", len(articles), len(subs))
    return articles, inactive


def _is_one_year_stale(date_str: str) -> bool:
    """date_str(YYYY-MM-DD) 距今 >1 年判定失效. 空/解析失败不算失效(防误删)."""
    if not date_str:
        return False
    try:
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_str)
        if not m:
            return False
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return (datetime.now() - dt) > timedelta(days=365)
    except Exception:
        return False

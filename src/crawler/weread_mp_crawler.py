"""微信公众号抓取 — 微信读书方案(weread.qq.com).

原理(源码 we-mp-rss issue #442 + weread_mp 实证):
  - 公众号 bookId = "MP_WXS_" + base64解码(fakeid)
  - 用微信读书 wr_* cookie 调 weread.qq.com/api/mp/cover?bookId= 拿最新一篇(reviewId)
  - reviewId 可拼 mp.weixin.qq.com 原文直链
限制(源码实证): 每号只最新 1 篇(cover 增量)。稳定、无频控(appmsg 的 200013)。

依赖:
  - 微信读书 wr_* cookie: 从 weread.json(扫码产生)或 WEREAD_COOKIE env
  - sources.yaml 里公众号 fakeid(已全部生成)→ bookId
"""

import base64
import json
import logging
import os
import time
from typing import Any

import requests
import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SOURCES_PATH = os.path.join(PROJECT_ROOT, "config", "sources.yaml")
WEREAD_SESSION = os.environ.get("WEREAD_SESSION_PATH", "/tmp/we-mp-rss-data/weread.json")

WEREAD_API = "https://weread.qq.com/api/mp/cover"
# 每个公众号之间间隔(微信读书/page_interval 源码默认 1s)
PAGE_INTERVAL = 1.0

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def get_weread_cookie() -> str:
    """从 session.json 或 env 取微信读书 cookie."""
    if os.path.exists(WEREAD_SESSION):
        try:
            with open(WEREAD_SESSION, "r", encoding="utf-8") as f:
                d = json.load(f)
            c = d.get("cookie", "")
            if c:
                return c
        except Exception:
            pass
    return os.environ.get("WEREAD_COOKIE", "")


def fakeid_to_bookid(fakeid: str) -> str:
    """bookId = MP_WXS_ + base64解码(fakeid)."""
    if fakeid.startswith("biz="):
        fakeid = fakeid[len("biz="):]
    try:
        dec = base64.b64decode(fakeid).decode("utf-8")
    except Exception:
        dec = fakeid
    return f"MP_WXS_{dec}"


def fetch_latest_article(cookie: str, book_id: str, timeout: int = 15) -> dict[str, Any] | None:
    """调 /api/mp/cover 拿某个公众号最新一篇.

    Returns: {"title","link","publish_time","digest"} 或 None(无文章/失败).
    """
    headers = {
        "Cookie": cookie,
        "User-Agent": UA,
        "Referer": "https://weread.qq.com/",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        r = requests.get(WEREAD_API, params={"bookId": book_id},
                         headers=headers, timeout=timeout)
    except Exception as e:
        logger.warning("微信读书 cover 请求异常 %s: %s", book_id, e)
        return None
    # 详细日志: HTTP 状态 + 响应前 300 字符, 便于判断鉴权/风控/空
    logger.info("微信读书 cover %s HTTP=%s 前300: %s",
                book_id, r.status_code, r.text[:300].replace("\n", " "))
    try:
        d = r.json()
    except Exception as e:
        logger.warning("cover 返回非JSON(%s): %s", r.status_code, r.text[:200])
        return None
    if not d or "reviewId" not in d:
        # 详情: 空dict / 缺字段 / 含错误码
        keys = list(d.keys()) if isinstance(d, dict) else type(d).__name__
        err = d.get("errCode") if isinstance(d, dict) else None
        logger.info("微信读书 %s 无 reviewId(keys=%s) errCode=%s", book_id, keys, err)
        return None
    review_id = d.get("reviewId", "")
    title = d.get("title", "")
    # reviewId = MP_WXS_<bookId>_<token>, 拼原文直链
    link = f"https://mp.weixin.qq.com/s/{review_id.split('_')[-1]}"
    return {
        "title": title,
        "link": link,
        "publish_time": "",
        "digest": d.get("digest", ""),
    }


def fetch_wechat_articles(account_names: list[str] | None = None) -> list[dict[str, Any]]:
    """抓取公众号(每号最新1篇) — 微信读书方案.

    Args:
        account_names: 要抓的公众号名列表; None 则全部(type=wechat_rss)

    Returns:
        [{"title","link","publish_time","summary","source"}, ...]
    """
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    gzh = [s for s in data.get("sources", []) if s.get("type") == "wechat_rss" and s.get("fakeid")]
    if account_names:
        nameset = set(account_names)
        gzh = [s for s in gzh if s["name"] in nameset]
    # 测试样本: WECHAT_SAMPLE=N 只抓前 N 个
    try:
        sample = int(os.environ.get("WECHAT_SAMPLE", "0") or 0)
        if sample > 0:
            gzh = gzh[:sample]
            logger.info("WECHAT_SAMPLE=%s, 本次仅抓前 %d 个公众号", sample, len(gzh))
    except ValueError:
        pass

    cookie = get_weread_cookie()
    if not cookie:
        logger.warning("无微信读书 cookie(先运行 scan_weread 扫码), 跳过公众号抓取")
        return []

    articles = []
    for i, s in enumerate(gzh):
        book_id = fakeid_to_bookid(s["fakeid"])
        art = fetch_latest_article(cookie, book_id)
        if art:
            art["source"] = s["name"]
            articles.append(art)
            logger.info("微信读书[%s] 拿到: %s", s["name"], art["title"][:30])
        if i < len(gzh) - 1:
            time.sleep(PAGE_INTERVAL)
    logger.info("微信读书抓取共 %d 篇来自 %d 个公众号", len(articles), len(gzh))
    return articles

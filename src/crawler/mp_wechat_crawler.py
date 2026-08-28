"""微信公众号文章爬虫 — 纯远程方案（MP API 优先，RSSHub 降级）。

背景：
    为支持 GitHub Actions 完全远程运行，不再依赖本地 we-mp-rss 服务。
    改为直接调用微信公众平台官方接口 mp.weixin.qq.com，公网可达。

数据源层次（fetcher 双层架构）：
    Layer 1 - MP API：mp.weixin.qq.com/cgi-bin/appmsg，需 Cookie + Token + fakeid
    Layer 2 - RSSHub：/wechat/mp/{fakeid}，公网 RSS 降级

关键能力：
    - Cookie 过期检测：API 返回 ret!=0 时置为过期，供上层触发邮件告警
    - CI 环境支持：检测 CI=true 时仍走 MP API（不再跳过公众号，因为公网可达）
    - 每公众号需 fakeid，来源 config/sources.yaml 的 fakeid 字段
"""

import logging
import os
import re
import time
from datetime import datetime
from typing import Any

import feedparser
import requests
import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SOURCES_PATH = os.path.join(PROJECT_ROOT, "config", "sources.yaml")
REQUEST_TIMEOUT = 15

# 每个公众号之间的默认延迟（秒）。微信 appmsg 频控严格，长延迟降低触发频率控制的风险。
DEF_DELAY_SECONDS = 6.0

# 抓取状态常量
MP_STATUS = {
    "OK": "ok",
    "COOKIE_EXPIRED": "cookie_expired",   # Cookie/Token 失效，需通知用户更新 Secret
    "RATE_LIMITED": "rate_limited",       # 200013 freq control，频率受限，稍后重试
    "NO_CONFIG": "no_config",             # 该条目缺 fakeid
    "EMPTY": "empty",                     # 正常但无文章
    "ERROR": "error",                     # 其他错误
}

# 微信 appmsg base_resp 常见错误码
# 200013 = freq control（频率控制），不属于 Cookie 过期
RATE_LIMIT_RET = {200013}


def is_ci_environment() -> bool:
    """检测是否在 GitHub Actions 环境."""
    return os.environ.get("CI", "").lower() == "true"


def load_wechat_sources() -> list[dict[str, Any]]:
    """加载 sources.yaml 中所有公众号信源."""
    try:
        with open(SOURCES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return [s for s in data.get("sources", []) if s.get("type") == "wechat_rss"]
    except Exception as e:
        logger.error("Failed to load sources.yaml: %s", e)
        return []


def _extract_fakeid(s) -> str:
    """从条目中稳健提取 fakeid.

    兼容两种存储形式：
    - 纯 fakeid（base64，如 MjM5NzkwNzU0Mg==，尾部 = 是合法字符，勿裁）
    - 完整参数形式（biz=MjM5...）
    """
    fakeid = s.get("fakeid", "") or ""
    if fakeid.startswith("biz="):
        fakeid = fakeid[len("biz="):]
    return fakeid.strip()


def _fetch_via_mp_api(fakeid: str, cookie: str, token: str, count: int = 5) -> tuple[list[dict], str]:
    """Layer 1 - 直连微信 MP API 获取文章.

    Returns:
        (articles, status)
    """
    url = "https://mp.weixin.qq.com/cgi-bin/appmsg"
    params = {
        "action": "list_ex",
        "begin": "0",
        "count": str(count),
        "fakeid": fakeid,
        "type": "9",
        "query": "",
        "token": token,
        "lang": "zh_CN",
        "f": "json",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie,
        "Referer": "https://mp.weixin.qq.com/",
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        data = resp.json()
    except requests.RequestException as e:
        logger.warning("MP API request failed: %s", e)
        return [], MP_STATUS["ERROR"]
    except ValueError:
        logger.warning("MP API returned non-JSON")
        return [], MP_STATUS["ERROR"]

    ret = data.get("ret", data.get("base_resp", {}).get("ret", 0))
    if ret != 0:
        err_msg = data.get("base_resp", {}).get("err_msg", "")
        logger.warning("MP API returned ret=%s err=%s", ret, err_msg)
        # 区分频率控制与真正的 Cookie 过期
        if ret in RATE_LIMIT_RET or "freq" in str(err_msg).lower():
            return [], MP_STATUS["RATE_LIMITED"]
        return [], MP_STATUS["COOKIE_EXPIRED"]

    articles = []
    for item in data.get("app_msg_list", []):
        update_ts = item.get("update_time")
        publish_time = ""
        if update_ts:
            try:
                publish_time = datetime.fromtimestamp(update_ts).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                publish_time = ""
        articles.append({
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "publish_time": publish_time,
            "summary": item.get("digest", ""),
        })
    return articles, MP_STATUS["OK"]


def _fetch_via_rsshub(
    fakeid: str,
    base_url: str | list[str] = "https://rsshub.app",
) -> tuple[list[dict], str]:
    """Layer 2 - 通过 RSSHub 实例获取文章（MP API 降级）.

    base_url 可传单个地址或地址列表；列表则逐个尝试，直到一个实例成功。

    Returns:
        (articles, status)
    """
    # 规范化：支持单字符串或列表
    bases = base_url if isinstance(base_url, list) else [base_url]
    for b in bases:
        base = b.rstrip("/")
        url = f"{base}/wechat/mp/{fakeid}"
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                                headers={"User-Agent": "TJU-Daily-Bot/1.0"})
            resp.encoding = "utf-8"
            feed = feedparser.parse(resp.text)
        except requests.RequestException as e:
            logger.warning("RSSHub request failed %s: %s", base, e)
            continue

        if feed.bozo and not feed.entries:
            logger.warning("RSSHub feed empty/failed %s: %s", base, feed.bozo_exception)
            continue

        articles = []
        for entry in feed.entries:
            publish_time = ""
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    publish_time = datetime(*entry.published_parsed[:6]).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    publish_time = ""
            elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                try:
                    publish_time = datetime(*entry.updated_parsed[:6]).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    publish_time = ""
            articles.append({
                "title": getattr(entry, "title", ""),
                "link": getattr(entry, "link", ""),
                "publish_time": publish_time,
                "summary": getattr(entry, "summary", ""),
            })
        return articles, MP_STATUS["OK"]

    logger.warning("All RSSHub instances failed for %s", fakeid)
    return [], MP_STATUS["ERROR"]


def fetch_one_gzh(s, cookie: str, token: str, rsshub_base: str = "https://rsshub.app") -> dict[str, Any]:
    """抓取单个公众号文章，MP API 优先，RSSHub 降级.

    Returns:
        dict with keys: name, articles(list), status(str)
    """
    name = s.get("name", "Unknown")
    fakeid = _extract_fakeid(s)

    if not fakeid:
        logger.info("[%s] no fakeid configured, skip", name)
        return {"name": name, "articles": [], "status": MP_STATUS["NO_CONFIG"]}

    use_mp = bool(cookie and token)
    articles = []
    status = MP_STATUS["EMPTY"]

    # Layer 1: MP API
    if use_mp:
        articles, status = _fetch_via_mp_api(fakeid, cookie, token)
        if status == MP_STATUS["OK"]:
            logger.info("[%s] MP API fetched %d articles", name, len(articles))
            return {"name": name, "articles": articles, "status": status}
        if status == MP_STATUS["COOKIE_EXPIRED"]:
            # 不降级，直接报告过期以便触发通知
            logger.warning("[%s] cookie expired", name)
            return {"name": name, "articles": [], "status": status}
        # RATE_LIMITED / ERROR / EMPTY → 尝试 RSSHub 降级

    # Layer 2: RSSHub
    articles, status = _fetch_via_rsshub(fakeid, rsshub_base)
    logger.info("[%s] RSSHub fetched %d articles", name, len(articles))
    return {"name": name, "articles": articles, "status": status}


def fetch_all_wechat(
    cookie: str,
    token: str,
    rsshub_base: str = "https://rsshub.app",
    delay_seconds: float | None = None,
) -> dict[str, Any]:
    """获取所有公众号文章（顺序抓取 + 延迟，避免触发微信频率控制）.

    Args:
        cookie: mp.weixin.qq.com Cookie
        token: mp.weixin.qq.com token
        rsshub_base: RSSHub 地址或地址列表（降级用）
        delay_seconds: 每个公众号之间的延迟秒数。默认取环境变量 MP_DELAY_SECONDS，
                       否则用 DEF_DELAY_SECONDS。微信对 appmsg 频控严格，长延迟降低触发风险。

    Returns:
        {
          "articles": [ {title, link, publish_time, summary, source}, ... ],
          "cookie_expired": False/True,   # 是否任一公众号 Cookie 过期
          "status_counts": {status: count},
        }
    """
    if delay_seconds is None:
        try:
            delay_seconds = float(os.environ.get("MP_DELAY_SECONDS", str(DEF_DELAY_SECONDS)))
        except ValueError:
            delay_seconds = DEF_DELAY_SECONDS

    sources = load_wechat_sources()
    gzh_with_fakeid = [s for s in sources if _extract_fakeid(s)]
    logger.info(
        "Fetching %d/%d gzh accounts sequentially (delay=%ss)",
        len(gzh_with_fakeid), len(sources), delay_seconds,
    )

    results: list[dict] = []
    for idx, s in enumerate(gzh_with_fakeid):
        results.append(fetch_one_gzh(s, cookie, token, rsshub_base))
        # 每个公众号之间延迟（含已到最后一个则不用等）
        if idx < len(gzh_with_fakeid) - 1 and delay_seconds and delay_seconds > 0:
            time.sleep(delay_seconds)

    # 汇总
    all_articles = []
    status_counts = {}
    cookie_expired = False
    for r in results:
        st = r["status"]
        status_counts[st] = status_counts.get(st, 0) + 1
        if st == MP_STATUS["COOKIE_EXPIRED"]:
            cookie_expired = True
        for a in r["articles"]:
            a["source"] = r["name"]
            all_articles.append(a)

    return {
        "articles": all_articles,
        "cookie_expired": cookie_expired,
        "cookie_expired_count": status_counts.get(MP_STATUS["COOKIE_EXPIRED"], 0),
        "status_counts": status_counts,
    }
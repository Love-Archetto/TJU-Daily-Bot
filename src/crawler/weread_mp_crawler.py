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
PAGE_INTERVAL = 1.0  # 公众号请求间隔(秒); 遇499限流则退避5min重试(见 fetch_latest_article)
# 一次性告警守卫: 同一轮抓取内 401 只在首次触发邮件, 避免每个号都轰炸
_ALERTED_LOGIN_FAIL = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def get_weread_cookie() -> str:
    """取微信读书 cookie. 优先级: env(WEREAD_COOKIE, 公开方案的 GitHub Secret) > /tmp 兜底.

    云端: daily workflow 注入 WEREAD_COOKIE secret。本地: 可在 .env 设或临时用 /tmp 文件。
    """
    # 1. env 主来源(云端 Secret / 本地 .env)
    env_cookie = os.environ.get("WEREAD_COOKIE", "").strip()
    if env_cookie:
        return env_cookie
    # 2. /tmp 兜底(本地手工调试时用 weread.json)
    if os.path.exists(WEREAD_SESSION):
        try:
            with open(WEREAD_SESSION, "r", encoding="utf-8") as f:
                d = json.load(f)
            c = d.get("cookie", "")
            if c:
                return c
        except Exception:
            pass
    return ""


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

    微信读书 499(请求频率过高)时限流退避: 等 5min 重试, 每个最多重试 2 次。

    Returns: {"title","link","publish_time","digest"} 或 None(无文章/失败).
    """
    headers = {
        "Cookie": cookie,
        "User-Agent": UA,
        "Referer": "https://weread.qq.com/",
        "Accept": "application/json, text/plain, */*",
    }
    MAX_499_RETRY = 2
    RETRY_WAIT = 300  # 秒: 遇499等待5min
    d: dict[str, Any] | None = None
    r_status = 0
    for attempt in range(MAX_499_RETRY + 1):
        try:
            r = requests.get(WEREAD_API, params={"bookId": book_id},
                             headers=headers, timeout=timeout)
        except Exception as e:
            logger.warning("微信读书 cover 请求异常 %s: %s", book_id, e)
            return None
        r_status = r.status_code
        logger.info("微信读书 cover %s HTTP=%s 前300: %s",
                    book_id, r.status_code, r.text[:300].replace("\n", " "))
        # 499 = 请求频率过高(限流): 等待重试, 最多2次
        if r.status_code == 499:
            try:
                j = r.json()
                errmsg = (j.get("data") or {}).get("errmsg", "")
            except Exception:
                errmsg = ""
            if "频率" in errmsg or "过高" in errmsg or r.status_code == 499:
                if attempt < MAX_499_RETRY:
                    logger.warning("微信读书 %s 请求频率过高(499), 等待 %ds 后重试(%d/%d)",
                                   book_id, RETRY_WAIT, attempt + 1, MAX_499_RETRY)
                    time.sleep(RETRY_WAIT)
                    continue
                else:
                    logger.warning("微信读书 %s 连续 %d 次499限流, 放弃", book_id, MAX_499_RETRY)
                    return None
        try:
            d = r.json()
        except Exception as e:
            logger.warning("cover 返回非JSON(%s): %s", r.status_code, r.text[:200])
            return None
        break  # 非499, 拿到响应
    if d is None:
        return None

    if not d or "reviewId" not in d:
        # 详情: 空dict / 缺字段 / 含错误码
        keys = list(d.keys()) if isinstance(d, dict) else type(d).__name__
        err = d.get("errCode") if isinstance(d, dict) else None
        status_msg = d.get("statusMessage", "") if isinstance(d, dict) else ""
        if r_status == 401 or "LOGIN ERR" in str(status_msg) or err == -2010:
            # 微信读书 cookie 失效 → 明示并提醒用户重扫(一轮只发一封)
            logger.warning("微信读书 %s login 失效(HTTP=%s %s errmsg=%s), 需重扫更新 wr_* cookie",
                           book_id, r_status, status_msg,
                           (d.get("data") or {}).get("errmsg", ""))
            _maybe_alert_cookie_expired(book_id, r_status, str(d)[:200])
        else:
            logger.info("微信读书 %s 无 reviewId(keys=%s) errCode=%s", book_id, keys, err)
        return None
    review_id = d.get("reviewId", "")
    title = d.get("title", "")
    # reviewId = MP_WXS_<bookId>_<token>; 微信读书 cover 的 token 用 "~" 表示
    # base64url 的 "_"(URL-safe 变体), 拼真实 mp.weixin 短码前须还原为 "_"
    # (带 ~ 的短码在 mp.weixin 打开报"参数错误", 改成 _ 即可打开)
    token = review_id.split("_")[-1].replace("~", "_")
    link = f"https://mp.weixin.qq.com/s/{token}"
    return {
        "title": title,
        "link": link,
        "publish_time": "",
        "digest": d.get("digest", ""),
        "image": d.get("pic", ""),  # 封面图(cover 接口 pic), 供报告展示
    }


def _maybe_alert_cookie_expired(book_id: str, status: int, detail: str) -> None:
    """微信读书登录失效时发一封提醒邮件(每轮仅一次).

    本地 CDP 模式(复用真实 Edge): 失效= Edge 里微信读书会话过期, 需在 Edge 窗口重扫登录。
    本地无 SMTP 配置则静默(日志提示), 云端有则发到 NOTIFY_TO。
    """
    global _ALERTED_LOGIN_FAIL
    if _ALERTED_LOGIN_FAIL:
        return
    _ALERTED_LOGIN_FAIL = True
    try:
        from src.notifier import send_cookie_expired_alert
        guide = (
            "【微信读书】登录会话失效, 公众号抓取中断。\n\n"
            f"首个失败: {book_id} (HTTP {status})\n"
            f"详情: {detail}\n\n"
            "解决办法(本地复用 Edge 模式):\n"
            "1. 找到跑着调试端口的 Edge 窗口(或运行 tools/start_weread_edge.py)\n"
            "2. 在那个 Edge 里退出登录 → 用手机微信重新扫码登录微信读书\n"
            "3. 点进任意一个订阅的公众号, 等阅读器页标题出现「公众号」后重试\n"
        )
        if not send_cookie_expired_alert(1, "微信读书 " + guide):
            logger.warning("登录失效邮件发送失败(检查 SMTP secret)")
    except Exception as e:
        logger.warning("发送登录失效邮件异常: %s", e)


def fetch_wechat_articles(account_names: list[str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """抓取公众号 — 复用真实 Edge(CDP) + /web/mp/articles + UA 伪装正文.

    取代旧 cover(/api/mp/cover)方案与 headless+cookie 方案。仅追踪微信读书里"已订阅"的公众号。
    保持返回 (articles, inactive) 契约, 使 main.py 调用点(含 _prune_inactive_gzh)不变。

    Returns:
        (articles, inactive)
        articles: [{"title","link","publish_time","source","content","summary"}...]
        inactive: [{"name","bookId","last_update"}] 1年未更新订阅号(仅记录)
    """
    from .weread_subscribe import fetch_subscribed_articles
    # CDP 复用真实 Edge 会话; Edge 未起/未登录时 fetch_subscribed_articles 内部会友好提示并返回空。
    return fetch_subscribed_articles()


def _fetch_article_create_time(link: str) -> str:
    """从原文页抓公众号最新一篇 createTime(仅为判定活跃度, 失败返回空串)."""
    try:
        from .wechat_summary import _fetch_publish_time
        return _fetch_publish_time(link)
    except Exception:
        return ""


def _now_date_str() -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _is_two_years_stale(create_time: str, name: str) -> bool:
    """createTime 距今 > 1 年判定失效(用户要求: 一年未更新删除).
    createTime 为空(拿不到)不算失效, 避免误删."""
    if not create_time:
        return False
    try:
        from datetime import datetime, timedelta
        # createTime 形如 "YYYY-MM-DD HH:MM" 或 "YYYY-MM-DD"
        import re as _re
        m = _re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", create_time)
        if not m:
            return False
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return (datetime.now() - dt) > timedelta(days=365)
    except Exception:
        return False

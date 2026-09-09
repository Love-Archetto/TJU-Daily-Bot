"""微信读书新接口公众号抓取 — 复用真实 Edge(CDP) + /web/mp/articles + UA 伪装正文.

取代"自开 headless + 注入 cookie"(那会触发腾讯防水墙验证码, articles 返回 -2041)。
三条能力:
  1. /web/shelf/sync 书架接口 → 动态发现"已订阅"的公众号(MP_WXS_开头), 每个号带 readerUrl
  2. /web/mp/articles?bookId=&offset= → 拿该号文章列表(多篇), 含 mp.weixin 原文直链
  3. UA 伪装(MicroMessenger UA + requests) → 抓 mp.weixin 正文(绕滑块, 替代 Playwright 渲染)
关键上下文(对齐 Pengyf04/weread-mp-fetcher): /web/mp/articles 必须在**阅读器页**
  /web/mp/reader/<hash> 的页面上下文里 fetch(首页发返回 -2041; 无头自开会弹验证码)。
  readerUrl 由书架 deepLink 的校验 hash `v` 拼出(不可自拼)。执行上下文来自登录过的真实 Edge
  (见 weread_cdp), 不新开浏览器。

依赖: playwright(项目已装) / MS Edge。不再需要 WEREAD_COOKIE(登录态在 Edge 会话里)。
默认间隔: 所有请求(书架/articles/UA正文)之间 3s±1s(原 180±60s 过慢; 本地人工运行靠
  下方反爬闸门兜底)。触发反爬(articles -2041 防水墙 / mp.weixin UA 正文风控)时打印提示,
  等待用户在 Edge/网页里刷新并完成验证后回终端按回车继续; 非交互环境(CI/管道)退避 180s。
"""

import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# 请求间隔(秒): 3s ± 1s(原 180±60s 过慢; 间隔短时靠下方反爬闸门人工兜底)
REQUEST_INTERVAL_MEAN = 3.0
REQUEST_INTERVAL_SPREAD = 1.0

# 非交互环境(CI/管道, stdin 非终端)触发反爬后的退避秒数(无真人可刷新, 慢速重试更安全)
NON_INTERACTIVE_BACKOFF = 180.0

# mp.weixin UA 正文响应的风控/验证页特征(命中视为反爬, 需在真实 Edge 里打开该链接过验证)
ANTIBOT_MARKERS = ("环境异常", "访问过于频繁", "请完成验证", "完成验证", "去验证", "请输入验证码")

# UA 伪装(weflow-cli 实测有效): 微信内置浏览器
WECHAT_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
             "MicroMessenger/8.0.34(0x16082222) NetType/WIFI Language/zh_CN")

CYAN = "\033[36m"
EOM = "\033[0m"


def _sleep_interval():
    """请求间隔 3s±1s. FAST_TEST=1 时跳过(仅用于调试)."""
    if os.environ.get("FAST_TEST", "") in ("1", "true"):
        return
    wait = random.uniform(REQUEST_INTERVAL_MEAN - REQUEST_INTERVAL_SPREAD,
                          REQUEST_INTERVAL_MEAN + REQUEST_INTERVAL_SPREAD)
    logger.info("请求间隔 %.1fs", wait)
    time.sleep(wait)


def _antibot_wait(tag: str, hint: str) -> None:
    """反爬闸门: 提示用户在真实 Edge/网页里刷新并完成验证, 回终端按回车后继续.

    交互终端(stdin 是 tty)用 input() 阻塞等按键; 非交互(CI/管道)没有真人可刷新, 改退避
    NON_INTERACTIVE_BACKOFF 后继续, 避免云端任务无限阻塞。Ctrl+C 可中断本轮。
    """
    print(f"\n{CYAN}[反爬/风控] {tag}{EOM}", flush=True)
    print(f"  {hint}", flush=True)
    print(f"{CYAN}  请刷新页面并完成验证后, 回终端按 回车 继续(Ctrl+C 终止本轮){EOM}", flush=True)
    logger.warning("反爬触发, 等待人工处理: %s", tag)
    try:
        if sys.stdin.isatty():
            input()
            return
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        pass
    logger.info("非交互环境(stdin 非终端), 退避 %.0fs 后继续", NON_INTERACTIVE_BACKOFF)
    time.sleep(NON_INTERACTIVE_BACKOFF)


def _antibot_reason(final_url: str, status: int, html: str) -> str:
    """判 mp.weixin UA 正文响应是否风控/验证页; 正常/空正文/文章被删等返回空串(不算反爬)."""
    if "mp/verifypage" in final_url or "/verify" in final_url:
        return "被重定向到验证页(mp/verifypage)"
    if status == 403:
        return "HTTP 403"
    s = html or ""
    for m in ANTIBOT_MARKERS:
        if m in s:
            return f"页面含风控标记「{m}」"
    return ""


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


def _run_page_js(cookie: str, js: str, tag: str, page_url: str = "https://weread.qq.com/") -> dict:
    """在 weread 页面上下文执行 JS(书架/articles).

    复用已登录、已过验证码的真实 Edge(见 weread_cdp): 不新开浏览器, connect_over_cdp 到
    调试端口上的 Edge。阅读器页 URL 走 `/web/mp/reader/<hash>` 上下文(否则 articles 返回 -2041),
    其余(书架)用 edge 里任一 weread 页上下文。cookie 参数在 CDP 模式下不再需要(登录态在 Edge 会话里)。
    """
    import json
    from . import weread_cdp

    try:
        raw = weread_cdp.evaluate_on_reader_tab(js, page_url)
    except Exception as e:
        logger.warning("CDP 执行 %s 失败: %s", tag, str(e)[:160])
        return {}
    return json.loads(raw) if isinstance(raw, str) else raw


def list_subscribed(cookie: str) -> list[dict]:
    """书架接口发现已订阅的公众号. 返回 [{"name","bookId","readerUrl"}...].

    readerUrl 由每条 deepLink 的 `v`(微信读书校验 hash, 每号各不相同, 不可自拼)
    拼出: it 是 /web/mp/articles 唯一能用(非 -2041)的发起页上下文, 对齐
    Pengyf04/weread-mp-fetcher 的核心约束。
    """
    # 与 tools/test_weread_reader.py 验证过的 SHELF 脚本一致(逐字符), 仅把 v 换成 readerUrl
    # (块箭头 map + return: 整串经 node --check 验证括号平衡)
    js = (
          "(()=>fetch('/web/shelf/sync?synckey=0&teenmode=0&album=1',{credentials:'include'}).then(r=>r.json()).then(o=>JSON.stringify({errCode:o.errCode,books:(o.books||[]).filter(b=>String(b.bookId||'').indexOf('MP_WXS_')===0).map(b=>{var v=(b.deepLink||'').match(/[?&]v=([^&]+)/)?.[1]||null;return {name:b.title,bookId:b.bookId,readerUrl:v?('https://weread.qq.com/web/mp/reader/'+v):null};})})).catch(e=>JSON.stringify({err:String(e)})))()"
    )
    d = _run_page_js(cookie, js, "shelf")
    if d.get("errCode"):
        logger.warning("书架接口 errCode=%s", d.get("errCode"))
        return []
    subs = d.get("books", [])
    logger.info("书架发现公众号 %d 个: %s", len(subs),
                ", ".join(s.get("name", "") for s in subs))
    return subs


def fetch_articles(cookie: str, book_id: str, reader_url: str = "",
                   offset: int = 0) -> list[dict]:
    """/web/mp/articles 拿某订阅号文章列表.

    reader_url: 阅读器页 (https://weread.qq.com/web/mp/reader/<hash>)。**必须**在该页
    上下文里发 articles 请求, 首页发返回 -2041。缺省退回首字母书页(=原首页路径)。
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
    page_url = reader_url or "https://weread.qq.com/"
    d = _run_page_js(cookie, js, "articles", page_url)
    if d.get("errCode"):
        err = d.get("errCode")
        # -2041 = 触发腾讯防水墙/验证码: 需在真实 Edge 的阅读器页刷新过验证后才恢复
        if err == -2041:
            logger.warning("articles errCode=-2041(防水墙验证码) bookId=%s", book_id)
            _antibot_wait(
                f"articles -2041(bookId={book_id})",
                "请到打开的 Edge 窗口刷新公众号阅读器页, 手动完成验证码/滑块",
            )
            d = _run_page_js(cookie, js, "articles", page_url)  # 人工处理后重试一次
            if d.get("errCode"):
                logger.warning("人工处理后 articles 仍 errCode=%s bookId=%s, 跳过该号",
                               d.get("errCode"), book_id)
                return []
        else:
            logger.warning("articles errCode=%s bookId=%s", err, book_id)
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
    """UA 伪装抓 mp.weixin 正文. 返回 {"content": 纯文本, "create_time": 字符串}.

    命中风控(HTTP 403 / mp/verifypage / 风控标记页)视为反爬: 打开反爬闸门, 提示在真实
    Edge 里打开该链接过验证后回终端按回车, 再重试一次(仍失败则返回空正文, 不阻塞后续)。
    网络异常 / 文章被删(404 等)维持原降级, 不进闸门。
    """
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

    def _get() -> tuple[int, str, str]:
        """GET 正文页 → (status, final_url, html). 网络异常返回 (0, "", "")."""
        try:
            r = _requests.get(url, headers=headers, timeout=20)
            return (r.status_code, r.url, r.content.decode("utf-8", errors="replace"))
        except Exception as e:
            logger.warning("UA抓正文异常 %s: %s", url, str(e)[:80])
            return (0, "", "")

    status, final_url, html = _get()
    if status == 0:  # 网络异常, 原样跳过
        return {"content": "", "create_time": ""}
    reason = _antibot_reason(final_url, status, html)
    if reason:
        _antibot_wait(
            f"mp.weixin UA 正文({reason})",
            f"请在真实 Edge 里打开该链接并完成验证/刷新后回终端按回车:\n    {url}",
        )
        status, final_url, html = _get()  # 人工处理后重试一次
        if _antibot_reason(final_url, status, html):
            logger.warning("人工处理后 UA 抓正文仍被风控, 跳过: %s", url)
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

    复用真实 Edge(CDP)拿公众号列表; 正文用 UA 伪装(fetch_body_ua)。cookie 参数已不再需要
    (登录态在 Edge 会话里), 保留仅为兼容旧签名。
    articles: [{"title","url","publish_time","summary","source","createTime"}...]
    inactive: 1年未更新订阅号记录(仅用于日志, 无 fakeid 概念)
    """
    from . import weread_cdp as cdp

    # 没有可复用的调试 Edge 时, 友好提示而不是静默空抓
    try:
        cdp.find_or_launch_edge()
    except Exception as e:
        logger.warning("公众号抓取跳过: %s", e)

    subs = list_subscribed("")
    if not subs:
        logger.info("书架无订阅公众号, 跳过")
        return [], []
    _sleep_interval()

    # 阅读器页上下文: 取任一所订阅号的 readerUrl 作为统一抓取页(参考: 进任意一个阅读器页)。
    # /web/mp/articles 必须在此上下文发, 否则 -2041。
    reader_url = next((s.get("readerUrl") for s in subs if s.get("readerUrl")), "")
    if not reader_url:
        logger.warning("书架条目均无 readerUrl(deepLink 缺 v), articles 会回退首页上下文(可能 -2041)")

    articles = []
    inactive = []
    for sub in subs:
        book_id = sub.get("bookId", "")
        if not book_id:
            continue
        logger.info("== 抓取订阅号[%s] %s ==", sub.get("name"), book_id)
        item_list = fetch_articles("", book_id, reader_url)
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

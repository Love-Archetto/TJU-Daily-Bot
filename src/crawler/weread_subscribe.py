"""微信读书新接口公众号抓取 — 复用真实 Edge(CDP) + /web/mp/articles + /web/mp/content.

取代"自开 headless + 注入 cookie"(那会触发腾讯防水墙验证码, articles 返回 -2041)。
三条能力:
  1. /web/shelf/sync 书架接口 → 动态发现"已订阅"的公众号(MP_WXS_开头), 每个号带 readerUrl
  2. /web/mp/articles?bookId=&offset= → 拿该号文章列表(多篇), 每篇带加密 reviewId
  3. /web/mp/content?reviewId= → 拿**单篇完整正文页 HTML**, 从中解析
        og:url      → 规范原文链接(全格式: __biz/mid/idx/sn/chksm)
        og:title    → 标题
        og:image    → 封面图
        createTime  → 发布时间
        #js_content → 正文纯文本
关键上下文(对齐 Pengyf04/weread-mp-fetcher): 上述请求必须在**阅读器页**
  /web/mp/reader/<hash> 的页面上下文里 fetch(首页发返回 -2041; 无头自开会弹验证码)。
  readerUrl 由书架 deepLink 的校验 hash `v` 拼出(不可自拼)。执行上下文来自登录过的真实 Edge
  (见 weread_cdp), 不新开浏览器。

为什么不再用 UA 伪装抓 mp.weixin 正文: /web/mp/articles 的 mpInfo.originalId 已变成 75 字符
加密串(既拼不出 /s/<短码>, 也无法解密回原链接)。唯一能拿到原链接的途径是 /web/mp/content,
而它连同正文/时间/封面一起给出 —— 于是整条链路都留在 weread 会话内, 完全不触碰 mp.weixin,
反爬/UA 伪装/验证码问题一并消失(旧的 fetch_body_ua 已删除)。

依赖: playwright(项目已装) / MS Edge。不再需要 WEREAD_COOKIE(登录态在 Edge 会话里)。
默认间隔: 所有请求之间 4s±1s。触发反爬(articles -2041 防水墙)时打印提示, 等待用户在
  Edge 里刷新并完成验证后回终端按回车继续; 非交互环境(CI/管道)退避 180s。
"""

import html
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# 请求间隔(秒): 4s ± 1s。2026-10-15 由 3±1s 调高: 实测连续拉多个 3.5MB 正文页时,
# 约 1/3 会撞上 mp.weixin 的「验证」拦截页(仅 2KB)而取不到 og:url, 拉长间隔可降触发率。
# 回填(一次性重抓全部在列文章, 上千次请求)可用 WEREAD_INTERVAL/WEREAD_INTERVAL_SPREAD 调快。
REQUEST_INTERVAL_MEAN = float(os.environ.get("WEREAD_INTERVAL", "4.0") or 4.0)
REQUEST_INTERVAL_SPREAD = float(os.environ.get("WEREAD_INTERVAL_SPREAD", "1.0") or 1.0)

# 非交互环境(CI/管道, stdin 非终端)触发反爬后的退避秒数(无真人可刷新, 慢速重试更安全)
NON_INTERACTIVE_BACKOFF = 180.0

# 单篇正文页 HTML 很大(~3.5MB, 主要是页面自带的 JS/CSS), 解析后只回传这些小字段;
# 正文只取前 N 字符(供 LLM 摘要足够, 无需整篇)。
CONTENT_CHARS = 4000

# 每号最多处理多少篇列表文章(0 = 不限制)。仅用于回填/调试时缩小范围。
MAX_ITEMS_PER_SOURCE = int(os.environ.get("WEREAD_MAX_ITEMS_PER_SOURCE", "0") or 0)
# 最多处理多少个订阅号(0 = 不限制)
MAX_ACCOUNTS = int(os.environ.get("WEREAD_MAX_ACCOUNTS", "0") or 0)

# errCode 分类(2026-10-17 实测拿到微信 errMsg 原文后确认):
#   -2014 = "请求频率过高"  -> 限流。不是会话坏了, 正确做法是退避后重试, 不该中止。
#   -2010 = "用户不存在"    -> 登录态被吊销
#   -2012 = "登录超时"      -> wr_skey 过期(约 90 分钟寿命)
#   -2013 = /web/login/renewal 续期被风控(见 spirodelazz/weread-keepalive)
#   -2041 = 腾讯防水墙验证码 -> 需人工在浏览器里过, 见 _antibot_wait
RATE_LIMIT_CODES = (-2014,)
SESSION_DEAD_CODES = (-2010, -2012, -2013)

# 每轮最多取多少篇链接(省流模式下每篇只读正文页第一块, 实测约 36KB)。
# 列表(/web/mp/articles, 每号几 KB)不限额, 保证 92 个号一定扫得完 —— 这是
# "覆盖全部公众号"的底线。取链阶段的总量也只有 200x36KB≈7MB, 远低于实测安全的 10.9MB,
# 所以上限主要防"某天突发几百篇"的病态情况, 不是限流闸门。超出上限的候选不进 seen,
# 下一轮仍在回溯窗口内 → 会重新列为候选, 多跑几轮自然补齐。
CONTENT_MAX_PER_RUN = int(os.environ.get("WEREAD_CONTENT_MAX_PER_RUN", "200") or 0)

# 同一个"会话失效类"错误码连续出现多少次 -> 判定会话已废, 提前中止本轮。
# 只统计 SESSION_DEAD_CODES: 限流会自愈, 不该触发中止
# (2026-10-17 那次就是被连续的 -2014 误判成"会话已失效"而中止的)。
SAME_ERR_ABORT = int(os.environ.get("WEREAD_SAME_ERR_ABORT", "8") or 0)

# 最近一次 fetch_articles 的微信错误码与原文(errMsg)。None = 正常。
# errMsg 是排查的关键: -2010 的原文是"用户不存在"(登录态失效), 只看数字认不出病因。
_LAST_ARTICLES_ERR: int | None = None
_LAST_ARTICLES_MSG: str = ""


def _env_float(name: str, default: float) -> float:
    """读浮点环境变量; 缺省/空/坏值都用 default(注意 '0' 是合法值, 不能掉进 or 分支)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是数字, 用默认 %s", name, raw, default)
        return default


# 限流退避: 首次 RATE_LIMIT_BACKOFF 秒, 每次翻倍至 RATE_LIMIT_MAX_BACKOFF 封顶,
# 单个号最多重试 RATE_LIMIT_TRIES 次。放在 _env_float 之后, 因为它要用它。
RATE_LIMIT_BACKOFF = _env_float("WEREAD_RATE_BACKOFF", 30.0)
RATE_LIMIT_MAX_BACKOFF = _env_float("WEREAD_RATE_BACKOFF_MAX", 300.0)
RATE_LIMIT_TRIES = int(os.environ.get("WEREAD_RATE_TRIES", "3") or 3)


# 只对"最近 N 天"发表的列表文章去抓正文(0 = 不限制, 回填时用)。
# 必要性: /web/mp/articles 的 originalId 已加密, 原文链接只有抓完 /web/mp/content 才知道,
# 于是无法像旧代码那样在抓正文前用链接过滤已收录文章 —— 不做窗口限制的话, 每天都会把
# 92 个号 × 约 20 篇(每篇 ~3.5MB 正文页)全部重抓一遍。列表页本身带 createTime,
# 用它做闸门即可把每日请求压到接近"真正的新文章"数量级。
# 与 config/bootstrap.yaml 的 window_days 取同一个"近 N 天"尺度(那边是 3)。
LOOKBACK_DAYS = _env_float("WECHAT_LOOKBACK_DAYS", 3.0)

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


# 会话保活间隔(秒): wr_skey 寿命约 90 分钟, 长轮次里每隔这么久访问一次微信读书首页,
# 让浏览器把会话续上。0 = 关闭。15 分钟对齐 GitHub spirodelazz/weread-keepalive 的
# WEREAD_DAEMON_REFRESH_SECONDS 默认值。
KEEPALIVE_SECONDS = _env_float("WEREAD_KEEPALIVE_SECONDS", 900.0)
_last_keepalive = 0.0   # 上次保活的 monotonic 时间; 0 = 尚未开始计时


def _maybe_keepalive() -> None:
    """距上次保活超过 KEEPALIVE_SECONDS 就访问一次微信读书首页。

    为什么需要: wr_skey 约 90 分钟过期, 过期后接口回 -2012「登录超时」, 再恶化成
    -2010「用户不存在」。一轮 92 个号的抓取如果拖长, 后半段就会开始报错。
    实现用"另开临时标签页访问首页", 不动阅读器页(它是 articles 的必需上下文)。
    """
    global _last_keepalive
    if KEEPALIVE_SECONDS <= 0:
        return
    now = time.monotonic()
    if _last_keepalive <= 0.0:
        _last_keepalive = now   # 刚开始抓取, 不需要立刻保活, 只记起点
        return
    if now - _last_keepalive < KEEPALIVE_SECONDS:
        return
    _last_keepalive = now
    logger.info("会话保活: 已超过 %.0f 分钟, 访问一次微信读书首页续期 wr_skey",
                KEEPALIVE_SECONDS / 60)
    try:
        from . import weread_cdp
        if weread_cdp.keepalive():
            logger.info("会话保活完成")
    except Exception as e:
        logger.warning("会话保活异常(忽略, 不影响抓取): %s", str(e)[:120])


# 列表阶段专用间隔(秒)。比取链阶段慢得多, 因为 2026-10-18 实测:
#   30 次 /web/mp/articles 请求(约 3 分钟) 就撞 -2014;
#   而 80 次 /web/mp/content 首块读取(约 8 分钟, 10.9MB) 一次都没报错。
# 说明列表接口的限额比正文接口严格得多 —— 它才是 92 个号跑不完的真瓶颈。
LIST_INTERVAL = _env_float("WEREAD_LIST_INTERVAL", 8.0)

# 连续多少个号"退避后仍被限流"才认定整轮跑不动(之前是 1 个就中止, 覆盖面太差)。
RATE_HIT_ABORT = int(os.environ.get("WEREAD_RATE_ABORT", "5") or 5)

# ===== 每轮请求预算(单位) =====
# 从 2026-10-16 ~ 10-19 五次真实运行反推出来的配额模型:
#   10-18 15:08  纯列表 30 次                -> 第 30 次撞 -2014
#   10-19 11:12  列表 30 + 正文首块 1 次     -> 第 31 次撞
#   10-17 21:36  列表 27 + 正文全量 37 次    -> 第 64 次撞
#   10-18 压测   正文首块 80 次              -> 完全不撞
# 只有"列表 1.0 + 正文 0.1, 总预算 30"这个模型能同时解释四组数据
#   (30            = 30 ✓)
#   (30 + 1x0.1    = 30.1 ✓)
#   (27 + 37x0.1   = 30.7 ✓)
#   (80x0.1        = 8 ✓)
# 配额窗口长度未知: 2026-10-19 13:16 撞墙, 到 18:20(5 小时)仍未恢复,
# 所以按"每轮"预算, 而不是"每分钟"。用完就停, 没扫到的号由 gzh_last_list_ok
# 的优先级在下一轮自动补上。
#
# 两道闸门(同时生效, 先触发的那个说话):
#   1) MAX_LIST_REQUESTS: 列表请求**次数**硬上限, 默认 28(实测撞墙点是 30, 留 2 次余量)
#   2) UNIT_BUDGET: 总配额单位, 列表 1.0/次 + 正文首块 0.1/次, 默认 30
# 28 次列表 = 28 单位, 还能剩 2 单位给正文 ≈ 20 篇取链。
MAX_LIST_REQUESTS = int(os.environ.get("WEREAD_MAX_LIST_REQUESTS", "28") or 28)
UNIT_BUDGET = _env_float("WEREAD_UNIT_BUDGET", 30.0)
BUDGET_COST_LIST = _env_float("WEREAD_BUDGET_COST_LIST", 1.0)
BUDGET_COST_CONTENT = _env_float("WEREAD_BUDGET_COST_CONTENT", 0.1)


def _sleep_list_interval() -> None:
    """列表请求之间的间隔(带抖动)。FAST_TEST=1 时跳过。"""
    if os.environ.get("FAST_TEST", "") in ("1", "true"):
        return
    wait = random.uniform(LIST_INTERVAL * 0.8, LIST_INTERVAL * 1.2)
    logger.info("列表间隔 %.1fs", wait)
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
    except KeyboardInterrupt:
        raise
    except EOFError:
        # stdin 自称终端却读不到输入(后台作业/重定向/cmd 的 *> 重定向下 stdin 会被关掉)。
        # 2026-10-17 实测: 后台跑抓取时这里抛 EOFError, 整轮直接崩, 一个号都没扫。
        # 按非交互处理, 退避后继续 —— 反爬闸门不该让无人值守的任务崩溃。
        logger.warning("stdin 看似终端但读不到输入(EOF), 按非交互处理")
    except Exception:
        pass
    logger.info("非交互环境(stdin 非终端), 退避 %.0fs 后继续", NON_INTERACTIVE_BACKOFF)
    time.sleep(NON_INTERACTIVE_BACKOFF)


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
    """在 weread 页面上下文执行 JS(书架/articles/content).

    复用已登录、已过验证码的真实 Edge(见 weread_cdp): 不新开浏览器, connect_over_cdp 到
    调试端口上的 Edge。阅读器页 URL 走 `/web/mp/reader/<hash>` 上下文(否则 articles 返回 -2041),
    其余(书架)用 edge 里任一 weread 页上下文。cookie 参数在 CDP 模式下不再需要(登录态在 Edge 会话里)。
    """
    from . import weread_cdp

    try:
        raw = weread_cdp.evaluate_on_reader_tab(js, page_url)
    except Exception as e:
        logger.warning("CDP 执行 %s 失败: %s", tag, str(e)[:160])
        return {}
    return json.loads(raw) if isinstance(raw, str) else raw


def list_subscribed(cookie: str) -> list[dict]:
    """书架接口发现已订阅的公众号.

    返回 [{"name","bookId","readerUrl","last_article_ts"}...]。
    last_article_ts 来自书架的 lastChapterCreateTime —— 该号**最新一篇的发布时间**。
    它让抓取阶段能在发列表请求之前就判断"这个号最近有没有更新", 从而把
    /web/mp/articles 的请求数从"92 个号"压到"真正有更新的十几个号" ——
    这是绕开列表接口请求配额的关键(2026-10-18 实测: 30 次列表请求就撞 -2014,
    而 80 次正文首块读取毫无压力, 说明瓶颈在列表接口的请求次数而非流量)。

    readerUrl 由每条 deepLink 的 `v`(微信读书校验 hash, 每号各不相同, 不可自拼)
    拼出: it 是 /web/mp/articles 唯一能用(非 -2041)的发起页上下文, 对齐
    Pengyf04/weread-mp-fetcher 的核心约束。
    """
    # 与 tools/test_weread_reader.py 验证过的 SHELF 脚本一致(逐字符), 仅把 v 换成 readerUrl
    # (块箭头 map + return: 整串经 node --check 验证括号平衡)
    js = (
          "(()=>fetch('/web/shelf/sync?synckey=0&teenmode=0&album=1',{credentials:'include'}).then(r=>r.json()).then(o=>JSON.stringify({errCode:o.errCode,books:(o.books||[]).filter(b=>String(b.bookId||'').indexOf('MP_WXS_')===0).map(b=>{var v=(b.deepLink||'').match(/[?&]v=([^&]+)/)?.[1]||null;return {name:b.title,bookId:b.bookId,readerUrl:v?('https://weread.qq.com/web/mp/reader/'+v):null,last_article_ts:Number(b.lastChapterCreateTime||0)};})})).catch(e=>JSON.stringify({err:String(e)})))()"
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
    offset 是"已跳过的群发条数"(非文章篇数)。

    返回 [{"t":标题,"rid":reviewId,"ct":发布ts,"date":北京日期}...]。
    注意: mpInfo.originalId 已是加密长串, 拼不出原文链接 —— 链接必须再调
    fetch_article_content(rid) 从正文页 og:url 取。
    """
    url = f"/web/mp/articles?bookId={book_id}&offset={offset}"
    js_safe = url.replace("\\", "\\\\").replace("'", "\\'")
    js = ("(()=>fetch('" + js_safe + "',{credentials:'include'})"
          ".then(r=>r.json()).then(o=>JSON.stringify({errCode:o.errCode,errMsg:o.errMsg||'',"
          "items:(o.reviews||[]).map(g=>(g.subReviews||[]).map(s=>{var mi=(s.review&&s.review.mpInfo)||{};"
          "return {t:mi.title,rid:(s.review&&s.review.reviewId)||'',"
          "ct:s.review?Number(s.review.createTime||0):0,"
          # 列表自带的四件套: 封面图 pic_url / 微信自动摘要 content / 阅读数 / 点赞数。
          # 有它们, Part 3 那种"不抓正文"的条目也能有封面和摘要。
          "pic:mi.pic_url||'',dig:mi.content||'',"
          "read:Number(mi.readNum||0),like:Number(mi.likeNum||0)}})).flat(),"
          "n:(o.reviews||[]).length}))"
          ".catch(e=>JSON.stringify({err:String(e)})))()")
    page_url = reader_url or "https://weread.qq.com/"
    d = _run_page_js(cookie, js, "articles", page_url)
    global _LAST_ARTICLES_ERR, _LAST_ARTICLES_MSG
    if d.get("errCode"):
        err = d.get("errCode")
        msg = str(d.get("errMsg") or "")
        _LAST_ARTICLES_ERR, _LAST_ARTICLES_MSG = err, msg
        # -2041 = 触发腾讯防水墙/验证码: 需在真实 Edge 的阅读器页刷新过验证后才恢复
        if err == -2041:
            logger.warning("articles errCode=-2041(%s) bookId=%s", msg or "防水墙验证码", book_id)
            _antibot_wait(
                f"articles -2041(bookId={book_id})",
                "请到打开的 Edge 窗口刷新公众号阅读器页, 手动完成验证码/滑块",
            )
            d = _run_page_js(cookie, js, "articles", page_url)  # 人工处理后重试一次
            if d.get("errCode"):
                _LAST_ARTICLES_ERR = d.get("errCode")
                _LAST_ARTICLES_MSG = str(d.get("errMsg") or "")
                logger.warning("人工处理后 articles 仍 errCode=%s(%s) bookId=%s, 跳过该号",
                               _LAST_ARTICLES_ERR, _LAST_ARTICLES_MSG, book_id)
                return []
        else:
            # 必须带上 errMsg: errCode 本身认不出病因。例如 -2010 的原文是"用户不存在",
            # 一眼就知道是登录态失效而不是频控/UA —— 只记数字会把排查带偏。
            logger.warning("articles errCode=%s(%s) bookId=%s", err, msg, book_id)
            return []
    _LAST_ARTICLES_ERR, _LAST_ARTICLES_MSG = None, ""
    items = [it for it in d.get("items", []) if it.get("rid")]
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


def _content_js(review_id: str) -> str:
    """构造 /web/mp/content 的页面内 JS: fetch 正文页 → DOMParser 解析 → 只回传小字段。

    正文页有 ~3.5MB(整页 JS/CSS), 全量回传会把 CDP 通道堵死; 故在页面里解析
    (og:url/og:title/og:image/createTime/#js_content), 只把提炼后的 JSON 交给 Python。
    """
    rid_lit = json.dumps(review_id)  # JS 字符串字面量(引号/转义安全)
    return (
        "(()=>fetch('/web/mp/content?reviewId='+encodeURIComponent(" + rid_lit + "),"
        "{credentials:'include'}).then(r=>r.text()).then(t=>{"
        "var doc=new DOMParser().parseFromString(t,'text/html');"
        # meta 标签手工收集(避免在 JS 里写带引号的属性选择器, 少一层转义)
        "var m={};var ms=doc.getElementsByTagName('meta');"
        "for(var i=0;i<ms.length;i++){var k=ms[i].getAttribute('property')||ms[i].getAttribute('name');"
        "if(k&&!m[k])m[k]=ms[i].getAttribute('content')||'';}"
        "var jc=doc.querySelector('#js_content');"
        "var txt=jc?(jc.innerText||jc.textContent||''):'';"
        "txt=txt.replace(/[ \\t\\u00a0]+/g,' ').replace(/\\s*\\n\\s*/g,'\\n').trim();"
        "var ct=(t.match(/createTime\\s*=\\s*'([^']*)'/)||[])[1]||'';"
        # 个别正文页没有 og:url(转载/分享样式), 退回经典页里的 msg_link 变量
        "var ml=(t.match(/msg_link\\s*=\\s*[\\x22']([^\\x22']+)[\\x22']/)||[])[1]||'';"
        "return JSON.stringify({link:m['og:url']||ml||'',title:m['og:title']||'',"
        "image:m['og:image']||'',createTime:ct,contentLen:txt.length,"
        "content:txt.slice(0," + str(CONTENT_CHARS) + ")});"
        "}).catch(e=>JSON.stringify({err:String(e)})))()"
    )


def _content_head_js(review_id: str, limit: int) -> str:
    """构造【边读边找链接, 一找到就停】的 /web/mp/content JS(流式读取 + reader.cancel)。

    **为什么不按字节数停**: getReader() 按块投递, 无法预知下一块多大, 所以
    【读到 >= N 字节就停】必然整块溢出。2026-10-17 实测: 数据块粒度约 36KB,
    阈值设 96KB 时实际每题读 ~144KB(4 块), 平均 143,318 字节。

    **改为能提取出链接就立刻停**: og:url 出现在第一块内, 命中即 cancel, 实测
    2026-10-18 对天津大学 8 篇各读 36,295~36,403 字节(平均 36,361), 全部 stopped=found,
    没有任何一篇走到兜底上限 —— 比字节阈值版又省 4 倍, 是全文(3.5MB)的 1/96。
    而且比"猜一个字节数"鲁棒: 页面结构变了也不会失效。

    注意: 因为只读第一块, 正文页里的 createTime 变量拿不到(它在后面)。这对调用方无影响
    —— 列表阶段本身带 createTime 时间戳, _consume_one 会回退到它。

    limit 退化成**兜底上限**: 万一链接迟迟不出现(验证页/异常页), 读到该字节数为止。

    stopped 字段: found=命中链接 / limit=触到兜底上限 / eof=响应读完。
    """
    rid_lit = json.dumps(review_id)  # JS 字符串字面量(引号/转义安全)
    return (
        "(()=>fetch('/web/mp/content?reviewId='+encodeURIComponent(" + rid_lit + "),"
        "{credentials:'include'}).then(r=>{"
        "if(!r.body)return JSON.stringify({err:'no_body_stream'});"
        "var rd=r.body.getReader(),dec=new TextDecoder('utf-8'),t='',total=0;"
        # 链接提取: og:url 优先, 其次经典页里的 msg_link 变量。
        # \x22 表示双引号, 让正则字符类只认双引号/单引号两种写法, 避开 Python 转义。
        "function link(){"
        "var og=t.match(/property=[\x22']og:url[\x22'][^>]*content=[\x22']([^\x22']+)[\x22']/i)"
        "||t.match(/content=[\x22']([^\x22']+)[\x22'][^>]*property=[\x22']og:url[\x22']/i);"
        "if(og&&og[1])return og[1];"
        "var ml=t.match(/msg_link\\s*=\\s*[\x22']([^\x22']+)[\x22']/);"
        "return (ml&&ml[1])||'';}"
        # 收尾: 拼结果 JSON, stopped 标明是哪种停法
        "function finish(stop){"
        "var ot=t.match(/property=[\x22']og:title[\x22'][^>]*content=[\x22']([^\x22']*)[\x22']/i)"
        "||t.match(/content=[\x22']([^\x22']*)[\x22'][^>]*property=[\x22']og:title[\x22']/i);"
        "var im=t.match(/property=[\x22']og:image[\x22'][^>]*content=[\x22']([^\x22']+)[\x22']/i)"
        "||t.match(/content=[\x22']([^\x22']+)[\x22'][^>]*property=[\x22']og:image[\x22']/i);"
        "var ct=(t.match(/createTime\\s*=\\s*'([^']*)'/)||[])[1]||'';"
        "return JSON.stringify({bytes:total,chars:t.length,stopped:stop,link:link(),"
        "title:ot?ot[1]:'',image:im?im[1]:'',createTime:ct});}"
        # 读取循环: 每读一块先看链接出现没有 —— 出现就 cancel, 这是省流的关键
        "function pump(){return rd.read().then(res=>{"
        "if(res.done)return finish('eof');"
        "total+=res.value.length;"
        "t+=dec.decode(res.value,{stream:true});"
        "if(link()){try{rd.cancel()}catch(e){}return finish('found');}"
        "if(total>=" + str(int(limit)) + "){try{rd.cancel()}catch(e){}return finish('limit');}"
        "return pump();});}"
        "return pump();"
        "}).catch(e=>JSON.stringify({err:String(e)})))()"
    )



# 是否用"只读前 N 字节"的方式取链接(0 = 关闭, 一律全量读取)。
# 96KB 是 steptian/weread-mp 给"只要链接"场景的值(og:url 在 ~17KB 处)。
HEAD_BYTES = int(os.environ.get("WEREAD_HEAD_BYTES", "98304") or 0)


def fetch_article_head(reader_url: str, review_id: str, cookie: str = "") -> dict:
    """只读正文页前 HEAD_BYTES 字节, 取回链接/标题/封面/发布时间(不含正文文本)。

    与 fetch_article_content 相同的返回结构, 但 content 恒为空、content_len=0,
    并额外带 head_bytes=True 表示"这是省流模式"。
    """
    if not review_id or HEAD_BYTES <= 0:
        return {"link": "", "title": "", "content": "", "create_time": "",
                "cover": "", "content_len": 0, "head_bytes": False}
    d = _run_page_js(cookie, _content_head_js(review_id, HEAD_BYTES), "content-head",
                     reader_url or "https://weread.qq.com/")
    if d.get("err"):
        logger.warning("content-head 失败 rid=%s: %s", review_id[:40], str(d.get("err"))[:120])
    return {
        "link": (d.get("link") or "").strip(),
        "title": (d.get("title") or "").strip(),
        "content": "",
        "create_time": (d.get("createTime") or "").strip(),
        "cover": (d.get("image") or "").strip(),
        "content_len": 0,
        "head_bytes": True,
        "bytes_read": int(d.get("bytes") or 0),
        "chars_read": int(d.get("chars") or 0),
        # found=命中链接就停 / limit=触到兜底上限 / eof=响应读完
        "stopped": d.get("stopped") or "",
    }


def fetch_article_content(reader_url: str, review_id: str, cookie: str = "") -> dict:
    """调 /web/mp/content 拿单篇正文页 → {"link","title","content","create_time","cover"}.

    失败/无正文返回空字典里的空值(link 为空时调用方应丢弃该篇)。
    """
    if not review_id:
        return {"link": "", "title": "", "content": "", "create_time": "", "cover": ""}
    d = _run_page_js(cookie, _content_js(review_id), "content",
                     reader_url or "https://weread.qq.com/")
    if d.get("err"):
        logger.warning("content 失败 rid=%s: %s", review_id[:40], str(d.get("err"))[:120])
    return {
        "link": (d.get("link") or "").strip(),
        "title": (d.get("title") or "").strip(),
        "content": d.get("content") or "",
        "create_time": (d.get("createTime") or "").strip(),
        "cover": (d.get("image") or "").strip(),
        "content_len": int(d.get("contentLen") or 0),
    }


def _normalize_link(link: str) -> str:
    """规范化原文链接用于去重: 去 fragment(#rd)、去空白、反转义 HTML 实体、补 https。

    同一篇文章在不同上下文里可能带 `#rd`/`#wechat_redirect` 等锚点, 不归一会导致同一篇
    被当成多篇重复收录。
    """
    if not link:
        return ""
    link = html.unescape(link).strip()
    link = link.split("#", 1)[0]
    if link.startswith("//"):
        link = "https:" + link
    return link


def _consume_one(entry: dict, reader_url: str, seen: set[str],
                 articles: list[dict]) -> bool:
    """抓取一篇(链接+正文+时间+封面)并写入 articles。

    Args:
        entry: {"sub": 订阅号 dict, "item": 列表条目 dict}
        reader_url: 阅读器页 URL(请求上下文)
        seen: 已收录链接集合(原地增补)
        articles: 产出列表(原地追加)

    Returns:
        True  = 该条已了结(拿到链接; "已收录因而不产出"也算了结)
        False = 取不到链接, 调用方应把它留在待重试集里
    """
    it, sub = entry["item"], entry["sub"]
    # 只做【省流取链】: 读正文页第一块(实测约 36KB)拿到 og:url 就 cancel, 不再全量抓正文。
    # 摘要来自列表自带的 mpInfo.content(微信自动摘要), 正文文本对本项目已无用。
    # 全量读取会把每篇从 36KB 抬到 3.5MB(约 96 倍), 正是 -2014 限流的主要来源。
    info = fetch_article_head(reader_url, it.get("rid", ""))
    _sleep_interval()
    link = _normalize_link(info.get("link", ""))
    if not link:
        # 多为瞬时风控: 返回 2KB 的「验证」页而非 3.5MB 正文, og:url/msg_link 都是空。
        # 不是文章本身的问题 —— 何时重试由调用方决定(见 fetch_subscribed_articles 轮末重试)。
        logger.warning("取不到原文链接(留待重试, stopped=%s): %s",
                       info.get("stopped") or "?", (it.get("t") or "")[:30])
        return False
    if link in seen:
        return True  # 已收录(或本轮已被别的号转载过), 不产出
    seen.add(link)  # 本轮内跨账号去重: 同一篇被多号转载只留首次
    articles.append({
        "title": info.get("title") or it.get("t", ""),
        "link": link,
        "publish_time": info.get("create_time") or it.get("date", ""),
        "createTime": it.get("date", ""),
        "source": sub.get("name"),
        # 摘要直接用微信自带的那句(mpInfo.content): 它是微信从正文里抽的, 质量够用、零成本,
        # 而且列表阶段就拿到了 —— 这样不抓正文的条目(Part 3 / 省流模式)也能有摘要。
        "summary": (it.get("dig") or "").strip(),
        "content": info.get("content", ""),
        "image": info.get("cover") or it.get("pic") or "",
        "read_num": int(it.get("read") or 0),
        "like_num": int(it.get("like") or 0),
    })
    return True


def _ask_continue_retry(count: int) -> bool:
    """问用户是否继续重抓取不到链接的 count 篇。Y=继续, N=停止。

    非交互环境(stdin 非终端, 如 CI/管道)返回 False —— 没人能回答, 不能无限循环。
    """
    print(f"\n{CYAN}[待重试] 仍有 {count} 篇取不到原文链接(多为瞬时风控拦截)。{EOM}",
          flush=True)
    try:
        if not sys.stdin.isatty():
            logger.info("非交互环境, 不再重试剩余 %d 篇", count)
            return False
        ans = input(f"{CYAN}继续抓取这 {count} 篇? [Y=继续 / N=停止]: {EOM}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        logger.info("用户中断, 停止重试")
        return False
    return ans in ("y", "yes", "是")


def fetch_subscribed_articles(cookie: str = "", seen_links: set[str] | None = None,
                             gzh_last_update: dict[str, int] | None = None,
                             gzh_last_list_ok: dict[str, int] | None = None,
                             ) -> tuple[list[dict], list[dict]]:
    """主入口: 书架订阅号 → articles列表 → 逐篇 content(链接/正文/时间/封面). 返回 (articles, inactive).

    复用真实 Edge(CDP)。每篇先调 /web/mp/content 取规范原文链接(全格式), 再按链接去重:
      - 列表页 createTime 超出回溯窗口(WECHAT_LOOKBACK_DAYS, 默认 3 天)的篇不抓正文;
      - 命中 seen_links(已收录) 的, 跳过不产出(但请求已发出 —— 因为链接只有抓完才知道);
      - 同一次运行内跨账号重复的(同一篇被多个号转载), 只保留首次出现的。
    取不到链接的篇(多为瞬时风控, 返回 2KB「验证」页而非 3.5MB 正文)**不当场丢弃**:
    条目连同所属号留在待重试集(位置保留), 整轮跑完后再整体重试 2 次; 若仍有剩余,
    交互式问用户是否继续(Y=继续重抓 / N=停止); 非交互环境(CI/管道)直接停, 不无限循环。
    最终仍取不到的篇不写进报告/state —— 链接是去重键, 空链接进去会污染 processed_links。

    seen_links: 已收录链接集合(只读, 不改写调用方的集合)。
    articles: [{"title","link","publish_time","createTime","source","content","summary","image"}]
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
    if MAX_ACCOUNTS:
        subs = subs[:MAX_ACCOUNTS]
        logger.info("WEREAD_MAX_ACCOUNTS=%d, 只处理前 %d 个订阅号", MAX_ACCOUNTS, len(subs))

    # 阅读器页上下文: 取任一所订阅号的 readerUrl 作为统一抓取页(参考: 进任意一个阅读器页)。
    # /web/mp/articles 必须在此上下文发, 否则 -2041。
    reader_url = next((s.get("readerUrl") for s in subs if s.get("readerUrl")), "")
    if not reader_url:
        logger.warning("书架条目均无 readerUrl(deepLink 缺 v), articles 会回退首页上下文(可能 -2041)")

    seen = set(seen_links or ())  # 拷贝: 不改写调用方集合
    articles = []
    inactive = []
    # 取不到链接的条目(保留所属号与列表位置), 整轮跑完后统一重试
    pending: list[dict] = []
    same_err: int | None = None   # 上一个号的错误码(用于判"同一错误连续出现")
    same_err_hits = 0
    aborted = False
    # 第一阶段只列列表, 候选正文留到第二阶段限额抓取(见下方"第二阶段")
    candidates: list[dict] = []
    processed = 0                 # 实际发过列表请求的号数
    skipped: list[str] = []       # 失败被跳过的号(下轮优先补)
    rate_hit_streak = 0           # 连续多少个号失败
    dormant = 0                   # 窗口内不可能有更新、免列表请求的号
    unchanged = 0                 # 自上次运行以来无新文章、免列表请求的号
    _now = datetime.now()
    cutoff_ts = (_now - timedelta(days=LOOKBACK_DAYS)).timestamp() if LOOKBACK_DAYS > 0 else None
    one_year_ts = (_now - timedelta(days=365)).timestamp()
    last_ok = gzh_last_list_ok if gzh_last_list_ok is not None else {}
    last_lct = gzh_last_update if gzh_last_update is not None else {}

    # ---- 第一步: 纯本地预筛(零请求), 决定哪些号需要发列表请求 ----
    # 顺序: 1 年未更新 -> 记失效; 窗口外 -> 跳过; 书架 lct 与上次记录相同 -> 无新文章跳过。
    todo_subs: list[dict] = []
    for sub in subs:
        book_id = sub.get("bookId", "")
        if not book_id:
            continue
        lct = int(sub.get("last_article_ts") or 0)
        if lct > 0:
            lct_date = datetime.fromtimestamp(lct, timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
            if lct < one_year_ts:
                inactive.append({"name": sub.get("name"), "bookId": book_id,
                                 "last_update": lct_date})
                logger.warning("订阅号[%s] 1年未更新(%s), 记失效(免列表请求)",
                               sub.get("name"), lct_date)
                continue
            if cutoff_ts is not None and lct < cutoff_ts:
                dormant += 1
                continue
            if last_lct.get(book_id) == lct:
                unchanged += 1
                continue
        todo_subs.append(sub)

    # ---- 第二步: 排序 —— "最久没成功抓过完整列表"的号优先 ----
    # gzh_last_list_ok 只在**成功**时更新, 所以失败过的号时间戳天然偏旧, 会自动排到前面;
    # 从未成功过的(不在表里)最优先。这样即使配额紧张, 也不会有号被永久饿死。
    todo_subs.sort(key=lambda s: last_ok.get(s.get("bookId", ""), 0))
    logger.info("列表阶段待办: %d 个号需要发列表请求(预筛免请求 %d + 无新文章 %d, "
                "共 %d 个号); 按\"最久未成功抓全\"排序",
                len(todo_subs), dormant, unchanged, len(subs))

    # ---- 第三步: 逐个抓列表。**任何失败立即跳过, 不重试** ----
    # 列表接口按请求次数限额, 重试等于额外烧配额, 还会把后面的号一起拖进限流。
    # 失败的号不更新 gzh_last_list_ok -> 下轮自动排到前面优先补抓。
    budget_left = UNIT_BUDGET      # 本轮剩余配额(单位), 见 UNIT_BUDGET 注释
    list_used = 0                  # 本轮已发的列表请求数
    budget_stop = False            # 预算/限流导致提前收手
    for sub in todo_subs:
        book_id = sub.get("bookId", "")
        lct = int(sub.get("last_article_ts") or 0)
        # 闸门 1(次数硬上限): 列表请求每轮最多 MAX_LIST_REQUESTS 次。
        if list_used >= MAX_LIST_REQUESTS:
            logger.warning("列表请求已达每轮上限 %d 次 —— 剩余 %d 个号留到下轮优先补抓",
                           MAX_LIST_REQUESTS, len(todo_subs) - processed)
            budget_stop = True
            break
        # 闸门 2(配额单位): 列表 1.0 + 正文 0.1 共享 UNIT_BUDGET。
        if budget_left < BUDGET_COST_LIST - 1e-9:   # 1e-9: 避免 0.1 累减的浮点误差
            logger.warning("本轮配额已用完(%.1f 单位) —— 剩余 %d 个号留到下轮优先补抓",
                           UNIT_BUDGET, len(todo_subs) - processed)
            budget_stop = True
            break
        budget_left -= BUDGET_COST_LIST
        list_used += 1
        processed += 1
        _maybe_keepalive()   # 长轮次里定期续 wr_skey, 见 KEEPALIVE_SECONDS
        logger.info("== 抓取订阅号[%d/%d %s] %s ==", processed, len(todo_subs),
                    sub.get("name"), book_id)
        item_list = fetch_articles("", book_id, reader_url)
        _sleep_list_interval()

        if not item_list:
            err = _LAST_ARTICLES_ERR
            if err in RATE_LIMIT_CODES:
                # 限流: 立即跳过, 不重试。省下的配额留给还没扫的号。
                rate_hit_streak += 1
                skipped.append(sub.get("name") or book_id)
                logger.warning("该号被限流 errCode=%s(%s) -> 立即跳过(不重试, 省配额); "
                               "已标记, 下轮优先补抓(累计跳过 %d 个)",
                               err, _LAST_ARTICLES_MSG, len(skipped))
                if rate_hit_streak >= RATE_HIT_ABORT:
                    logger.error("连续 %d 个号被限流 —— 配额已耗尽, 中止列表阶段。"
                                 "已扫 %d 个号, 跳过 %d 个(下轮优先补抓)。",
                                 rate_hit_streak, processed, len(skipped))
                    aborted = True
                    budget_stop = True
                    break
                continue
            # 会话失效类: 连续出现说明登录态废了, 本轮没意义
            if err in SESSION_DEAD_CODES and SAME_ERR_ABORT > 0:
                if err == same_err:
                    same_err_hits += 1
                else:
                    same_err, same_err_hits = err, 1
                if same_err_hits >= SAME_ERR_ABORT:
                    logger.error(
                        "连续 %d 个订阅号都返回同一个错误 errCode=%s(%s) —— 判定登录态已失效, "
                        "提前中止本轮公众号抓取(已抓 %d 篇)。请在调试 Edge 里重新登录微信读书后重跑。",
                        same_err_hits, err, _LAST_ARTICLES_MSG, len(articles),
                    )
                    aborted = True
                    break
            skipped.append(sub.get("name") or book_id)
            continue
        rate_hit_streak = 0   # 成功即清零
        same_err, same_err_hits = None, 0

        # 判定 1 年未更新: 用最新一篇 createTime
        latest_ct = item_list[0].get("date", "")
        if latest_ct and _is_one_year_stale(latest_ct):
            inactive.append({"name": sub.get("name"), "bookId": book_id,
                             "last_update": latest_ct})
            logger.warning("订阅号[%s] 1年未更新(%s), 记失效", sub.get("name"), latest_ct)

        todo = item_list[:MAX_ITEMS_PER_SOURCE] if MAX_ITEMS_PER_SOURCE else item_list
        cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)) if LOOKBACK_DAYS > 0 else None
        skipped_old = 0
        new_here = 0
        for it in todo:
            rid = it.get("rid", "")
            if not rid:
                continue
            if cutoff is not None:
                d = _list_date(it.get("date", ""))
                if d is not None and d < cutoff:
                    skipped_old += 1
                    continue  # 超出回溯窗口: 不抓正文
            candidates.append({"sub": sub, "item": it})
            new_here += 1
        if new_here:
            logger.info("  [%s] 窗口内候选 %d 篇", sub.get("name"), new_here)
        if skipped_old:
            logger.info("  [%s] 超出回溯窗口(%s 天)跳过 %d 篇(列表共 %d 篇)",
                        sub.get("name"), LOOKBACK_DAYS, skipped_old, len(todo))
        # 成功抓到完整列表 -> 记下"什么时候抓成功的"和"当时的最新文章时间"。
        # 只有成功路径更新: 失败的号时间戳保持旧值, 下轮自动优先补抓。
        last_ok[book_id] = int(time.time())
        if lct > 0:
            last_lct[book_id] = lct


    # ===== 第二阶段: 取原文链接(省流, 限额) =====
    # 第一阶段每个号只发一个几 KB 的 /web/mp/articles, 所以 92 个号一定能扫完 ——
    # 这是"覆盖全部公众号"的底线。第二阶段每篇只读正文页第一块(~36KB)拿 og:url,
    # 不再全量读正文(3.5MB); 超出的候选没进 seen, 下一轮仍在窗口内 → 自然补上。
    logger.info("列表阶段完成: 列表请求 %d/%d 次(上限), 书架 %d 个号(预筛免请求 %d + 无新文章 %d"
                "%s); 窗口内候选 %d 篇; 本轮取链上限 %d 篇; 剩余配额 %.1f 单位%s",
                list_used, MAX_LIST_REQUESTS, len(subs), dormant, unchanged,
                (f" + 失败跳过 {len(skipped)}" if skipped else ""),
                len(candidates), CONTENT_MAX_PER_RUN, budget_left,
                ("(闸门触发, 剩余号下轮优先补)" if budget_stop else ""))
    todo_content = candidates[:CONTENT_MAX_PER_RUN] if CONTENT_MAX_PER_RUN else candidates
    deferred = len(candidates) - len(todo_content)
    if deferred > 0:
        logger.warning("本轮正文限额 %d 篇, 顺延 %d 篇到下一轮(仍在回溯窗口内, 会重新成为候选)",
                       CONTENT_MAX_PER_RUN, deferred)
    for entry in todo_content:
        # 取链也吃配额(实测约 0.1 单位/篇), 同样受预算约束
        if budget_left < BUDGET_COST_CONTENT - 1e-9:   # 1e-9: 避免 0.1 累减的浮点误差
            logger.warning("本轮配额已用完(%.1f 单位, 列表已用 %d/%d 次) —— 取链阶段提前收手, "
                           "剩余候选未进 seen, 下轮仍在窗口内会自动补上",
                           UNIT_BUDGET, list_used, MAX_LIST_REQUESTS)
            budget_stop = True
            break
        budget_left -= BUDGET_COST_CONTENT
        _maybe_keepalive()   # 取链阶段同样可能拖长
        if not _consume_one(entry, reader_url, seen, articles):
            # 不当场丢弃: 保留条目与位置, 轮末统一重试(见下方轮末重试)
            pending.append(entry)
    logger.info("订阅公众号抓取: %d 篇来自 %d 个订阅号(本轮实扫 %d/%d 个号)",
                len(articles), len({a.get("source") for a in articles}),
                processed, len(subs))

    # 已判定会话失效/持续限流: 重试没有意义(报错来自账号层面, 不是单篇抽取失败), 直接跳过
    if aborted:
        logger.warning("本轮已提前中止, 跳过轮末重试")
        return articles, inactive

    # 轮末重试: 对取不到链接的条目再整体循环两次。这类失败多为瞬时风控
    # (返回 2KB「验证」页而非 3.5MB 正文), 隔几秒重试通常就拿到了。
    for rnd in (1, 2):
        if not pending:
            break
        logger.info("第 %d 轮重试: %d 篇待重抓", rnd, len(pending))
        pending = [e for e in pending
                   if not _consume_one(e, reader_url, seen, articles)]

    # 两次重试后仍有剩余 -> 问用户是否继续(Y=继续 / N=停止)。
    # 非交互环境直接停, 避免无人值守时无限循环。
    while pending:
        if not _ask_continue_retry(len(pending)):
            break
        logger.info("用户选择继续: 再抓 %d 篇", len(pending))
        pending = [e for e in pending
                   if not _consume_one(e, reader_url, seen, articles)]

    if pending:
        logger.warning("最终仍有 %d 篇取不到原文链接, 本轮不收录(下轮窗口内会再试): %s",
                       len(pending),
                       ", ".join((e["item"].get("t") or "")[:20] for e in pending[:5]))
    return articles, inactive


def _is_one_year_stale(date_str: str) -> bool:
    """date_str(YYYY-MM-DD) 距今 >1 年判定失效. 空/解析失败不算失效(防误删)."""
    dt = _list_date(date_str)
    if dt is None:
        return False
    return (datetime.now() - dt) > timedelta(days=365)


def _list_date(date_str: str) -> datetime | None:
    """把列表页的日期串(YYYY-MM-DD 或 YYYY-MM-DD HH:MM)解析成 naive datetime; 失败返回 None."""
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_str or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None

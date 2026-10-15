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
默认间隔: 所有请求之间 3s±1s。触发反爬(articles -2041 防水墙)时打印提示, 等待用户在
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

# 请求间隔(秒): 3s ± 1s(原 180±60s 过慢; 间隔短时靠下方反爬闸门人工兜底)。
# 回填(一次性重抓全部在列文章, 上千次请求)可用 WEREAD_INTERVAL/WEREAD_INTERVAL_SPREAD 调快。
REQUEST_INTERVAL_MEAN = float(os.environ.get("WEREAD_INTERVAL", "3.0") or 3.0)
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
    offset 是"已跳过的群发条数"(非文章篇数)。

    返回 [{"t":标题,"rid":reviewId,"ct":发布ts,"date":北京日期}...]。
    注意: mpInfo.originalId 已是加密长串, 拼不出原文链接 —— 链接必须再调
    fetch_article_content(rid) 从正文页 og:url 取。
    """
    url = f"/web/mp/articles?bookId={book_id}&offset={offset}"
    js_safe = url.replace("\\", "\\\\").replace("'", "\\'")
    js = ("(()=>fetch('" + js_safe + "',{credentials:'include'})"
          ".then(r=>r.json()).then(o=>JSON.stringify({errCode:o.errCode,"
          "items:(o.reviews||[]).map(g=>(g.subReviews||[]).map(s=>{var mi=(s.review&&s.review.mpInfo)||{};"
          "return {t:mi.title,rid:(s.review&&s.review.reviewId)||'',"
          "ct:s.review?Number(s.review.createTime||0):0}})).flat(),"
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


def fetch_subscribed_articles(cookie: str = "", seen_links: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """主入口: 书架订阅号 → articles列表 → 逐篇 content(链接/正文/时间/封面). 返回 (articles, inactive).

    复用真实 Edge(CDP)。每篇先调 /web/mp/content 取规范原文链接(全格式), 再按链接去重:
      - 列表页 createTime 超出回溯窗口(WECHAT_LOOKBACK_DAYS, 默认 3 天)的篇不抓正文;
      - 命中 seen_links(已收录) 的, 跳过不产出(但请求已发出 —— 因为链接只有抓完才知道);
      - 同一次运行内跨账号重复的(同一篇被多个号转载), 只保留首次出现的。
    链接为空(抓失败)的篇直接丢弃, 不写进报告/state。

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

        todo = item_list[:MAX_ITEMS_PER_SOURCE] if MAX_ITEMS_PER_SOURCE else item_list
        cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)) if LOOKBACK_DAYS > 0 else None
        skipped_old = 0
        for it in todo:
            rid = it.get("rid", "")
            if not rid:
                continue
            if cutoff is not None:
                d = _list_date(it.get("date", ""))
                if d is not None and d < cutoff:
                    skipped_old += 1
                    continue  # 超出回溯窗口: 不抓正文(省下 ~3.5MB/篇 的请求)
            info = fetch_article_content(reader_url, rid)
            _sleep_interval()
            link = _normalize_link(info.get("link", ""))
            if not link:
                logger.warning("跳过(取不到原文链接): %s", (it.get("t") or "")[:30])
                continue
            if link in seen:
                continue  # 已收录(或本轮已被别的号转载过), 不产出
            seen.add(link)  # 本轮内跨账号去重: 同一篇被多号转载只留首次
            articles.append({
                "title": info.get("title") or it.get("t", ""),
                "link": link,
                "publish_time": info.get("create_time") or it.get("date", ""),
                "createTime": it.get("date", ""),
                "source": sub.get("name"),
                "summary": "",
                "content": info.get("content", ""),
                "image": info.get("cover", ""),
            })
        if skipped_old:
            logger.info("  [%s] 超出回溯窗口(%s 天)跳过 %d 篇(列表共 %d 篇)",
                        sub.get("name"), LOOKBACK_DAYS, skipped_old, len(todo))
    logger.info("订阅公众号抓取: %d 篇来自 %d 个订阅号", len(articles), len(subs))
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

"""TJU Daily Bot 主入口 — 整合爬虫、AI 分类、报告生成、检查、推送。

流程：
1. 加载 state.json
2. 遍历 sources.yaml 信源，抓取文章
3. 增量过滤
4. AI 分类（Part1 关键词命中 / Part2 AI 推荐 / Part3 其余）
5. 生成 Markdown 报告
6. 独立检查
7. 更新 state.json 和搜索索引
8. CI 环境下自动推送
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Any

import yaml

# 北京时区 (UTC+8)；GitHub Actions 运行于 UTC，报告时间戳须用北京时间
_BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """返回当前北京时间 (带时区)."""
    return datetime.now(_BEIJING_TZ)


def _daily_window_date() -> str:
    """当天调度窗口日期: 北京 hour>=4 用当天日期, hour<4 用前一天.

    窗口 = 北京时间 4:00 ~ 次日 4:00 归一个"天"。
    """
    now = beijing_now()
    if now.hour < 4:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 加载 .env（GitHub Actions 用 env: 注入，本地用 .env）
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from src.crawler.web_crawler import fetch_articles_from_list_page
from src.crawler.weread_mp_crawler import fetch_wechat_articles, get_weread_cookie
from src.crawler.wechat_summary import enhance_wechat_articles
from src.ai_engine.fault_tolerant_client import FaultTolerantClient
from src.ai_engine.independent_checker import IndependentChecker
from tui.local_git import commit_and_push
from tui.search_handler import SearchHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(PROJECT_ROOT, "..", "state.json")
SOURCES_PATH = os.path.join(PROJECT_ROOT, "..", "config", "sources.yaml")
KEYWORDS_PATH = os.path.join(PROJECT_ROOT, "..", "config", "keywords.txt")
PROFILE_PATH = os.path.join(PROJECT_ROOT, "..", "config", "user_profile.yaml")
BOOTSTRAP_PATH = os.path.join(PROJECT_ROOT, "..", "config", "bootstrap.yaml")

# 信源引导默认阈值(与 config/bootstrap.yaml 保持一致)
DEFAULT_WINDOW_DAYS = 3
DEFAULT_DORMANT_DAYS = 30

# processed_links 上限(超出后从最旧的开始淘汰)
MAX_PROCESSED_LINKS = 10000


def load_state() -> dict[str, Any]:
    """加载或初始化 state.json."""
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        if "initialized_sources" not in state:
            # 迁移: 升级前已抓过的信源一律视为"已初始化", 否则全部老信源
            # 会在升级后第一轮被当成新信源, 每轮只出 1 篇。
            state["initialized_sources"] = sorted(state.get("source_last_fetch", {}).keys())
            logger.info(
                "state.json 迁移: 由 source_last_fetch 初始化 %d 个已有信源",
                len(state["initialized_sources"]),
            )
        return state
    return {
        "last_run": "",
        "processed_links": [],
        "source_last_fetch": {},
        "initialized_sources": [],
    }


def save_state(state: dict[str, Any]) -> None:
    """保存 state.json."""
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_sources() -> list[dict[str, Any]]:
    """加载信源配置."""
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def load_keywords() -> list[str]:
    """加载关键词."""
    if not os.path.exists(KEYWORDS_PATH):
        return []
    with open(KEYWORDS_PATH, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def load_profile() -> dict[str, str]:
    """加载用户画像."""
    if not os.path.exists(PROFILE_PATH):
        return {}
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_bootstrap_config() -> tuple[int, int]:
    """加载信源引导阈值 -> (window_days, dormant_days).

    配置缺失或字段非法时回退默认值, 保证不会中断流水线。
    """
    window_days, dormant_days = DEFAULT_WINDOW_DAYS, DEFAULT_DORMANT_DAYS
    try:
        if os.path.exists(BOOTSTRAP_PATH):
            with open(BOOTSTRAP_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            window_days = int(cfg.get("window_days", window_days))
            dormant_days = int(cfg.get("dormant_days", dormant_days))
    except Exception as e:
        logger.warning("bootstrap.yaml 读取失败, 使用默认值 %d/%d: %s",
                       DEFAULT_WINDOW_DAYS, DEFAULT_DORMANT_DAYS, e)
        return DEFAULT_WINDOW_DAYS, DEFAULT_DORMANT_DAYS
    return window_days, dormant_days


def is_new_article(article: dict, state: dict) -> bool:
    """检查文章是否为新内容（增量过滤）."""
    link = article.get("link", "")
    if link in state.get("processed_links", []):
        return False
    # 如果有发布时间，检查是否在 last_run 之后
    publish_time = article.get("publish_time", "")
    last_run = state.get("last_run", "")
    # 简单字符串比较（更精确的时间解析由具体爬虫负责）
    return True


def _ai_rank_and_summarize(
    articles: list[dict],
    keywords: list[str],
    profile: dict,
) -> tuple[list[dict], list[dict], list[dict]]:
    """真 AI 推荐 + LLM 精简摘要.

    - Part1 仍由关键词规则命中(确定性)
    - 其余文章用 LLM 判断"学生需做事项"(报名/申请/公示/通知/竞赛) → part2, 否则 part3
    - 每条生成精简摘要(≤120字), 写回 article["summary"]

    失败降级: LLM 调用失败 → 回退 classify_articles 规则(part2=前5, 无摘要), 不阻塞。

    Returns:
        (part1, part2, part3)
    """
    part1, _rest = [], []
    for a in articles:
        text = f"{a.get('title','')} {a.get('summary','')}".lower()
        if any(kw.lower() in text for kw in keywords):
            part1.append(a)
        else:
            _rest.append(a)

    if not articles:
        return part1, [], []

    # 所有文章(含 part1)都给 LLM 生成摘要; priority 只对 rest 排序用
    all_articles = articles  # part1 + _rest
    # 构造 LLM 输入: 每条 index + title + source + content 片段
    lines = []
    for i, a in enumerate(all_articles):
        hint = (a.get("content") or a.get("summary") or "").strip()[:300]
        lines.append(
            f"[{i}] 来源:{a.get('source','')} | 标题:{a.get('title','')}"
            + (f" | 内容:{hint}" if hint else "")
        )
    user_prompt = (
        "以下是若干条校园资讯(索引号标注)。用户画像: "
        + (profile.get('degree','') + '/' + profile.get('college','') + '/' + profile.get('major','')).strip('/')
        + "。\n"
        "任务:\n"
        "1. 对每条生成一句精简摘要(≤120字, 中文)。\n"
        "2. 判断每条 priority: 'high' 必须是【需要学生采取行动】的事项(如报名/申请/申报/选课/缴费/竞赛/评奖/公示/通知/截止/会议提醒/招聘); "
        "'normal' 是纯资讯/新闻/成果报道/科普, 无需学生操作。宁可少标 high, 也不要高估。\n"
        "只返回严格 JSON: {\"items\":[{\"index\":n,\"summary\":\"...\",\"priority\":\"high|normal\"}]}\n\n"
        + "\n".join(lines)
    )

    summaries: dict = {}
    priorities: dict = {}
    try:
        client = FaultTolerantClient()
        resp = client.call(
            prompt=user_prompt,
            system_prompt=(
                "你是面向天津大学的智能信息简报助手。输出必须为合法 JSON, 不得有多余文字。"
            ),
            temperature=0.2,
            max_tokens=2000,
        )
        content = resp.choices[0].message.content or ""
        import json as _json
        try:
            data = _json.loads(content)
        except Exception:
            from json_repair import repair_json
            data = _json.loads(repair_json(content))
        for it in data.get("items", []):
            idx = it.get("index")
            if isinstance(idx, int) and 0 <= idx < len(all_articles):
                summaries[idx] = it.get("summary", "")
                priorities[idx] = it.get("priority", "normal")
        logger.info("AI 摘要/推荐成功: %d 条", len(summaries))
    except Exception as e:
        logger.warning("AI 摘要/推荐失败, 回退规则式: %s", e)

    # 写回摘要(所有文章)
    for i, a in enumerate(all_articles):
        if i in summaries and summaries[i]:
            a["summary"] = summaries[i]

    # rest 按 priority 分 part2/part3(part1 不走此排序)
    # 规则兜底: 标题含明确行动类字眼的强制 high, 保证"学生需做事项"不被漏掉
    ACTION_KEYWORDS = ("申报", "报名", "申请", "选课", "缴费", "竞", "评选", "公示",
                       "通知", "提交", "截止", "动员", "启动", "征集", "招聘", "会议通知")
    rest_start = len(part1)
    part2, part3 = [], []
    for i, a in zip(range(rest_start, len(all_articles)), _rest):
        title = a.get("title", "")
        rule_high = any(k in title for k in ACTION_KEYWORDS)
        ai_high = priorities.get(i, "normal") == "high"
        if rule_high or ai_high:
            part2.append(a)
        else:
            part3.append(a)
    # AI 失败时降级: 无 priority 信息 → 至少规则保留入 part2
    if not priorities:
        part2 = _rest[:5]
        part3 = _rest[5:]
    if not part2:
        part2, part3 = _rest, []
    return part1, part2, part3


def generate_report(
    part1: list[dict],
    part2: list[dict],
    part3: list[dict],
    profile: dict,
    checker_result: dict | None = None,
    bootstrap_notes: list[dict] | None = None,
) -> str:
    """生成 Markdown 报告."""
    now = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# 天津大学每日智能信息简报",
        f"",
        f"**生成时间**: {now}",
        f"**用户画像**: {profile.get('degree', '未知')} | {profile.get('college', '未知')} | {profile.get('major', '未知')}",
        f"",
        f"---",
        f"",
        f"## Part 1: 关键词命中 ({len(part1)} 条)",
        f"",
    ]

    if part1:
        for i, article in enumerate(part1, 1):
            lines.append(f"### {i}. {article.get('title', '无标题')}")
            lines.append(f"- **来源**: {article.get('source', '未知')}")
            lines.append(f"- **链接**: [{article.get('link', '#')}]({article.get('link', '#')})")
            if article.get("image"):
                lines.append(f"![封面]({article['image']})")
            if article.get("publish_time"):
                lines.append(f"- **时间**: {article.get('publish_time')}")
            if article.get("summary"):
                lines.append(f"- **摘要**: {article['summary']}")
            lines.append("")
    else:
        lines.append("> 本日无关键词命中内容。")
        lines.append("")

    lines.extend([
        f"---",
        f"",
        f"## Part 2: AI 智能推荐 ({len(part2)} 条)",
        f"",
    ])

    if part2:
        for i, article in enumerate(part2, 1):
            lines.append(f"### {i}. {article.get('title', '无标题')}")
            lines.append(f"- **来源**: {article.get('source', '未知')}")
            lines.append(f"- **链接**: [{article.get('link', '#')}]({article.get('link', '#')})")
            if article.get("image"):
                lines.append(f"![封面]({article['image']})")
            if article.get("publish_time"):
                lines.append(f"- **时间**: {article.get('publish_time')}")
            if article.get("summary"):
                lines.append(f"- **摘要**: {article['summary']}")
            lines.append("")
    else:
        lines.append("> 本日无 AI 推荐内容。")
        lines.append("")

    lines.extend([
        f"---",
        f"",
        f"## Part 3: 其余信息 ({len(part3)} 条)",
        f"",
    ])

    if part3:
        for i, article in enumerate(part3, 1):
            lines.append(f"{i}. **{article.get('title', '无标题')}** — [{article.get('link', '#')}]({article.get('link', '#')})")
            lines.append(f"   - 来源: {article.get('source', '未知')} | 时间: {article.get('publish_time', '未知')}")
            lines.append("")
    else:
        lines.append("> 本日无其余信息。")
        lines.append("")

    # 信源引导说明: 只给计数, 不带标题/链接, 避免干扰独立检查的"有效链接"判定
    if bootstrap_notes:
        lines.extend([
            f"---",
            f"",
            f"## 信源引导 ({len(bootstrap_notes)} 个)",
            f"",
            f"> 以下信源为首次收录或长期失效后恢复，为免历史旧文刷屏，仅保留最新 1 篇"
            f"及近期文章，其余已标记为已收录。",
            f"",
        ])
        for n in bootstrap_notes:
            lines.append(
                f"- **{n['source']}**（{n['reason']}）：保留最新 1 篇 + 近 "
                f"{n.get('window_days', DEFAULT_WINDOW_DAYS)} 天，跳过 {n['skipped']} 篇历史文章"
            )
        lines.append("")

    # 检查报告
    if checker_result:
        lines.extend([
            f"---",
            f"",
            f"## 独立检查报告",
            f"",
            f"- **检查结果**: {'✅ 通过' if checker_result.get('passed') else '❌ 未通过'}",
        ])
        for err in checker_result.get("errors", []):
            lines.append(f"- **错误**: {err}")
        for warn in checker_result.get("warnings", []):
            lines.append(f"- **警告**: {warn}")
        lines.append("")

    return "\n".join(lines)


def update_index(articles: list[dict], output_file: str) -> None:
    """更新搜索索引."""
    handler = SearchHandler()
    for article in articles:
        article["output_file"] = output_file
        handler.index_article(article)


# 发布时间: 兼容 '2026-9-3'(网站源 _extract_date) 与 '2026-09-03 10:31'(公众号)
_PUBLISH_TS_RE = re.compile(
    r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[\sT]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


def _parse_publish_ts(s: str) -> datetime | None:
    """宽松解析发布时间, 解析失败返回 None(调用方回退抓取顺序)."""
    if not s:
        return None
    m = _PUBLISH_TS_RE.search(str(s))
    if not m:
        return None
    try:
        return datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
            tzinfo=_BEIJING_TZ,
        )
    except ValueError:
        return None


def _newest_article(articles: list[dict]) -> dict:
    """取最新一篇: 有可解析时间的取时间最大者; 全部无时间时回退抓取顺序第 1 篇.

    两个爬虫的列表页(TRS-CMS 列表、微信读书订阅列表)都是最新在前。
    """
    best = articles[0]
    best_ts = _parse_publish_ts(best.get("publish_time", ""))
    for a in articles[1:]:
        ts = _parse_publish_ts(a.get("publish_time", ""))
        if ts is None:
            continue
        if best_ts is None or ts > best_ts:
            best, best_ts = a, ts
    return best


def _bootstrap_sources(
    sources: list[dict], state: dict, now: datetime, dormant_days: int
) -> set[str]:
    """抓取前算出本轮需要"信源引导"的信源名集合.

    规则(满足其一):
      1. 全新信源 — 不在 state["initialized_sources"] 中
      2. 恢复信源 — 已初始化, 但距上次成功抓到文章超过 dormant_days

    必须在抓取循环之前调用: 抓取过程会写 source_last_fetch,
    晚算会让全新信源看起来"刚抓过"。
    """
    initialized = set(state.get("initialized_sources", []))
    last_fetch = state.get("source_last_fetch", {})
    boot: set[str] = set()
    for src in sources:
        name = src.get("name", "")
        if not name:
            continue
        if name not in initialized:
            boot.add(name)
            continue
        ts = _parse_publish_ts(last_fetch.get(name, ""))
        if ts is not None and now - ts > timedelta(days=dormant_days):
            boot.add(name)
    return boot


def _apply_source_bootstrap(
    all_articles: list[dict],
    new_articles: list[dict],
    boot_set: set[str],
    state: dict,
    window_days: int,
    now: datetime,
) -> tuple[list[dict], list[dict]]:
    """信源引导: boot_set 中的信源只保留最新 1 篇 + 近 window_days 天, 其余标记已收录.

    就地改写 state["processed_links"] / state["initialized_sources"], 由调用方既有的
    save_state 统一落盘(不新增落盘点); 中途崩溃则磁盘未变, 下轮重走引导。

    Returns:
        (过滤后的 new_articles, [{"source","kept","skipped","reason"}, ...])
        仅当真的跳过了文章时才产出记录。
    """
    if not boot_set:
        return new_articles, []

    initialized = set(state.setdefault("initialized_sources", []))
    processed = state.setdefault("processed_links", [])
    processed_set = set(processed)

    # 按信源分组(只保留有链接的, 空链接既不判定也不写入 processed_links)
    by_source: dict[str, list[dict]] = {}
    for a in all_articles:
        src = a.get("source", "")
        if src in boot_set and a.get("link"):
            by_source.setdefault(src, []).append(a)

    dropped: set[tuple[str, str]] = set()
    records: list[dict] = []
    cutoff = now - timedelta(days=window_days)
    for src, arts in by_source.items():
        reason = "失效恢复" if src in initialized else "首次收录"
        if not any(_parse_publish_ts(a.get("publish_time", "")) for a in arts):
            logger.warning(
                "信源引导[%s]: 发布时间全部无法解析, 仅保留抓取顺序第 1 篇", src)

        newest = _newest_article(arts)
        marked = 0
        for a in arts:
            if a is newest:
                continue
            ts = _parse_publish_ts(a.get("publish_time", ""))
            if ts is not None and ts >= cutoff:
                continue  # 窗口内, 一并收录
            dropped.add((src, a["link"]))
            if a["link"] not in processed_set:
                processed.append(a["link"])
                processed_set.add(a["link"])
                marked += 1

        initialized.add(src)
        if marked:
            records.append({"source": src, "kept": newest.get("link", ""),
                            "skipped": marked, "reason": reason,
                            "window_days": window_days})

    if initialized != set(state["initialized_sources"]):
        state["initialized_sources"] = sorted(initialized)

    filtered = [
        a for a in new_articles
        if (a.get("source", ""), a.get("link", "")) not in dropped
    ]
    return filtered, records


def main() -> None:
    """主流程."""
    logger.info("TJU Daily Bot starting...")

    # 1. 加载状态
    state = load_state()
    now_dt = beijing_now()
    now = now_dt.isoformat()  # 写入 state 的时间戳用 ISO 字符串
    is_ci = os.environ.get("CI", "").lower() == "true"

    # 1.5 一天一次闸门(替代旧的2h控闸): cron 每30min触发, 但当天(北京4:00~次日4:00)
    #    已运行则跳过。触发时进入执行前立即写 last_daily_date(中途失败下次也跳过)。
    #    RUN_FORCE(手动 workflow_dispatch) 或 FORCE(本地) 可强制绕过, 便于调试。
    force = os.environ.get("RUN_FORCE", "") == "true" or os.environ.get("FORCE", "") == "1"
    today_window = _daily_window_date()
    if state.get("last_daily_date") == today_window and not force:
        logger.info("当天(%s)已运行过, 跳过", today_window)
        return
    # 触发即写: 进入执行前立即标记"当天已运行"(即使中途失败, 后续30min触发也跳过)
    state["last_daily_date"] = today_window
    save_state(state)

    # 2. 加载信源，抓取网站 + 公众号（公众号经 we-mp-rss 拉 RSS）
    sources = load_sources()
    all_articles = []
    fetch_summary = {}
    # 已收录链接集合(公众号正文/网页详情抓取前提前跳过, 避免重复请求)
    seen_links = set(state.get("processed_links", []))
    # 信源引导阈值 + 本轮待引导信源。必须在抓取前算: 抓取过程会写 source_last_fetch,
    # 晚算会让全新信源看起来"刚抓过", 从而漏掉引导。
    window_days, dormant_days = load_bootstrap_config()
    boot_set = _bootstrap_sources(sources, state, now_dt, dormant_days)
    if boot_set:
        logger.info("待引导信源 %d 个: %s", len(boot_set), ", ".join(sorted(boot_set)))

    for source in sources:
        source_name = source.get("name", "Unknown")
        source_type = source.get("type", "")
        s = source.get("selectors", {})

        if source_type == "web":
            url = source.get("url", "")
            selectors = source.get("selectors", {})
            try:
                articles = fetch_articles_from_list_page(url, selectors, seen_links=seen_links)
            except Exception as e:
                logger.error("网站信源 %s 抓取失败(跳过): %s", source_name, e)
                articles = []
            for a in articles:
                a["source"] = source_name
            all_articles.extend(articles)
            fetch_summary[source_name] = len(articles)
            # 只在真正抓到内容时更新时间戳(该字段代表"最后一次成功抓到文章",
            # 用于判定信源是否"失效后恢复"; 列表页即使全部已收录也会返回条目)
            if articles:
                state["source_last_fetch"][source_name] = now

    # 3. 公众号批量抓取（微信读书 cover, 顺带判定 2 年未更新失效号）
    gzh_sources = [x for x in sources if x.get("type") == "wechat_rss"]
    if gzh_sources:
        # 本地/本地仿真可传 account_names；默认从 sources.yaml 读公众号名
        wechat_articles, inactive_gzh = fetch_wechat_articles(seen_links=seen_links)
        all_articles.extend(wechat_articles)
        fetch_summary["wechat_total"] = len(wechat_articles)
        # 处理失效公众号: 记录到 deprecated_accounts.yaml + 从 sources.yaml 真删
        if inactive_gzh:
            _prune_inactive_gzh(inactive_gzh, sources)
        # 只给本轮真正返回文章的号更新"最后成功抓到文章"时间戳
        for name in {a.get("source") for a in wechat_articles if a.get("link")}:
            if name:
                state["source_last_fetch"][name] = now

    # 3. 增量过滤
    new_articles = [a for a in all_articles if is_new_article(a, state)]
    # 3.5 信源引导: 新加入/失效恢复的信源只保留最新 1 篇 + 近 N 天, 其余标记为已收录,
    #     避免新信源的历史旧文一次性涌入简报
    new_articles, bootstrap_records = _apply_source_bootstrap(
        all_articles, new_articles, boot_set, state, window_days, now_dt)
    for r in bootstrap_records:
        log = logger.warning if r["skipped"] > 50 else logger.info
        log("信源引导[%s](%s): 保留最新 1 篇 + 近 %d 天, 跳过 %d 篇历史文章(已标记已收录)",
            r["source"], r["reason"], window_days, r["skipped"])
    logger.info("Total: %d, New: %d", len(all_articles), len(new_articles))

    if not new_articles:
        logger.info("No new articles, skipping report generation")
        # 即便本轮无新文章, 也更新 last_run(作为 2h 控闸基准)
        state["last_run"] = now
        # 北京6点后仍未生成当天总结则生成(可能当天已有历史报告)
        _maybe_daily_summary(state)
        save_state(state)
        return

    # 4. 加载关键词和用户画像
    keywords = load_keywords()
    profile = load_profile()

    # 5. 公众号文章增强: 补发布时间 + 正文(Playwright 渲染), 供 LLM 摘要
    try:
        cookie = get_weread_cookie()
        enhance_wechat_articles(new_articles, cookie)
    except Exception as e:
        logger.warning("公众号增强失败(跳过): %s", e)

    # 6. 真 AI 推荐 + LLM 摘要(失败降级为规则式)
    part1, part2, part3 = _ai_rank_and_summarize(new_articles, keywords, profile)

    # 7. 生成报告
    report = generate_report(part1, part2, part3, profile,
                             bootstrap_notes=bootstrap_records)

    # 7. 独立检查
    try:
        checker = IndependentChecker()
        checker_result = checker.check(report)
    except Exception as e:
        logger.warning("Checker skipped: %s", e)
        checker_result = None

    # 重新生成带检查结果的报告
    final_report = generate_report(part1, part2, part3, profile, checker_result,
                                   bootstrap_notes=bootstrap_records)

    # 8. 写入输出文件
    timestamp = beijing_now().strftime("%Y-%m-%d_%H-%M-%S")
    output_filename = f"{timestamp}.md"
    output_path = os.path.join(PROJECT_ROOT, "..", "output", output_filename)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(final_report)
    logger.info("Report written: %s", output_filename)

    # 9. 更新状态
    for a in new_articles:
        if a.get("link") and a["link"] not in state["processed_links"]:
            state["processed_links"].append(a["link"])
    state["last_run"] = now
    # 限制 processed_links 大小。上限过小(旧值 5000)会在约十几天后开始淘汰仍挂在
    # 列表页上的旧链接, 导致旧文被重复收录。
    if len(state["processed_links"]) > MAX_PROCESSED_LINKS:
        state["processed_links"] = state["processed_links"][-MAX_PROCESSED_LINKS:]
    save_state(state)

    # 10. 更新搜索索引
    update_index(new_articles, output_filename)

    # 11. CI 环境自动推送
    if is_ci:
        result = commit_and_push(f"daily report {beijing_now().strftime('%Y-%m-%d')}")
        logger.info("CI push: %s", result)

    # 12. 北京6点后仍未生成当天总结则生成并发邮件(独立于本轮有无新文章)
    _maybe_daily_summary(state)

    logger.info("TJU Daily Bot finished.")


def _maybe_daily_summary(state: dict) -> str | None:
    """若当天(北京 4:00~次日4:00 窗口)总结尚未生成, 则生成当日汇总并邮件发送.

    一天一次调度下, 当天首次跑即生成总结。幂等: 当天已生成过则跳过。
    Returns: 汇总文件路径, 或 None(当天已生成 / 失败)。
    """
    today = _daily_window_date()
    if state.get("last_summary_date") == today:
        logger.info("当天总结已生成过(%s), 跳过", today)
        return None
    try:
        summary_path = build_daily_summary(state)
        logger.info("每日汇总已生成: %s", summary_path)
        if summary_path and os.path.exists(summary_path):
            with open(summary_path, "r", encoding="utf-8") as f:
                summary_text = f.read()
            from src.notifier import send_alert
            ok = send_alert(
                f"📰 TJU Daily Bot 每日汇总 · {today}",
                summary_text,
            )
            logger.info("每日汇总邮件发送: %s", "成功" if ok else "失败(检查SMTP)")
        # 记录本轮已生成, 防止同一天重复汇总(此处即持久化, 不依赖调用方)
        state["last_summary_date"] = today
        state["last_summary_time"] = beijing_now().isoformat()
        save_state(state)
        return summary_path
    except Exception as e:
        logger.warning("每日汇总失败: %s", e)
        return None


def _prune_inactive_gzh(inactive: list[dict], sources: list[dict]) -> int:
    """把失效公众号从 sources.yaml 真删, 并追加记录到 config/deprecated_accounts.yaml.

    Args:
        inactive: [{"name","fakeid","last_update","removed_date"}, ...]
        sources: load_sources() 返回的列表(同步内存, 供后续 len 用)

    Returns:
        实际删除数
    """
    import yaml
    dep_path = os.path.join(PROJECT_ROOT, "..", "config", "deprecated_accounts.yaml")
    # 读已有记录(去重)
    dep = []
    if os.path.exists(dep_path):
        try:
            with open(dep_path, "r", encoding="utf-8") as f:
                dd = yaml.safe_load(f) or {}
            dep = dd.get("deprecated", [])
        except Exception:
            dep = []
    names = {d.get("name") for d in dep}
    added = 0
    for d in inactive:
        if d["name"] not in names:
            dep.append(d)
            names.add(d["name"])
            added += 1
    # 写回 deprecated_accounts.yaml
    with open(dep_path, "w", encoding="utf-8") as f:
        yaml.safe_dump({"deprecated": dep}, f, allow_unicode=True, sort_keys=False)

    # 从 sources.yaml 真删
    if added:
        src_path = os.path.join(PROJECT_ROOT, "..", "config", "sources.yaml")
        data = yaml.safe_load(open(src_path, encoding="utf-8"))
        keep = [s for s in data["sources"]
                if not (s.get("type") == "wechat_rss" and s.get("name") in names)]
        data["sources"] = keep
        with open(src_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        # 同步内存 sources(供 fetch_summary)
        sources[:] = keep[:]
        logger.warning("已从 sources.yaml 删除 %d 个失效公众号, 记录于 deprecated_accounts.yaml", added)
    return added


def build_daily_summary(state: dict) -> str | None:
    """把自上次当天总结(若无则全部)以来的所有短时总结, 其 Part1/2/3 完整内容
    移动聚合为当天总结。各 Part 内部按短时总结时间先后排序。

    Args:
        state: 含 last_summary_time(上次当天总结的 ISO 时间, 窗口基准)

    Returns:
        当天总结文件路径, 或 None(无可聚合内容)
    """
    import re
    today = beijing_now().strftime("%Y-%m-%d")
    output_dir = os.path.join(PROJECT_ROOT, "..", "output")
    summary_dir = os.path.join(output_dir, "summary")
    os.makedirs(summary_dir, exist_ok=True)

    # 窗口基准: 上次当天总结时间; 若无则取最早的短时总结时间(即从全部起算)
    base_ts = state.get("last_summary_time")

    # 收集 output/ 顶层所有短时总结(排除 summary/ 子目录), 解析文件名时间
    def _fname_ts(fn: str):
        # 形如 2026-08-30_01-39-06.md
        m = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.md$", fn)
        return m.groups() if m else None

    reports = []
    for fn in os.listdir(output_dir):
        ts = _fname_ts(fn)
        if not ts:
            continue
        fpath = os.path.join(output_dir, fn)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue
        # 组合成可比较时间字符串 YYYY-MM-DD HH:MM:SS
        dt = f"{ts[0]} {ts[1]}:{ts[2]}:{ts[3]}"
        reports.append({"file": fn, "dt": dt, "content": content})

    # 过滤窗口: dt > 上次当天总结时间
    if base_ts:
        try:
            base_dt = datetime.fromisoformat(base_ts)
            reports = [r for r in reports if _parse_dt(r["dt"]) > base_dt]
        except Exception:
            pass  # 基准解析失败则从全部起算
    reports.sort(key=lambda r: r["dt"])  # 按时间先后

    if not reports:
        logger.info("无可聚合的短时总结(窗口内无新增)")
        return None

    # 聚合三部分: 用正则切出每个 #…总结里 "## Part N" 到下一个标题的段落
    part_keys = {
        "1": "Part 1",
        "2": "Part 2",
        "3": "Part 3",
    }
    # blocks[part] = [(dt, segment), ...] 段含标题(如 "### 1. ...")
    blocks: dict[str, list] = {"1": [], "2": [], "3": []}

    for r in reports:
        content = r["content"]
        # 按 "## Part N:" 或 "## Part N" 切
        head = None
        for part, name in part_keys.items():
            marker = f"## {name}"
            # 找到所有该 part 标题出现位置的下一个同级标题
            for m in re.finditer(rf"^##\s+{name}[^\n]*$", content, re.M):
                # 段内容从 marker 标题行的下一行开始(不含 "## Part N" 标题本身)
                after_marker = content.find("\n", m.end())
                start = after_marker + 1 if after_marker != -1 else len(content)
                # 下一个 "## " 同级标题(注意 "### " 三个#不匹配 "^## ", 安全)
                nxt = re.search(r"^##\s+", content[start:], re.M)
                end = start + (nxt.start() if nxt else len(content[start:]))
                seg = content[start:end].strip()
                if seg:
                    blocks[part].append((r["dt"], seg))
    # 也可用更强健方式: 直接按 Part 数字序号分割全文
    # (上面已按 "## Part N" 标题精确切段)

    # 组装当天总结: 各 Part 合并, 段内按时间排序
    header = [
        f"# 天津大学每日信息汇总 · {today}",
        "",
        f"**生成时间(北京)**: {beijing_now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**覆盖**: {len(reports)} 个短时总结(自上次当天总结), 按时间排序",
        "",
        "---",
        "",
    ]
    out_parts = []
    titles = {"1": "Part 1: 关键词命中", "2": "Part 2: AI 智能推荐", "3": "Part 3: 其余信息"}
    any_content = False
    for part in ["1", "2", "3"]:
        segs = sorted(blocks[part], key=lambda x: x[0])  # 内部按时间排
        if not segs:
            continue
        any_content = True
        out_parts.append(f"## {titles[part]} ({len(segs)} 段)")
        out_parts.append("")
        for dt, seg in segs:
            out_parts.append(f"> 来源窗口: {dt}")
            out_parts.append(seg)
            out_parts.append("")

    if not any_content:
        out_parts = ["> 窗口内短时总结均无可聚合的三部分内容。"]

    summary_text = "\n".join(header + out_parts)
    summary_path = os.path.join(summary_dir, f"{today}.md")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary_text)
    return summary_path


def _parse_dt(s: str):
    """把 'YYYY-MM-DD HH:MM:SS' 字符串解析成 aware datetime(北京), 用于窗口比较."""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_BEIJING_TZ)
    except Exception:
        return None


if __name__ == "__main__":
    main()
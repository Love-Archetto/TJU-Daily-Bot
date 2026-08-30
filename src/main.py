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
import sys
from datetime import datetime, timezone, timedelta
from typing import Any

import yaml

# 北京时区 (UTC+8)；GitHub Actions 运行于 UTC，报告时间戳须用北京时间
_BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """返回当前北京时间 (带时区)."""
    return datetime.now(_BEIJING_TZ)

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


def load_state() -> dict[str, Any]:
    """加载或初始化 state.json."""
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "last_run": "",
        "processed_links": [],
        "source_last_fetch": {},
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


def main() -> None:
    """主流程."""
    logger.info("TJU Daily Bot starting...")

    # 1. 加载状态
    state = load_state()
    now = beijing_now().isoformat()
    is_ci = os.environ.get("CI", "").lower() == "true"

    # 1.5 2h 控闸(自愈调度): cron 每15min触发, 距上次真正执行 <2h 则秒退。
    #    RUN_FORCE=1(手动发现 workflow_dispatch)或 FORCE=1(本地)时绕过, 便于调试。
    force = os.environ.get("RUN_FORCE", "") == "true" or os.environ.get("FORCE", "") == "1"
    last = state.get("last_run")
    if last and not force:
        try:
            last_dt = datetime.fromisoformat(last)
            if beijing_now() - last_dt < timedelta(hours=2):
                logger.info("距上次运行 <2h, 跳过本轮(自愈调度, 等待满2h)")
                return
        except (ValueError, TypeError):
            logger.warning("state.last_run 解析失败(%r), 忽略控闸", last)

    # 2. 加载信源，抓取网站 + 公众号（公众号经 we-mp-rss 拉 RSS）
    sources = load_sources()
    all_articles = []
    fetch_summary = {}

    for source in sources:
        source_name = source.get("name", "Unknown")
        source_type = source.get("type", "")
        s = source.get("selectors", {})

        if source_type == "web":
            url = source.get("url", "")
            selectors = source.get("selectors", {})
            try:
                articles = fetch_articles_from_list_page(url, selectors)
            except Exception as e:
                logger.error("网站信源 %s 抓取失败(跳过): %s", source_name, e)
                articles = []
            for a in articles:
                a["source"] = source_name
            all_articles.extend(articles)
            fetch_summary[source_name] = len(articles)
            state["source_last_fetch"][source_name] = now

    # 3. 公众号批量抓取（搜狗微信搜索——纯云端零人工拿多篇）
    gzh_sources = [x for x in sources if x.get("type") == "wechat_rss"]
    if gzh_sources:
        # 本地/本地仿真可传 account_names；默认从 sources.yaml 读公众号名
        wechat_articles = fetch_wechat_articles()
        all_articles.extend(wechat_articles)
        fetch_summary["wechat_total"] = len(wechat_articles)
        for name in [x.get("name") for x in gzh_sources]:
            state["source_last_fetch"][name] = now

    # 3. 增量过滤
    new_articles = [a for a in all_articles if is_new_article(a, state)]
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
    report = generate_report(part1, part2, part3, profile)

    # 7. 独立检查
    try:
        checker = IndependentChecker()
        checker_result = checker.check(report)
    except Exception as e:
        logger.warning("Checker skipped: %s", e)
        checker_result = None

    # 重新生成带检查结果的报告
    final_report = generate_report(part1, part2, part3, profile, checker_result)

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
    # 限制 processed_links 大小
    if len(state["processed_links"]) > 5000:
        state["processed_links"] = state["processed_links"][-5000:]
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
    """若为北京 6:00 之后、且当天总结尚未生成, 则生成当日汇总并邮件发送.

    幂等: 当天已生成过( state.last_summary_date == today )则不重复。
    Returns: 汇总文件路径, 或 None(非6点后 / 当天已生成 / 失败)。
    """
    if beijing_now().hour < 6:
        return None
    today = beijing_now().strftime("%Y-%m-%d")
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
        # 记录本轮已生成, 防止同一天重复汇总
        state["last_summary_date"] = today
        state["last_summary_time"] = beijing_now().isoformat()
        return summary_path
    except Exception as e:
        logger.warning("每日汇总失败: %s", e)
        return None


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
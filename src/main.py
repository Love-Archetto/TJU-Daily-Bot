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

    # 12. 若当前为北京 6:00 窗口(6:00-6:59), 生成当日汇总文件并邮件发送
    bj_hour = beijing_now().hour
    if bj_hour == 6:
        summary_path = build_daily_summary()
        logger.info("每日汇总已生成: %s", summary_path)
        if summary_path and os.path.exists(summary_path):
            try:
                with open(summary_path, "r", encoding="utf-8") as f:
                    summary_text = f.read()
                from src.notifier import send_alert
                ok = send_alert(
                    f"📰 TJU Daily Bot 每日汇总 · {beijing_now().strftime('%Y-%m-%d')}",
                    summary_text,
                )
                logger.info("每日汇总邮件发送: %s", "成功" if ok else "失败(检查SMTP)")
            except Exception as e:
                logger.warning("发送每日汇总邮件失败: %s", e)

    logger.info("TJU Daily Bot finished.")


def build_daily_summary() -> str | None:
    """把 output/ 下今天生成的所有 2h 窗口报告合并成当日汇总(去重).

    Returns:
        汇总文件路径, 或 None
    """
    import re
    today = beijing_now().strftime("%Y-%m-%d")
    output_dir = os.path.join(PROJECT_ROOT, "..", "output")
    summary_dir = os.path.join(output_dir, "summary")
    os.makedirs(summary_dir, exist_ok=True)

    seen_links = set()
    seen_titles = set()
    merged: list[dict] = []
    today_reports = [f for f in os.listdir(output_dir)
                     if f.startswith(today) and f.endswith(".md")]
    today_reports.sort()

    for fname in today_reports:
        fpath = os.path.join(output_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue
        # 粗糙提取: 匹配 "### N. 标题" 或 "- **链接**: url" 行, 去重
        for line in content.splitlines():
            m = re.search(r"\- \*\*链接\*\*: \[.*\]\((https?://[^)]+)\)", line)
            if m:
                link = m.group(1)
                if link not in seen_links:
                    seen_links.add(link)
                    merged.append({"link": link})
            ti = re.search(r"^###\s+\d+\.\s+(.+)$", line)
            if ti and ti.group(1) not in seen_titles:
                seen_titles.add(ti.group(1))

    summary_lines = [
        f"# 天津大学每日信息汇总 · {today}",
        "",
        f"**生成时间(北京)**: {beijing_now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**覆盖**: 距上次 6:00 以来的 {len(today_reports)} 个窗口更新",
        "",
        "---",
    ]
    if merged:
        summary_lines += ["## 汇总链接"]
        for i, it in enumerate(merged, 1):
            summary_lines.append(f"{i}. {it['link']}")
    else:
        summary_lines.append("> 今日暂无汇总内容。")

    summary_path = os.path.join(summary_dir, f"{today}.md")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines))
    return summary_path


if __name__ == "__main__":
    main()
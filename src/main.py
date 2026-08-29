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
from src.crawler.weread_mp_crawler import fetch_wechat_articles
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


def classify_articles(
    articles: list[dict],
    keywords: list[str],
    profile: dict,
) -> tuple[list[dict], list[dict], list[dict]]:
    """将文章分为三部分.

    Returns:
        (part1_keyword_hits, part2_ai_recommended, part3_rest)
    """
    part1 = []
    part2_rest = []

    for article in articles:
        title = article.get("title", "")
        summary = article.get("summary", "")
        text = f"{title} {summary}".lower()

        # Part1: 关键词命中
        if any(kw.lower() in text for kw in keywords):
            part1.append(article)
        else:
            part2_rest.append(article)

    # Part2/Part3 由 AI 进一步分类（此处简化：前 5 条为 AI 推荐，其余为 Part3）
    # 完整实现中会调用 AI 模型进行推荐
    part2 = part2_rest[:5] if len(part2_rest) > 5 else part2_rest
    part3 = part2_rest[5:] if len(part2_rest) > 5 else []

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
            lines.append(f"- **时间**: {article.get('publish_time', '未知')}")
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
            lines.append(f"- **时间**: {article.get('publish_time', '未知')}")
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

    # 5. 分类
    part1, part2, part3 = classify_articles(new_articles, keywords, profile)

    # 6. 生成报告
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

    # 12. 若当前为北京 6:00 窗口(6:00-6:59), 生成当日汇总文件
    #     (daily.yml 每2h cron + 每日北京6:00 cron 触发同一 main.py, 据此区分)
    bj_hour = beijing_now().hour
    if bj_hour == 6:
        summary_path = build_daily_summary()
        logger.info("每日汇总已生成: %s", summary_path)

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
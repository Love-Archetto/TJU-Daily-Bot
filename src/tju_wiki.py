#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
北洋维基 (Peiyang Wiki / TJU Wiki) 查询工具
==========================================
数据来源: https://wiki.tjubot.cn/  (天津大学校园维基, Typecho 系统)

注意: 该站点会拦截 curl / 普通 wget (TLS 指纹检测), 必须用 requests 并携带
浏览器 User-Agent 访问。本脚本已封装好全部请求逻辑。

用法:
  python3 tju_wiki.py search <关键词> [页码]
  python3 tju_wiki.py cat <分类名或slug> [页码]
  python3 tju_wiki.py read <词条URL或slug> [--no-images]
  python3 tju_wiki.py latest [数量]
  python3 tju_wiki.py cats             # 列出全部分类
  python3 tju_wiki.py home             # 首页推荐词条

示例:
  python3 tju_wiki.py search 转专业
  python3 tju_wiki.py search 宿舍
  python3 tju_wiki.py cat 图书馆
  python3 tju_wiki.py read https://wiki.tjubot.cn/calendar/calender-26-27
  python3 tju_wiki.py read calendar/calender-26-27
  python3 tju_wiki.py latest 10
"""

import sys
import re
import argparse
from urllib.parse import quote, urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

BASE = "https://wiki.tjubot.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ---------------------------------------------------------------------------
# 分类表: 中文名 -> (slug, 简要说明)
# ---------------------------------------------------------------------------
CATEGORIES = [
    ("天大概况", "category/tju", "学校历史、概况"),
    ("统计数据", "category/statistics", "在校生数、师资等数据"),
    ("组织机构", "category/organization", "校内机构设置"),
    ("规章制度", "category/regulations", "校规校纪、管理办法"),
    ("通知通告", "category/notices", "校内通知"),
    ("门户网站和APP", "category/portal-websites-and-apps", "常用网站与应用"),
    ("校庆", "category/anniversary", "校庆活动"),
    ("招生", "category/admission", "招生总览"),
    ("招生简章", "category/admission-requirements", "各类型招生简章"),
    ("招生大类及专业", "category/major", "招生专业介绍"),
    ("录取分数线与招生计划", "category/scores-and-plans", "分数线、计划"),
    ("录取通知书", "category/admission-notification", "录取通知书"),
    ("报到/迎新", "category/check-in", "迎新报到总览"),
    ("党团关系及档案户口", "category/membership-shift", "党团、档案、户口"),
    ("报到流程", "category/orientation", "报到流程指引"),
    ("新生周安排", "category/arrangement", "新生周日程"),
    ("入学考试", "category/exam", "入学考试"),
    ("军训", "category/training", "军训安排"),
    ("天麟班", "category/tianlin", "天麟班选拔与安排"),
    ("辅导员", "category/instructor", "辅导员相关"),
    ("小班，室友与班级", "category/student-mentor", "小班、班级指导员"),
    ("贫困资助", "category/poverty-funding", "资助政策"),
    ("学习", "category/study", "学习总览"),
    ("校历", "category/calendar", "各学年校历"),
    ("主修专业确认", "category/majors", "主修专业确认"),
    ("未来技术学院(求是学部)", "category/qiushi", "未来技术学院"),
    ("特殊培养类型班级", "category/cultivate", "拔尖班、大师班等"),
    ("本科生培养计划", "category/training-program", "培养方案"),
    ("研究生培养方案", "category/postgraduate-training-program", "研究生培养"),
    ("办公网", "category/websites", "校内办公网站"),
    ("课程", "category/courses", "课程相关"),
    ("成绩制度", "category/score", "成绩、绩点"),
    ("考试安排", "category/exam-arrangements", "期末考试等安排"),
    ("辅修", "category/minors", "辅修专业"),
    ("微专业", "category/micro-major", "微专业"),
    ("自习室", "category/classrooms", "自习室信息"),
    ("图书馆", "category/library", "图书馆服务"),
    ("转专业", "category/major-changing", "转专业政策"),
    ("奖助励学金与贫困生制度", "category/scholarship", "奖学金、助学金"),
    ("等级考试与证书", "category/certifications", "四六级等考试"),
    ("数据库与资源网站", "category/resources", "学术资源"),
    ("科研竞赛", "category/competitions", "竞赛信息"),
    ("竞赛", "category/competition", "各类竞赛"),
    ("数学建模", "category/mathematical-contest-in-modeling", "数学建模竞赛"),
    ("科研", "category/research", "科研信息"),
    ("推免与优异生", "category/postgraduate", "保研推免"),
    ("考研", "category/graduate-exam", "考研信息"),
    ("出国与交流", "category/study-abroad", "出国交流项目"),
    ("生活", "category/life", "生活总览"),
    ("学生宿舍及设施", "category/dorm", "宿舍信息"),
    ("学生票", "category/train-ticket", "学生火车票"),
    ("饮水及水卡", "category/water", "饮水、水卡"),
    ("快递", "category/express", "快递服务"),
    ("洗澡与洗衣", "category/bathing-and-washing", "洗浴洗衣"),
    ("医疗与心理健康", "category/hospital", "校医院、心理"),
    ("打印店", "category/printing", "打印服务"),
    ("卡片证件", "category/cards", "校园卡、证件"),
    ("运动", "category/sport", "运动场馆"),
    ("食堂与餐饮店", "category/canteen", "食堂餐饮"),
    ("休闲娱乐", "category/entertainment", "娱乐休闲"),
    ("新校区周边商圈", "category/playing", "北洋园周边商圈"),
    ("公共交通", "category/public-transit", "公交地铁"),
    ("游览", "category/travelling", "游玩指南"),
    ("校园设施", "category/campus-infrastructure", "校园设施"),
    ("假期校园生活指南", "category/campus-life-guide", "寒暑假校园服务"),
    ("实践", "category/practice", "实践总览"),
    ("班级干部", "category/class-cadres", "班干部"),
    ("社团相关", "category/clubs", "社团"),
    ("课外实践", "category/extracurricular", "社会实践"),
    ("校园活动", "category/activities", "海棠季等活动"),
    ("入团和入党流程", "category/party", "入党入团"),
    ("其他", "category/others", "其他"),
    ("恋爱", "category/relationship", "校园恋爱"),
    ("电脑与上网", "category/e-life", "校园网、电脑"),
    ("挂科与试读", "category/fail-probation", "挂科、试读"),
    ("假期留校", "category/vacations", "假期留校"),
    ("体育课与体育锻炼", "category/sports", "体育课"),
    ("校园景点", "category/campus-attractions", "校园景观"),
    ("就业", "category/employment", "就业信息"),
]

CAT_BY_NAME = {name: (slug, desc) for name, slug, desc in CATEGORIES}


def session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


def fetch(url):
    r = session().get(url, timeout=25, verify=False)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
def cmd_search(keyword, page=1):
    url = f"{BASE}/search/{quote(keyword)}/{page}/" if page > 1 else f"{BASE}/search/{quote(keyword)}/"
    try:
        html = fetch(url)
    except Exception as e:
        print(f"[错误] 搜索请求失败: {e}")
        return 1
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith(BASE) or "/search/" in href or "category/" in href:
            continue
        title = a.get_text(" ", strip=True)
        if title and len(title) > 2 and title not in ("贡献者", "关于我们"):
            items.append((title, href))
    # 去重(保留首次)
    seen, results = set(), []
    for t, h in items:
        if h not in seen:
            seen.add(h)
            results.append((t, h))
    if not results:
        print(f"没有找到与「{keyword}」相关的词条。可尝试换关键词，或用 `cat` 浏览分类。")
        return 0
    print(f"🔎 搜索「{keyword}」共 {len(results)} 条结果 (第 {page} 页):\n")
    for i, (t, h) in enumerate(results, 1):
        # 摘要截断
        brief = re.sub(r"\s+", " ", t)
        if len(brief) > 90:
            brief = brief[:90] + "…"
        print(f"{i}. {brief}")
        print(f"   {h}")
    # 是否有下一页
    if any(a.get_text(strip=True) == "›" or "下一页" in a.get_text(strip=True) for a in soup.find_all("a")):
        print(f"\n提示: 还有更多结果，可加页码查看: search {keyword} {page+1}")
    return 0


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------
def cmd_cats():
    print("北洋维基分类总览 (共 %d 个):\n" % len(CATEGORIES))
    for i, (name, slug, desc) in enumerate(CATEGORIES, 1):
        print(f"{i:2d}. {name}  [{slug}]  — {desc}")
    print("\n用法: cat <分类名或slug>，例如: cat 图书馆 / cat category/library")


def cmd_cat(name_or_slug, page=1):
    if name_or_slug in CAT_BY_NAME:
        slug, desc = CAT_BY_NAME[name_or_slug]
    elif name_or_slug.startswith("category/"):
        slug = name_or_slug
        desc = ""
    else:
        # 尝试直接按 slug 访问
        slug = f"category/{name_or_slug.lstrip('/')}"
        desc = ""
    url = f"{BASE}/{slug}/" if page == 1 else f"{BASE}/{slug}/{page}/"
    try:
        html = fetch(url)
    except Exception as e:
        print(f"[错误] 分类请求失败: {e}")
        return 1
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.find("title")
    title = title_el.get_text(strip=True) if title_el else "未知分类"
    items = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith(BASE) or any(x in href for x in ("/category/", "/search/", "/page/", "/feed", "/admin", "/assets", "/usr/", "/about")):
            continue
        t = a.get_text(" ", strip=True)
        if t and len(t) > 2:
            items.append((t, href))
    seen, results = set(), []
    for t, h in items:
        if h not in seen:
            seen.add(h)
            results.append((t, h))
    print(f"📂 {title} — 共 {len(results)} 条词条 (第 {page} 页):\n")
    for i, (t, h) in enumerate(results, 1):
        brief = re.sub(r"\s+", " ", t)
        if len(brief) > 80:
            brief = brief[:80] + "…"
        print(f"{i}. {brief}")
        print(f"   {h}")
    return 0


# ---------------------------------------------------------------------------
# 阅读词条
# ---------------------------------------------------------------------------
def cmd_read(ref, no_images=False):
    if ref.startswith("http"):
        url = ref
    elif ref.startswith("/"):
        url = BASE + ref
    else:
        url = BASE + "/" + ref.lstrip("/")
    try:
        html = fetch(url)
    except Exception as e:
        print(f"[错误] 读取失败: {e}")
        return 1
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.find("title")
    print(f"# {title_el.get_text(strip=True) if title_el else '北洋维基'}\n")

    # 元信息: 作者 / 编辑时间 / 浏览量
    body = soup.find("body")
    meta_text = ""
    if body:
        m = re.search(r"作者[：:]\s*([^\s<]+).*?最后编辑于[：:]\s*([0-9\-: ]+).*?浏览量[：:]\s*([\d,]+)", body.get_text(" ", strip=True))
        if m:
            meta_text = f"作者: {m.group(1)} | 最后编辑: {m.group(2)} | 浏览量: {m.group(3)}"
    if meta_text:
        print(f"> {meta_text}\n")

    doc = soup.select_one("div.doc_content") or soup.select_one("article") or soup.select_one(".post-content")
    if not doc:
        # 回退: 正文主体
        doc = soup.find("main") or body
        print("[提示] 未识别到正文容器，以下为页面主体文本\n")

    # 图片处理
    if no_images:
        for img in doc.find_all("img"):
            img.decompose()
    else:
        for img in doc.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            alt = img.get("alt", "")
            if src:
                img.replace_with(f"[图片: {alt}]({src})" if alt else f"[图片]({src})")

    # 表格 -> 文本表格
    for tbl in doc.find_all("table"):
        rows = []
        for tr in tbl.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            rows.append(cells)
        if rows:
            width = max((len(r) for r in rows), default=1)
            text = "\n".join(" | ".join(c for c in r) for r in rows)
            tbl.replace_with(BeautifulSoup(f"\n<pre>{text}</pre>\n", "html.parser"))

    text = doc.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    print(text)
    print(f"\n--- 原文链接: {url} ---")
    return 0


# ---------------------------------------------------------------------------
# 最新词条 (RSS)
# ---------------------------------------------------------------------------
def cmd_latest(n=10):
    try:
        html = fetch(f"{BASE}/feed/")
    except Exception as e:
        print(f"[错误] 获取最新词条失败: {e}")
        return 1
    soup = BeautifulSoup(html, "xml")
    entries = soup.find_all("item")[:n]
    if not entries:
        print("未获取到词条。")
        return 0
    print(f"🆕 北洋维基最近更新 (前 {len(entries)} 条):\n")
    for it in entries:
        t = it.find("title")
        link = it.find("link")
        pub = it.find("pubDate")
        title = re.sub(r"<[^>]+>", "", t.get_text(strip=True)) if t else ""
        print(f"- {title}")
        print(f"  {link.get_text(strip=True) if link else ''}")
        if pub:
            print(f"  📅 {pub.get_text(strip=True)}")
        print()
    return 0


# ---------------------------------------------------------------------------
# 首页推荐
# ---------------------------------------------------------------------------
def cmd_home():
    try:
        html = fetch(BASE + "/")
    except Exception as e:
        print(f"[错误] 请求失败: {e}")
        return 1
    soup = BeautifulSoup(html, "html.parser")
    print("🏠 北洋维基首页推荐词条:\n")
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith(BASE) or any(x in href for x in ("/category/", "/search/", "/page/", "/feed", "/admin", "/assets", "/usr/", "/about")):
            continue
        t = a.get_text(" ", strip=True)
        if t and len(t) > 2 and href not in seen:
            seen.add(href)
            brief = re.sub(r"\s+", " ", t)
            if len(brief) > 90:
                brief = brief[:90] + "…"
            print(f"- {brief}")
            print(f"  {href}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="北洋维基查询工具")
    ap.add_argument("cmd", choices=["search", "cat", "cats", "read", "latest", "home"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--no-images", action="store_true", help="read 时不输出图片")
    args = ap.parse_args()

    if args.cmd == "search":
        if not args.args:
            print("用法: tju_wiki.py search <关键词> [页码]")
            return 1
        kw = args.args[0]
        page = int(args.args[1]) if len(args.args) > 1 else 1
        return cmd_search(kw, page)
    if args.cmd == "cats":
        return cmd_cats()
    if args.cmd == "cat":
        if not args.args:
            print("用法: tju_wiki.py cat <分类名或slug> [页码]")
            return 1
        page = int(args.args[1]) if len(args.args) > 1 else 1
        return cmd_cat(args.args[0], page)
    if args.cmd == "read":
        if not args.args:
            print("用法: tju_wiki.py read <词条URL或slug>")
            return 1
        return cmd_read(args.args[0], no_images=args.no_images)
    if args.cmd == "latest":
        n = int(args.args[0]) if args.args else 10
        return cmd_latest(n)
    if args.cmd == "home":
        return cmd_home()
    return 0


if __name__ == "__main__":
    sys.exit(main())

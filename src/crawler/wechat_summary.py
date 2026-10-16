"""公众号文章增强 — 发布时间兜底.

正文与封面由 weread_subscribe 在抓取时一并取回(/web/mp/content 的 og:url/og:image/
#js_content), 本模块不再抓正文。这里只剩一件事: 万一某篇的 publish_time 仍为空,
用轻量 requests 试着补一次(仅取 mp.weixin 原文页里的 createTime)。失败即跳过, 不阻塞。

请求 mp.weixin 时使用"微信内置浏览器"UA(见下方 WECHAT_UA 的说明与实测结论)。

策略:
  只对增量过滤后的"公众号新文章"处理(数量通常很少)。
  每篇独立 try/except, 失败跳过不阻塞。
"""

import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

# 每轮最多补时间的篇数(防某轮突然大量新文章拖垮任务; 可通过 WECHAT_SUMMARY_MAX 调)
MAX_ARTICLES = int(os.environ.get("WECHAT_SUMMARY_MAX", "10"))

# mp.weixin 请求用"微信内置浏览器"UA —— 语义上最贴近真实来源(文章本来就该在微信里打开)。
#
# 但别指望它解锁什么。2026-10-16 实测对比(同一 URL, 只换 UA):
#   /s/<短码> 文章页 : 桌面 Chrome 与 MicroMessenger 结果完全一致
#                      (均 200 / 3.48MB / 都取到 createTime, 无风控页)
#   ?__biz=..&mid=.. 长链: 两种 UA 都被跳到 wappoc_appmsgcaptcha 验证页
#                      (18213 字节, 含"环境异常")
# 即: 拦的是 URL 形式(长链), 不是 UA。保留 MicroMessenger 是因为它更贴合真实访问,
# 且是本项目一直以来的做法 —— 不是因为它能绕过风控。
WECHAT_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
             "MicroMessenger/8.0.34(0x16082222) NetType/WIFI Language/zh_CN")

# 带 Referer/Accept-Language 更像"从微信里点进来"的正常访问
WECHAT_HEADERS = {
    "User-Agent": WECHAT_UA,
    "Referer": "https://mp.weixin.qq.com/",
    "Origin": "https://mp.weixin.qq.com",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# mp.weixin 风控/验证页特征: 命中说明拿到的不是正文, 直接判失败, 不去正则里瞎匹配
ANTIBOT_MARKERS = ("环境异常", "访问过于频繁", "请完成验证", "完成验证", "请输入验证码")


def _is_mp_article(article: dict) -> bool:
    """判断是否为公众号文章(有 mp.weixin.qq.com 原文链接)."""
    link = article.get("link", "")
    return "mp.weixin.qq.com" in link


def _fetch_publish_time(link: str, timeout: int = 12) -> str:
    """用微信内置浏览器 UA 抓原文页 createTime(轻量). 失败/被风控返回空."""
    try:
        r = requests.get(link, headers=WECHAT_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return ""
        html = r.content.decode("utf-8", errors="replace")
        # 风控页也返回 200, 只是内容变成"环境异常/请完成验证"; 不当成正文去解析
        if any(mk in html for mk in ANTIBOT_MARKERS):
            logger.warning("  补时间被风控拦(验证页), 跳过: %s", link[:70])
            return ""
        m = re.search(r"createTime\s*=\s*'([^']*)'", html)
        if m:
            return m.group(1).strip()
        m2 = re.search(r'var\s+createTime\s*=\s*"([^"]*)"', html)
        if m2:
            return m2.group(1).strip()
    except Exception:
        pass
    return ""


def enhance_wechat_articles(articles: list[dict], cookie: str = "") -> list[dict]:
    """对公众号新文章补 publish_time(仅当缺失时).

    正文/封面已由 weread_subscribe 抓取时写入(content/image), 此处不动; 网页文章不处理。

    Args:
        articles: 增量过滤后的文章列表(改写原地, 返回同一列表)
        cookie: 保留兼容签名, 未使用

    Returns:
        增强后的同一列表
    """
    mp = [a for a in articles if _is_mp_article(a)]
    if not mp:
        return articles

    missing = [a for a in mp if not a.get("publish_time")][:MAX_ARTICLES]
    if missing:
        logger.info("公众号补时间: %d 篇缺失(处理上限 %d)", len(missing), MAX_ARTICLES)
    for a in missing:
        a["publish_time"] = _fetch_publish_time(a["link"])
        if a["publish_time"]:
            logger.info("  公众号补时间[%s]: %s", a.get("source", "?"), a["publish_time"])
    return articles

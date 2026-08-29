"""严格复刻 we-mp-rss web.py 的 appmsgpublish 抓取(会话/header/verify 对齐源码).

关键差异(之前没对齐导致 200013):
- 用 requests.Session(连续请求维持 cookie jar, 而非每次新 get)
- verify=False(跳过 TLS 校验, 源码如此)
- fix_header: 随机 UA + 完整 Accept/Encoding/Language + Connection: keep-alive
- get_token: 从会话取 token
- 换测试号(排除假号干扰): --accounts 指定名字
"""

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

import requests
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_PATH = "/tmp/we-mp-rss-data/session.json"
SOURCES_PATH = PROJECT_ROOT / "config" / "sources.yaml"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


def p(msg):
    print(msg, flush=True)


def load_session() -> dict:
    with open(SESSION_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_accounts(names: list[str], limit: int) -> list[tuple[str, str]]:
    """按名字从 sources.yaml 取 (name, fakeid); 没指定则前 limit 个."""
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    gzh = [s for s in data.get("sources", []) if s.get("type") == "wechat_rss" and s.get("fakeid")]
    if names:
        found = {s["name"]: s["fakeid"].strip() for s in gzh}
        out = [(n, found[n]) for n in names if n in found]
        if not out:
            p(f"[!] 指定的名字均未在 sources.yaml 找到: {names}")
            return []
        return out[:limit]
    return [(s["name"], s["fakeid"].strip()) for s in gzh[:limit]]


def fix_header(url):
    """复刻 web.py fix_header: 随机UA + 完整headers."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": url,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


def fetch_publish(session, fakeid, token, cookie, interval, pages, backoff):
    all_arts = []
    for page in range(pages):
        slp = random.randint(0, interval)
        p(f"  [page{page+1}] sleep {slp}s")
        time.sleep(slp)
        url = "https://mp.weixin.qq.com/cgi-bin/appmsgpublish"
        params = {"sub": "list", "sub_action": "list_ex", "begin": page * 5,
                  "count": 5, "fakeid": fakeid, "token": token,
                  "lang": "zh_CN", "f": "json", "ajax": 1}
        headers = fix_header(url)
        headers["Cookie"] = cookie
        retry = 0
        while True:
            try:
                r = session.get(url, params=params, headers=headers, verify=False, timeout=(10, 30))
                msg = r.json()
                p(f"  [page{page+1}] req ret={msg.get('base_resp',{}).get('ret')}")
            except Exception as e:
                p(f"  [page{page+1}] 请求异常: {type(e).__name__}: {e}")
                return all_arts, f"请求异常:{type(e).__name__}"
            ret = msg.get("base_resp", {}).get("ret", 0)
            if ret == 200013:
                retry += 1
                if retry < 3:
                    wait = backoff * retry
                    p(f"  [page{page+1}] 200013 频控, 退避 {wait}s (第{retry}/3)")
                    time.sleep(wait)
                    continue
                return all_arts, "freq control(200013) 重试仍失败"
            if ret != 0:
                err = msg.get("base_resp", {}).get("err_msg", "")
                p(f"  [page{page+1}] ret={ret} err={err}")
                return all_arts, f"ret={ret} err={err}"
            pp = msg.get("publish_page")
            if not pp:
                p(f"  [page{page+1}] 无 publish_page")
                break
            try:
                pp = pp if isinstance(pp, str) else json.dumps(pp)
                data = json.loads(pp)
                for item in data.get("publish_list", []):
                    pinfo = item.get("publish_info")
                    if isinstance(pinfo, str):
                        pinfo = json.loads(pinfo)
                    for a in (pinfo or {}).get("appmsgex", []):
                        all_arts.append({"title": a.get("title", ""),
                                         "link": a.get("link", ""),
                                         "aid": a.get("aid", "")})
                p(f"  [page{page+1}] 解析到 {len(all_arts)} 篇(累计)")
            except Exception as e:
                p(f"  [page{page+1}] 解析失败: {e}")
                return all_arts, f"解析失败:{e}"
            break
    return all_arts, ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--interval", type=int, default=3)
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--backoff", type=int, default=8)
    ap.add_argument("--accounts", default="", help="逗号分隔的公众号名, 指定测试号")
    args = ap.parse_args()

    if not os.path.exists(SESSION_PATH):
        p("[FAIL] 无会话, 先运行 scan_wechat 扫码")
        return 1
    sess = load_session()
    token = sess.get("token", "")
    cookie = sess.get("cookie", "")
    p(f"会话: token={'有' if token else '无'} cookie len={len(cookie)}")

    names = [n.strip() for n in args.accounts.split(",") if n.strip()]
    accounts = load_accounts(names, args.limit)
    if not accounts:
        p("无可用测试公众号")
        return 1

    session = requests.Session()  # 连续会话维持 cookie jar(对齐 web.py)
    any_ok = False
    for name, fakeid in accounts:
        p(f"== 抓取 [{name}] fakeid={fakeid} ==")
        arts, err = fetch_publish(session, fakeid, token, cookie, args.interval, args.pages, args.backoff)
        if err or not arts:
            p(f"[{name}] ❌ {err or '无文章'}")
        else:
            any_ok = True
            p(f"[{name}] ✅ 抓到 {len(arts)} 篇 (appmsgpublish):")
            for a in arts[:4]:
                p(f"     - {a['title'][:35]} | {a['link'][:40]}")
    p("结论: 出现'✅ 抓到' 则多篇可用")
    return 0 if any_ok else 2


if __name__ == "__main__":
    sys.exit(main())

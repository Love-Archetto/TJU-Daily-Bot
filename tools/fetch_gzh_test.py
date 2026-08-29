"""用 appmsgpublish(we-mp-rss web 模式主用接口) + 源码节奏抓公众号多篇。

带实时日志(flush)，避免长时间无输出看不到进度。
参考 core/wx/model/web.py: 每页 sleep(random(0,interval)); 200013 退避(可用 --backoff 调小)。
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


def p(msg):
    """带缓冲刷新的打印."""
    print(msg, flush=True)


def load_session() -> dict:
    with open(SESSION_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_fakeids(limit: int) -> list[tuple[str, str]]:
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    out = []
    for s in data.get("sources", []):
        if s.get("type") == "wechat_rss" and s.get("fakeid") and len(out) < limit:
            out.append((s["name"], s["fakeid"].strip()))
    return out


def fetch_publish(fakeid, token, cookie, interval, pages, backoff):
    headers = {"User-Agent": "Mozilla/5.0 Chrome/126.0", "Cookie": cookie,
               "Referer": "https://mp.weixin.qq.com/"}
    all_arts = []
    for page in range(pages):
        slp = random.randint(0, interval)
        p(f"  [page{page+1}] sleep {slp}s")
        time.sleep(slp)
        url = "https://mp.weixin.qq.com/cgi-bin/appmsgpublish"
        params = {"sub": "list", "sub_action": "list_ex", "begin": page * 5,
                  "count": 5, "fakeid": fakeid, "token": token,
                  "lang": "zh_CN", "f": "json", "ajax": 1}
        retry = 0
        while True:
            try:
                r = requests.get(url, params=params, headers=headers, timeout=20)
                msg = r.json()
                p(f"  [page{page+1}] req ok ret={msg.get('base_resp',{}).get('ret')}")
            except Exception as e:
                p(f"  [page{page+1}] 请求异常: {e}")
                return all_arts, f"请求异常:{e}"
            ret = msg.get("base_resp", {}).get("ret", 0)
            if ret == 200013:
                retry += 1
                if retry < 3:
                    wait = backoff * retry
                    p(f"  [page{page+1}] 200013 频控, 退避 {wait}s (第{retry}/3)")
                    time.sleep(wait)
                    continue
                p(f"  [page{page+1}] 200013 重试3次仍失败")
                return all_arts, "freq control(200013) 重试仍失败"
            if ret != 0:
                err = msg.get("base_resp", {}).get("err_msg", "")
                p(f"  [page{page+1}] ret={ret} err={err}")
                return all_arts, f"ret={ret} err={err}"
            pp = msg.get("publish_page")
            if not pp:
                p(f"  [page{page+1}] 无 publish_page, 结束")
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
    ap.add_argument("--backoff", type=int, default=8,
                    help="200013 退避基础秒数(源码默认60, 测试用短些)")
    args = ap.parse_args()

    if not os.path.exists(SESSION_PATH):
        p("[FAIL] 无会话文件, 先运行 scan_wechat 扫码登录")
        return 1
    sess = load_session()
    cookie = sess.get("cookie", "")
    token = sess.get("token", "")
    p(f"会话: token={'有' if token else '无'} cookie len={len(cookie)}")

    any_ok = False
    for name, fakeid in load_fakeids(args.limit):
        p(f"== 抓取 [{name}] ==")
        arts, err = fetch_publish(fakeid, token, cookie, args.interval, args.pages, args.backoff)
        if err or not arts:
            p(f"[{name}] ❌ {err or '无文章'}")
        else:
            any_ok = True
            p(f"[{name}] ✅ 抓到 {len(arts)} 篇 (appmsgpublish):")
            for a in arts[:4]:
                p(f"     - {a['title'][:35]} | {a['link'][:40]}")
    p("结论: 出现'✅ 抓到 N 篇(appmsgpublish)' 则多篇可用")
    return 0 if any_ok else 2


if __name__ == "__main__":
    sys.exit(main())

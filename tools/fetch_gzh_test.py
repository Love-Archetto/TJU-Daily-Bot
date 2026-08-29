"""用 Scan 登录捉到的新鲜会话 + appmsg 直连抓公众号(验证能否多篇).

读取 /tmp/we-mp-rss-data/session.json(由 wemp_full_login 生成), 对 sources.yaml 前 N 个
公众号的 fakeid 调 mp.weixin.qq.com/cgi-bin/appmsg 抓文章列表。控制每个号间隔降频控。

用法:
    python tools/fetch_gzh_test.py [--limit 3] [--interval 3]
"""

import argparse
import json
import os
import time
from pathlib import Path

import requests
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_PATH = "/tmp/we-mp-rss-data/session.json"
SOURCES_PATH = PROJECT_ROOT / "config" / "sources.yaml"


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--interval", type=float, default=3.0)
    args = ap.parse_args()

    if not os.path.exists(SESSION_PATH):
        print("[FAIL] 无会话文件, 先运行 scan_wechat 扫码登录")
        return 1
    sess = load_session()
    cookie = sess.get("cookie", "")
    token = sess.get("token", "")
    print(f"会话: token={'有' if token else '无'} cookie len={len(cookie)}")

    for name, fakeid in load_fakeids(args.limit):
        url = "https://mp.weixin.qq.com/cgi-bin/appmsg"
        params = {"action": "list_ex", "begin": "0", "count": "5",
                  "fakeid": fakeid, "type": "9", "query": "", "token": token,
                  "lang": "zh_CN", "f": "json"}
        headers = {"User-Agent": "Mozilla/5.0 Chrome/126.0", "Cookie": cookie,
                   "Referer": "https://mp.weixin.qq.com/"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            j = r.json()
            ret = j.get("ret", j.get("base_resp", {}).get("ret", 0))
            lst = j.get("app_msg_list", [])
            if lst:
                print(f"[{name}] ✅ 抓到 {len(lst)} 篇: {lst[0].get('title','')[:30]}")
            else:
                err = j.get("base_resp", {}).get("err_msg", "")
                print(f"[{name}] ret={ret} err={err} 文章=0")
        except requests.RequestException as e:
            print(f"[{name}] 请求异常: {e}")
        time.sleep(args.interval)

    print("结论: 出现'✅ 抓到 N 篇' 则新鲜会话+appmsg 可抓多篇")


if __name__ == "__main__":
    raise SystemExit(main())

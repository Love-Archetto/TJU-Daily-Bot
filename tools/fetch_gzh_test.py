"""用 appmsgpublish(we-mp-rss web 模式主用接口) + 源码节奏抓公众号多篇.

背景: appmsg(action=list_ex) 是 we-mp-rss 的兜底接口, 有 freq control(200013)。
主接口是 appmsgpublish(sub=list / sub_action=list_ex), 避开 appmsg 的频控。
参考 core/wx/model/web.py:
  - 每页前 sleep(random.randint(0, interval))  (interval 默认10)
  - 遇 200013 退避 60*retry_count 重试最多3次
  - 响应 publish_page(publish_list[]->publish_info->appmsgex[])

用法:
    python tools/fetch_gzh_test.py --limit 3 --interval 10 [--pages 2]
"""

import argparse
import json
import os
import random
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


def fetch_publish(fakeid, token, cookie, interval, pages):
    """按源码节奏抓 appmsgpublish 多篇."""
    headers = {"User-Agent": "Mozilla/5.0 Chrome/126.0", "Cookie": cookie,
               "Referer": "https://mp.weixin.qq.com/"}
    all_arts = []
    for page in range(pages):
        time.sleep(random.randint(0, interval))
        url = "https://mp.weixin.qq.com/cgi-bin/appmsgpublish"
        params = {"sub": "list", "sub_action": "list_ex", "begin": page * 5,
                  "count": 5, "fakeid": fakeid, "token": token,
                  "lang": "zh_CN", "f": "json", "ajax": 1}
        retry = 0
        while True:
            try:
                r = requests.get(url, params=params, headers=headers, timeout=20)
                msg = r.json()
            except Exception as e:
                return all_arts, f"请求异常:{e}"
            ret = msg.get("base_resp", {}).get("ret", 0)
            if ret == 200013:
                retry += 1
                if retry < 3:
                    time.sleep(60 * retry)
                    continue
                return all_arts, "freq control(200013) 重试仍失败"
            if ret != 0:
                err = msg.get("base_resp", {}).get("err_msg", "")
                return all_arts, f"ret={ret} err={err}"
            # 解析 publish_page
            pp = msg.get("publish_page")
            if not pp:
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
            except Exception as e:
                return all_arts, f"解析失败:{e}"
            break  # 单页
    return all_arts, ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--pages", type=int, default=1)
    args = ap.parse_args()

    if not os.path.exists(SESSION_PATH):
        print("[FAIL] 无会话文件, 先运行 scan_wechat 扫码登录")
        return 1
    sess = load_session()
    cookie = sess.get("cookie", "")
    token = sess.get("token", "")
    print(f"会话: token={'有' if token else '无'} cookie len={len(cookie)}")

    any_ok = False
    for name, fakeid in load_fakeids(args.limit):
        arts, err = fetch_publish(fakeid, token, cookie, args.interval, args.pages)
        if err or not arts:
            print(f"[{name}] ❌ {err or '无文章'}")
        else:
            any_ok = True
            print(f"[{name}] ✅ 抓到 {len(arts)} 篇 (appmsgpublish):")
            for a in arts[:3]:
                print(f"     - {a['title'][:35]}")
    print("结论: 出现'✅ 抓到 N 篇(appmsgpublish)' 则多篇可用")
    return 0 if any_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

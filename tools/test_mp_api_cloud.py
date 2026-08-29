"""云端测试 MP API 直连抓公众号(wechat-grab 思路).

验证在 GitHub Actions 云端 IP 上, 能否直接调 mp.weixin.qq.com/cgi-bin/appmsg
拉取公众号文章列表(而非搜狗搜索)。

前提:
    - GitHub Secrets 配 MP_COOKIE(mp.weixin.qq.com Cookie) + MP_TOKEN(url 里的 token)
    - config/sources.yaml 里公众号有 fakeid(base64)

用法:
    python tools/test_mp_api_cloud.py [--limit 3]
"""

import argparse
import os
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load_first_fakeids(limit: int) -> list[tuple[str, str]]:
    """从 sources.yaml 读前 N 个公众号的 (name, fakeid)."""
    import yaml
    sp = PROJECT_ROOT / "config" / "sources.yaml"
    with open(sp, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    out = []
    for s in data.get("sources", []):
        if s.get("type") == "wechat_rss" and s.get("fakeid") and len(out) < limit:
            fakeid = s["fakeid"]
            if fakeid.startswith("biz="):
                fakeid = fakeid[len("biz="):]
            out.append((s["name"], fakeid.strip()))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()

    cookie = os.environ.get("MP_COOKIE", "").strip()
    token = os.environ.get("MP_TOKEN", "").strip()

    print("=" * 50)
    print("云端 MP API 直连测试")
    print(f"  有 cookie: {'是' if cookie else '否'}, 有 token: {'是' if token else '否'}")
    print("=" * 50)

    if not cookie or not token:
        print("[FAIL] 未配置 MP_COOKIE / MP_TOKEN Secret")
        print("       在 Settings→Secrets 加这两个(值=mp.weixin.qq.com 的 Cookie/token)")
        return 1

    for name, fakeid in _load_first_fakeids(args.limit):
        url = "https://mp.weixin.qq.com/cgi-bin/appmsg"
        # appmsg 的 fakeid 参数用 base64 原值(如 MjM5...) 而非解码数字
        params = {
            "action": "list_ex", "begin": "0", "count": "3",
            "fakeid": fakeid, "type": "9", "query": "", "token": token,
            "lang": "zh_CN", "f": "json",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0",
            "Cookie": cookie,
            "Referer": "https://mp.weixin.qq.com/",
        }
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            try:
                j = r.json()
            except Exception:
                print(f"[{name}] HTTP {r.status_code} 非JSON: {r.text[:120]}")
                continue
            ret = j.get("ret", j.get("base_resp", {}).get("ret", 0))
            lst = j.get("app_msg_list", [])
            if lst:
                print(f"[{name}] ✅ 抓到 {len(lst)} 篇:")
                for a in lst[:2]:
                    print(f"     - {a.get('title','')[:40]}")
            else:
                err = j.get("base_resp", {}).get("err_msg", "")
                print(f"[{name}] ret={ret} err={err} 文章=0")
        except requests.RequestException as e:
            print(f"[{name}] 请求异常: {e}")

    print("=" * 50)
    print("结论: 上方若出现 '✅ 抓到 N 篇' 则云端 MP API 直连可行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

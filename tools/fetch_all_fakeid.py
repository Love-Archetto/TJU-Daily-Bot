#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量获取公众号 fakeid, 处理 searchbiz 限流(200013)重试.

读 config/sources.yaml 里无 fakeid 的 wechat_rss 条目逐个查询,
成功后回写 fakeid 到 sources.yaml。遇限流递增等待重试。

凭据一律从环境变量读取(自动加载 .env), 禁止硬编码入库——
仓库是公开的, 后台 Cookie/token 一旦提交即视为泄露:
    MP_COOKIE=<mp.weixin.qq.com 后台 Cookie>   (兼容旧变量 WEREAD_COOKIE)
    MP_QUERY_TOKEN=<mp.weixin.qq.com token>
"""
import os, sys, time, re, json
import requests
import yaml

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)
SOURCES_PATH = os.path.join(PROJECT_ROOT, "config", "sources.yaml")

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except Exception:
    pass

# 凭据只经环境变量注入, 不入库
COOKIE = (os.environ.get("MP_COOKIE") or os.environ.get("WEREAD_COOKIE") or "").strip()
TOKEN = os.environ.get("MP_QUERY_TOKEN", "").strip()
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
     "Cookie": COOKIE, "Referer": "https://mp.weixin.qq.com/"}


def norm(s):
    return re.sub(r"\s+", "", s or "")


def search_biz(name, tries=6):
    """查询, 遇限流200013递增等待重试. 返回 ("ok", list) / ("rlimit", None) 一直限流 / ("fail", None)"""
    url = "https://mp.weixin.qq.com/cgi-bin/searchbiz"
    base_wait = 10
    for i in range(tries):
        try:
            r = requests.get(url, headers=H, params={
                "action": "search_biz", "begin": "0", "count": "10",
                "query": name, "token": TOKEN, "lang": "zh_CN", "f": "json",
            }, timeout=15)
            d = r.json()
            ret = d.get("base_resp", {}).get("ret")
            if ret == 0:
                return ("ok", d.get("list", []))
            if ret in (200013, 200002, 200003):  # 频控/需要验证
                wait = base_wait * (2 ** i)
                print(f"    [限流] ret={ret} 等待 {wait}s", flush=True)
                time.sleep(wait)
                continue
            # 其他: cookie 失效(ret=-3等)
            print(f"    [异常] base_resp={d.get('base_resp')}", flush=True)
            return ("fail", None)
        except Exception as e:
            print(f"    [异常请求] {e}", flush=True)
            time.sleep(2)
    return ("rlimit", None)


def pick_best(results, target):
    t = norm(target)
    for r_ in results:
        if norm(r_.get("nickname", "")) == t:
            return r_.get("fakeid", "")
    for r_ in results:
        n = norm(r_.get("nickname", ""))
        if r_.get("fakeid") and (t in n or n in t):
            return r_.get("fakeid", "")
    return results[0].get("fakeid", "") if results else ""


if __name__ == "__main__":
    if not COOKIE or not TOKEN:
        print("缺少公众号后台凭据，请先配置环境变量（或写入 .env）后运行：")
        print("  MP_COOKIE=<mp.weixin.qq.com 后台 Cookie>")
        print("  MP_QUERY_TOKEN=<mp.weixin.qq.com token>")
        sys.exit(1)
    data = yaml.safe_load(open(SOURCES_PATH, encoding="utf-8"))
    sources = data["sources"]
    wechat = [s for s in sources if s.get("type") == "wechat_rss" and not s.get("fakeid")]
    already = [s for s in sources if s.get("type") == "wechat_rss" and s.get("fakeid")]
    print(f"待查 {len(wechat)} 个, 已有 {len(already)} 个", flush=True)

    failed = []
    ok = []
    cookies_failed = 0
    for i, s in enumerate(wechat, 1):
        name = s["name"]
        stat, results = search_biz(name)
        if stat == "ok" and results:
            fid = pick_best(results, name)
            if fid:
                s["fakeid"] = fid
                ok.append(name)
                print(f"[{i}/{len(wechat)}] OK {name} -> {fid}", flush=True)
            else:
                failed.append(name); print(f"[{i}/{len(wechat)}] EMPTY {name}", flush=True)
        elif stat == "rlimit":
            failed.append(name); print(f"[{i}/{len(wechat)}] RLIMIT(放弃) {name}", flush=True)
        else:  # fail = cookie 问题
            cookies_failed += 1
            failed.append(name); print(f"[{i}/{len(wechat)}] FAIL(疑cookie) {name}", flush=True)
            if cookies_failed >= 3:
                print("连续多次失败, 疑似cookie失效, 中止", flush=True)
                break
        # 控制请求频率: 每1个等待0.8s, 每20个额外等3s(缓解频控)
        time.sleep(0.8)
        if i % 20 == 0:
            print(f"  --- 已处理 {i} 个, 休息3s ---", flush=True)
            time.sleep(3)

    # 回写
    with open(SOURCES_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump({"sources": sources}, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print(f"\n完成: 成功 {len(ok)}, 失败 {len(failed)}", flush=True)
    print(f"失败名单: {failed}", flush=True)

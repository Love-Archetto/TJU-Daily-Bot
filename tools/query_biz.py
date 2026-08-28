"""查询微信公众号 fakeid 并批量回写 config/sources.yaml。

背景：
    为支持纯 GitHub Actions 远程抓取公众号，需要每个公众号的 fakeid（biz）。
    本工具读入 config/sources.yaml 中 type=wechat_rss 的名称，用 MP 后台 Cookie+Token
    searchbiz 接口查询 fakeid，并把结果回写到对应条目（新增 fakeid 字段）。

用法：
    1. 登录 https://mp.weixin.qq.com 后台,按 F12 获取 Cookie 和 token
    2. 设置环境变量（本项目配置，见 .env.example）：
       export WEREAD_COOKIE=<你的mp.weixin.qq.com Cookie>   # 沿用现有变量避免新增
       或用工具内部指定的 MP_QUERY_TOKEN
    3. 运行：
       python tools/query_biz.py

    - 默认只查询缺失 fakeid 的公众号
    - 已含 fakeid 的条目自动跳过
    - 查询失败的公众号写入 tools/query_biz_failed.txt，便于复查
    - Cookie 过期时会在文件头标注，提示用户及时更新 Secret
"""

import os
import re
import sys
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = PROJECT_ROOT / "config" / "sources.yaml"
FAILED_LOG_PATH = PROJECT_ROOT / "tools" / "query_biz_failed.txt"

# 公众号名->搜索参数容错：有些名称需加关键字才能精确查中
SEARCH_QUERY_OVERRIDES = {
    # 示例：若某名称搜不准，在此指定更精确的查询词
}


def load_sources() -> list[dict]:
    """加载 config/sources.yaml."""
    try:
        import yaml
        with open(SOURCES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("sources", [])
    except Exception as e:
        print(f"加载 sources.yaml 失败: {e}")
        sys.exit(1)


def save_sources(sources: list[dict]) -> None:
    """写回 config/sources.yaml（保留原有字段和顺序，附加 fakeid 字段）."""
    import yaml
    payload = {"sources": sources}
    with open(SOURCES_PATH, "w", encoding="utf-8") as f:
        # 允许 unicode，避免 \uXXXX 转义；保留可读块样式
        yaml.dump(payload, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def query_biz(name: str, cookie: str, token: str, timeout: int = 15) -> list[dict]:
    """用 searchbiz 接口搜索公众号，返回匹配列表."""
    query = SEARCH_QUERY_OVERRIDES.get(name, name)
    url = "https://mp.weixin.qq.com/cgi-bin/searchbiz"
    params = {
        "action": "search_biz",
        "begin": "0",
        "count": "5",
        "query": query,
        "token": token,
        "lang": "zh_CN",
        "f": "json",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": cookie,
        "Referer": "https://mp.weixin.qq.com/",
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        data = resp.json()
        if "list" not in data:
            return []
        return [
            {"name": it.get("nickname", ""), "biz": it.get("fakeid", "")}
            for it in data["list"]
        ]
    except Exception:
        return []


def pick_best(results: list[dict], target: str) -> str:
    """在多个匹配结果中挑选最佳 fakeid：精确名优先，否则取第一个."""
    # 精确名匹配
    for r in results:
        if re.sub(r"\s+", "", r["name"]) == re.sub(r"\s+", "", target):
            return r["biz"]
    # 名称包含匹配
    for r in results:
        if target in r["name"] or r["name"] in target:
            return r["biz"]
    # 兜底取第一个
    return results[0]["biz"] if results else ""


def main() -> None:
    cookie = os.environ.get("WEREAD_COOKIE", "").strip()
    token = os.environ.get("MP_QUERY_TOKEN", "").strip()
    if not cookie or not token:
        print("请设置环境变量后运行：")
        print("  WEREAD_COOKIE=<mp.weixin.qq.com Cookie>")
        print("  MP_QUERY_TOKEN=<mp.weixin.qq.com token>")
        sys.exit(1)

    sources = load_sources()
    wechat = [s for s in sources if s.get("type") == "wechat_rss" and not s.get("fakeid")]
    done = [s for s in sources if s.get("type") == "wechat_rss" and s.get("fakeid")]
    print(f"共 {len(wechat)} 个公众号需查询 fakeid（{len(done)} 个已有，跳过）")

    failed: list[str] = []
    for s in wechat:
        name = s["name"]
        sys.stdout.write(f'  [{ "?" }] {name} ... ')
        sys.stdout.flush()
        results = query_biz(name, cookie, token)
        if not results:
            print("[未找到]")
            failed.append(name)
            continue
        biz = pick_best(results, name)
        if biz:
            s["fakeid"] = biz
            print(f"[OK] fakeid={biz}")
        else:
            print("[空失败]")
            failed.append(name)
        time.sleep(1)  # 避免请求过快触发风控

    save_sources(sources)
    print(f"\n已回写 sources.yaml，新增 {len(wechat) - len(failed)} 个 fakeid")

    if failed:
        with open(FAILED_LOG_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(failed))
        print(f"\n⚠️ 以下 {len(failed)} 个公众号未查得 fakeid，已记录到 tools/query_biz_failed.txt：")
        for n in failed:
            print(f"   - {n}")

    # 提示：检查 Cookie 是否仍有效
    if len(failed) == len(wechat) and wechat:
        print("\n⚠️ 全部查询失败，疑似 Cookie 已过期。请更新后再试。")


if __name__ == "__main__":
    main()

"""向 we-mp-rss 初始化公众号订阅。

用法（本地或 GitHub Actions）：
    python tools/init_wechat_subscriptions.py --base http://localhost:8001 --limit 5

作用：
    1. 用 admin/admin@123 登录 we-mp-rss 拿 token
    2. 从 config/sources.yaml 读取 type=wechat_rss 的公众号（含 fakeid）
    3. 逐个 POST /mps 添加订阅（mp_id 用 base64 fakeid）—— we-mp-rss 幂等，重复添加会更新而非新增
    4. --limit 控制只加前 N 个（少量测试用），默认 None 表示全部

依赖：
    - 环境变量 WE_MP_RSS_BASE 或 --base 指定 we-mp-rss 地址
    - config/sources.yaml 里公众号条目需有 fakeid 字段
"""

import argparse
import base64
import json
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = PROJECT_ROOT / "config" / "sources.yaml"

DEFAULT_USER = "admin"
DEFAULT_PASS = "admin@123"


def login(base: str, username: str, password: str) -> str:
    """登录 we-mp-rss, 返回 access token."""
    url = f"{base}/auth/login"
    # OAuth2 密码表单
    r = requests.post(url, data={"username": username, "password": password}, timeout=30)
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token") or (data.get("data") or {}).get("access_token")
    if not token:
        raise RuntimeError(f"we-mp-rss 登录失败: {data}")
    return token


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def add_subscription(base: str, token: str, name: str, fakeid_b64: str) -> None:
    """添加一个公众号订阅. mp_id = base64 fakeid."""
    url = f"{base}/mps"
    payload = {
        "mp_name": name,
        "mp_id": fakeid_b64,   # we-mp-rss 内部会 base64 解码
    }
    try:
        r = requests.post(url, json=payload, headers=_auth_headers(token), timeout=30)
        if r.status_code in (200, 201):
            print(f"  [OK] 添加订阅: {name}")
        else:
            body = r.text[:200]
            print(f"  [WARN] 添加订阅 {name} 失败 status={r.status_code}: {body}")
    except requests.RequestException as e:
        print(f"  [WARN] 添加订阅 {name} 请求异常: {e}")


def load_subscriptions() -> list[dict]:
    """从 sources.yaml 读公众号订阅（name + fakeid）."""
    import yaml
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    wechat = [s for s in data.get("sources", []) if s.get("type") == "wechat_rss" and s.get("fakeid")]
    return [{"name": s["name"], "fakeid": s["fakeid"]} for s in wechat]


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 we-mp-rss 公众号订阅")
    parser.add_argument("--base", default=None, help="we-mp-rss 地址, 默认取环境变量 WE_MP_RSS_BASE 或 http://localhost:8001")
    parser.add_argument("--limit", type=int, default=None, help="只添加前 N 个公众号（少量测试），默认全部")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASS)
    args = parser.parse_args()

    import os
    base = (args.base
            or os.environ.get("WE_MP_RSS_BASE")
            or "http://localhost:8001").rstrip("/")

    subs = load_subscriptions()
    if args.limit:
        subs = subs[: args.limit]
    print(f"共 {len(subs)} 个公众号待添加，目标 we-mp-rss: {base}")

    token = login(base, args.user, args.password)
    print("登录成功")
    for s in subs:
        add_subscription(base, token, s["name"], s["fakeid"])

    print("订阅初始化完成")


if __name__ == "__main__":
    main()

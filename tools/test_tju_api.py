"""诊断 TJU API 在云端的连通性。

用于确认 TJU 开源大模型平台在 GitHub Actions 环境里是否可访问、响应慢不慢。
（本地曾经测通过，但云端 Actions 跨网络可能延迟高或不可达。）

用法：
    TJU_API_KEY=tk-xxx python -c "import sys; sys.path.insert(0,'tools'); import test_tju_api; test_tju_api.main()"
    或直接：TJU_API_KEY=tk-xxx python tools/test_tju_api.py

输出：
    - /models 端点是否可达
    - 用 tju-llm 发一个请求，测响应时间与结果
"""

import os
import time

import requests


def main() -> int:
    api_key = os.environ.get("TJU_API_KEY", "")
    base = os.environ.get("TJU_API_BASE", "https://ai.tju.edu.cn/api/v3")

    print("=" * 50)
    print("TJU API 诊断")
    print(f"  base   : {base}")
    print(f"  has_key: {'是' if api_key else '否'}")
    print("=" * 50)

    if not api_key:
        print("[FAIL] 未设置 TJU_API_KEY 环境变量")
        return 1

    # 1) 端点可达性
    try:
        r = requests.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
        print(f"[models] HTTP {r.status_code} 耗时~")
        if r.status_code != 200:
            print(f"  响应: {r.text[:300]}")
        else:
            print(f"  模型: {r.json().get('data', [])[:5]}")
    except requests.RequestException as e:
        print(f"[FAIL] /models 请求异常: {e}")

    # 2) 实际对话
    t0 = time.time()
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "tju-llm",
                "messages": [{"role": "user", "content": "回复 OK 即可"}],
                "max_tokens": 20,
            },
            timeout=30,
        )
        elapsed = time.time() - t0
        print(f"[chat] HTTP {resp.status_code} 耗时 {elapsed:.1f}s")
        if resp.status_code != 200:
            print(f"  响应: {resp.text[:300]}")
        else:
            content = resp.json()["choices"][0]["message"]["content"]
            print(f"  回复: {content.strip()[:80]}")
            print(f"  -> 用时 {elapsed:.1f}s（>15s 则 checker 的 15s 超时会失败）")
            return 0 if elapsed < 15 else 2
    except requests.RequestException as e:
        print(f"[FAIL] /chat 请求异常: {e}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

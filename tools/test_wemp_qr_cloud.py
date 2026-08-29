"""按 we-mp-rss 方法在云端测试获取微信公众平台登录二维码.

复刻 driver/wx_api.py:
  - GET mp.weixin.qq.com/ 首页 → 提取 loginqrcode 的 qr_url
  - 用 qr_url 下载二维码图片

目的: 验证云端 GitHub Actions 能否真正拿到可用的登录二维码(之前 is_exists=False, len=43)。
若能, 则 we-mp-rss web 多篇模式在云端可复活(需人工扫码+72h过期维护)。
"""

import re
import os
import sys
from pathlib import Path

import requests

BASE = "https://mp.weixin.qq.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def main() -> None:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/html,*/*",
                      "Accept-Language": "zh-CN,zh;q=0.9"})
    out = []

    out.append(f"请求微信公众平台首页: {BASE}/")
    try:
        r = s.get(BASE + "/", timeout=20)
        out.append(f"首页 HTTP {r.status_code} len={len(r.text)}")
    except requests.RequestException as e:
        out.append(f"首页请求失败: {e}")
        _dump(out)
        return 1

    # 1) 提取 loginqrcode URL(同 we-mp-rss 正则)
    qr_url = None
    m = re.search(r"(https?://mp\.weixin\.qq\.com/cgi-bin/loginqrcode\?action=getqrcode&param=\d+)", r.text)
    if m:
        qr_url = m.group(1)
        out.append(f"提取到 loginqrcode URL: {qr_url[:80]}")
    else:
        out.append("首页未提取到 loginqrcode(可能需先登录/或结构变化)")
        # 尝试其它特征
        alt = re.findall(r'loginqrcode[^"\'\s]*', r.text)[:3]
        out.append(f"  其它 loginqrcode 片段: {alt}")
    if not qr_url:
        _dump(out)
        return 1

    # 2) 下载二维码图片
    try:
        img = s.get(qr_url, timeout=20)
        out.append(f"loginqrcode 请求 HTTP {img.status_code} len={len(img.content)}")
        dest = Path(os.path.join(str(Path(__file__).resolve().parent.parent), "tools", "_mp_qr.png"))
        if img.status_code == 200 and len(img.content) > 500:
            dest.write_bytes(img.content)
            out.append(f"✅ 二维码图片已保存: {dest} ({len(img.content)} 字节) - 云端可生成, we-mp-rss 思路可行")
        else:
            out.append(f"⚠️ 二维码非有效图片(HTTP {img.status_code} len={len(img.content)}), 前40字节: {img.content[:40]}")
    except requests.RequestException as e:
        out.append(f"loginqrcode 请求异常: {e}")

    _dump(out)
    return 0


def _dump(out):
    text = "\n".join(out)
    print(text)
    p = Path(__file__).resolve().parent.parent / "tools" / "_mp_qr_diag.txt"
    p.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

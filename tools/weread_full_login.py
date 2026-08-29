"""云端微信读书扫码登录 — 捉 wr_* cookie.

复用公众平台 Playwright 扫码登录闭环, 换域名 weread.qq.com。
扫码后在浏览器 context 捉 wr_vid/wr_skey/wr_gid/wr_fp 等 cookie, 存 /tmp/we-mp-rss-data/weread.json。
"""

import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.notifier import send_alert  # noqa: E402

WEREAD_URL = "https://weread.qq.com/"
SAVE = "/tmp/we-mp-rss-data/weread.json"
DATA_DIR = "/tmp/we-mp-rss-data"


async def main():
    from playwright.async_api import async_playwright
    out = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True,
                                          args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            locale="zh-CN",
        )
        page = await ctx.new_page()
        try:
            await page.goto(WEREAD_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)
            out.append(f"页面URL: {page.url[:80]}")

            # 尝试等二维码(微信读书网页版登录)或检测是否已登录(直接进首页)
            # 策略: 若 URL 仍是登录/含 login, 尝试找二维码; 若已进首页说明已有会话
            qr = await page.query_selector("img[src*='qrcode'], img[src*='wx_qr'], .qr-code img, canvas")
            if qr:
                img_path = PROJECT_ROOT / "tools" / "_wr_qr.png"
                try:
                    await qr.screenshot(path=str(img_path))
                except Exception:
                    await page.screenshot(path=str(img_path))
                size = img_path.stat().st_size if img_path.exists() else 0
                out.append(f"✅ 微信读书二维码已截图: {size} 字节")
                send_alert("📱 TJU Daily Bot: 请扫码登录微信读书(获取公众号抓取凭据)",
                           "请用手机微信扫码登录微信读书。扫码后本轮自动同步 wr_* cookie。",
                           image_path=str(img_path))
                out.append("已发码到邮箱")
                # 等扫码(轮询 page 内容/URL 变化或 120s)
                for _ in range(120):
                    await page.wait_for_timeout(2000)
                    # 扫码后 wr_* cookie 应出现
                    cookies = await ctx.cookies()
                    if any(c["name"].startswith("wr_") for c in cookies):
                        out.append("✅ 检测到 wr_* cookie, 登录成功")
                        break
                else:
                    out.append("⚠️ 120s 未检测到 wr_* cookie(可能未扫码/登录方式不同)")
            else:
                out.append("未找到二维码元素, 直接检查是否已有会话")
                cookies = await ctx.cookies()
                wr = [c for c in cookies if c["name"].startswith("wr_")]
                out.append(f"已有 wr_* cookie: {len(wr)}")

            # 取最终 wr_* cookie
            cookies = await ctx.cookies()
            wr = {c["name"]: c["value"] for c in cookies if c["name"].startswith("wr_")}
            if wr:
                cookie_str = "; ".join(f"{k}={v}" for k, v in wr.items())
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(SAVE, "w", encoding="utf-8") as f:
                    json.dump({"cookie": cookie_str, "cookies": wr}, f, ensure_ascii=False, indent=2)
                out.append(f"✅ wr_* cookie 已保存: {SAVE} ({len(wr)} 个: {list(wr.keys())})")
            else:
                out.append("❌ 未获得 wr_* cookie(扫码未完成或微信读书网页版无此 cookie)")
        except Exception as e:
            out.append(f"异常: {type(e).__name__}: {str(e)[:200]}")
        finally:
            await browser.close()
    text = "\n".join(out)
    print(text, flush=True)
    (PROJECT_ROOT / "tools" / "_weread_login.txt").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    import asyncio
    raise SystemExit(asyncio.run(main()))

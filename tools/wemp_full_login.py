"""云端完整微信公众平台扫码登录(独立 Playwright, 绕开 we-mp-rss 的坏扫码接口).

流程:
  1. chromium headless 打开 mp.weixin.qq.com 登录页(不加异常http头)
  2. 等二维码可见 → 截图 → SMTP 发用户邮箱
  3. 轮询等用户扫码(页面跳转到 cgi-bin/home)
  4. 捕捉浏览器 cookies + token
  5. 验证会话有效(请求 home), 存到 /tmp/we-mp-rss-data/session.json 供主流程用
"""

import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.notifier import send_alert  # noqa: E402

LOGIN_URL = "https://mp.weixin.qq.com/"
SESSION_SAVE = "/tmp/we-mp-rss-data/session.json"


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
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # 1. 等二维码可见
            qr_ready = False
            for _ in range(60):
                qr = await page.query_selector(".login__type__container__scan__qrcode")
                if qr:
                    try:
                        ok = await qr.evaluate(
                            "el => { const i=el.tagName==='IMG'?el:el.querySelector('img');"
                            " return i? i.naturalWidth>50 : el.getBoundingClientRect().width>50; }")
                        if ok:
                            qr_ready = True
                            break
                    except Exception:
                        qr_ready = True
                        break
                await page.wait_for_timeout(1000)
            if not qr_ready:
                out.append("❌ 二维码未生成")
                _finish(out)
                await browser.close()
                return 2

            # 2. 截图 + 发邮箱
            qr = await page.query_selector(".login__type__container__scan__qrcode")
            img_path = PROJECT_ROOT / "tools" / "_qr_current.png"
            await qr.screenshot(path=str(img_path))
            out.append(f"✅ 二维码已生成: {img_path.stat().st_size} 字节")
            sent = send_alert(
                "📱 TJU Daily Bot: 请扫码登录微信公众平台(有效时间有限)",
                "请用手机微信尽快扫码登录(约2-5分钟内有效)。扫码后本轮会自动同步会话。",
                image_path=str(img_path),
            )
            out.append(f"发码到邮箱: {'成功' if sent else '失败(检查SMTP)'}")

            # 3. 等扫码(跳转 cgi-bin/home)
            logged_in = False
            for _ in range(120):  # ~120s
                try:
                    await page.wait_for_url(lambda u: "cgi-bin/home" in u, timeout=2000)
                    logged_in = True
                    break
                except Exception:
                    pass
                if await page.query_selector(".login__type__container__scan__qrcode") is None:
                    pass
                await page.wait_for_timeout(2000)
            if not logged_in:
                out.append("⚠️ 等待扫码超时(用户未在规定时间扫?)")
                _finish(out)
                await browser.close()
                return 3

            # 4. 捕捉会话
            await page.wait_for_timeout(3000)
            cookies = await ctx.cookies()
            url = page.url
            token = ""
            import re
            m = re.search(r"[?&]token=(\d+)", url)
            if m:
                token = m.group(1)
            out.append(f"✅ 登录成功, 捕获 {len(cookies)} cookies, token={token[:50]}")
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            os.makedirs("/tmp/we-mp-rss-data", exist_ok=True)
            with open(SESSION_SAVE, "w", encoding="utf-8") as f:
                json.dump({"token": token, "cookie": cookie_str, "cookies": cookies}, f,
                          ensure_ascii=False, indent=2)
            out.append(f"会话已保存: {SESSION_SAVE}")

        except Exception as e:
            out.append(f"异常: {type(e).__name__}: {str(e)[:200]}")
        finally:
            await browser.close()

    _finish(out)
    return 0


def _finish(out):
    text = "\n".join(out)
    print(text)
    (PROJECT_ROOT / "tools" / "_full_login.txt").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    import asyncio
    raise SystemExit(asyncio.run(main()))

"""云端复刻 we-mp-rss wxLogin 生成登录二维码(Playwright 真实浏览器).

按 #428 修复 + driver/wx.py:
  - 用 chromium headless
  - 不注入异常 http 头(#428: Cache-Control/Upgrade-Insecure 致登录页白屏)
  - 打开 mp.weixin.qq.com 登录页, 等二维码可见(css .login__type__container__scan__qrcode)
  - 若默认"快捷登录", 点"扫码登录"切回
  - 校验二维码图片可加载(naturalWidth>50)后截图

目的: 判断云端能否生成有效登录二维码(若能, we-mp-rss web 多篇模式云端可复活)。
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

LOGIN_URL = "https://mp.weixin.qq.com/"
QR_TAG = ".login__type__container__scan__qrcode"


async def _wait_qrcode_ready(page, timeout=30) -> bool:
    """等待二维码出现且真正可加载(#428: naturalWidth>50)."""
    import asyncio
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        qr = await page.query_selector(QR_TAG)
        if qr:
            # 尝试切换到"扫码登录"(有些页默认"快捷登录")
            for sel in ['.login__type__container__scan__link',
                        'a:has-text("扫码登录")',
                        '.login__type__container__scan']:
                link = await page.query_selector(sel)
                if link:
                    try:
                        await link.click()
                        await page.wait_for_timeout(800)
                        break
                    except Exception:
                        pass
            qr = await page.query_selector(QR_TAG)
            if qr:
                try:
                    ok = await qr.evaluate(
                        "el => { const img=el.tagName.toLowerCase()==='img'?el:el.querySelector('img');"
                        " return img ? img.naturalWidth > 50 : el.getBoundingClientRect().width > 50; }"
                    )
                    if ok:
                        return True
                except Exception:
                    return True
        await page.wait_for_timeout(500)
    return False


async def main():
    from playwright.async_api import async_playwright
    out = []

    out.append(f"尝试打开登录页: {LOGIN_URL} (chromium headless, 无额外http头)")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        # 不加 extra_http_headers(#428); 用正常浏览器 UA
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            locale="zh-CN",
        )
        page = await ctx.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            out.append(f"页面URL: {page.url[:80]}")
            title = await page.title()
            out.append(f"标题: {title[:60]}")

            body = await page.content()
            if "验证" in body and "安全" in body:
                out.append("⚠️ 可能触发安全验证(内容含'验证/安全')")

            ready = await _wait_qrcode_ready(page, timeout=30)
            out.append(f"二维码已就绪(可见且可加载): {ready}")

            if ready:
                qr = await page.query_selector(QR_TAG)
                dest = PROJECT_ROOT / "tools" / "_wx_login_qr.png"
                await qr.screenshot(path=str(dest))
                size = dest.stat().st_size if dest.exists() else 0
                out.append(f"✅ 二维码已截图: {dest} ({size} 字节) -> 云端可生成, we-mp-rss 思路可行")
            else:
                out.append("❌ 30s 内二维码未就绪(可能被拦/页面异常)")
        except Exception as e:
            out.append(f"异常: {type(e).__name__}: {str(e)[:200]}")
        finally:
            await browser.close()

    text = "\n".join(out)
    print(text)
    (PROJECT_ROOT / "tools" / "_wxlogin_diag.txt").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

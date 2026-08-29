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
        ls = {}  # localStorage(登录态可能存这里而非 cookie)
        try:
            await page.goto(WEREAD_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)
            out.append(f"页面URL: {page.url[:80]}")

            # 微信读书需先点「登录」按钮才弹出二维码登录弹窗
            try:
                login_btn = (await page.query_selector("text=登录")
                             or await page.query_selector("button:has-text('登录')")
                             or await page.query_selector("a:has-text('登录')"))
                if login_btn and await login_btn.is_visible():
                    await login_btn.click()
                    out.append("已点击「登录」按钮")
                    await page.wait_for_timeout(2000)
                else:
                    out.append("未找到可点登录按钮(可能已登录/布局不同)")
            except Exception as e:
                out.append(f"点登录异常: {e}")

            SEL_SAVE = os.path.join(DATA_DIR, "weread_qr_selector.json")
            # 读记忆
            used_sel = None
            if os.path.exists(SEL_SAVE):
                try:
                    used_sel = json.load(open(SEL_SAVE, encoding="utf-8")).get("selector")
                except Exception:
                    used_sel = None
            qr = None
            if used_sel:
                try:
                    qr = await page.query_selector(used_sel)
                    if qr and await qr.is_visible():
                        out.append(f"用记忆选择器定位二维码: {used_sel}")
                except Exception:
                    qr = None
            # 全量探测: 优先登录弹窗内二维码 + 校验近似方形(避免截到书封)
            if qr is None:
                qr_selectors = [
                    ".wr-nest-dialog img[src*='qr'], [class*='login'] img[src*='qr'], img[src*='qrcode']",
                    ".wr-nest-dialog img, [class*='login'] img, img[src*='qr']",
                    "img[src*='qr']", "img[src*='qrcode']",
                ]
                for try_sel in qr_selectors:
                    for _ in range(10):
                        try:
                            q = await page.query_selector(try_sel)
                            if q and await q.is_visible():
                                box = await q.bounding_box()
                                if box and box["width"] >= 100 and abs(box["width"] - box["height"]) < box["width"] * 0.3:
                                    qr = q
                                    os.makedirs(DATA_DIR, exist_ok=True)
                                    json.dump({"selector": try_sel}, open(SEL_SAVE, "w", encoding="utf-8"))
                                    out.append(f"✅ 探到二维码选择器并记忆: {try_sel} ({int(box['width'])}x{int(box['height'])})")
                                    break
                        except Exception:
                            pass
                        await page.wait_for_timeout(1200)
                    if qr:
                        break
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
                # 等扫码: 必须 wr_vid + wr_skey 都出现(wr_gid/fp 是游客cookie一直在, 不能作登录判据)
                need = {"wr_vid", "wr_skey"}
                logined = False
                for _ in range(180):  # ~360s
                    await page.wait_for_timeout(2000)
                    names = {c["name"] for c in await ctx.cookies()}
                    if need.issubset(names):
                        logined = True
                        out.append("✅ 检测到 wr_vid+wr_skey, 登录成功")
                        break
                if not logined:
                    out.append("⚠️ 360s 内未出现 wr_vid/wr_skey(扫码可能未真正完成, 或登录态在别处)")
                    try:
                        names = {c["name"] for c in await ctx.cookies()}
                        out.append(f"  当前cookie名: {sorted(names)}")
                    except Exception:
                        pass

                # 无论如何, 导航到 weread.qq.com / i.weread.qq.com 尝试触发完整 set-cookie
                for nav in ["https://weread.qq.com/", "https://i.weread.qq.com/web/feed/home"]:
                    try:
                        await page.goto(nav, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(4000)
                        out.append(f"已访问 {nav.split('//')[-1].split('/')[0]} 触发cookie")
                        break
                    except Exception as e:
                        out.append(f"访问异常: {e}")
                # 导航后再查一次是否补齐 wr_vid/skey
                cur = {c["name"] for c in await ctx.cookies()}
                if need.issubset(cur):
                    out.append("✅ 导航后补齐 wr_vid+wr_skey")
                    logined = True
                # 收集 localStorage(登录态可能存这) + cookies
                ls = {}
                try:
                    ls = await page.evaluate("() => { const o={}; for (let i=0;i<localStorage.length;i++){const k=localStorage.key(i);o[k]=localStorage.getItem(k)?.slice(0,80);} return o; }")
                except Exception:
                    pass
                cookies = await ctx.cookies()
            else:
                out.append("未找到二维码元素(可能未弹出登录弹窗/布局不同)")
                # 诊断: 截图 + 页面文本, 供下轮定位
                try:
                    page.screenshot(path=str(PROJECT_ROOT / "tools" / "_wr_diag.png"))
                    body = await page.content()
                    import re
                    text = re.sub(r"<[^>]+>", " ", body)
                    out.append("页面文本片段: " + " ".join(text.split())[:200])
                except Exception as e:
                    out.append(f"诊断截图失败: {e}")
                cookies = await ctx.cookies()
                wr = [c for c in cookies if c["name"].startswith("wr_")]
                out.append(f"已有 wr_* cookie: {len(wr)}")

            # 取最终 wr_* cookie
            cookies = await ctx.cookies()
            wr = {c["name"]: c["value"] for c in cookies if c["name"].startswith("wr_")}
            if wr:
                cookie_str = "; ".join(f"{k}={v}" for k, v in wr.items())
                os.makedirs(DATA_DIR, exist_ok=True)
                payload = {"cookie": cookie_str, "cookies": wr, "localStorage": ls}
                with open(SAVE, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                out.append(f"✅ wr_* cookie 已保存: {SAVE} ({len(wr)} 个: {list(wr.keys())})")
            else:
                # 若 cookie 无 wr_*, 也许登录态在 localStorage
                out.append("⚠️ cookie 无 wr_*(检查 localStorage)")
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(SAVE, "w", encoding="utf-8") as f:
                    json.dump({"cookie": "", "cookies": {}, "localStorage": ls}, f, ensure_ascii=False, indent=2)
                # 列出所有 cookie 名便于诊断
                alln = [c["name"] for c in cookies]
                out.append(f"  所有 cookie 名: {alln}")
                out.append(f"  localStorage keys: {list(ls.keys())[:20]}")
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

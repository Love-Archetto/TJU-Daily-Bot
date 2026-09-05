#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证: headless 打开阅读器页, 轮询等待是否真就绪(title 从'微信读书'变'公众号名-微信读书')."""
import os
import sys
import time

sys.path.insert(0, os.path.abspath("."))
try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass
COOKIE = os.environ.get("WEREAD_COOKIE", "")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
READER = "https://weread.qq.com/web/mp/reader/21642c422d505f5758535f323339373930373534326bf"


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            ctx = b.new_context(user_agent=UA, locale="zh-CN", viewport={"width":1280,"height":900})
            for pair in COOKIE.split(";"):
                if "=" in pair:
                    k, v = pair.strip().split("=", 1)
                    try:
                        ctx.add_cookies([{"name": k, "value": v, "domain": ".qq.com", "path": "/"}])
                    except Exception:
                        pass
            pg = ctx.new_page()
            pg.goto(READER, wait_until="domcontentloaded", timeout=30000)
            # 轮询等待就绪(最多12次×5s=60s)
            ready = False
            for i in range(12):
                time.sleep(5)
                try:
                    title = pg.title()
                    hdr = pg.evaluate("location.pathname")
                    print(f"  [{i+1}] title='{title[:30]}' path={hdr[:40]}")
                    # 就绪 = title 不再只是"微信读书"且含 公众号
                    if title and "微信读书" in title and "公众号" in title:
                        ready = True
                        break
                    if title and title != "微信读书":
                        ready = True  # title 变了就算(可能不同格式)
                        break
                except Exception as e:
                    print(f"  [{i+1}] 探针异常: {str(e)[:60]}")
            print("就绪?", ready)
            if ready:
                js = ("(()=>fetch('/web/mp/articles?bookId=MP_WXS_2397907542&offset=0',{credentials:'include'})"
                      ".then(r=>r.json()).then(o=>JSON.stringify({errCode:o.errCode,n:(o.reviews||[]).length})))")
                try:
                    print("articles:", pg.evaluate(js))
                except Exception as e:
                    print("articles异常:", str(e)[:80])
        finally:
            b.close()


if __name__ == "__main__":
    main()

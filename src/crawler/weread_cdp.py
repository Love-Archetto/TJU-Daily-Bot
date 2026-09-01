"""CDP 连接层 — 复用「已登录、已过验证码的真实 Edge 浏览器」面板上下文发 weread 请求.

背景:
  自开 headless 浏览器进阅读器页会触发腾讯防水墙验证码, 导致 /web/mp/articles 返回
  weread errCode=-2041。参考 Pengyf04/weread-mp-fetcher 的做法: 复用用户已登录的
  真实浏览器(此处为 Edge)的阅读器 page 页上下文, 不发新浏览器实例 → 会话已就绪、无验证码。

本模块:
  - find_or_launch_edge(): 找调试端口上已有 Edge, 没有则以固定 --user-data-dir + 调试端口拉起一个有头 Edge。
  - connect()/evaluate_on_reader_tab(): 用 playwright connect_over_cdp(零新增依赖)连到该 Edge,
    定位一个 title 含「公众号」的阅读器 page 页, 在其上下文 page.evaluate(js)。
  配置:
  - WEREAD_EDGE_PORT  (默认 9333)
  - WEREAD_EDGE_PROFILE (默认 %LOCALAPPDATA%\\TJUNews\\weread_edge_profile, 仓库外)
"""

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import time

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9333
DEFAULT_PROFILE = os.path.join(
    os.getenv("LOCALAPPDATA", os.path.expanduser("~")), "TJUNews", "weread_edge_profile"
)


def _load_env():
    """惰性加载仓库根 .env(与 weread_subscribe 一致)."""
    try:
        from dotenv import load_dotenv
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        load_dotenv(os.path.join(root, ".env"))
    except Exception:
        pass


def edge_port() -> int:
    _load_env()
    try:
        return int(os.environ.get("WEREAD_EDGE_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def edge_profile() -> str:
    _load_env()
    return os.environ.get("WEREAD_EDGE_PROFILE", DEFAULT_PROFILE)


def find_edge_executable() -> str | None:
    """定位 msedge.exe. 找不到返回 None."""
    paths = [
        shutil.which("msedge") or shutil.which("msedge.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Microsoft", "Edge",
                     "Application", "msedge.exe"),
    ]
    for p in paths:
        if p and os.path.isfile(os.path.normpath(p)):
            return os.path.normpath(p)
    return None


def _http_json(url: str) -> dict | None:
    """GET 一个 http json 端点(读 /json/version,/json/list). 失败/非200 返回 None."""
    try:
        import requests as _r
        resp = _r.get(url, timeout=2)
        if resp.status_code != 200:
            return None
        j = resp.json()
        return j if isinstance(j, dict) else None
    except Exception:
        return None


def _debug_port_alive(port: int) -> bool:
    return _http_json(f"http://127.0.0.1:{port}/json/version") is not None


def find_or_launch_edge(port: int | None = None, profile_dir: str | None = None) -> str:
    """确保一个带调试端口的 Edge 在跑, 返回 connect_over_cdp 用的 http 地址.

    若端口上已有 Edge 则复用; 否则用固定 --user-data-dir 有头拉起一个并导航到微信读书登录页。
    """
    port = port or edge_port()
    dbg = f"http://127.0.0.1:{port}"
    if _debug_port_alive(port):
        logger.info("复用已运行的调试 Edge: %s", dbg)
        return dbg

    exe = find_edge_executable()
    if not exe:
        raise RuntimeError(
            "找不到 Edge(msedge.exe)。请安装 Microsoft Edge 或在 .env 设 WEREAD_EDGE_PROFILE。"
        )
    profile_dir = profile_dir or edge_profile()
    os.makedirs(profile_dir, exist_ok=True)

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "https://weread.qq.com/web/login",
    ]
    logger.info("拉起调试 Edge: %s (port=%d)", exe, port)
    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except Exception as e:
        raise RuntimeError(f"拉起 Edge 失败: {e}")

    # 等端口就绪(最多 ~20s)
    for _ in range(20):
        time.sleep(1)
        if _debug_port_alive(port):
            return dbg
    raise RuntimeError(
        f"Edge 调试端口 {port} 在规定时间内未就绪。请确认 Edge 启动方式, 或改 WEREAD_EDGE_PORT。"
    )


def _is_reader_target(url: str) -> bool:
    return bool(re.search(r"web/mp/reader/[0-9a-f]+", url))


def _tab_is_reader_ready(title: str, url: str) -> bool:
    """阅读器页就绪判据(参考 PROBE_JS): title 含公众号(或离开裸"微信读书"/login) +
    url 是阅读器页."""
    if not _is_reader_target(url):
        return False
    t = title or ""
    if "公众号" in t:
        return True
    # title 变了但还没到"公众号"也当就绪(可能是格式差异)
    if t and "微信读书" in t and t.strip() != "微信读书":
        return True
    return False


@contextlib.contextmanager
def connect(port: int | None = None):
    """context manager: 连到调试 Edge, 产出一个已就绪的 reader 页面对象.

    用法: with connect() as page: page.evaluate(js)
    不 new_context()(那会丢登录态), 复用既有浏览器 contexts 里的 page。
    """
    dbg = find_or_launch_edge(port)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(dbg)

        def find_reader():
            for ctx in browser.contexts:
                for pg in ctx.pages:
                    if _tab_is_reader_ready(pg.title(), pg.url):
                        return pg
            return None

        try:
            page = find_reader()
            if page is not None:
                yield page
                return
            # 没有就绪的阅读器页: 看有没有任一 weread 页, 在其上发书架请求推导 readerUrl,
            # 或用配置/书架第一个号的 readerUrl 进入。
            homepage = None
            for ctx in browser.contexts:
                for pg in ctx.pages:
                    if "weread.qq.com" in pg.url:
                        homepage = pg
                        break
                if homepage:
                    break
            if homepage is None:
                page = browser.contexts[0].new_page()
                page.goto("https://weread.qq.com/", wait_until="domcontentloaded", timeout=30000)
            raise RuntimeError(
                "未检测到就绪的公众号阅读器页。请在该 Edge 窗口点进任意一个订阅的公众号, "
                "等页面加载出「XXX - 公众号」标题后重试(那一步会要你手动过验证码)。"
            )
        finally:
            browser.close()


def evaluate_on_reader_tab(js: str, page_url: str = "", port: int | None = None):
    """在已就绪的阅读器页上下文里执行 JS 并返回结果(契约同 weread_subscribe._run_page_js).

    若 page_url 是阅读器页且当前 tab 不在其上, 导航过去并等就绪; 否则直接用当前就绪 tab。
    """
    dbg = find_or_launch_edge(port)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(dbg)
        try:
            # 找当前就绪的阅读器 tab
            page = None
            for ctx in browser.contexts:
                for pg in ctx.pages:
                    if _tab_is_reader_ready(pg.title(), pg.url):
                        page = pg
                        break
                if page:
                    break

            want_reader = page_url and "/web/mp/reader/" in page_url
            if want_reader:
                # 目标阅读器 URL: 尽量导航(若当前 tab 不同)后等就绪
                if page is None or (page.url != page_url and not _is_reader_target(page.url)):
                    if page is None:
                        page = browser.contexts[0].new_page()
                    try:
                        page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    # 等 title 含 公众号(最多 12×5s)
                    for _ in range(12):
                        page.wait_for_timeout(5000)
                        if "公众号" in (page.title() or ""):
                            break
                # 即便导航过去后仍未就绪(可能又弹验证码, 需真人过), 直接尝试 evaluate
                raw = page.evaluate(js)
                return json.loads(raw) if isinstance(raw, str) else raw

            # 非阅读器目标(书架可首页发): 用任一 weread 页
            if page is None:
                for ctx in browser.contexts:
                    for pg in ctx.pages:
                        if "weread.qq.com" in pg.url:
                            page = pg
                            break
                    if page:
                        break
            if page is None:
                page = browser.contexts[0].new_page()
                page.goto("https://weread.qq.com/", wait_until="domcontentloaded", timeout=30000)
            raw = page.evaluate(js)
            return json.loads(raw) if isinstance(raw, str) else raw
        finally:
            browser.close()


# 供 list_subscribed/fetch_articles 便捷调用的薄封装(保 _run_page_js 签名不变时的可测性)
def run_js(js: str, page_url: str = ""):
    return evaluate_on_reader_tab(js, page_url)

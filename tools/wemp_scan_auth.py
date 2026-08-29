"""云端微信公众平台扫码认证 — 让 we-mp-rss 抓公众号多篇文章。

背景：
    要抓每个公众号多篇（本地一次发 3 篇），we-mp-rss 必须用「微信公众平台」会话
    （web 采集模式，默认），而该会话首次/过期需扫码登录。GitHub Actions 无显示器，
    无法本地扫码。故本脚本：云端触发扫码 → 下载二维码 PNG → 通过 SMTP 发到用户邮箱
    → 轮询等待用户扫码成功 → 会话持久化到 /app/data/wx.lic（后续靠 actions/cache 沿用）。

前提：
    - we-mp-rss 容器已启动（端口 8001）
    - we-mp-rss 的 GATHER.MODEL 为默认 web（不要设 weread_mp）
    - 配好 SMTP_* 与 NOTIFY_TO 环境变量（发码到邮箱）

用法：
    python tools/wemp_scan_auth.py [--base http://localhost:8001] [--wait-seconds 180]
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.notifier import send_alert  # noqa: E402

API_PREFIX = "/api/v1/wx"
USER = "admin"
PWD = "admin@123"


def _login(base: str) -> str:
    """登录 we-mp-rss 拿 access_token."""
    r = requests.post(f"{base}{API_PREFIX}/auth/login",
                      data={"username": USER, "password": PWD}, timeout=30)
    r.raise_for_status()
    token = r.json().get("access_token") or (r.json().get("data") or {}).get("access_token") or ""
    if not token:
        raise RuntimeError(f"登录 we-mp-rss 失败: {r.text[:200]}")
    return token


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _get_json(url, token, timeout=30):
    r = requests.get(url, headers=_hdr(token), timeout=timeout)
    try:
        return r.status_code, r.json() if r.status_code == 200 else r.text
    except Exception:
        return r.status_code, r.text


def _download_qr(base: str, token: str, dest: Path, max_retries: int = 12, retry_delay: float = 2.0) -> bool:
    """触发后轮询下载扫码二维码图片. we-mp-rss 异步生成 PNG, 需等待 is_exists 变 true."""
    for i in range(max_retries):
        url = f"{base}{API_PREFIX}/auth/qr/image"
        try:
            r = requests.get(url, headers=_hdr(token), timeout=30)
        except requests.RequestException as e:
            logger.warning("二维码下载异常 (%d/%d): %s", i + 1, max_retries, e)
            time.sleep(retry_delay)
            continue
        if r.status_code == 200 and len(r.content) > 500:  # 有效 PNG 一般 >500 字节
            dest.write_bytes(r.content)
            logger.info("二维码已保存: %s (len=%d)", dest, len(r.content))
            return True
        logger.info("二维码尚未生成 (HTTP=%s len=%s)，%.1fs 后重试", r.status_code, len(r.content), retry_delay)
        time.sleep(retry_delay)
    logger.warning("二维码图片始终未生成，放弃")
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("WE_MP_RSS_BASE", "http://localhost:8001"))
    parser.add_argument("--wait-seconds", type=int, default=180, help="等待扫码秒数")
    parser.add_argument("--poll-interval", type=int, default=5)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    logger.info("we-mp-rss 扫码认证开始: %s", base)

    token = _login(base)
    logger.info("登录 we-mp-rss 成功")

    # 1) 触发扫码（生成二维码）；we-mp-rss 异步生成 PNG，先等一下再下载
    sc, data = _get_json(f"{base}{API_PREFIX}/auth/qr/code", token)
    logger.info("qr/code 触发 HTTP=%s resp=%s", sc, str(data)[:200])
    time.sleep(3)

    # 2) 下载二维码并发邮箱
    qr_path = PROJECT_ROOT / "tools" / "_wx_qrcode.png"
    if not _download_qr(base, token, qr_path):
        logger.error("未能获取二维码图片，无法发送扫码邮件")
        sys.exit(1)

    sent = send_alert(
        "📱 TJU Daily Bot：请扫码登录微信公众平台",
        "微信公众号多篇采集需要扫码登录微信公众平台。\n\n"
        "请用手机微信扫附件中的二维码完成登录（二维码约 2-3 分钟内有效）。\n"
        f"登录状态会自动同步到云端，之后即可自动采集多篇文章。\n\n"
        f"we-mp-rss: {base}",
        image_path=str(qr_path),
    )
    if not sent:
        logger.error("二维码邮件发送失败（检查 SMTP_* 与 NOTIFY_TO 配置）")
        # 不退出，仍轮询等待（也许用户通过其它方式拿到）
    else:
        logger.info("扫码二维码已发送到邮箱")

    # 3) 轮询扫码状态，直到成功或超时
    deadline = time.time() + args.wait_seconds
    while time.time() < deadline:
        time.sleep(args.poll_interval)
        sc, data = _get_json(f"{base}{API_PREFIX}/auth/qr/status", token)
        status = ""
        if isinstance(data, dict):
            status = str(data.get("data") or data.get("status") or data)
        else:
            status = str(data)
        logger.info("扫码状态: HTTP=%s %s", sc, status[:120])

        # 判断成功
        low = status.lower()
        if "success" in low or "ok" in low or "true" in low or "登录成功" in status or "1" == str(sc and data):
            if "not" not in low and "false" not in low and "fail" not in low:
                logger.info("✅ 扫码成功，微信公众平台会话已建立")
                return
        if "expired" in low or "fail" in low or "error" in low:
            logger.warning("二维码可能已过期/失败: %s", status)
            # 继续等待或由上层决定

    logger.warning("等待扫码超时（%ss），本轮未完成扫码。下次运行若需扫码会再发码。", args.wait_seconds)
    sys.exit(2)


if __name__ == "__main__":
    main()

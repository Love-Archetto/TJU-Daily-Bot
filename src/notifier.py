"""邮件通知 — 用于 Cookie 过期等异常时及时反馈用户。

后台背景：
    微信 MP Cookie 会过期，导致抓取失败。在 CI 中无法交互，需通过 SMTP 邮件
    第一时间通知用户更新 GitHub Secret。

配置（环境变量）：
    SMTP_HOST, SMTP_PORT, SMTP_TLS, SMTP_USER, SMTP_PASSWORD, SMTP_FROM,
    NOTIFY_TO（接收邮箱，; 分隔多个）
"""

import logging
import os
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def _bool_env(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes", "on")


def email_configured() -> bool:
    """检查邮件配置是否齐备."""
    return bool(
        os.environ.get("SMTP_USER")
        and os.environ.get("SMTP_PASSWORD")
        and os.environ.get("NOTIFY_TO")
    )


def send_alert(subject: str, body: str) -> bool:
    """发送告警邮件.

    Args:
        subject: 邮件主题
        body: 邮件正文（纯文本）

    Returns:
        True 如果发送成功
    """
    if not email_configured():
        logger.warning("SMTP 未配置，无法发送邮件告警：%s", subject)
        return False

    smtp_host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    use_tls = _bool_env("SMTP_TLS", "true")   # 465 默认 SSL/TLS
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    from_addr = os.environ.get("SMTP_FROM", smtp_user)
    recipients = [r.strip() for r in os.environ.get("NOTIFY_TO", "").split(";") if r.strip()]

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        server = None
        if use_tls:
            # SSL 模式（465）
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
        else:
            # STARTTLS 模式（587）
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            server.ehlo()
            server.starttls()
            server.ehlo()
        server.login(smtp_user, smtp_password)
        server.sendmail(from_addr, recipients, msg.as_string())
        logger.info("Alert email sent to %s", ", ".join(recipients))
        return True
    except Exception as e:
        logger.error("Failed to send alert email: %s", e)
        return False
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


def send_cookie_expired_alert(expired_count: int, detail: str = "") -> bool:
    """Cookie 过期专用告警."""
    subject = "⚠️ TJU Daily Bot：微信公众号 Cookie 已过期"
    body = (
        "【需要操作】TJU Daily Bot 检测到微信公众号抓取 Cookie 已过期。\n"
        f"过期公众号数：{expired_count}\n\n"
        "请手动更新 GitHub Actions 中的 Secret：\n"
        "  - WEREAD_COOKIE（某公众号平台 Cookie）\n"
        "  - MP_QUERY_TOKEN（token）\n\n"
        "操作步骤：\n"
        "1. 登录 https://mp.weixin.qq.com 后台\n"
        "2. F12 → Network → 刷新\n"
        "3. 复制某个请求中的 Cookie 和 token\n"
        "4. GitHub → Settings → Secrets → Actions → 更新对应 Secret\n\n"
        "详细错误：\n" + (detail or "（无）")
    )
    return send_alert(subject, body)
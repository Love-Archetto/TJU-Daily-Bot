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
from email.mime.image import MIMEImage

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


def _smtp_params():
    return (
        os.environ.get("SMTP_HOST", "smtp.qq.com"),
        int(os.environ.get("SMTP_PORT", "465")),
        _bool_env("SMTP_TLS", "true"),
        os.environ.get("SMTP_USER", ""),
        os.environ.get("SMTP_PASSWORD", ""),
        os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", ""),
        [r.strip() for r in os.environ.get("NOTIFY_TO", "").split(";") if r.strip()],
    )


def send_alert(subject: str, body: str, image_path: str | None = None) -> bool:
    """发送告警邮件（可选附二维码图片）.

    Args:
        subject: 邮件主题
        body: 正文（纯文本）
        image_path: 可选，附件图片路径（如扫码二维码 PNG）

    Returns:
        True 如果发送成功
    """
    if not email_configured():
        logger.warning("SMTP 未配置，无法发送邮件：%s", subject)
        return False

    smtp_host, smtp_port, use_tls, smtp_user, smtp_password, from_addr, recipients = _smtp_params()

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if image_path:
        try:
            with open(image_path, "rb") as f:
                img = MIMEImage(f.read(), name=os.path.basename(image_path))
            img.add_header("Content-Disposition",
                           "attachment", filename=os.path.basename(image_path))
            msg.attach(img)
        except Exception as e:
            logger.warning("附加图片失败（继续发送正文）: %s", e)

    try:
        server = None
        if use_tls:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
        else:
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
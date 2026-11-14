"""SMTP 发信：只负责把验证码邮件投递出去，标准库 smtplib 实现，不新增依赖。

QQ 邮箱用 smtp.qq.com:465（SSL）+ 授权码登录；授权码在
QQ 邮箱网页版「设置 → 账号 → POP3/IMAP/SMTP 服务」里生成。
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from app.config import get_settings

logger = logging.getLogger("fandazi.mailer")


def email_verification_enabled() -> bool:
    """配了发件账号和授权码才启用邮箱验证码注册。"""
    settings = get_settings()
    return bool(getattr(settings, "smtp_user", "") and getattr(settings, "smtp_password", ""))


def _tls_mode() -> str:
    return (getattr(get_settings(), "smtp_tls", "ssl") or "ssl").strip().lower()


def send_verification_email(to_email: str, code: str, purpose: str = "register") -> None:
    """发送验证码邮件（注册、免密登录或修改密码）；失败抛异常。"""
    settings = get_settings()
    ttl = getattr(settings, "email_code_ttl_minutes", 10) or 10
    from_name = getattr(settings, "email_from_name", "") or "饭搭子"
    sender = settings.smtp_user
    scene = {"register": "注册", "login": "登录", "reset": "修改密码"}.get(purpose, "验证")

    message = EmailMessage()
    message["Subject"] = f"{from_name}{scene}验证码：{code}"
    message["From"] = formataddr((from_name, sender))
    message["To"] = to_email
    message.set_content(
        f"你的{scene}验证码是 {code}，{ttl} 分钟内有效。\n"
        f"如果不是你本人操作，请忽略这封邮件。\n\n—— {from_name}"
    )

    host = getattr(settings, "smtp_host", "smtp.qq.com") or "smtp.qq.com"
    port = int(getattr(settings, "smtp_port", 465) or 465)
    mode = _tls_mode()
    if port == 465 and mode == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context()) as smtp:
            smtp.login(sender, settings.smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            if mode == "starttls":
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(sender, settings.smtp_password)
            smtp.send_message(message)
    logger.info("verification_email_sent to=%s", to_email)

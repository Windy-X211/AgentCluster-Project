"""send_email 插件执行代码 — 对应 manifest.yaml + tool.yaml

run(provider="qq" | smtp_server + smtp_port + use_ssl, username, password, to_addrs, subject, body, ...)
"""
import smtplib, sys
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any

SMTP_PRESETS: dict[str, dict[str, Any]] = {
    "qq":     {"smtp_server": "smtp.qq.com",        "smtp_port": 465, "use_ssl": True},
    "163":    {"smtp_server": "smtp.163.com",       "smtp_port": 465, "use_ssl": True},
    "gmail":  {"smtp_server": "smtp.gmail.com",     "smtp_port": 465, "use_ssl": True},
    "outlook":{"smtp_server": "smtp.office365.com", "smtp_port": 587, "use_ssl": False},
    "exmail": {"smtp_server": "smtp.exmail.qq.com", "smtp_port": 465, "use_ssl": True},
}


def run(provider: str = "", smtp_server: str = "", smtp_port: int | None = None,
        use_ssl: bool | None = None, username: str = "", password: str = "",
        from_addr: str = "", to_addrs: str = "", subject: str = "(无主题)", body: str = "") -> dict[str, Any]:
    # 1) 应用 preset
    if provider and provider in SMTP_PRESETS:
        p = SMTP_PRESETS[provider]
        if not smtp_server: smtp_server = p["smtp_server"]
        if smtp_port is None: smtp_port = p["smtp_port"]
        if use_ssl is None: use_ssl = p["use_ssl"]

    smtp_port = smtp_port or 465
    if use_ssl is None: use_ssl = True

    # 2) 参数校验
    if not smtp_server:
        return {"error": "请指定 provider (qq/163/gmail/outlook/exmail) 或手动填 smtp_server"}
    if not username or not password:
        return {"error": "username 和 password（授权码）必填"}
    tos = [t.strip() for t in to_addrs.replace("；", ",").split(",") if t.strip()]
    if not tos:
        return {"error": "收件人列表 to_addrs 为空"}

    try:
        msg = MIMEMultipart()
        msg["From"] = from_addr or username
        msg["To"] = ", ".join(tos)
        msg["Subject"] = subject or "(无主题)"
        msg.attach(MIMEText(body or "", "plain", "utf-8"))

        if use_ssl:
            client = smtplib.SMTP_SSL(smtp_server, int(smtp_port), timeout=30)
        else:
            client = smtplib.SMTP(smtp_server, int(smtp_port), timeout=30)
            client.starttls()
        client.login(username, password)
        client.sendmail(msg["From"], tos, msg.as_string())
        client.quit()
        return {"ok": True, "sent_to": tos}
    except Exception as e:
        return {"error": str(e)}


# 保留历史导入兼容
PLUGIN_NAME = "send_email"
PLUGIN_LABEL = "发送邮箱"
PLUGIN_DESC = "通过 SMTP 发送文本邮件，支持主流邮箱预设"
PLUGIN_PARAMS = ["provider", "smtp_server", "smtp_port", "username", "password",
                "from_addr", "to_addrs", "subject", "body", "use_ssl"]

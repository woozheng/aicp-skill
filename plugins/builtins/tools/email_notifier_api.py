import asyncio
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

CONFIG_PATH = Path("data/smart_timer/config.json")


def load_smtp_config():
    if not CONFIG_PATH.exists():
        raise ValueError(f"SMTP 配置文件不存在: {CONFIG_PATH}")
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        raise ValueError(f"SMTP 配置文件 JSON 格式错误: {e}")
    smtp = data.get("smtp", {})
    user = smtp.get("username") or smtp.get("user")
    if not user:
        raise ValueError("SMTP 配置缺少字段: username/user")
    required = ["host", "port", "password"]
    for key in required:
        if not smtp.get(key):
            raise ValueError(f"SMTP 配置缺少字段: {key}")
    smtp["user"] = user
    return smtp


def send_email_sync(smtp_cfg, to_addrs, subject, body, is_html=False):
    msg = MIMEMultipart()
    msg["From"] = smtp_cfg["user"]
    msg["To"] = ", ".join(to_addrs) if isinstance(to_addrs, list) else to_addrs
    msg["Subject"] = subject
    mime_type = "html" if is_html else "plain"
    msg.attach(MIMEText(body, mime_type, "utf-8"))

    host = smtp_cfg["host"]
    port = int(smtp_cfg["port"])
    user = smtp_cfg["user"]
    password = smtp_cfg["password"]
    use_ssl = smtp_cfg.get("use_ssl", True)

    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
        server.starttls()
    try:
        server.login(user, password)
        recipients = to_addrs if isinstance(to_addrs, list) else [a.strip() for a in to_addrs.split(",")]
        server.sendmail(user, recipients, msg.as_string())
    finally:
        server.quit()


async def execute(envelop, agent):
    action = envelop.payload.get("action", "send")

    if action == "send":
        to_addrs = envelop.payload.get("to")
        subject = envelop.payload.get("subject", "")
        body = envelop.payload.get("body", "")
        is_html = envelop.payload.get("is_html", False)

        if not to_addrs:
            envelop.payload = {"ok": False, "error": "缺少 to 参数（收件人）"}
            return envelop
        if not subject:
            envelop.payload = {"ok": False, "error": "缺少 subject 参数（主题）"}
            return envelop

        try:
            smtp_cfg = load_smtp_config()
        except ValueError as e:
            envelop.payload = {"ok": False, "error": str(e)}
            return envelop

        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: send_email_sync(smtp_cfg, to_addrs, subject, body, is_html)
            )
        except Exception as e:
            envelop.payload = {"ok": False, "error": f"发送邮件失败: {e}"}
            return envelop

        envelop.payload = {"ok": True, "data": {"sent": True, "to": to_addrs, "subject": subject}}
        return envelop

    elif action == "test_config":
        try:
            smtp_cfg = load_smtp_config()
            envelop.payload = {
                "ok": True,
                "data": {
                    "host": smtp_cfg["host"],
                    "port": smtp_cfg["port"],
                    "user": smtp_cfg["user"],
                    "use_ssl": smtp_cfg.get("use_ssl", True),
                },
            }
        except ValueError as e:
            envelop.payload = {"ok": False, "error": str(e)}
        return envelop

    else:
        envelop.payload = {"ok": False, "error": f"未知操作: {action}"}
        return envelop


# @AICP_ALIGN: actions=send,test_config | output_fields=sent,to,subject,host,port,user,use_ssl | input_fields=to,subject,body,is_html | type_values=
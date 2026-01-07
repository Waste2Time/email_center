import email
import imaplib
import json
import logging
import os
import smtplib
import time
from email.header import make_header, decode_header
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
from email.mime.text import MIMEText
from typing import Optional, List

from dotenv import load_dotenv

from command_processor import parse_command, handle_command

# ==============================
#  环境变量 & 基本配置
# ==============================

ENV_PATH = os.path.expanduser("./.env")
load_dotenv(ENV_PATH)

EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")  # QQ 邮箱授权码
API_KEY = os.getenv("API_KEY")

send_logger = logging.getLogger("send_logger")
request_logger = logging.getLogger("request_logger")


if not EMAIL_FROM or not EMAIL_PASSWORD or not API_KEY:
    raise RuntimeError(
        f"EMAIL_FROM / EMAIL_PASSWORD / API_KEY 未在 {ENV_PATH} 中正确配置"
    )

# IMAP 轮询间隔（秒）：为避免触发风控，建议 >= 30 秒
IMAP_POLL_INTERVAL_SECONDS = 30

# 发送频率限制：对 email_to 列表中的每个收件人，发送间隔 1 秒
SEND_INTERVAL_SECONDS = 1

# QQ 邮箱 SMTP / IMAP 配置（写死在代码中：你说 .env 暂时不需要这些）
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 587

IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993  # SSL

# QQ 邮箱中你创建的命令文件夹名，建议在 Web 端建一个名为 "command" 的文件夹
COMMAND_FOLDER = "command"


def send_email_to_recipients(
    subject: str,
    text_content: str,
    html_content: Optional[str],
    from_name: str,
    recipients: List[str],
):
    """
    使用 QQ SMTP (smtp.qq.com:587 + STARTTLS) 向 recipients 列表中的收件人逐个发送邮件。
    每个收件人之间间隔 SEND_INTERVAL_SECONDS 秒。

    返回一个结果列表，每个元素形如：
    {"to": "xxx@example.com", "success": True/False, "error": "..."}
    """
    results = []

    # 建立 SMTP 连接（非 SSL，后面用 STARTTLS）
    server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    # 如果你不想看 debug，可以注释掉下一行
    # server.set_debuglevel(1)

    try:
        # 发送 EHLO
        server.ehlo()

        # 启动 TLS 加密
        if server.has_extn("STARTTLS"):
            server.starttls()
            server.ehlo()  # 启动 TLS 后建议再 EHLO 一次

        # 登录 QQ 邮箱
        server.login(EMAIL_FROM, EMAIL_PASSWORD)

        # 逐个收件人发送
        for idx, to_addr in enumerate(recipients):
            # 构建一封带 text/html 的 MIMEMultipart 邮件
            msg = MIMEMultipart("alternative")

            # From: "名称 <邮箱地址>"
            msg["From"] = formataddr((from_name, EMAIL_FROM))
            msg["To"] = to_addr
            msg["Subject"] = Header(subject, "utf-8")

            # 纯文本部分
            if text_content:
                text_part = MIMEText(text_content, "plain", "utf-8")
                msg.attach(text_part)

            # HTML 部分（可选）
            if html_content:
                html_part = MIMEText(html_content, "html", "utf-8")
                msg.attach(html_part)

            try:
                server.sendmail(EMAIL_FROM, [to_addr], msg.as_string())
                ok = True
                err_msg = ""
                if "send_logger" in globals() and send_logger:
                    send_logger.info("SUCCESS to=%s subject=%s", to_addr, subject)
            except Exception as e:
                ok = False
                err_msg = repr(e)
                if "send_logger" in globals() and send_logger:
                    send_logger.error(
                        "FAIL to=%s subject=%s error=%s", to_addr, subject, err_msg
                    )

            results.append(
                {
                    "to": to_addr,
                    "success": ok,
                    "error": err_msg,
                }
            )

            if idx != len(recipients) - 1:
                time.sleep(SEND_INTERVAL_SECONDS)

    finally:
        try:
            server.quit()
        except Exception:
            pass

    return results


def build_email_message(
    subject: str,
    text_content: str,
    html_content: Optional[str],
    from_name: str,
    to_addr: str,
) -> EmailMessage:
    """
    构造单个收件人的 EmailMessage。
    如果有 html_content，则构造 multipart/alternative。
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{EMAIL_FROM}>"
    msg["To"] = to_addr

    if html_content:
        msg.set_content(text_content or "")
        msg.add_alternative(html_content, subtype="html")
    else:
        msg.set_content(text_content or "")

    return msg


def decode_mime_header(value: str) -> str:
    """解码 MIME 编码的邮件头（如 Subject、From）。"""
    if not value:
        return ""
    return str(make_header(decode_header(value)))

# ==============================
#  QQ IMAP 命令收取部分
# ==============================

def extract_text_body(msg: email.message.Message) -> str:
    """
    从邮件中尽可能拿到 text/plain 正文。
    如无 text/plain，则退回 text/html 或整个 payload。
    """
    if msg.is_multipart():
        # 优先 text/plain
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp:
                charset = part.get_content_charset() or "utf-8"
                try:
                    return part.get_payload(decode=True).decode(charset, errors="ignore")
                except Exception:
                    continue

        # 再找 text/html
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/html":
                charset = part.get_content_charset() or "utf-8"
                try:
                    return part.get_payload(decode=True).decode(charset, errors="ignore")
                except Exception:
                    continue

        return ""
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            try:
                return payload.decode(charset, errors="ignore")
            except Exception:
                return payload.decode("utf-8", errors="ignore")
        return ""


def process_single_imap_message(M: imaplib.IMAP4_SSL, num: bytes):
    """
    处理某一封 IMAP 邮件：
    - 读取 RFC822 数据
    - 检查 Subject 是否为 COMMAND
    - 提取正文并解析命令
    - 调用 command_processor.handle_command
    - 最后将邮件标记为已读
    """
    typ, msg_data = M.fetch(num, "(RFC822)")
    if typ != "OK":
        return

    raw_email = msg_data[0][1]
    msg = email.message_from_bytes(raw_email)

    subject = decode_mime_header(msg.get("Subject", ""))
    from_raw = decode_mime_header(msg.get("From", ""))

    # 仅处理主题为 "COMMAND" 的邮件（大小写不敏感）
    if subject.strip().upper() != "COMMAND":
        # 标记为已读，防止下次继续看到
        M.store(num, "+FLAGS", "\\Seen")
        request_logger.info(
            "IMAP_SKIP_NON_COMMAND subject=%s from=%s", subject, from_raw
        )
        return

    body_text = extract_text_body(msg)
    cmd = parse_command(body_text)
    if not cmd:
        # 无有效命令，标记已读
        M.store(num, "+FLAGS", "\\Seen")
        request_logger.warning(
            "IMAP_NO_COMMAND_PARSED subject=%s from=%s", subject, from_raw
        )
        return

    meta = {
        "subject": subject,
        "from": from_raw,
        "message_id": msg.get("Message-ID", ""),
    }

    request_logger.info(
        "IMAP_COMMAND_RECEIVED name=%s args=%s meta=%s",
        cmd.name,
        cmd.args,
        meta,
    )

    # 调用命令处理框架
    result = handle_command(cmd, meta, logger=request_logger)

    request_logger.info(
        "IMAP_COMMAND_RESULT name=%s result=%s", cmd.name, json.dumps(result, ensure_ascii=False)
    )

    # 处理完成后标记为已读
    M.store(num, "+FLAGS", "\\Seen")


def imap_command_loop():
    """
    IMAP 轮询主循环：
    - 持续连接 QQ IMAP
    - 周期性选择 COMMAND_FOLDER
    - 搜索 UNSEEN 未读邮件
    - 对每封邮件调用 process_single_imap_message
    """
    while True:
        try:
            with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as M:
                M.login(EMAIL_FROM, EMAIL_PASSWORD)
                request_logger.info("IMAP_LOGIN_SUCCESS as %s", EMAIL_FROM)

                while True:
                    # 选择 command 文件夹（请确保 QQ 邮箱中存在该文件夹）
                    typ, _ = M.select(COMMAND_FOLDER, readonly=False)
                    if typ != "OK":
                        request_logger.error(
                            "IMAP_SELECT_FAIL folder=%s", COMMAND_FOLDER
                        )
                        time.sleep(IMAP_POLL_INTERVAL_SECONDS)
                        continue

                    typ, data = M.search(None, "UNSEEN")
                    if typ != "OK":
                        request_logger.error("IMAP_SEARCH_FAIL UNSEEN")
                        time.sleep(IMAP_POLL_INTERVAL_SECONDS)
                        continue

                    nums = data[0].split()
                    if nums:
                        request_logger.info(
                            "IMAP_FOUND_UNSEEN_COUNT=%d", len(nums)
                        )

                    for num in nums:
                        process_single_imap_message(M, num)

                    time.sleep(IMAP_POLL_INTERVAL_SECONDS)

        except Exception as e:
            request_logger.error("IMAP_LOOP_ERROR error=%s", repr(e))
            # 避免频繁重连触发风控，出错时稍微等一会儿
            time.sleep(10)

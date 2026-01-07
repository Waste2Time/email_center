#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
mail_gateway.py

功能：
1. HTTP 网关：
   - POST /send_email：从 Request Body 中读取邮件信息，使用 QQ SMTP 进行发送
   - GET  /health：健康检查
   - 使用 X-API-KEY 头进行简单鉴权（与 .env 中的 API_KEY 对应）

2. QQ 邮箱命令收取：
   - 使用 IMAP 连接 QQ 邮箱
   - 周期性轮询 command 文件夹
   - 仅处理“未读 + 主题为 COMMAND”的邮件
   - 解析正文为“命令文本”，交给 command_processor 处理

3. 日志：
   - 请求日志：/var/log/email_gateway/requests.log
   - 发送日志：/var/log/email_gateway/send.log

注意：
- 本服务为自用极简服务，没有做复杂输入校验与防护。
- 请务必妥善保护 QQ 授权码和 API_KEY。
"""

import os
import json
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, request, jsonify
from dotenv import load_dotenv

from mail_service import send_email_to_recipients, imap_command_loop

# ==============================
#  环境变量 & 基本配置
# ==============================

ENV_PATH = os.path.expanduser("./.env")
load_dotenv(ENV_PATH)

EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")  # QQ 邮箱授权码
API_KEY = os.getenv("API_KEY")

if not EMAIL_FROM or not EMAIL_PASSWORD or not API_KEY:
    raise RuntimeError(
        f"EMAIL_FROM / EMAIL_PASSWORD / API_KEY 未在 {ENV_PATH} 中正确配置"
    )

# HTTP 服务配置
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8899

# 日志配置
LOG_DIR = Path("/var/log/email_gateway")
REQUEST_LOG_FILE = LOG_DIR / "requests.log"
SEND_LOG_FILE = LOG_DIR / "send.log"

# ==============================
#  日志初始化
# ==============================

LOG_DIR.mkdir(parents=True, exist_ok=True)

request_logger = logging.getLogger("request_logger")
request_logger.setLevel(logging.INFO)
req_handler = RotatingFileHandler(
    REQUEST_LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
req_fmt = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
req_handler.setFormatter(req_fmt)
request_logger.addHandler(req_handler)

send_logger = logging.getLogger("send_logger")
send_logger.setLevel(logging.INFO)
send_handler = RotatingFileHandler(
    SEND_LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
send_fmt = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
send_handler.setFormatter(send_fmt)
send_logger.addHandler(send_handler)


# ==============================
#  Flask 应用
# ==============================

app = Flask(__name__)


def check_api_key(req: request) -> bool:
    """校验 X-API-KEY 头是否匹配 .env 中的 API_KEY。"""
    client_key = req.headers.get("X-API-KEY")
    return client_key == API_KEY


@app.route("/health", methods=["GET"])
def health_check():
    """健康检查接口。"""
    return jsonify({"status": "ok"}), 200


@app.route("/send_email", methods=["POST"])
def send_email():
    """
    发送邮件接口：
    Header:
        X-API-KEY: <你的API_KEY>
    Body(JSON):
        subject (必需)
        text_content (必需)
        html_content (可选；兼容html_context)
        email_to (必需，列表)
        from_name (必需)
    """
    if not check_api_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json(force=True)
    except Exception as e:
        return jsonify({"error": f"Invalid JSON: {e}"}), 400

    # 为了简单，我们假设调用方按约定传字段，不做复杂校验
    subject = data.get("subject")
    text_content = data.get("text_content")
    html_content = data.get("html_content") or data.get("html_context")
    email_to = data.get("email_to")
    from_name = data.get("from_name")

    # 记录请求日志，方便排查
    try:
        request_logger.info(
            "HTTP_REQUEST from_name=%s subject=%s recipients=%s body=%s",
            from_name,
            subject,
            json.dumps(email_to, ensure_ascii=False),
            json.dumps(data, ensure_ascii=False),
        )
    except Exception:
        # 避免日志问题影响主流程
        pass

    # 极简防御：email_to 不是列表直接返回错误，否则 for 会乱掉
    if not isinstance(email_to, list):
        return jsonify({"error": "email_to must be a list"}), 400

    try:
        results = send_email_to_recipients(
            subject=subject,
            text_content=text_content,
            html_content=html_content,
            from_name=from_name,
            recipients=email_to,
        )
    except Exception as e:
        send_logger.error("GLOBAL_FAIL subject=%s error=%s", subject, repr(e))
        return jsonify({"error": "Failed to send emails", "detail": repr(e)}), 500

    return jsonify({"status": "ok", "results": results}), 200


# ==============================
#  入口：启动 Flask + IMAP 线程
# ==============================

def start_imap_thread():
    t = threading.Thread(target=imap_command_loop, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    # 启动 IMAP 命令收取线程
    start_imap_thread()

    # 启动 Flask HTTP 服务（适配 systemd，关闭 debug 和 reloader）
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, use_reloader=False)

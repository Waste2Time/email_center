#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
command_processor.py

负责对从邮件正文中解析出的文本进行：
1. 解析为“命令对象”
2. 通过装饰器注册的命令处理函数进行分发

用法示例（在本文件或其它导入本文件的模块中）：

    from command_processor import command, Command

    @command("reload")
    def reload_something(*args):
        # /reload arg1 arg2 → args == ["arg1", "arg2"]
        print("reloading with args:", args)

当前约定：
- 邮件正文第一行被视为命令行，例如：
    /reload arg1 arg2
- 第一个单词为命令名（可带 /），后面的单词为参数。
- 命令名会被标准化：
    - 去掉开头的 "/"
    - 转成小写
- 注册和查找命令均使用“标准化后的命令名”。
"""
import subprocess
from datetime import datetime
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Callable

import pytz
import requests

# 东八区时区
tz = pytz.timezone("Asia/Shanghai")


# ==============================
#  Command 数据结构
# ==============================

@dataclass
class Command:
    """
    一个简单的命令数据结构：
    - name: 标准化后的命令名，例如 "reload"
    - args: 命令参数列表，例如 ["arg1", "arg2"]
    - raw_text: 整个邮件正文原文，便于调试和扩展
    """
    name: str
    args: List[str]
    raw_text: str


# ==============================
#  命令注册表 & 装饰器
# ==============================

# 命令名（标准化后，如 "reload"） → 处理函数
COMMAND_REGISTRY: Dict[str, Callable[..., Any]] = {}


def normalize_command_name(name: str) -> str:
    """
    命令名标准化：
    - 去掉开头的 "/"
    - 去掉前后空格
    - 转为小写
    """
    return name.strip().lstrip("/").lower()


def command(name: str):
    """
    命令装饰器，用于注册命令处理函数。

    示例：
        @command("reload")
        def reload_something(*args):
            ...

    解析到 "/reload arg1 arg2" 时，会调用：
        reload_something("arg1", "arg2")
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        key = normalize_command_name(name)
        COMMAND_REGISTRY[key] = func
        return func

    return decorator


# ==============================
#  命令解析
# ==============================

def parse_command(body_text: str) -> Optional[Command]:
    """
    从邮件正文中解析出命令。

    当前约定：
    - 使用正文的第一行作为命令行
    - 例如：
        /health
        /reload arg1 arg2
    - 第一个单词视为命令名，其余视为参数。
    - 命令名会被标准化为不带 "/" 的小写形式（如 "reload"）。

    如果正文为空或解析不到有效命令，则返回 None。
    """
    if not body_text:
        return None

    stripped = body_text.strip()
    if not stripped:
        return None

    # 第一行视为命令行
    first_line = stripped.splitlines()[0].strip()
    if not first_line:
        return None

    parts = first_line.split()
    if not parts:
        return None

    raw_name = parts[0]
    args = parts[1:]

    normalized_name = normalize_command_name(raw_name)
    if not normalized_name:
        # 名字为空（例如只有 "/"），视为无效命令
        return None

    return Command(name=normalized_name, args=args, raw_text=body_text)


# ==============================
#  命令处理入口
# ==============================

def handle_command(
    cmd: Command,
    meta: Dict[str, Any],
    logger,
) -> Dict[str, Any]:
    """
    处理解析出来的命令。

    - cmd: 解析后的 Command 对象（name 已标准化，如 "reload"）
    - meta: 一些元信息，例如：
        {
            "subject": ...,
            "from": ...,
            "message_id": ...,
        }
    - logger: 日志记录器（建议传 request_logger）

    当前调用约定：
    - 根据 cmd.name 在 COMMAND_REGISTRY 中查找对应的处理函数 handler
    - 如果存在 handler，则调用：
        handler(*cmd.args)
      也就是说，命令参数列表被展开传入。
    - 如果不存在 handler，则记录 warning 日志。

    返回一个结果 dict，方便日志或上层使用。
    """

    logger.info(
        "COMMAND_HANDLE_START name=%s args=%s meta=%s",
        cmd.name,
        cmd.args,
        meta,
    )

    handler = COMMAND_REGISTRY.get(cmd.name)

    if handler is None:
        # 未找到对应命令处理器
        logger.warning("COMMAND_HANDLER_NOT_FOUND name=%s", cmd.name)
        result = {
            "handled": False,
            "reason": "handler_not_found",
            "command": cmd.name,
            "args": cmd.args,
            "meta": meta,
        }
        logger.info("COMMAND_HANDLE_END result=%s", result)
        return result

    # 调用处理函数。目前约定：handler(*cmd.args)
    # 例如：/reload a b → reload_something("a", "b")
    try:
        handler_result = handler(*cmd.args)
        result = {
            "handled": True,
            "command": cmd.name,
            "args": cmd.args,
            "meta": meta,
            "handler_result": handler_result,
        }
    except Exception as e:
        logger.error(
            "COMMAND_HANDLER_ERROR name=%s error=%s", cmd.name, repr(e)
        )
        result = {
            "handled": False,
            "reason": "handler_exception",
            "command": cmd.name,
            "args": cmd.args,
            "meta": meta,
            "error": repr(e),
        }

    logger.info("COMMAND_HANDLE_END result=%s", result)
    return result


@command("check_campus_ip")
def check_campus_ip(*args):
    """
    访问 WireGuard 内网：
        GET http://10.66.66.2:8081/
    并在 Header 中附带 X-API-KEY
    """

    # 避免循环导入：只在函数执行时获取 API_KEY
    from mail_gateway import API_KEY, send_email_to_recipients

    url = "http://10.66.66.2:8081/"
    headers = {
        "X-API-KEY": API_KEY,
    }

    try:
        resp = requests.get(url, headers=headers, timeout=5)

        # 如果返回非 2xx，抛异常让外层 handle_command 统一记录日志
        resp.raise_for_status()

        data = resp.json()

        subject = data.get("subject", "默认主题")
        text_content = data.get("text_content", "")
        html_content = data.get("html_content")
        from_name = data.get("from_name", "系统通知")
        email_to = data.get("email_to", [])

        results = send_email_to_recipients(
            subject=subject,
            text_content=text_content,
            html_content=html_content,
            from_name=from_name,
            recipients=email_to,
        )

        return {
            "ok": True,
            "status_code": resp.status_code,
            "result": results,
        }

    except Exception as e:
        # 返回错误信息，handle_command 会写日志
        return {
            "ok": False,
            "error": repr(e),
        }

@command("/health")
def self_health(*args):

    from mail_gateway import send_email_to_recipients

    email_to = ["zhangbuqiu@gmail.com"]
    from_name = "Email Service"
    ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    subject = "Email Service - 邮件服务在线检查"
    text_content = (
        f"Email Service 邮件服务在线检查\n\n"
        f"时间: {ts} \n\n"
        f"邮件服务状态: 在线\n"
    )
    html_content = f"""
    <h3>Email Service 邮件服务在线检查<h3>
    <p><b>时间: </b>{ts}</p>
    <p><b>邮件服务状态: </b>在线</p>
    """
    results = send_email_to_recipients(
        subject=subject,
        text_content=text_content,
        html_content=html_content,
        from_name=from_name,
        recipients=email_to,
    )
    return {
        "result": results,
    }

@command("/device_health")
def device_health(*args):

    from mail_gateway import send_email_to_recipients

    target_ip = "10.66.66.2"

    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "1", target_ip],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        ping_ok = (result.returncode == 0)

        if ping_ok:
            email_to = ["zhangbuqiu@gmail.com"]
            from_name = "Email Service"
            subject = "Email Service - 宿舍主机虚拟内网在线检查"
            ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
            text_content = (
                f"Email Service 宿舍主机虚拟内网在线检查\n\n"
                f"时间: {ts} \n\n"
                f"状态: 在线\n"
            )
            html_content = f"""
                <h3>Email Service 宿舍主机虚拟内网在线检查<h3>
                <p><b>时间: </b>{ts}</p>
                <p><b>状态: </b>在线</p>
                """

            results = send_email_to_recipients(
                subject=subject,
                text_content=text_content,
                html_content=html_content,
                from_name=from_name,
                recipients=email_to
            )

            return {
                "ok": True,
                "ping": "success",
                "mail_results": results
            }

        else:
            return {
                "ok": "False",
                "ping": "failed",
                "stdout": result.stdout,
                "stderr": result.stderr
            }

    except Exception as e:
        return {
            "ok": False,
            "error": repr(e),
        }



# ==============================
#  可选：示例命令（你可以删掉或注释掉）
# ==============================

# @command("example")
# def example_command(*args):
#     """
#     示例命令：
#     邮件正文第一行为：
#         /example foo bar
#     则 args == ["foo", "bar"]
#     """
#     print("example_command called with args:", args)
#     # 你可以返回一些值，handle_command 会帮你放到 handler_result 里
#     return {"echo": args}

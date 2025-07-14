import logging
import sys
import os
import json
import threading
from logging import Logger, StreamHandler
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from pythonjsonlogger import jsonlogger
from typing import Literal, Optional

# 全局共享的错误handler，用于所有answer logger
_shared_error_handler = None
_handler_lock = threading.Lock()


class ColoredStructuredFormatter(logging.Formatter):
    """带颜色的结构化日志格式化器"""

    COLORS = {
        'DEBUG': '\033[36m',  # Cyan
        'INFO': '\033[32m',  # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',  # Red
        'RESET': '\033[0m'
    }

    def format(self, record):
        # 对于验证操作，使用特殊格式
        if hasattr(record, 'op_id'):
            op_id = record.op_id
            level_color = self.COLORS.get(record.levelname, '')
            reset = self.COLORS['RESET']

            # 构建主消息 - 移除重复的levelname
            msg_parts = [
                f"{level_color}[{op_id}]{reset}"
            ]

            # 添加节点信息
            if hasattr(record, 'node_id') and record.node_id:
                msg_parts.append(f"Node({record.node_id})")

            # 添加验证类型
            if hasattr(record, 'verify_type'):
                msg_parts.append(f"<{record.verify_type}>")

            # 添加主消息
            msg_parts.append(record.getMessage())

            # 构建详细信息（缩进显示）
            details = []

            if hasattr(record, 'node_desc') and record.node_desc:
                details.append(f"  📋 Description: {record.node_desc}")

            if hasattr(record, 'url') and record.url:
                details.append(f"  🔗 URL: {record.url}")

            if hasattr(record, 'claim_preview'):
                details.append(f"  💬 Claim: {record.claim_preview}")

            if hasattr(record, 'reasoning') and record.reasoning:
                reasoning = record.reasoning
                # if len(reasoning) > 200:
                #     reasoning = reasoning[:200] + "..."
                details.append(f"  💭 Reasoning: {reasoning}")

            if hasattr(record, 'result'):
                result_str = "✅ PASS" if record.result else "❌ FAIL"
                details.append(f"  📊 Result: {result_str}")

            # 组合所有部分
            full_msg = " ".join(msg_parts)
            if details:
                full_msg += "\n" + "\n".join(details)

            return full_msg

        # 对于其他日志，使用标准格式 - 只在ERROR时显示级别
        level_indicator = ""
        if record.levelname == 'ERROR':
            level_indicator = f"{self.COLORS['ERROR']}[ERROR]{self.COLORS['RESET']} "
        elif record.levelname == 'WARNING':
            level_indicator = f"{self.COLORS['WARNING']}[WARN]{self.COLORS['RESET']} "

        return f"{level_indicator}{record.getMessage()}"


class ErrorWithContextFormatter(logging.Formatter):
    """专门用于错误的格式化器，添加上下文信息"""

    COLORS = {
        'ERROR': '\033[31m',  # Red
        'WARNING': '\033[33m',  # Yellow
        'RESET': '\033[0m'
    }

    def format(self, record):
        level_color = self.COLORS.get(record.levelname, '')
        reset = self.COLORS['RESET']

        # 构建上下文信息
        context_parts = []

        # 添加agent和answer信息
        if hasattr(record, 'agent_name') and record.agent_name:
            context_parts.append(f"Agent:{record.agent_name}")
        if hasattr(record, 'answer_name') and record.answer_name:
            context_parts.append(f"Answer:{record.answer_name}")
        if hasattr(record, 'node_id') and record.node_id:
            context_parts.append(f"Node:{record.node_id}")
        if hasattr(record, 'op_id') and record.op_id:
            context_parts.append(f"Op:{record.op_id}")

        context_str = " | ".join(context_parts)
        context_prefix = f"[{context_str}] " if context_str else ""

        return f"{level_color}[{record.levelname}]{reset} {context_prefix}{record.getMessage()}"


class HumanReadableFormatter(logging.Formatter):
    """人类可读的文件日志格式，保留emoji"""

    def format(self, record):
        # 时间戳 - 精确到秒
        timestamp = self.formatTime(record, '%Y-%m-%d %H:%M:%S')

        # 基本信息 - 只在重要级别显示level
        level_prefix = ""
        if record.levelname in ['ERROR', 'WARNING']:
            level_prefix = f"[{record.levelname}] "

        base_info = f"[{timestamp}] {level_prefix}{record.getMessage()}"

        # 添加结构化信息
        extras = []
        skip_fields = {
            'name', 'msg', 'args', 'levelname', 'levelno', 'pathname',
            'filename', 'module', 'lineno', 'funcName', 'created',
            'msecs', 'relativeCreated', 'thread', 'threadName',
            'processName', 'process', 'getMessage', 'exc_info',
            'exc_text', 'stack_info', 'message'
        }

        for key, value in record.__dict__.items():
            if key not in skip_fields and value is not None:
                # 特殊处理一些字段的显示
                if key == 'final_score' and isinstance(value, (int, float)):
                    extras.append(f"score={value}")
                elif key == 'agent_name':
                    extras.append(f"agent={value}")
                elif key == 'node_id':
                    extras.append(f"node={value}")
                elif key == 'op_id':
                    extras.append(f"op={value}")
                else:
                    extras.append(f"{key}={value}")

        if extras:
            base_info += f" | {' | '.join(extras)}"

        return base_info


class CompactJsonFormatter(jsonlogger.JsonFormatter):
    """精简的 JSON 格式化器，移除冗余字段"""

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)

        # 移除不需要的字段
        fields_to_remove = ['name', 'levelname']
        for field in fields_to_remove:
            log_record.pop(field, None)

        # 简化时间格式到秒
        if 'asctime' in log_record:
            try:
                asctime = log_record['asctime']
                if ',' in asctime:
                    log_record['asctime'] = asctime.split(',')[0]
            except:
                pass


def _get_shared_error_handler() -> StreamHandler:
    """获取或创建全局共享的错误handler"""
    global _shared_error_handler

    with _handler_lock:
        if _shared_error_handler is None:
            _shared_error_handler = StreamHandler(sys.stderr)  # 使用stderr显示错误
            _shared_error_handler.setFormatter(ErrorWithContextFormatter())
            _shared_error_handler.setLevel(logging.ERROR)  # 只显示ERROR级别

    return _shared_error_handler


def create_logger(
        lgr_nm: str,
        log_folder: str,
        enable_console: bool = True,
        file_format: Literal["jsonl", "readable", "both"] = "both",
        enable_shared_errors: bool = False  # 新增参数
) -> tuple[Logger, str]:
    """
    创建独立的logger实例，支持多种文件格式

    Args:
        lgr_nm: logger名称
        log_folder: 日志文件夹
        enable_console: 是否启用控制台输出
        file_format: 文件日志格式
        enable_shared_errors: 是否将ERROR级别的日志输出到共享的terminal

    Returns:
        (logger实例, 时间戳)
    """
    if not os.path.exists(log_folder):
        os.makedirs(log_folder)

    current_time = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 创建唯一的logger名称，避免重复
    unique_logger_name = f"{lgr_nm}_{current_time}_{id(log_folder)}"

    # 检查logger是否已存在，如果存在则先清理
    existing_logger = logging.getLogger(unique_logger_name)
    if existing_logger.handlers:
        for handler in existing_logger.handlers[:]:
            existing_logger.removeHandler(handler)
            handler.close()

    # 创建新的logger
    new_logger = logging.getLogger(unique_logger_name)
    new_logger.setLevel(logging.DEBUG)
    new_logger.propagate = False

    # 文件handlers
    if file_format in ["jsonl", "both"]:
        # JSON Lines 格式
        jsonl_file = os.path.join(log_folder, f"{current_time}_{lgr_nm}.jsonl")
        jsonl_handler = TimedRotatingFileHandler(
            jsonl_file,
            when="D",
            backupCount=14,
            encoding="utf-8"
        )
        jsonl_formatter = CompactJsonFormatter('%(asctime)s %(message)s')
        jsonl_handler.setFormatter(jsonl_formatter)
        jsonl_handler.setLevel(logging.DEBUG)
        new_logger.addHandler(jsonl_handler)

    if file_format in ["readable", "both"]:
        # 人类可读格式
        readable_file = os.path.join(log_folder, f"{current_time}_{lgr_nm}.log")
        readable_handler = TimedRotatingFileHandler(
            readable_file,
            when="D",
            backupCount=14,
            encoding="utf-8"
        )
        readable_formatter = HumanReadableFormatter()
        readable_handler.setFormatter(readable_formatter)
        readable_handler.setLevel(logging.DEBUG)
        new_logger.addHandler(readable_handler)

    # 控制台handler - 使用彩色结构化格式
    if enable_console:
        console_handler = StreamHandler(sys.stdout)
        console_handler.setFormatter(ColoredStructuredFormatter())
        console_handler.setLevel(logging.INFO)
        new_logger.addHandler(console_handler)

    # 共享错误handler - 用于在并行执行时显示错误
    if enable_shared_errors:
        shared_error_handler = _get_shared_error_handler()
        new_logger.addHandler(shared_error_handler)

    return new_logger, current_time


def create_sub_logger(parent_logger: Logger, sub_name: str) -> Logger:
    """
    Create sublogger based on parent logger, inherit parent logger's handlers
    Used to create hierarchical logs within the same evaluation
    """
    parent_name = parent_logger.name
    sub_logger_name = f"{parent_name}.{sub_name}"

    sub_logger = logging.getLogger(sub_logger_name)
    sub_logger.setLevel(parent_logger.level)
    sub_logger.propagate = True  # Allow propagation to parent logger

    return sub_logger


def cleanup_logger(logger: Logger) -> None:
    """清理logger的所有handlers（但不清理共享的错误handler）"""
    global _shared_error_handler

    for handler in logger.handlers[:]:
        # 不要清理共享的错误handler
        if handler is not _shared_error_handler:
            logger.removeHandler(handler)
            handler.close()
        else:
            logger.removeHandler(handler)  # 只移除，不关闭


def cleanup_shared_error_handler():
    """在程序结束时清理共享的错误handler"""
    global _shared_error_handler

    with _handler_lock:
        if _shared_error_handler is not None:
            _shared_error_handler.close()
            _shared_error_handler = None


# 使用示例和说明
"""
在 evaluation runner 中的使用方法：

1. 主logger - 正常的控制台输出：
   main_logger, timestamp = create_logger("main_task", log_folder, enable_console=True)

2. 各个answer的logger - 错误会显示到terminal：
   logger, timestamp = create_logger(
       log_tag, 
       str(log_dir), 
       enable_console=False,  # 不启用常规控制台输出
       enable_shared_errors=True  # 启用共享错误输出
   )

这样的效果：
- 主要的进度信息在主terminal显示
- 各个answer的ERROR级别信息也会显示到terminal（带上下文）
- 所有详细日志仍然保存到各自的文件中

终端输出示例：
🚀 Starting concurrent evaluation of 10 answers
👉 Processing human/answer_1.md
[ERROR] [Agent:human | Answer:answer_1.md | Node:price_check] Failed to verify price claim
👉 Processing openai_deep_research/answer_1.md
✅ Successfully evaluated human/answer_1.md
"""
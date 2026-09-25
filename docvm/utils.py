# -*- coding: utf-8 -*-
"""通用工具：哈希、格式化、隐藏属性等。"""

from __future__ import annotations

import hashlib
from datetime import datetime

from .paths import IS_WINDOWS


def compute_file_hash(filepath: str) -> str:
    """计算文件 MD5 哈希。"""
    h = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def format_file_size(size_bytes: int | float) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def format_timestamp(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(ts)


def set_hidden(filepath: str) -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(filepath), 0x02)
    except Exception:
        pass

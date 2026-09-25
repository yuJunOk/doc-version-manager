# -*- coding: utf-8 -*-
"""路径与运行环境：软件目录、静态资源、端口探测。"""

from __future__ import annotations

import os
import platform
import socket
import sys

IS_WINDOWS = platform.system() == "Windows"

STATIC_MIME = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def get_package_dir() -> str:
    """docvm 包目录。"""
    return os.path.dirname(os.path.abspath(__file__))


def get_app_dir() -> str:
    """软件所在目录（打包 exe 为可执行文件目录，源码为项目根目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # 源码：项目根 = docvm 包的上一级
    return os.path.dirname(get_package_dir())


def get_static_dir() -> str:
    """前端静态资源目录（兼容 PyInstaller --onefile 的 _MEIPASS）。"""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "docvm", "web", "static")
    return os.path.join(get_package_dir(), "web", "static")


def get_template_path() -> str:
    """主页面 HTML 模板路径。"""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "docvm", "web", "templates", "index.html")
    return os.path.join(get_package_dir(), "web", "templates", "index.html")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

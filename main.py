#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DocVM — 本地文档版本管理工具
入口：python main.py

运行后驻留系统托盘：打开浏览器 / 结束运行。
再次启动时若已有实例，仅打开浏览器定位到页面。
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser

# 保证直接运行 main.py（任意 cwd / IDE）时能找到本地包
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from docvm.manager import VersionManager
from docvm.paths import IS_WINDOWS
from docvm.server import create_server

APP_HOST = "127.0.0.1"
APP_PORT = 404


def _app_url() -> str:
    return f"http://{APP_HOST}:{APP_PORT}"


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _is_instance_running(timeout: float = 0.6) -> bool:
    """探测本机固定端口上是否已有 DocVM 在响应。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        _app_url() + "/",
        headers={"User-Agent": "DocVM-launcher"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _port_busy_error() -> None:
    msg = (
        f"端口 {APP_PORT} 已被其他程序占用，无法启动 DocVM。\n"
        f"请关闭占用该端口的程序后重试。"
    )
    if IS_WINDOWS:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, msg, "文档版本管理工具 - 启动失败", 0x10)
    else:
        print(msg)


def main() -> None:
    url = _app_url()

    # 已运行：只打开浏览器，不启第二实例
    if _is_instance_running():
        print(f"DocVM 已在运行，打开浏览器: {url}")
        _open_browser(url)
        return

    vm = VersionManager()

    try:
        server = create_server(vm, host=APP_HOST, port=APP_PORT)
    except OSError:
        # 启动竞态：探测后又被占上了
        if _is_instance_running():
            print(f"DocVM 已在运行，打开浏览器: {url}")
            _open_browser(url)
            return
        _port_busy_error()
        sys.exit(1)

    print(f"DocVM 已启动（托盘运行）: {url}")

    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()

    def shutdown():
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass

    try:
        from docvm.tray import run_tray

        run_tray(url, on_quit=shutdown, open_on_start=True)
    except RuntimeError as e:
        # 无托盘依赖时降级：仍开浏览器，控制台 Ctrl+C 结束
        print(str(e))
        import time

        time.sleep(0.8)
        _open_browser(url)
        try:
            http_thread.join()
        except KeyboardInterrupt:
            pass
    finally:
        shutdown()


if __name__ == "__main__":
    main()

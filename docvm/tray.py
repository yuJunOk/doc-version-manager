# -*- coding: utf-8 -*-
"""系统托盘：打开浏览器 / 结束运行。"""

from __future__ import annotations

import threading
import webbrowser
from typing import Callable


def _make_icon_image():
    """生成简易托盘图标（深蓝底 + 白色文档形）。"""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 圆角底
    draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=12, fill=(30, 58, 95, 255))
    # 文档
    draw.rounded_rectangle((18, 12, 46, 52), radius=3, fill=(255, 255, 255, 255))
    draw.polygon([(34, 12), (46, 12), (46, 24)], fill=(232, 240, 254, 255))
    # 行线
    for y in (28, 34, 40):
        draw.line((24, y, 40, y), fill=(47, 111, 237, 255), width=2)
    return img


def run_tray(
    url: str,
    on_quit: Callable[[], None],
    *,
    open_on_start: bool = True,
) -> None:
    """
    阻塞运行托盘图标。

    菜单：
    - 打开 DocVM
    - 结束运行
    左键双击 / 单击（视平台）：打开浏览器
    """
    try:
        import pystray
        from pystray import MenuItem as Item
    except ImportError as e:
        raise RuntimeError(
            "缺少依赖 pystray，请执行: pip install pystray pillow"
        ) from e

    def open_ui(icon=None, item=None):
        webbrowser.open(url)

    def quit_app(icon, item=None):
        try:
            icon.visible = False
            icon.stop()
        except Exception:
            pass
        try:
            on_quit()
        except Exception:
            pass

    menu = pystray.Menu(
        Item("打开 DocVM", open_ui, default=True),
        Item("结束运行", quit_app),
    )
    icon = pystray.Icon(
        "DocVM",
        _make_icon_image(),
        f"DocVM 文档版本管理\n{url}",
        menu,
    )

    if open_on_start:
        def _delayed_open():
            import time

            time.sleep(0.8)
            webbrowser.open(url)

        threading.Thread(target=_delayed_open, daemon=True).start()

    # Windows 上 run() 阻塞直到 stop
    icon.run()

# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
ROOT = os.path.dirname(SPEC_DIR)

datas = [
    (os.path.join(ROOT, "docvm", "web", "templates"), os.path.join("docvm", "web", "templates")),
    (os.path.join(ROOT, "docvm", "web", "static"), os.path.join("docvm", "web", "static")),
]
binaries = []
hiddenimports = [
    "win32com.client",
    "docvm",
    "docvm.repo",
    "docvm.manager",
    "docvm.server",
    "docvm.paths",
    "docvm.utils",
    "docvm.preview",
    "docvm.tray",
    "pystray",
    "PIL",
]
for pkg in ("docx", "lxml", "mammoth", "pywin32", "pystray", "PIL"):
    tmp = collect_all(pkg)
    datas += tmp[0]
    binaries += tmp[1]
    hiddenimports += tmp[2]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DocVersionManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

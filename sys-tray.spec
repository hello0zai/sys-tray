# -*- mode: python ; coding: utf-8 -*-
import os

# ---------------------------------------------------------
# Data files
# ---------------------------------------------------------
# icon.ico and .env live inside the sys_tray/ package folder, so the
# paths must include it. With contents_directory="." both end up next
# to sys-tray.exe, which is where the code looks for them.
# NOTE: bundling .env bakes your SERVER / SERVER_ENDPOINT values into the
# build output. If those differ per machine, remove the .env entry below
# and copy a .env next to sys-tray.exe by hand instead.
PKG_DIR = os.path.join(SPECPATH, "sys_tray")
ICON_FILE = os.path.join(PKG_DIR, "icon.ico")
ENV_FILE = os.path.join(PKG_DIR, ".env")

icon = 'icons/icon.ico'
binaries = []

hiddenimports = [
    "win32gui",
    "win32con",
    "win32gui_struct",
    "pywintypes",
    "win32api",
    "psutil",
    "dotenv",
]

# ---------------------------------------------------------
# Analysis
# ---------------------------------------------------------
block_cipher = None

a = Analysis(
    ["sys_tray/__main__.py"],
    pathex=[SPECPATH],          # lets `import sys_tray...` resolve
    binaries=binaries,
    datas=[
        ('icons', 'icons'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # The tray app uses none of these; excluding keeps the build small.
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide6",
        "matplotlib",
        "numpy",
        "scipy",
        "torch",
        "IPython",
        "ipdb",
        "pdbpp",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

# ---------------------------------------------------------
# PYZ
# ---------------------------------------------------------
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------
# EXE
# ---------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="sys-tray",
    icon=icon,
    debug=False,
    strip=False,
    upx=False,        # UPX can corrupt pywin32 DLLs; the size saving is not worth it here
    console=False,    # tray app: no console window. Set True temporarily to debug.
    contents_directory=".",
)

# ---------------------------------------------------------
# COLLECT
# ---------------------------------------------------------
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="sys-tray",
)

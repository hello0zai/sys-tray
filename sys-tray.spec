import os


# ---------------------------------------------------------
# Merge package data
# ---------------------------------------------------------

datas = []

binaries = []

hiddenimports = []


# ---------------------------------------------------------
# Analysis
# ---------------------------------------------------------

block_cipher = None

a = Analysis(
    ["sys_tray/__main__.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'PyQt5', 
        'PyQt6',
        'PySide6.QtWebChannel', 
        'PySide6.QtWebEngineCore', 
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebContext',
        'PySide6.QtNetwork',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtSvg',
        'PySide6.QtWidgets',
        'PySide6.QtSvg',
        'PySide6.QtQuick',
        'PySide6.QtQml',
        "tkinter",
        "torch.utils.benchmark",
        "torch.testing._internal",
        "matplotlib.tests",
        "numpy.tests",
        "scipy.tests",
        "pdbpp",
        "IPython",
        "ipdb",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

# ---------------------------------------------------------
# PYZ
# ---------------------------------------------------------

pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher,
)


# ---------------------------------------------------------
# EXE
# ---------------------------------------------------------

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="sys-tray",
    debug=False,
    strip=False,
    upx=True,
    console=True,
    contents_directory=".",
    upx_exclude=[
        'vcruntime140.dll',
        'ucrtbase.dll',
        'python3.dll',
        'python311.dll',
    ],
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
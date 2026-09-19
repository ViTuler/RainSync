# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the sync tool.

Build from the project root:

    pyinstaller --noconfirm main.spec

``console=True`` is deliberate and must stay that way:

* ``watch``     runs in the terminal that started it, keeps printing, and is
                stopped with Ctrl+C.
* any ``-d``   (``watch -d``, ``sync -d``) re-launches itself with
                CREATE_NO_WINDOW, so it survives the terminal and does not
                open a second console.

With ``--noconsole`` Windows treats the exe as a GUI app: the shell stops
waiting for it and stdout/stderr are dropped, so ``watch`` looks like it
exits immediately and its output is invisible. Do not set console=False.
"""

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="main",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

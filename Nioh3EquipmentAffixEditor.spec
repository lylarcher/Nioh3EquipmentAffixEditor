# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the single-file Nioh 3 Equipment Affix Editor executable.

Layout produced by this spec:

* ``Nioh3EquipmentAffixEditor.exe`` -- one file, no ``.py`` sources, windowed
  subsystem so ``--version`` / ``list`` / ``edit`` work from a terminal while the
  no console window is created at all (the GUI needs none), and a CLI subcommand run
from a terminal attaches to that terminal so its output stays visible.
  Explorer, the taskbar and the window title bar take their icon from
  ``assets/app.ico`` (all nine sizes are compiled into the PE resources).
* Inside it: the Python runtime, ``nioh3_equipment_affix_editor`` (including the
  generated ``_buildinfo``) and ``app-payload.zip``.

``app-payload.zip`` is *not* read from the temporary extraction directory at
run time: :mod:`nioh3_equipment_affix_editor.bootstrap` unpacks it next to the
executable on first run, so the configuration file, the affix catalogue and the
bundled crypto helper end up as ordinary files the user can edit.

Build it through ``build.ps1`` (which generates the payload and the build
identity first):

    pyinstaller --noconfirm --clean --distpath dist --workpath build/pyi \\
        Nioh3EquipmentAffixEditor.spec
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).resolve()  # noqa: F821 - provided by PyInstaller
PAYLOAD = PROJECT_ROOT / "build" / "app-payload.zip"
ENTRY_POINT = PROJECT_ROOT / "launch_editor.py"
ICON = PROJECT_ROOT / "assets" / "app.ico"

if not PAYLOAD.is_file():
    raise SystemExit(
        f"缺少载荷文件 {PAYLOAD}；请先运行 tools/make_payload.py 或使用 build.ps1"
    )

if not ICON.is_file():
    raise SystemExit(
        f"缺少图标 {ICON}；请先运行 python tools/make_icon.py"
    )

#: Ship the generated build identity even though it is imported defensively.
HIDDEN_IMPORTS = ["nioh3_equipment_affix_editor._buildinfo"]

#: Nothing here is needed at run time and it keeps the image smaller.
EXCLUDES = [
    "pydoc_data", "unittest", "pdb", "doctest", "lib2to3", "distutils",
    "setuptools", "pip", "PyInstaller", "test", "tests", "tools",
]

a = Analysis(  # noqa: F821 - PyInstaller injects Analysis/PYZ/EXE
    [str(ENTRY_POINT)],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[(str(PAYLOAD), ".")],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Nioh3EquipmentAffixEditor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    #: Windowed subsystem: a frozen build must never open a console window the
    #: user has to look at (or can accidentally kill).  The CLI path attaches to
    #: the terminal that launched it instead — see console.attach_parent_console().
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
)

# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — single-folder distributable for prompt-enhancer.

Build with:
    pyinstaller packaging/prompt-enhancer.spec --clean

Outputs:
    dist/prompt-enhancer/prompt-enhancer.exe       (windowed launcher → enhancer ui)
    dist/prompt-enhancer/...                        (bundled deps)

Inno Setup is invoked from packaging/installer.iss to wrap this folder
into a signed prompt-enhancer-setup.exe.

Notes:
* The schema.sql sibling of persistence/db.py is shipped via ``datas``.
* NiceGUI ships its own static assets — we let PyInstaller's hooks
  collect them automatically.
* httpx + h2 + h11 + sqlite3 are all stdlib-or-pure-python; no special
  hooks needed.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

datas = []
datas += collect_data_files("nicegui")
# package_data declared in pyproject.toml is honored only on installs;
# include it explicitly here for the bundled build.
datas += [
    ("../src/enhancer/persistence/schema.sql", "enhancer/persistence"),
]

hiddenimports = []
hiddenimports += collect_submodules("nicegui")

a = Analysis(
    ["entrypoint.py"],
    pathex=["../src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="prompt-enhancer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,           # windowed launcher; CLI users invoke via separate console exe
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,               # add packaging/icon.ico in v0.2
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="prompt-enhancer",
)

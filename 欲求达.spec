# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["run_software.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("software_app/crawlers/twitter", "software_app/crawlers/twitter"),
        ("software_app/crawlers/pixiv", "software_app/crawlers/pixiv"),
        ("software_app/crawlers/jmcomic", "software_app/crawlers/jmcomic"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

common_kwargs = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

gui_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="欲求达",
    console=False,
    **common_kwargs,
)

cli_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="欲求达-cli",
    console=True,
    **common_kwargs,
)

coll = COLLECT(
    gui_exe,
    cli_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="欲求达",
)

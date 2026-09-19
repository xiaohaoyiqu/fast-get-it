# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

import imageio_ffmpeg


root = Path(SPECPATH)
app_icon = root / "packaging" / "欲求达.ico"
if not app_icon.is_file():
    raise SystemExit("Missing packaging/欲求达.ico; run scripts/build_windows_icon.py first")
datas = []
binaries = [(str(Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()), "tools")]
hiddenimports = []

documents = root / "文档"
if documents.is_dir():
    datas.append((str(documents), "文档"))

aria2_archive = root / "aria2-1.37.0-win-64bit-build1.zip"
if not aria2_archive.is_file():
    raise SystemExit("缺少 aria2-1.37.0-win-64bit-build1.zip；请先放到项目根目录")
datas.append((str(aria2_archive), "."))

a = Analysis(
    [str(root / "run_software.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(root / "packaging" / "runtime_paths.py")],
    excludes=[
        "torch", "torchvision", "torchaudio", "tensorflow", "jax", "jaxlib",
        "scipy", "pandas", "matplotlib", "sklearn", "cv2", "pygame",
        "IPython", "notebook", "jupyter", "moviepy", "imageio", "imageio_ffmpeg",
    ],
    noarchive=False,
    optimize=0,
)
# ChromeDriver 必须按最终用户电脑上的 Chrome 版本获取，绝不把构建机驱动带入发行包。
a.binaries = [item for item in a.binaries if Path(item[0]).name.casefold() not in {"chromedriver", "chromedriver.exe"}]
a.datas = [item for item in a.datas if Path(item[0]).name.casefold() not in {"chromedriver", "chromedriver.exe"}]
pyz = PYZ(a.pure)

common = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
    icon=str(app_icon),
    console=False,
    **common,
)

cli_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="欲求达-cli",
    icon=str(app_icon),
    console=True,
    **common,
)

bundle = COLLECT(
    gui_exe,
    cli_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="欲求达",
)

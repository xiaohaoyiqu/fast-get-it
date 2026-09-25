"""Bundle only the ImageIO plugins used by MoviePy for media conversion."""

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("imageio", subdir="resources")
hiddenimports = [
    "imageio.plugins.ffmpeg",
    "imageio.plugins.pillow",
    "imageio.plugins.pillow_legacy",
]

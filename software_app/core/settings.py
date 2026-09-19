from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])).resolve()
INSTALL_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PROJECT_ROOT


def runtime_root(*, frozen: bool | None = None) -> Path:
    override = str(os.environ.get("YUQIUDA_HOME") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if not is_frozen:
        return PROJECT_ROOT
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return (base / "YuqiuDa").resolve()


RUNTIME_ROOT = runtime_root()
DATA_DIR = RUNTIME_ROOT / "data"
SOFTWARE_DATA_DIR = DATA_DIR / "software_app"
PLUGINS_DIR = SOFTWARE_DATA_DIR / "plugins"
JSON_DATA_DIR = SOFTWARE_DATA_DIR / "json"
TWITTER_DATA_DIR = SOFTWARE_DATA_DIR / "twitter"
PIXIV_DATA_DIR = JSON_DATA_DIR / "pixiv"
JMCOMIC_DATA_DIR = SOFTWARE_DATA_DIR / "jmcomic"
INSTAGRAM_DATA_DIR = SOFTWARE_DATA_DIR / "instagram"
EHENTAI_DATA_DIR = SOFTWARE_DATA_DIR / "ehentai"
GOOGLE_IMAGE_DATA_DIR = SOFTWARE_DATA_DIR / "google_image"
BROWSER_TOOLS_DIR = SOFTWARE_DATA_DIR / "browser"
SOFTWARE_TOOLS_DIR = SOFTWARE_DATA_DIR / "tools"
MANUAL_BROWSER_PROFILE_ROOT = BROWSER_TOOLS_DIR / "manual_login_profiles"
DB_PATH = SOFTWARE_DATA_DIR / "content_downloader_v2.db"

ORIGINAL_TWITTER_ROOT = PROJECT_ROOT / "推特爬虫"
TWITTER_ROOT = PROJECT_ROOT / "software_app" / "crawlers" / "twitter"
PIXIV_ROOT = PROJECT_ROOT / "Pixiv爬虫"
JMCOMIC_ROOT = PROJECT_ROOT / "JMComic爬虫"
GOOGLE_IMAGE_ROOT = PROJECT_ROOT / "谷歌图片搜索工具"
_OUTPUT_OVERRIDE = str(os.environ.get("YUQIUDA_OUTPUT_DIR") or "").strip()
DEFAULT_OUTPUT_DIR = (
    Path(_OUTPUT_OVERRIDE).expanduser().resolve()
    if _OUTPUT_OVERRIDE
    else (Path.home() / "Downloads" / "欲求达" if getattr(sys, "frozen", False) else DATA_DIR / "downloads")
)


def ensure_data_dirs() -> None:
    SOFTWARE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DATA_DIR.mkdir(parents=True, exist_ok=True)
    TWITTER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PIXIV_DATA_DIR.mkdir(parents=True, exist_ok=True)
    JMCOMIC_DATA_DIR.mkdir(parents=True, exist_ok=True)
    INSTAGRAM_DATA_DIR.mkdir(parents=True, exist_ok=True)
    EHENTAI_DATA_DIR.mkdir(parents=True, exist_ok=True)
    GOOGLE_IMAGE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    SOFTWARE_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    MANUAL_BROWSER_PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class ScriptPaths:
    twitter_root: Path = TWITTER_ROOT
    pixiv_root: Path = PIXIV_ROOT
    jmcomic_root: Path = JMCOMIC_ROOT
    google_image_root: Path = GOOGLE_IMAGE_ROOT






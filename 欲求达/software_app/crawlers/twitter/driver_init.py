import json
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from selenium.webdriver.chrome.webdriver import WebDriver as ChromeWebDriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from software_app.core.browser_driver import ensure_chromedriver  # noqa: E402 - direct script bootstrap
from software_app.core.browser_process import BrowserProcessError, ManagedChromeService as ChromeService  # noqa: E402
from software_app.core.settings import BROWSER_TOOLS_DIR  # noqa: E402

SKEB_BUTTON_EXTENSION_ID = "onjegdbehgoamaiochjfnkokondpgoim"


def find_skeb_button_extension(user_data_roots: list[Path] | None = None) -> Path | None:
    """Locate an installed, unpacked copy of the official Skeb Button extension."""
    if user_data_roots is None:
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or "")
        user_data_roots = [
            local_app_data / "Google" / "Chrome" / "User Data",
            local_app_data / "Microsoft" / "Edge" / "User Data",
        ]
    candidates: list[Path] = []
    for root in user_data_roots:
        if not root.is_dir():
            continue
        try:
            manifests = root.glob(f"*/Extensions/{SKEB_BUTTON_EXTENSION_ID}/*/manifest.json")
            for manifest_file in manifests:
                try:
                    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if str(manifest.get("name") or "") == "Skeb Button" and (manifest_file.parent / "index.js").is_file():
                    candidates.append(manifest_file.parent)
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _profile_identity(path):
    info = Path(path).lstat()
    return info.st_dev, info.st_ino


def _cleanup_temporary_profile(profile_dir, identity):
    path = Path(profile_dir)
    root = (Path(BROWSER_TOOLS_DIR) / "session_profiles").resolve()
    if path.parent.resolve() != root or not path.name.startswith("twitter_"):
        raise BrowserProcessError("临时浏览器目录不在本次受管理范围内，未删除")
    path = root / path.name
    deadline = time.monotonic() + 2
    while True:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or (info.st_dev, info.st_ino) != identity):
            raise BrowserProcessError("临时浏览器目录身份已变化，保留现有内容")
        try:
            shutil.rmtree(path)
            return
        except OSError as error:
            if time.monotonic() >= deadline:
                raise BrowserProcessError("临时浏览器资料仍被占用，已保留；请退出相关进程后检查") from error
            time.sleep(0.1)


class TemporaryProfileChrome(ChromeWebDriver):
    """Chrome session backed by an isolated, disposable user-data directory."""

    def __init__(self, *args, profile_dir: Path, skeb_extension_path: Path | None = None, **kwargs):
        self._temporary_profile_dir = Path(profile_dir)
        self._temporary_profile_identity = _profile_identity(profile_dir)
        self._skeb_extension_path = Path(skeb_extension_path) if skeb_extension_path else None
        try:
            super().__init__(*args, **kwargs)
        except BaseException:
            try:
                _cleanup_temporary_profile(self._temporary_profile_dir, self._temporary_profile_identity)
            except Exception:
                pass
            raise

    def quit(self):
        try:
            return super().quit()
        finally:
            _cleanup_temporary_profile(self._temporary_profile_dir, self._temporary_profile_identity)


def short_error(error):
    message = str(error).strip().splitlines()
    if message:
        return message[0]
    return error.__class__.__name__


def normalize_cookie_for_selenium(cookie):
    cookie = cookie.copy()

    if isinstance(cookie.get("expiry"), float):
        cookie["expiry"] = int(cookie["expiry"])

    if "expirationDate" in cookie and "expiry" not in cookie:
        try:
            cookie["expiry"] = int(float(cookie.pop("expirationDate")))
        except (TypeError, ValueError):
            cookie.pop("expirationDate", None)

    same_site = cookie.get("sameSite")
    if same_site:
        same_site = str(same_site).strip().lower()
        same_site_map = {
            "lax": "Lax",
            "strict": "Strict",
            "none": "None",
            "no_restriction": "None",
            "unspecified": None,
        }
        same_site = same_site_map.get(same_site)
        if same_site:
            cookie["sameSite"] = same_site
        else:
            cookie.pop("sameSite", None)

    for unsupported_key in ("hostOnly", "session", "storeId", "id"):
        cookie.pop(unsupported_key, None)

    return cookie


def initialize_driver(proxy_url=""):
    # 初始化Chrome浏览器驱动
    options = ChromeOptions()
    endpoint = str(proxy_url or "").strip()
    if endpoint:
        if "://" not in endpoint:
            endpoint = f"http://{endpoint}"
        # Chrome's SOCKS5 proxy resolves target hostnames remotely already.
        if endpoint.lower().startswith("socks5h://"):
            endpoint = "socks5://" + endpoint[len("socks5h://"):]
        options.add_argument(f"--proxy-server={endpoint}")
    options.set_capability(
        "goog:loggingPrefs", {"performance": "ALL"}
    )
    # # 启用无头浏览器模式
    # options.add_argument("--headless")
    # options.add_argument("--disable-gpu")  # 禁用GPU加速
    # options.add_argument("--window-size=1920x1080")  # 设置窗口大小，防止某些元素不可见

    # Preserve Chrome's certificate validation; proxy trust belongs in the OS/browser trust store.
    # -------------------------- 防检测配置（避免Twitter反爬） --------------------------
    options.add_argument('--no-sandbox')  # 禁用沙箱模式
    options.add_argument('--disable-dev-shm-usage')  # 解决/dev/shm内存不足
    options.add_argument('--disable-blink-features=AutomationControlled')  # 隐藏自动化标识
    options.add_experimental_option('useAutomationExtension', False)  # 禁用自动化扩展

    # 不启用无头模式时开启，调式代码或者看报错的时候使用。
    options.add_argument("window-position=660,0")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-logging")
    options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])  # 关闭自动化和日志提示

    profile_root = Path(BROWSER_TOOLS_DIR) / "session_profiles"
    profile_root.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(tempfile.mkdtemp(prefix="twitter_", dir=profile_root))
    profile_identity = _profile_identity(profile_dir)
    options.add_argument(f"--user-data-dir={profile_dir}")
    skeb_extension_path = find_skeb_button_extension()
    if skeb_extension_path:
        options.add_argument(f"--load-extension={skeb_extension_path}")

    try:
        driver_path = ensure_chromedriver()
        service = ChromeService(str(driver_path), log_output=os.devnull) if driver_path else ChromeService(log_output=os.devnull)
        driver = TemporaryProfileChrome(
            service=service, options=options, profile_dir=profile_dir, skeb_extension_path=skeb_extension_path,
        )
    except BaseException:
        try:
            _cleanup_temporary_profile(profile_dir, profile_identity)
        except Exception:
            pass
        raise
    try:
        driver.set_page_load_timeout(5)
    except BaseException:
        try:
            driver.quit()
        except Exception:
            pass
        raise
    return driver


def cookies_web(driver, cookie_path):
    # 设置浏览器的cookie
    print("设置cookie中.....")
    with open(cookie_path, 'r', encoding='utf-8') as f:
        cookies = json.load(f)
    cookie_names = {cookie.get("name") for cookie in cookies}
    if not {"auth_token", "ct0"}.issubset(cookie_names):
        print("警告：cookie文件中未检测到完整登录cookie(auth_token/ct0)")

    added_count = 0
    for cookie in cookies:
        cookie = normalize_cookie_for_selenium(cookie)
        try:
            driver.add_cookie(cookie)
            added_count += 1
        except Exception as error:
            print(f'跳过无效cookie: {cookie.get("name")} ({short_error(error)})')
    print(f"已设置 {added_count}/{len(cookies)} 个 cookie")


def get_twitter_name(driver):
    # 获取Twitter用户的名称
    name = driver.find_elements(By.XPATH, '//*[@id="react-root"]/div/div/div[2]/main/div/div/div/div/div/div[1]/div[1]/div/div/div/div/div/div[2]/div/h2/div/div/div/div/span[1]/span/span[1]')
    folder = name[0].text.replace('/', '-')  # 防止斜杠视作创建多级文件夹

    return folder




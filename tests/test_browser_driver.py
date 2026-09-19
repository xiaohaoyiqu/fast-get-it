from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from software_app.core.browser_driver import (
    BrowserDriverError,
    chromedriver_status,
    download_url,
    ensure_chromedriver,
    find_chromedriver_download_url,
    versions_compatible,
)
from software_app.core.settings import runtime_root


class BrowserDriverTests(unittest.TestCase):
    def test_versions_match_by_major_version(self):
        self.assertTrue(versions_compatible("122.0.6182.0", "122.0.6229.0"))
        self.assertFalse(versions_compatible("123.0.1.0", "122.0.6229.0"))
        self.assertFalse(versions_compatible("123.0.1.0", ""))

    def test_existing_mismatch_is_reported_and_auto_downloaded(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            old_driver = Path(directory) / "chromedriver.exe"
            old_driver.write_bytes(b"old")
            with patch("software_app.core.browser_driver.find_existing_driver", return_value=(old_driver, "global")), patch(
                "software_app.core.browser_driver.chrome_version", return_value="123.0.1.0"
            ), patch("software_app.core.browser_driver.chromedriver_version", return_value="122.0.1.0"):
                status = chromedriver_status(auto_download=False)
            self.assertEqual(status.source, "incompatible")
            self.assertIn("不匹配", status.message)

            replacement = Path(directory) / "replacement.exe"
            with patch("software_app.core.browser_driver.find_existing_driver", return_value=(old_driver, "global")), patch(
                "software_app.core.browser_driver.chrome_version", return_value="123.0.1.0"
            ), patch("software_app.core.browser_driver.chromedriver_version", return_value="122.0.1.0"), patch(
                "software_app.core.browser_driver.download_chromedriver", return_value=replacement
            ) as download:
                self.assertEqual(ensure_chromedriver(auto_download=True), replacement)
            download.assert_called_once_with()

    def test_download_lookup_requires_installed_chrome_version(self):
        with self.assertRaisesRegex(BrowserDriverError, "未检测到已安装的 Chrome"):
            find_chromedriver_download_url("")

    def test_download_lookup_never_uses_a_different_major(self):
        payload = b'{"builds":{},"milestones":{"123":{"version":"124.0.1.0","downloads":{"chromedriver":[{"platform":"win64","url":"https://invalid.example/driver.zip"}]}}}}'
        with patch("software_app.core.browser_driver.driver_platform_name", return_value="win64"), patch(
            "software_app.core.browser_driver.download_url", return_value=payload
        ):
            with self.assertRaisesRegex(BrowserDriverError, "未提供与 Chrome 123.0.1.0 匹配"):
                find_chromedriver_download_url("123.0.1.0")

    def test_download_retries_one_transient_network_failure(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.read.return_value = b"archive"
        with patch(
            "software_app.core.browser_driver.urllib.request.urlopen",
            side_effect=[urllib.error.URLError("temporary"), response],
        ) as urlopen, patch("software_app.core.browser_driver.time.sleep") as pause:
            self.assertEqual(download_url("https://example.invalid/driver.zip"), b"archive")
        self.assertEqual(urlopen.call_count, 2)
        pause.assert_called_once_with(1)

    def test_frozen_runtime_root_uses_local_app_data_and_override_wins(self):
        with patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\Example\AppData\Local"}, clear=False):
            with patch.dict("os.environ", {"YUQIUDA_HOME": ""}, clear=False):
                self.assertEqual(runtime_root(frozen=True), Path(r"C:\Users\Example\AppData\Local\YuqiuDa"))
        with patch.dict("os.environ", {"YUQIUDA_HOME": str(Path.cwd() / "portable-data")}, clear=False):
            self.assertEqual(runtime_root(frozen=True), (Path.cwd() / "portable-data").resolve())


if __name__ == "__main__":
    unittest.main()

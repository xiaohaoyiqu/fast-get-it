"""Exercise staged installers using local binaries; no download or browser launch."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import patch


def zipped(name, content):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    return buffer.getvalue()


def main():
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    from software_app.core import aria2_bt, browser_driver

    root = repo / "data/software_app/verification" / f"tool-install-runtime-{time.time_ns()}"
    root.mkdir(parents=True)
    report = {"ok": False, "checks": []}
    try:
        source = repo / "software_app/crawlers/twitter/chromedriver.exe"
        driver_version = browser_driver.chromedriver_version(source)
        if not driver_version:
            raise RuntimeError("Local ChromeDriver is unavailable")
        aria_source, _ = aria2_bt.find_aria2c()
        if aria_source is None:
            raise RuntimeError("Local aria2 is unavailable")
        with tempfile.TemporaryDirectory(dir=root) as directory:
            work = Path(directory)
            chrome = work / "chrome/chromedriver.exe"
            chrome_payload = zipped("chromedriver.exe", source.read_bytes())
            with (
                patch.object(browser_driver, "global_driver_path", return_value=chrome),
                patch.object(browser_driver, "chrome_version", return_value=driver_version),
                patch.object(browser_driver, "find_chromedriver_download_url", return_value="unused"),
                patch.object(browser_driver, "download_url", return_value=chrome_payload),
            ):
                browser_driver.download_chromedriver()
                assert browser_driver.chromedriver_version(chrome) == driver_version
                assert chrome.read_bytes() == source.read_bytes()
                report["checks"].append("real ChromeDriver staged --version and first installation")
                old = chrome.read_bytes()
                wrong_version = str(int(driver_version.split(".")[0]) + 1) + ".0.0.0"
                with patch.object(browser_driver, "chrome_version", return_value=wrong_version):
                    try:
                        browser_driver.download_chromedriver()
                    except browser_driver.BrowserDriverError:
                        pass
                    else:
                        raise AssertionError("Incompatible driver was committed")
                assert chrome.read_bytes() == old
                report["checks"].append("real incompatible ChromeDriver rejected with existing bytes preserved")
            aria = work / "aria/aria2c.exe"
            aria_payload = zipped("aria2c.exe", aria_source.read_bytes())
            # Generated archive of the local binary: does not verify official release integrity.
            with (
                patch.object(aria2_bt, "bundled_aria2_path", return_value=aria),
                patch.object(aria2_bt, "ARIA2_WINDOWS_X64_SHA256", hashlib.sha256(aria_payload).hexdigest()),
            ):
                result = aria2_bt._install_archive_payload(aria_payload)
                assert result.executable == aria.resolve()
                assert "aria2 version" in result.version.casefold()
                assert aria.read_bytes() == aria_source.read_bytes()
                report["checks"].append("real aria2 staged --version and first installation")
            invalid = zipped("aria2c.exe", b"invalid executable")
            with (
                patch.object(aria2_bt, "bundled_aria2_path", return_value=aria),
                patch.object(aria2_bt, "ARIA2_WINDOWS_X64_SHA256", hashlib.sha256(invalid).hexdigest()),
            ):
                try:
                    aria2_bt._install_archive_payload(invalid)
                except aria2_bt.Aria2Error:
                    pass
                else:
                    raise AssertionError("Invalid aria2 was committed")
                assert aria.read_bytes() == aria_source.read_bytes()
                report["checks"].append("real OS rejection of invalid aria2 preserves existing binary")
            assert {path.name for path in chrome.parent.iterdir()} == {chrome.name}
            assert {path.name for path in aria.parent.iterdir()} == {aria.name}
            report["checks"].append("all installer candidates cleaned after success and failure")
        report["ok"] = True
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc))
    target = root / "report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), **report}, ensure_ascii=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

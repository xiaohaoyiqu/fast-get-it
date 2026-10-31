"""Exercise local Chrome window closure and tab crash through production scanning."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch


def main():
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    import psutil
    from selenium.common.exceptions import WebDriverException
    from software_app.core.browser_driver import chrome_version, chromedriver_version, versions_compatible
    from software_app.core.browser_pool import BROWSER_POOL
    from software_app.core.browser_session import BrowserSessionLost
    from software_app.core.error_policy import classify_error
    from software_app.crawlers.twitter import driver_init, manga_downloader

    root = repo / "data/software_app/verification" / f"browser-failure-runtime-{time.time_ns()}"
    root.mkdir(parents=True)
    report = {"ok": False, "checks": []}
    try:
        executable = repo / "software_app/crawlers/twitter/chromedriver.exe"
        if not versions_compatible(chrome_version(), chromedriver_version(executable)):
            raise RuntimeError("Local browser/driver mismatch; no automatic download during smoke")
        for mode in ("window_close", "tab_crash"):
            initial = BROWSER_POOL.snapshot()
            driver, profile, children = None, None, []
            started = time.monotonic()
            try:
                with BROWSER_POOL.session():
                    try:
                        with (
                            patch.object(driver_init, "ensure_chromedriver", return_value=executable),
                            patch.object(driver_init, "find_skeb_button_extension", return_value=None),
                        ):
                            driver = driver_init.initialize_driver()
                        process = driver.service.process
                        children = psutil.Process(process.pid).children(recursive=True)
                        profile = Path(driver.capabilities["chrome"]["userDataDir"])
                        driver.get("data:text/html,<title>Local browser failure check</title>")
                        if mode == "window_close":
                            driver.close()
                        else:
                            try:
                                driver.get("chrome://crash")
                            except WebDriverException:
                                pass
                        try:
                            manga_downloader.download_media(
                                driver,
                                root,
                                root,
                                root,
                                "1",
                                True,
                                download_workers=2,
                                cancel_event=threading.Event(),
                            )
                        except BrowserSessionLost as error:
                            assert classify_error(error).code == "browser_session_lost"
                            assert not classify_error(error).retryable
                        else:
                            raise AssertionError("Interrupted browser was reported as completed")
                    finally:
                        if driver is not None:
                            driver.quit()
                assert BROWSER_POOL.snapshot() == initial
                assert profile is not None and not profile.exists()
                assert process.poll() is not None
                _gone, alive = psutil.wait_procs(children, timeout=5)
                assert not alive, "Owned browser child processes survived quit"
                report["checks"].append(
                    {
                        "mode": mode,
                        "seconds": round(time.monotonic() - started, 3),
                        "classification": "browser_session_lost",
                        "driver_stopped": True,
                        "profile_removed": True,
                        "pool_released": True,
                        "children_stopped": True,
                    }
                )
            finally:
                # Cleanup only captured children of this test's driver, even if
                # assertions fail. psutil Process identities guard PID reuse.
                for child in children:
                    try:
                        if child.is_running():
                            child.terminate()
                    except psutil.NoSuchProcess:
                        pass
        report["ok"] = True
    except Exception as error:
        report.update(error_type=type(error).__name__, error=str(error))
    target = root / "report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), **report}, ensure_ascii=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Kill only an isolated test driver and check its browser/sibling lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true", help="Use Selenium's original unguarded service")
    parser.add_argument("--child-state", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    import psutil
    from selenium.webdriver.chrome.service import Service
    from software_app.core.browser_driver import chrome_version, chromedriver_version, versions_compatible
    from software_app.core.browser_pool import BROWSER_POOL
    from software_app.crawlers.twitter import driver_init

    if args.child_state is not None:
        executable = repo / "software_app/crawlers/twitter/chromedriver.exe"
        with (
            patch.object(driver_init, "ensure_chromedriver", return_value=executable),
            patch.object(driver_init, "find_skeb_button_extension", return_value=None),
        ):
            driver = driver_init.initialize_driver()
        try:
            driver.get("data:text/html,<title>Isolated owner exit</title>")
            profile = Path(driver.capabilities["chrome"]["userDataDir"])
            temporary_state = args.child_state.with_suffix(".tmp")
            temporary_state.write_text(
                json.dumps(
                    {
                        "driver_pid": driver.service.process.pid,
                        "profile": str(profile),
                        "identity": driver_init._profile_identity(profile),
                    }
                ),
                encoding="utf-8",
            )
            temporary_state.replace(args.child_state)
            sys.stdin.buffer.read(1)
        finally:
            driver.quit()
        return 0

    root = repo / "data/software_app/verification" / f"browser-orphan-runtime-{time.time_ns()}"
    root.mkdir(parents=True)
    report = {"ok": False, "baseline": args.baseline, "checks": []}
    drivers, observed, leases = [], [], []
    try:
        executable = repo / "software_app/crawlers/twitter/chromedriver.exe"
        if not versions_compatible(chrome_version(), chromedriver_version(executable)):
            raise RuntimeError("Local browser/driver mismatch; no automatic download")
        initial = BROWSER_POOL.snapshot()
        with ExitStack() as stack:
            stack.enter_context(patch.object(driver_init, "ensure_chromedriver", return_value=executable))
            stack.enter_context(patch.object(driver_init, "find_skeb_button_extension", return_value=None))
            if args.baseline:
                stack.enter_context(patch.object(driver_init, "ChromeService", Service))
            for index in range(2):
                leases.append(BROWSER_POOL.acquire())
                driver = driver_init.initialize_driver()
                drivers.append(driver)
                driver.get(f"data:text/html,<title>Isolated session {index}</title>")
                observed.extend(psutil.Process(driver.service.process.pid).children(recursive=True))
            victim, sibling = drivers
            victim_children = psutil.Process(victim.service.process.pid).children(recursive=True)
            victim_profile = Path(victim.capabilities["chrome"]["userDataDir"])
            process = victim.service.process
            process.kill()  # The retained Popen handle belongs only to this test.
            process.wait(timeout=5)
            started = time.monotonic()
            victim.quit()
            leases[0].release()
            _gone, alive = psutil.wait_procs(victim_children, timeout=4)
            report.update(
                surviving_children=len(alive),
                profile_removed=not victim_profile.exists(),
                cleanup_seconds=round(time.monotonic() - started, 3),
            )
            assert sibling.title == "Isolated session 1"
            report["checks"].append("unrelated sibling session remains usable")
            if args.baseline:
                assert alive, "Baseline did not reproduce orphan processes"
                report["checks"].append("original service leaves owned Chrome processes after driver kill")
            else:
                assert not alive, "Owned Chrome children survived cleanup"
                assert not victim_profile.exists(), "Owned profile remained"
                report["checks"].extend(("owned children stopped after driver kill", "owned profile removed"))
            sibling_profile = Path(sibling.capabilities["chrome"]["userDataDir"])
            sibling.quit()
            leases[1].release()
            assert not sibling_profile.exists()
            assert BROWSER_POOL.snapshot() == initial
            report["checks"].append("normal sibling quit and browser slots released")
            if not args.baseline:
                child_state = root / "owner-state.json"
                child = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--child-state", str(child_state)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                captured, child_profile, child_identity = [], None, None
                try:
                    deadline = time.monotonic() + 30
                    while not child_state.exists():
                        if child.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Isolated owner failed to start")
                        time.sleep(0.1)
                    details = json.loads(child_state.read_text(encoding="utf-8"))
                    child_profile, child_identity = Path(details["profile"]), tuple(details["identity"])
                    captured = psutil.Process(child.pid).children(recursive=True)
                    assert any(item.pid == details["driver_pid"] for item in captured)
                    child.kill()  # Terminate only this script's retained child handle.
                    child.wait(timeout=5)
                    _gone, alive = psutil.wait_procs(captured, timeout=5)
                    assert not alive, "Kernel job did not close on owner process death"
                    report["checks"].append("owner process death closes job and stops its browser descendants")
                    report["owner_exit_profile_remained"] = child_profile.exists()
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                    child.stdin.close()
                    for item in captured:
                        try:
                            if item.is_running():
                                item.kill()
                        except psutil.NoSuchProcess:
                            pass
                    psutil.wait_procs(captured, timeout=5)
                    if child_profile is not None:
                        driver_init._cleanup_temporary_profile(child_profile, child_identity)
            report["ok"] = True
    except Exception as error:
        report.update(error_type=type(error).__name__, error=str(error))
    finally:
        for driver in drivers:
            try:
                driver.quit()
            except Exception:
                pass
        for lease in leases:
            lease.release()
        # Clean only children captured from these test driver objects. psutil
        # retains process creation identity; no global browser-name matching.
        for child in observed:
            try:
                if child.is_running():
                    child.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(observed, timeout=5)
        for driver in drivers:
            try:
                driver.quit()
            except Exception:
                pass
    target = root / "report.json"
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), **report}, ensure_ascii=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

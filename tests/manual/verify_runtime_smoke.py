"""Opt-in Windows checks using a real browser or backed-up user databases."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def snapshot(path, columns=None):
    with closing(readonly(path)) as conn:
        if columns is None:
            columns = {
                name: [row[1] for row in conn.execute("PRAGMA table_info(" + quote(name) + ")")]
                for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        fingerprints = {}
        for table, names in columns.items():
            rows = conn.execute("SELECT " + ",".join(map(quote, names)) + " FROM " + quote(table)).fetchall()
            encoded = sorted(json.dumps(row, ensure_ascii=False, default=repr) for row in rows)
            fingerprints[table] = {
                "count": len(rows),
                "sha256": hashlib.sha256("\n".join(encoded).encode()).hexdigest(),
            }
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        return columns, fingerprints


def database(root):
    os.environ["YUQIUDA_HOME"] = str(root / "runtime")
    os.environ["YUQIUDA_OUTPUT_DIR"] = str(root / "media")
    from software_app.core.storage import AppStorage

    results = []
    names = ("content_downloader.db", "content_downloader_v2.db", "content_downloader_v2.pre-platform-fix-20260912.bak")
    for name in names:
        source = REPO / "data/software_app" / name
        backup = root / (name + ".before.sqlite")
        with closing(readonly(source)) as original, closing(sqlite3.connect(backup)) as destination:
            original.backup(destination)
        columns, before = snapshot(backup)
        rehearsal = root / (name + ".migrated.sqlite")
        with closing(readonly(backup)) as original, closing(sqlite3.connect(rehearsal)) as destination:
            original.backup(destination)
        AppStorage(rehearsal)
        assert snapshot(rehearsal, columns)[1] == before, "migration changed existing column values"
        migrated = snapshot(rehearsal)[1]
        AppStorage(rehearsal)
        assert snapshot(rehearsal)[1] == migrated, "migration is not idempotent"
        live = name == "content_downloader.db"
        if live:
            assert snapshot(source, columns)[1] == before, "source changed during rehearsal"
            AppStorage(source)
            assert snapshot(source, columns)[1] == before, "live migration changed existing values"
            assert snapshot(source)[1] == migrated
        results.append(
            {
                "source": name,
                "backup": str(backup),
                "old_rows": {k: v["count"] for k, v in before.items()},
                "added_tables": sorted(set(migrated) - set(before)),
                "existing_values_preserved": True,
                "integrity": "ok",
                "idempotent": True,
                "live_schema_applied": live,
            }
        )
    return {"ok": True, "databases": results}


def browser(root):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from selenium.webdriver.common.by import By
    from software_app.core.browser_pool import BROWSER_POOL
    from software_app.crawlers.twitter.driver_init import initialize_driver

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            payload = b'<title>Yuqiuda browser smoke</title><button id="go" onclick="document.body.dataset.done=\'yes\'">Go</button>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    driver = None
    initial = BROWSER_POOL.snapshot()[0]
    lease = BROWSER_POOL.acquire()
    profile = None
    try:
        driver = initialize_driver()
        profile = Path(driver.capabilities["chrome"]["userDataDir"])
        driver.get(f"http://127.0.0.1:{server.server_port}/")
        assert driver.title == "Yuqiuda browser smoke"
        driver.find_element(By.ID, "go").click()
        assert driver.execute_script("return document.body.dataset.done") == "yes"
        driver.add_cookie({"name": "smoke", "value": "local-only", "path": "/"})
        driver.refresh()
        assert driver.get_cookie("smoke")["value"] == "local-only"
        driver.save_screenshot(str(root / "browser.png"))
        versions = {
            "chrome": driver.capabilities["browserVersion"],
            "driver": driver.capabilities["chrome"]["chromedriverVersion"].split()[0],
        }
    finally:
        if driver is not None:
            driver.quit()
        lease.release()
        server.shutdown()
        server.server_close()
        thread.join(3)
    assert BROWSER_POOL.snapshot()[0] == initial
    assert profile is not None and not profile.exists(), "temporary browser profile was not removed"
    return {
        "ok": True,
        "versions": versions,
        "checks": [
            "production driver starts",
            "HTTP navigation",
            "DOM click and JavaScript",
            "local cookie roundtrip",
            "screenshot",
            "quit removes temporary profile",
            "browser lease released",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("database", "browser"))
    args = parser.parse_args()
    root = REPO / "data/software_app/verification" / f"{args.kind}-{time.time_ns()}"
    root.mkdir(parents=True)
    try:
        result = globals()[args.kind](root)
    except Exception as exc:
        result = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    target = root / "report.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), **result}, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

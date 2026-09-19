"""Download one public X photo post into a disposable directory for smoke testing."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from pathlib import Path

from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask
from software_app.core.settings import DB_PATH, TWITTER_DATA_DIR
from software_app.crawlers.twitter.service import TwitterCrawlerService
from software_app.crawlers.twitter.twitter_Crawler_2 import DEFAULT_CONFIG


POST_URL = "https://x.com/NASA/status/2040059770237849635"


def main() -> None:
    cookie = TWITTER_DATA_DIR / "X_cookie.json"
    if not cookie.is_file():
        raise FileNotFoundError("X Cookie 文件不存在")
    with sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT value_json FROM settings WHERE key=?", ("proxy_url",)).fetchone()
    proxy = str(json.loads(row[0]) if row else "")
    with tempfile.TemporaryDirectory(prefix="twitter_post_smoke_") as directory:
        root = Path(directory)
        config = dict(DEFAULT_CONFIG)
        config.update({
            "proxy_url": proxy, "download_workers": 1, "max_idle_rounds": 2,
            "stable_scroll_rounds": 1, "cells_per_round": 2,
            "page_load_timeout": 15, "media_wait_timeout": 15, "bootstrap_timeout": 15,
        })
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        task = DownloadTask("live-x-smoke", "twitter", POST_URL, root / "downloads", {
            "types": "1", "cookie": str(cookie), "config": str(config_path),
            "record": str(root / "downloaded_urls.json"),
            "failed_record": str(root / "failed_urls.json"), "proxy_url": proxy,
        })
        cancel_event = threading.Event()
        timeout = threading.Timer(120, cancel_event.set)
        timeout.start()
        try:
            stats = TwitterCrawlerService(root / "runtime").download(
                task, CallbackSet(on_progress=lambda event: print("progress", event.message)), cancel_event
            )
            files = list((root / "downloads").rglob("*"))
            files = [path for path in files if path.is_file()]
            print("downloaded_file_count", len(files))
            print("downloaded_names", [path.name for path in files])
            print("stats", stats)
        finally:
            timeout.cancel()


if __name__ == "__main__":
    main()

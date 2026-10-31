"""Opt-in saved-account check and one safe, single-page artwork download.

Cookie values, account IDs and downloaded work metadata are never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Runtime root containing data/software_app (use dist for packaged-app cookies/settings)",
    )
    parser.add_argument("--proxy", default=None, help="Explicit test-only proxy; does not update saved settings")
    parser.add_argument(
        "--public", action="store_true", help="Test public download without sending saved account cookies"
    )
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    root = repo / "data/software_app/verification" / f"pixiv-live-{time.time_ns()}"
    root.mkdir(parents=True)
    os.environ["YUQIUDA_HOME"] = str(root / "runtime")
    os.environ["YUQIUDA_OUTPUT_DIR"] = str(root / "media")
    from PIL import Image
    from software_app.crawlers.common import load_cookie_file
    from software_app.crawlers.pixiv.client import PixivCrawler
    from software_app.core.events import CallbackSet
    from software_app.core.models import DownloadTask
    from software_app.core.security import safe_error_message

    result = {"ok": False, "checks": [], "platform": "pixiv", "scope": "public" if args.public else "account"}
    client = None
    phase = "saved_login"
    started = time.monotonic()
    try:
        data = args.home.resolve() / "data/software_app"
        cookie = data / "json/pixiv/cookies.json"
        account = ""
        if not args.public:
            session_id = str(load_cookie_file(cookie).get("PHPSESSID") or "")
            account = session_id.split("_", 1)[0]
            if not account.isdigit():
                raise ValueError("No saved Pixiv account session")
        proxy = ""
        candidates = []
        for name in ("content_downloader.db", "content_downloader_v2.db"):
            database = data / name
            # Fresh portable installs need not have the historical v2 database.
            if not database.is_file():
                continue
            conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                if name == "content_downloader.db":
                    row = conn.execute("SELECT value_json FROM settings WHERE key='proxy_url'").fetchone()
                    if row:
                        proxy = str(json.loads(row[0]) or "")
                for row in conn.execute("SELECT source_url FROM files WHERE module_id='pixiv' LIMIT 10"):
                    match = re.search(r"/artworks/(\d+)", row[0] or "")
                    if match and match[1] not in candidates:
                        candidates.append(match[1])
            finally:
                conn.close()
        if not args.public:
            result["checks"].append("saved PHPSESSID available")
        if args.proxy is not None:
            proxy = args.proxy
        result["explicit_proxy"] = bool(proxy)
        client = PixivCrawler(cookie_file=None if args.public else cookie, proxy_url=proxy)
        if args.public:
            assert not list(client.session.cookies), "public check must not send saved account cookies"
            result["checks"].append("anonymous session without saved account cookies")
        else:
            phase = "authenticated_private_bookmarks"
            body = client._body(f"/ajax/user/{account}/illusts/bookmarks?tag=&offset=0&limit=1&rest=hide")
            if not isinstance(body, dict) or not isinstance(body.get("works"), list):
                raise ValueError("Private-bookmark endpoint did not return authenticated data")
            result["checks"].append("authenticated private-bookmark endpoint")
        phase = "single_page_candidate"
        ranking = client._json("https://www.pixiv.net/ranking.php?mode=daily&content=illust&format=json&p=1")
        candidates.extend(str(row["illust_id"]) for row in ranking.get("contents", [])[:5] if row.get("illust_id"))
        selected = None
        for work_id in candidates[:8]:
            try:
                detail = client._body(f"/ajax/illust/{work_id}")
            except Exception:
                continue
            if (
                isinstance(detail, dict)
                and detail.get("xRestrict") == 0
                and detail.get("illustType") == 0
                and detail.get("pageCount") == 1
            ):
                selected = work_id
                break
        if selected is None:
            raise ValueError("No unrestricted single-page static artwork among bounded candidates")
        phase = "production_download"
        records, events = [], []
        task = DownloadTask(
            "live-smoke",
            "pixiv",
            f"https://www.pixiv.net/artworks/{selected}",
            root / "media",
            {
                "input_kind": "work",
                "max_works": 1,
                "filter_ai": False,
                "types": "1",
                "image_format": "original",
                "file_conflict": "error",
            },
        )
        client.download(task, CallbackSet(on_file=records.append, on_progress=events.append), threading.Event())
        if len(records) != 1 or not records[0].path.is_file():
            raise AssertionError("Production downloader did not publish exactly one file")
        path = records[0].path
        with Image.open(path) as image:
            image.verify()
        result.update(ok=True, bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        result["checks"].extend(
            ("production single-artwork downloader", "one image published", "Pillow image integrity")
        )
    except Exception as exc:
        response = getattr(exc, "response", None)
        result.update(
            failed_phase=phase,
            error_type=type(exc).__name__,
            http_status=getattr(response, "status_code", None),
            error=safe_error_message(exc),
        )
    finally:
        if client is not None:
            client.session.close()
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    target = root / "report.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), **result}, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

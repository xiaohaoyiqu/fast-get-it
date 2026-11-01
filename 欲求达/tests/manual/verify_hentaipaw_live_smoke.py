"""Bounded, opt-in HentaiPaw check; downloads at most one reader image."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="Article ID or HTTPS article URL")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--cookie-file", type=Path)
    parser.add_argument("--search", default="")
    parser.add_argument("--download-one", action="store_true")
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from PIL import Image
    from software_app.core.events import CallbackSet
    from software_app.core.models import DownloadTask
    from software_app.crawlers.hentaipaw import HentaiPawClient

    args.root.mkdir(parents=True, exist_ok=True)
    options = {"max_files": 1, "types": "1", "image_format": "original", "file_conflict": "reuse"}
    with tempfile.TemporaryDirectory(dir=args.root) as directory:
        # Anonymous by default: never silently consume the user's saved account.
        cookies = args.cookie_file or Path(directory) / "no-cookies.json"
        with HentaiPawClient(cookie_file=cookies, proxy_url=args.proxy) as client:
            info = client.article(args.target)
            pages = client.reader(args.target)
            result = {"article_id": info["target_id"], "reader_pages": len(pages),
                      "cookie_supplied": args.cookie_file is not None, "login_verified": False}
            if args.search:
                result["search_results"] = len(client.search(args.search, 5))
            if args.download_one:
                records = []
                task = DownloadTask("hentaipaw-live", "hentaipaw", args.target, args.root, options)
                client.download(task, CallbackSet(on_file=records.append), threading.Event())
                images = [record for record in records if record.media_type == "image"]
                assert len(images) == 1, "Expected exactly one image"
                with Image.open(images[0].path) as image:
                    result["image_size"] = list(image.size)
                    image.verify()
                result["image_bytes"] = images[0].size
                result["download_verified"] = True
    (args.root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Privacy-safe live EH download and ZIP post-processing smoke test.

The script prints no cookies, proxy URLs, gallery titles, IDs, tokens, tags, or
local filenames. All downloaded data is held in a temporary directory and
removed before the process exits.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from software_app.adapters.ehentai import EhentaiNativeAdapter  # noqa: E402
from software_app.core.archive_service import run_archive_postprocess  # noqa: E402
from software_app.core.events import CallbackSet  # noqa: E402
from software_app.core.models import DownloadTask  # noqa: E402
from software_app.core.storage import AppStorage  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="只读验证 EH 下载与压缩，不输出画廊或账号内容")
    parser.add_argument("--site", choices=("e-hentai", "exhentai"), default="e-hentai")
    parser.add_argument("--method", choices=("metadata", "images", "torrent"), default="metadata")
    parser.add_argument("--source-fragment", type=Path)
    parser.add_argument("--max-images", type=int, default=0)
    args = parser.parse_args()

    result: dict[str, object] = {
        "site": args.site,
        "requested_method": args.method,
        "candidate_found": False,
        "metadata_download_ok": False,
        "image_download_ok": False,
        "torrent_download_ok": False,
        "zip_created": False,
        "zip_integrity_ok": False,
        "temporary_data_removed": False,
    }
    adapter = EhentaiNativeAdapter()
    storage = AppStorage()
    proxy_url = str(storage.get_setting("proxy_url", "") or "")
    mode = "里站关键词" if args.site == "exhentai" else "表站关键词"
    options = {"proxy_url": proxy_url, "search_mode": mode}
    temporary_path: Path | None = None
    try:
        rows = []
        if args.source_fragment:
            raw = args.source_fragment.read_text(encoding="utf-8-sig")
            match = re.search(
                r'https://(?:e-hentai\.org|exhentai\.org)/g/\d+/[0-9a-f]{10}/?', raw, re.IGNORECASE
            )
            if match:
                rows = [{"target": match.group(0)}]
        else:
            rows = adapter.search_targets("全部", limit=25, options=options)
            if args.method == "torrent":
                rows = [row for row in rows if int(row.get("torrent_count") or 0) > 0]
        if not rows:
            result["error_type"] = "NoSuitableCandidate"
            print(json.dumps(result, ensure_ascii=False))
            return 3
        result["candidate_found"] = True
        target = str(rows[0].get("target") or rows[0].get("url") or "")
        with tempfile.TemporaryDirectory(prefix="eh-live-smoke-", dir=PROJECT_ROOT) as directory:
            temporary_path = Path(directory)
            records = []
            task = DownloadTask(
                "eh-live-smoke",
                "ehentai",
                target,
                temporary_path,
                {
                    **options,
                    "eh_download_method": args.method,
                    "eh_download_images": args.method == "images",
                    "eh_download_torrent": args.method == "torrent",
                    "eh_max_images": max(0, int(args.max_images)),
                    "max_torrents": 1,
                    "archive_mode": "task",
                    "archive_cleanup_sources": False,
                },
            )
            cancel_event = threading.Event()
            adapter.download(task, CallbackSet(on_file=records.append), cancel_event)
            metadata_records = [row for row in records if row.metadata.get("kind") == "eh_metadata"]
            torrent_records = [row for row in records if row.metadata.get("kind") == "torrent"]
            image_records = [row for row in records if row.metadata.get("kind") == "eh_display_image"]
            result["metadata_download_ok"] = (
                len(metadata_records) == 1
                and metadata_records[0].path.is_file()
                and metadata_records[0].path.stat().st_size > 0
            )
            result["torrent_download_ok"] = (
                args.method != "torrent"
                or (
                    len(torrent_records) == 1
                    and torrent_records[0].path.is_file()
                    and torrent_records[0].path.read_bytes()[:1] == b"d"
                )
            )
            result["image_download_ok"] = (
                args.method != "images"
                or (
                    len(image_records) == (max(1, int(args.max_images)) if args.max_images else len(image_records))
                    and bool(image_records)
                    and all(row.path.is_file() and row.path.stat().st_size > 0 for row in image_records)
                )
            )
            archived = run_archive_postprocess(
                task,
                "EH live smoke",
                records,
                cancel_event=cancel_event,
            )
            archives = [row for row in archived.records if row.media_type == "archive"]
            result["zip_created"] = len(archives) == 1 and archives[0].path.is_file()
            if archives:
                with zipfile.ZipFile(archives[0].path, "r") as handle:
                    result["zip_integrity_ok"] = (
                        handle.testzip() is None
                        and len([item for item in handle.infolist() if not item.is_dir()]) == len(records)
                    )
            if not all(
                bool(result[key])
                for key in (
                    "metadata_download_ok", "image_download_ok", "torrent_download_ok",
                    "zip_created", "zip_integrity_ok",
                )
            ):
                raise RuntimeError("live download verification failed")
        result["temporary_data_removed"] = bool(temporary_path and not temporary_path.exists())
    except Exception as exc:  # noqa: BLE001
        # Exception strings from HTTP stacks can contain credential-bearing
        # proxy URLs, so only expose the exception class.
        result["error_type"] = type(exc).__name__
        if type(exc).__name__ == "SSLError":
            lowered = str(exc).casefold()
            result["ssl_category"] = (
                "certificate_verification"
                if "certificate verify failed" in lowered
                else "unexpected_eof"
                if "unexpected eof" in lowered or "eof occurred" in lowered
                else "protocol_or_proxy_handshake"
                if "wrong version number" in lowered or "proxy" in lowered
                else "other_ssl_error"
            )
        result["temporary_data_removed"] = bool(temporary_path is None or not temporary_path.exists())
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

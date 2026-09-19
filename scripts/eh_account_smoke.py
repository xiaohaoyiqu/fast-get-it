"""Privacy-safe, read-only EH account smoke check.

The command intentionally prints no cookie values, proxy URLs, gallery titles,
IDs, tokens, notes, or favorite category numbers.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from software_app.adapters.ehentai import EhentaiNativeAdapter  # noqa: E402
from software_app.core.storage import AppStorage  # noqa: E402
from software_app.crawlers.ehentai import parse_ehentai_target  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="只读验证 EH 登录与收藏读取，不输出个人内容")
    parser.add_argument("--site", choices=("e-hentai", "exhentai"), default="e-hentai")
    args = parser.parse_args()

    adapter = EhentaiNativeAdapter()
    status = adapter.cookie_status(args.site)
    result: dict[str, object] = {
        "site": args.site,
        "cookie_file_present": bool(status["count"]),
        "required_cookie_names_present": bool(status["has_login"]),
        "settings_page_ok": False,
        "favorites_page_ok": False,
        "favorite_state_read_ok": False,
        "thumbnail_image_ok": False,
    }
    if not status["has_login"]:
        print(json.dumps(result, ensure_ascii=False))
        return 2

    storage = AppStorage()
    proxy_url = str(storage.get_setting("proxy_url", "") or "")
    client = adapter._client({"proxy_url": proxy_url}, site=args.site)
    try:
        if args.site == "e-hentai":
            response = client.session.get("https://e-hentai.org/uconfig.php", timeout=(10, 45), allow_redirects=True)
            response.raise_for_status()
            final = urlparse(str(response.url or ""))
            lowered = str(response.text or "")[:300000].casefold()
            result["settings_page_ok"] = (
                str(final.hostname or "").casefold() in {"e-hentai.org", "www.e-hentai.org"}
                and "uconfig.php" in final.path.casefold()
                and "act=login" not in lowered
                and "ips_username" not in lowered
            )
            if not result["settings_page_ok"]:
                raise PermissionError("table settings validation failed")
        else:
            result["settings_page_ok"] = bool(client._get_text("https://exhentai.org/").strip())
        rows = client.search("全部", "里站收藏夹" if args.site == "exhentai" else "表站收藏夹", limit=1)
        result["favorites_page_ok"] = True
        result["favorite_sample_available"] = bool(rows)
        if not rows:
            rows = client.search("全部", "里站关键词" if args.site == "exhentai" else "表站关键词", limit=1)
            result["search_page_ok"] = bool(rows)
        if rows:
            target = parse_ehentai_target(str(rows[0].get("target") or rows[0].get("url") or ""))
            preview = client.preview(target, live=True)
            result["gallery_preview_ok"] = bool(
                preview.title and preview.metadata.get("target_id") and preview.metadata.get("site") == args.site
            )
            thumbnail_urls = [str(preview.metadata.get("thumbnail_url") or "")]
            thumbnail_error: Exception | None = None
            for thumbnail_url in thumbnail_urls:
                if not thumbnail_url:
                    continue
                try:
                    payload = adapter.fetch_preview_image_bytes(thumbnail_url, {"proxy_url": proxy_url})
                    with Image.open(io.BytesIO(payload)) as opened:
                        opened.verify()
                    result["thumbnail_image_ok"] = True
                    break
                except Exception as exc:  # noqa: BLE001
                    thumbnail_error = exc
            if not result["thumbnail_image_ok"]:
                public_rows = client.search(
                    "全部", "里站关键词" if args.site == "exhentai" else "表站关键词", limit=5
                )
                for row in public_rows:
                    thumbnail_url = str(row.get("thumbnail_url") or "")
                    if not thumbnail_url:
                        continue
                    try:
                        payload = adapter.fetch_preview_image_bytes(thumbnail_url, {"proxy_url": proxy_url})
                        with Image.open(io.BytesIO(payload)) as opened:
                            opened.verify()
                        result["thumbnail_image_ok"] = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        thumbnail_error = exc
                if not result["thumbnail_image_ok"] and thumbnail_error is not None:
                    result["thumbnail_error_type"] = type(thumbnail_error).__name__
            state = client.favorite_state(target)
            result["favorite_state_read_ok"] = "favorited" in state and "category" in state
            categories = list(state.get("categories") or [])
            result["favorite_categories_read"] = len(categories) == 10
            result["custom_category_names_read"] = any(
                str(name) != f"分类 {index}" for index, name in enumerate(categories)
            )
    except Exception as exc:  # noqa: BLE001
        # Avoid printing exception text because networking libraries can embed
        # a credential-bearing proxy URL in it.
        result["error_type"] = type(exc).__name__
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

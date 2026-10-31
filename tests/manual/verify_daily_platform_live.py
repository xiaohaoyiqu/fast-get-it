"""Opt-in bounded platform reads/downloads; saved credentials are read, never rewritten."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "platform", choices=("bluesky", "instagram", "jmcomic", "website", "twitter", "google_image", "ehentai")
    )
    parser.add_argument("--home", type=Path, default=Path("dist"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--twitter-media", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    root = args.root.resolve() / args.platform
    root.mkdir(parents=True, exist_ok=True)
    os.environ["YUQIUDA_HOME"] = str(root / "runtime")
    os.environ["YUQIUDA_OUTPUT_DIR"] = str(root / "media")
    from PIL import Image
    from software_app.core.adapter import TaskCancelled
    from software_app.core.events import CallbackSet
    from software_app.core.models import DownloadTask
    from software_app.core.network_policy import network_task_scope
    from software_app.core.security import safe_error_message

    data = args.home.resolve() / "data/software_app"
    settings = {}
    database = data / "content_downloader.db"
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
            settings = {row[0]: json.loads(row[1]) for row in conn.execute("SELECT key,value_json FROM settings")}
    proxy = str(settings.get("proxy_url") or "")
    proxy_source = "saved" if proxy else "direct"
    if not proxy:
        proxy = str(getattr(urllib.request, "getproxies_registry", urllib.request.getproxies)().get("https") or "")
        if proxy:
            proxy_source = "system"
    cookies = [
        data / "twitter/X_cookie.json",
        data / "instagram/cookies.json",
        data / "jmcomic/cookies.json",
        data / "ehentai/e-hentai-cookies.json",
    ]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in cookies if path.is_file()}
    result = {
        "ok": False,
        "platform": args.platform,
        "proxy_source": proxy_source,
        "checks": [],
        "download_verified": False,
    }
    event = threading.Event()
    watchdog = threading.Timer(120, event.set)
    watchdog.start()
    records, client = [], None
    instagram_avatar_name = ""
    phase = "start"
    started = time.monotonic()
    options = {
        "proxy_url": proxy,
        "types": "1",
        "max_works": 1,
        "max_files": 1,
        "max_file_mb": 5,
        "image_format": "original",
        "file_conflict": "error",
        "retries": 0,
        "media_retries": 1,
        "headless": True,
        "browser_wait_seconds": 10,
    }

    def collect(record):
        records.append(record)
        if args.platform == "jmcomic" and record.media_type == "image":
            event.set()  # Intentional one-page acceptance, never a whole comic.

    def task(target):
        return DownloadTask("live-acceptance", args.platform, target, root / "media", options)

    callbacks = CallbackSet(on_file=collect)
    try:
        with network_task_scope(event):
            if args.platform == "bluesky":
                from software_app.crawlers.bluesky import BlueskyClient

                client = BlueskyClient(proxy_url=proxy)
                phase = "public_profile"
                assert client.profile("bsky.app").get("did")
                result["checks"].append("public profile")
                phase = "public_author_feed"
                rows = client.author_posts("bsky.app", 5)
                selected = next(
                    (row for row in rows if any(item.get("type") == "image" for item in row.get("media", []))), None
                )
                if selected is None:
                    raise ValueError("No image post in bounded public sample")
                result["checks"].append("public author media discovery")
                phase = "production_single_post_download"
                client.download(task(selected["url"]), callbacks, event)
            elif args.platform == "website":
                from software_app.crawlers.webpage.client import WebPageCrawler

                client = WebPageCrawler()
                phase = "production_public_webpage_download"
                client.download(task("https://www.python.org/"), callbacks, event)
            elif args.platform == "instagram":
                from software_app.adapters.instagram import InstagramNativeAdapter

                adapter = InstagramNativeAdapter()
                adapter.runtime_data_dir = data / "instagram"
                result["saved_cookie_available"] = (adapter.runtime_data_dir / "cookies.json").is_file()
                phase = "production_profile_metadata"
                preview = adapter.preview_target("https://www.instagram.com/nasa/", {**options, "live": True})
                instagram_avatar_name = Path(urlsplit(str(preview.metadata.get("avatar_url") or "")).path).name
                phase = "production_profile_download_with_browser_fallback"
                adapter.download(task("https://www.instagram.com/nasa/"), callbacks, event)
            elif args.platform == "jmcomic":
                from software_app.crawlers.jmcomic.client import JmComicCrawler

                headers_path = data / "jmcomic/browser_headers.json"
                headers = json.loads(headers_path.read_text(encoding="utf-8")) if headers_path.exists() else {}
                client = JmComicCrawler(
                    domain=str(settings.get("jmcomic_domain") or "https://18comic.vip"),
                    cookie_file=data / "jmcomic/cookies.json",
                    proxy_url=proxy,
                    browser_headers=headers,
                )
                phase = "connection"
                probe = client.check_connection()
                if not probe.get("ok"):
                    raise RuntimeError(str(probe.get("error") or "JM connection/challenge failed"))
                result["checks"].append("production connection check")
                phase = "bounded_search"
                rows = client.search("東方", limit=1)
                if not rows:
                    raise ValueError("No bounded JM search candidate")
                result["checks"].append("production search candidate")
                options.update(jm_download_cover=False, jm_postprocess="none")
                phase = "production_first_page_download"
                try:
                    client.download(task(rows[0]["url"]), callbacks, event)
                except TaskCancelled:
                    if not records:
                        raise
                result["intentional_stop_after_one_page"] = True
            elif args.platform == "twitter":
                from software_app.crawlers.twitter.profile_preview import collect_profile_preview

                cookie = data / "twitter/X_cookie.json"
                if not cookie.is_file():
                    raise FileNotFoundError("Saved X cookie is not available")
                phase = "production_profile_read"
                if not args.twitter_media:
                    profile = collect_profile_preview(
                        cookie,
                        root / "public-profile.json",
                        "https://x.com/NASA",
                        page_load_timeout=20,
                        proxy_url=proxy,
                    )
                else:
                    from software_app.crawlers.twitter.profile_preview import (
                        collect_profile_preview_with_driver,
                        BOOTSTRAP_URL,
                    )
                    from software_app.crawlers.twitter.driver_init import initialize_driver, cookies_web
                    from software_app.crawlers.twitter.following_collector import safe_get
                    from software_app.crawlers.twitter.service import TwitterCrawlerService

                    driver = initialize_driver(proxy_url=proxy)
                    selected = None
                    try:
                        safe_get(driver, BOOTSTRAP_URL, timeout=20)
                        cookies_web(driver, str(cookie))
                        profile = collect_profile_preview_with_driver(
                            driver, root / "public-profile.json", "https://x.com/NASA", 20
                        )
                        result["checks"].append("production public profile with saved session")
                        phase = "bounded_public_image_post_discovery"
                        for _ in range(3):
                            for article in driver.find_elements("css selector", 'article[data-testid="tweet"]'):
                                if not article.find_elements("css selector", 'img[src*="pbs.twimg.com/media/"]'):
                                    continue
                                for anchor in article.find_elements("css selector", 'a[href*="/NASA/status/"]'):
                                    match = re.search(r"/NASA/status/([0-9]+)", anchor.get_attribute("href") or "")
                                    if match:
                                        selected = "https://x.com/NASA/status/" + match[1]
                                        break
                                if selected:
                                    break
                            if selected:
                                break
                            driver.execute_script("window.scrollBy(0, 750)")
                            if event.wait(1):
                                raise TaskCancelled("Live test stopped")
                    finally:
                        driver.quit()
                    if not selected:
                        raise ValueError("No image post in bounded public timeline sample")
                    phase = "production_public_single_post_download"
                    options.update(cookie=str(cookie), max_retries=0, download_workers=1)
                    TwitterCrawlerService(root / "runtime/twitter").download(task(selected), callbacks, event)
                if not profile.get("display_name"):
                    raise ValueError("X did not return a recognizable public profile")
                result["checks"].append("production browser public profile read")
                result["coverage"] = "profile only; media download not verified"
                result["ok"] = True
            elif args.platform == "ehentai":
                from software_app.crawlers.ehentai.client import EhentaiClient

                cookie = data / "ehentai/e-hentai-cookies.json"
                if not cookie.exists():
                    raise FileNotFoundError("Saved EH table-site Cookie is not available")
                client = EhentaiClient(cookie_file=cookie, proxy_url=proxy, site="e-hentai")
                phase = "bounded_non_h_gallery_search"
                rows = client.search("non-h", "表站关键词", limit=1)
                if not rows:
                    raise ValueError("No gallery in bounded non-h search sample")
                result["checks"].append("production gallery search")
                options.update(
                    eh_download_method="images",
                    eh_download_images=True,
                    eh_max_images=1,
                    eh_download_torrent=False,
                    eh_bt_download_enabled=False,
                )
                phase = "production_one_display_image_download"
                client.download(task(rows[0]["url"]), callbacks, event)
            else:
                from software_app.crawlers.google_image.client import GoogleImageCrawler

                image = args.image
                if image is None:
                    image = root / "generated.png"
                    with Image.new("RGB", (256, 256), "blue") as picture:
                        picture.save(image)
                    result["image_sample"] = "generated solid-color sample"
                else:
                    result["image_sample"] = "public downloaded image sample"
                phase = "production_reverse_image_search"
                client = GoogleImageCrawler()
                links, _ = client.search(
                    image,
                    max_results=3,
                    headless=True,
                    proxy_url=proxy,
                    task_id="live-google",
                    cancel_event=event,
                    manual_wait_seconds=0,
                )
                if not links:
                    raise ValueError("No source candidate returned; verification or no match may be involved")
                result["checks"].append("production image upload and source discovery")
                result.update(ok=True, candidate_count=len(links), coverage="search only")
            if args.platform not in {"twitter", "google_image"} or args.twitter_media:
                images = [record for record in records if record.media_type == "image"]
                if not images:
                    raise ValueError("No image published by production downloader")
                dimensions = []
                for record in images:
                    with Image.open(record.path) as image:
                        dimensions.append(list(image.size))
                        image.verify()
                result.update(
                    ok=True,
                    download_verified=True,
                    image_count=len(images),
                    bytes=sum(record.path.stat().st_size for record in images),
                    image_dimensions=dimensions,
                )
                if args.platform == "twitter":
                    result["coverage"] = "public profile and single image post"
                if args.platform == "instagram":
                    result["coverage"] = "profile-target image only; post/Reels not tested"
                    result["avatar_metadata_available"] = bool(instagram_avatar_name)
                    result["avatar_only"] = (
                        all(Path(urlsplit(record.source_url).path).name == instagram_avatar_name for record in images)
                        if instagram_avatar_name
                        else None
                    )
                    if result["avatar_only"]:
                        result.update(ok=False, download_verified=False)
                        phase = "published_media_scope"
                        raise ValueError(
                            "Profile download published only its avatar; post/Reels download is not verified"
                        )
                result["checks"].extend(("production image output", "Pillow image validation"))
    except Exception as exc:
        result.update(failed_phase=phase, error_type=type(exc).__name__, error=safe_error_message(exc))
    finally:
        watchdog.cancel()
        watchdog.join()
        if client is not None and getattr(client, "session", None) is not None:
            client.session.close()
        result["saved_cookies_unchanged"] = all(
            Path(path).is_file() and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
            for path, digest in before.items()
        )
        result["seconds"] = round(time.monotonic() - started, 3)
    (root / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "report.json"), **result}, ensure_ascii=True))
    return 0 if result["ok"] and result["saved_cookies_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

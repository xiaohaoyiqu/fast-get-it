from __future__ import annotations

import json
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from PIL import Image

from software_app.adapters.ehentai import EhentaiNativeAdapter
from software_app.core.events import CallbackSet
from software_app.core.aria2_bt import BtDownloadResult
from software_app.core.models import DownloadTask
from software_app.crawlers.ehentai import (
    EhentaiClient,
    parse_ehentai_target,
    parse_gallery_html,
    parse_image_page_html,
    parse_search_html,
)


GALLERY_URL = "https://e-hentai.org/g/1234567/abcdef1234/"
VALID_TORRENT = (
    b"d4:infod6:lengthi4e4:name4:test12:piece lengthi4e6:pieces20:"
    + (b"a" * 20)
    + b"ee"
)


class Response:
    def __init__(self, *, text: str = "", payload: dict | None = None, content: bytes | None = None,
                 url: str = "", status_code: int = 200, headers: dict | None = None) -> None:
        self.text = text
        self._payload = payload
        self.content = content if content is not None else text.encode("utf-8")
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self) -> dict:
        if self._payload is None:
            return json.loads(self.text)
        return self._payload

    def iter_content(self, chunk_size: int = 128 * 1024):
        del chunk_size
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class Session:
    def __init__(self) -> None:
        self.cookies = requests.cookies.RequestsCookieJar()
        self.gallery_html = (
            '<h1 id="gn">Safe Sample Gallery</h1><h1 id="gj">サンプル</h1>'
            '<a href="#" onclick="return popUp(\'https://e-hentai.org/gallerytorrents.php?gid=1234567&amp;t=abcdef1234\')">Torrent</a>'
            '<a href="#" onclick="return popUp(\'https://e-hentai.org/archiver.php?gid=1234567&amp;token=abcdef1234\')">Archive</a>'
            '<a href="https://e-hentai.org/tag/sample"><div class="gt">other:sample</div></a>'
            '<a href="https://e-hentai.org/s/1111111111/1234567-1">Page 1</a>'
            '<a href="https://e-hentai.org/s/2222222222/1234567-2">Page 2</a>'
        )
        self.torrent_html = '<a href="https://ehtracker.org/get/123/hash.torrent">Torrent file</a>'
        self.metadata = {
            "gid": 1234567,
            "token": "abcdef1234",
            "title": "Safe Sample Gallery",
            "title_jpn": "サンプル",
            "category": "Non-H",
            "thumb": "https://ehgt.org/sample.jpg",
            "uploader": "sample-user",
            "posted": "1704067200",
            "filecount": "8",
            "filesize": 12345,
            "rating": "4.5",
            "torrentcount": "1",
            "tags": ["other:sample"],
        }
        buffer = io.BytesIO()
        Image.new("RGB", (3, 2), "blue").save(buffer, format="JPEG")
        self.image_bytes = buffer.getvalue()

    def post(self, url: str, **_kwargs) -> Response:
        self.assert_api_url = url
        return Response(payload={"gmetadata": [self.metadata]}, url=url)

    def get(self, url: str, **_kwargs) -> Response:
        if "gallerytorrents.php" in url:
            return Response(text=self.torrent_html, url=url)
        if url.endswith(".torrent"):
            return Response(content=VALID_TORRENT, url=url)
        if "/s/1111111111/1234567-1" in url:
            return Response(
                text=(
                    '<a id="next" href="https://e-hentai.org/s/2222222222/1234567-2">'
                    '<img id="img" src="https://a.hath.network/one.jpg"></a>'
                    '<a href="https://e-hentai.org/fullimg.php?gid=1234567&page=1">Download original</a>'
                ),
                url=url,
            )
        if "/s/2222222222/1234567-2" in url:
            return Response(
                text='<img src="https://b.hath.network/two.jpg" id="img">',
                url=url,
            )
        if url in {"https://a.hath.network/one.jpg", "https://b.hath.network/two.jpg"}:
            return Response(content=self.image_bytes, url=url, headers={"content-type": "image/jpeg"})
        if "/g/1234567/abcdef1234/" in url:
            return Response(text=self.gallery_html, url=url)
        raise AssertionError(f"unexpected URL: {url}")


class FavoriteSession(Session):
    def __init__(self) -> None:
        super().__init__()
        self.cookies.set("ipb_member_id", "member")
        self.cookies.set("ipb_pass_hash", "hash")
        self.category: int | None = None
        self.note = ""
        self.last_form: dict | None = None
        self.ignore_writes = False

    def _popup(self) -> str:
        inputs = []
        for category in range(10):
            checked = ' checked="checked"' if self.category == category else ""
            inputs.append(
                f'<div class="nosel"><input name="favcat" id="fav{category}" '
                f'value="{category}"{checked}>自定义分类 {category}</div>'
            )
        if self.category is not None:
            inputs.append('<input name="favcat" id="favdel" value="favdel">')
        return "".join(inputs) + f'<textarea name="favnote">{self.note}</textarea>'

    def get(self, url: str, **kwargs) -> Response:
        if "gallerypopups.php" in url:
            return Response(text=self._popup(), url=url)
        return super().get(url, **kwargs)

    def post(self, url: str, **kwargs) -> Response:
        if "gallerypopups.php" not in url:
            return super().post(url, **kwargs)
        self.last_form = dict(kwargs.get("data") or {})
        if not self.ignore_writes:
            value = self.last_form.get("favcat")
            self.category = None if value == "favdel" else int(str(value))
            self.note = str(self.last_form.get("favnote") or "") if self.category is not None else ""
        return Response(text="ok", url=url)


class EhentaiTests(unittest.TestCase):
    def test_target_routing_rejects_lookalike_hosts(self) -> None:
        target = parse_ehentai_target(GALLERY_URL)
        self.assertEqual((target.kind, target.site, target.gid, target.token),
                         ("gallery", "e-hentai", "1234567", "abcdef1234"))
        self.assertEqual(parse_ehentai_target("1234567/abcdef1234", default_site="exhentai").site, "exhentai")
        self.assertEqual(parse_ehentai_target("landscape", default_site="e-hentai").kind, "search")
        with self.assertRaises(ValueError):
            parse_ehentai_target("https://e-hentai.org.evil.example/g/1234567/abcdef1234/")

    def test_html_parsers_read_search_gallery_and_popup_links(self) -> None:
        rows = parse_search_html(
            '<a href="https://e-hentai.org/g/1234567/abcdef1234/">'
            '<div class="glink">Safe Sample Gallery</div></a>',
            "https://e-hentai.org/",
        )
        self.assertEqual(rows[0]["gid"], "1234567")
        self.assertEqual(rows[0]["title"], "Safe Sample Gallery")
        gallery = parse_gallery_html(Session().gallery_html, GALLERY_URL)
        self.assertEqual(gallery["title"], "Safe Sample Gallery")
        self.assertIn("gallerytorrents.php", gallery["torrent_page_url"])
        self.assertIn("archiver.php", gallery["archive_url"])
        self.assertEqual(gallery["tags"], ["other:sample"])

    def test_live_preview_combines_api_and_gallery_availability(self) -> None:
        preview = EhentaiClient(session=Session()).preview(parse_ehentai_target(GALLERY_URL), live=True)
        self.assertEqual(preview.title, "Safe Sample Gallery")
        self.assertEqual(preview.metadata["torrent_count"], 1)
        self.assertTrue(preview.metadata["archive_available"])
        self.assertIn("不会自动购买", preview.warnings[0])

    def test_image_page_parser_separates_display_next_and_original_urls(self) -> None:
        parsed = parse_image_page_html(
            '<a id="next" href="/s/2222222222/1234567-2">'
            '<img id="img" src="https://a.hath.network/one.jpg"></a>'
            '<a href="/fullimg.php?gid=1234567&page=1">Download original</a>',
            "https://e-hentai.org/s/1111111111/1234567-1",
        )
        self.assertEqual(parsed["image_url"], "https://a.hath.network/one.jpg")
        self.assertIn("/s/2222222222/1234567-2", parsed["next_page_url"])
        self.assertIn("fullimg.php", parsed["original_url"])

    def test_display_image_download_never_requests_original_and_torrent_is_opt_in(self) -> None:
        files = []
        session = Session()
        session.metadata["filecount"] = "2"
        with tempfile.TemporaryDirectory() as directory:
            EhentaiClient(session=session).download(
                DownloadTask(
                    "eh-images", "ehentai", GALLERY_URL, Path(directory),
                    {
                        "eh_download_method": "images",
                        "eh_download_images": True,
                        "eh_download_torrent": False,
                        "image_format": "original",
                    },
                ),
                CallbackSet(on_file=files.append),
                threading.Event(),
            )
            self.assertEqual([row.metadata["kind"] for row in files], [
                "eh_metadata", "eh_display_image", "eh_display_image",
            ])
            self.assertTrue(all(row.path.is_file() for row in files))
            self.assertTrue(all(not row.metadata.get("uses_original") for row in files[1:]))
            self.assertFalse(any(row.path.suffix == ".torrent" for row in files))

    def test_gallery_image_pages_follow_thumbnail_pagination(self) -> None:
        class PagingSession(Session):
            def get(self, url: str, **kwargs) -> Response:
                if "?p=1" in url:
                    return Response(
                        text='<a href="https://e-hentai.org/s/3333333333/1234567-3">Page 3</a>',
                        url=url,
                    )
                return super().get(url, **kwargs)

        client = EhentaiClient(session=PagingSession())
        target = parse_ehentai_target(GALLERY_URL)
        gallery = client.fetch_gallery(target)
        links = client.gallery_image_page_links(target, gallery, 3)
        self.assertEqual(len(links), 3)
        self.assertTrue(links[-1].endswith("/1234567-3"))

    def test_display_image_download_rejects_untrusted_cdn(self) -> None:
        class UntrustedImageSession(Session):
            def get(self, url: str, **kwargs) -> Response:
                if "/s/1111111111/1234567-1" in url:
                    return Response(text='<img id="img" src="https://evil.example/image.jpg">', url=url)
                return super().get(url, **kwargs)

        session = UntrustedImageSession()
        session.metadata["filecount"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "允许的 HTTPS 展示图"):
                EhentaiClient(session=session).download(
                    DownloadTask(
                        "eh-untrusted", "ehentai", GALLERY_URL, Path(directory),
                        {"eh_download_method": "images", "eh_download_images": True},
                    ),
                    CallbackSet(),
                    threading.Event(),
                )

    def test_exhentai_empty_page_is_reported_as_permission_failure(self) -> None:
        session = Session()
        session.get = lambda url, **_kwargs: Response(text="", url=url)
        with self.assertRaisesRegex(PermissionError, "空白页"):
            EhentaiClient(session=session)._get_text("https://exhentai.org/")

    def test_download_preserves_metadata_and_torrent_without_direct_token_url(self) -> None:
        files = []
        progress = []
        with tempfile.TemporaryDirectory() as directory:
            client = EhentaiClient(session=Session())
            client.download(
                DownloadTask(
                    "eh-test", "ehentai", GALLERY_URL, Path(directory),
                    {"eh_download_method": "torrent", "max_torrents": 1},
                ),
                CallbackSet(on_file=files.append, on_progress=progress.append),
                threading.Event(),
            )
            self.assertEqual({row.metadata["kind"] for row in files}, {"eh_metadata", "torrent"})
            metadata_record = next(row for row in files if row.metadata["kind"] == "eh_metadata")
            payload = json.loads(metadata_record.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["gid"], "1234567")
            self.assertNotIn("ehtracker.org/get", metadata_record.path.read_text(encoding="utf-8"))
            torrent = next(row for row in files if row.metadata["kind"] == "torrent")
            self.assertTrue(torrent.path.read_bytes().startswith(b"d"))
            self.assertEqual(torrent.metadata["torrent_file_count"], 1)
            self.assertEqual(torrent.metadata["torrent_total_size"], 4)
            self.assertEqual(len(torrent.metadata["torrent_info_hash"]), 40)
            self.assertEqual(torrent.source_url, GALLERY_URL)

    def test_bt_option_hands_validated_torrent_to_aria2_and_records_content(self) -> None:
        files = []
        with tempfile.TemporaryDirectory() as directory:
            def fake_download(_torrent, output, _cancel, **_kwargs):
                output.mkdir(parents=True, exist_ok=True)
                payload = output / "payload.bin"
                payload.write_bytes(b"downloaded")
                return BtDownloadResult(output, (payload.resolve(),), payload.stat().st_size)

            with patch("software_app.crawlers.ehentai.client.download_torrent_with_aria2", side_effect=fake_download):
                EhentaiClient(session=Session()).download(
                    DownloadTask(
                        "eh-bt", "ehentai", GALLERY_URL, Path(directory),
                        {
                            "eh_download_method": "images",
                            "eh_download_images": False,
                            "eh_download_torrent": False,
                            "eh_bt_download_enabled": True,
                            "max_torrents": 1,
                        },
                    ),
                    CallbackSet(on_file=files.append),
                    threading.Event(),
                )
            kinds = [row.metadata["kind"] for row in files]
            self.assertIn("torrent", kinds)
            self.assertIn("torrent_content", kinds)

    def test_download_refuses_paid_archive_before_any_request(self) -> None:
        session = Session()
        session.post = lambda *_args, **_kwargs: self.fail("archive refusal must not contact the site")
        session.get = lambda *_args, **_kwargs: self.fail("archive refusal must not contact the site")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "不会自动购买"):
                EhentaiClient(session=session).download(
                    DownloadTask(
                        "eh-archive-refusal", "ehentai", GALLERY_URL, Path(directory),
                        {"eh_download_method": "archive"},
                    ),
                    CallbackSet(),
                    threading.Event(),
                )
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_adapter_is_alpha_and_recognizes_only_eh_targets(self) -> None:
        adapter = EhentaiNativeAdapter()
        self.assertEqual(adapter.info.stage, "alpha")
        self.assertTrue(adapter.can_handle(GALLERY_URL))
        self.assertFalse(adapter.can_handle("https://example.com/g/1234567/abcdef1234/"))

    def test_table_and_inner_accounts_are_stored_and_sent_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = EhentaiNativeAdapter()
            adapter.runtime_data_dir = root
            (root / "e-hentai-cookies.json").write_text(json.dumps({
                "ipb_member_id": "table-account",
                "ipb_pass_hash": "table-hash",
            }), encoding="utf-8")
            (root / "exhentai-cookies.json").write_text(json.dumps({
                "ipb_member_id": "inner-account",
                "ipb_pass_hash": "inner-hash",
                "igneous": "inner-access",
            }), encoding="utf-8")
            (root / "e-hentai-browser.json").write_text(
                json.dumps({"user_agent": "Table Chrome UA"}), encoding="utf-8"
            )
            (root / "exhentai-browser.json").write_text(
                json.dumps({"user_agent": "Inner Chrome UA"}), encoding="utf-8"
            )

            table = adapter._client(site="e-hentai")
            inner = adapter._client(site="exhentai")
            table_header = table.session.prepare_request(
                requests.Request("GET", "https://e-hentai.org/favorites.php")
            ).headers.get("Cookie", "")
            inner_header = inner.session.prepare_request(
                requests.Request("GET", "https://exhentai.org/favorites.php")
            ).headers.get("Cookie", "")
            self.assertIn("table-account", table_header)
            self.assertNotIn("inner-account", table_header)
            self.assertIn("inner-account", inner_header)
            self.assertNotIn("table-account", inner_header)
            self.assertNotIn("Cookie", table.session.prepare_request(
                requests.Request("GET", "https://exhentai.org/")
            ).headers)
            self.assertNotIn("Cookie", inner.session.prepare_request(
                requests.Request("GET", "https://ehtracker.org/get/example.torrent")
            ).headers)
            self.assertEqual(table.session.headers["User-Agent"], "Table Chrome UA")
            self.assertEqual(inner.session.headers["User-Agent"], "Inner Chrome UA")
            self.assertTrue(adapter.cookie_status("e-hentai")["has_login"])
            self.assertTrue(adapter.cookie_status("exhentai")["has_login"])

    def test_cookie_import_requires_inner_access_cookie_and_never_overwrites_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = EhentaiNativeAdapter()
            adapter.runtime_data_dir = root / "runtime"
            table_source = root / "table.txt"
            inner_source = root / "inner.txt"
            incomplete_inner = root / "inner-incomplete.txt"
            table_source.write_text(
                "Cookie: ipb_member_id=table-id; ipb_pass_hash=table-hash", encoding="utf-8"
            )
            inner_source.write_text(
                "Cookie: ipb_member_id=inner-id; ipb_pass_hash=inner-hash; igneous=inner-access",
                encoding="utf-8",
            )
            incomplete_inner.write_text(
                "Cookie: ipb_member_id=inner-id; ipb_pass_hash=inner-hash", encoding="utf-8"
            )
            adapter.import_cookie_file(table_source, site="e-hentai")
            before = (adapter.runtime_data_dir / "e-hentai-cookies.json").read_text(encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "里站.*不完整"):
                adapter.import_cookie_file(incomplete_inner, site="exhentai")
            adapter.import_cookie_file(inner_source, site="exhentai")
            self.assertEqual(
                (adapter.runtime_data_dir / "e-hentai-cookies.json").read_text(encoding="utf-8"), before
            )
            self.assertNotEqual(
                json.loads(before)["ipb_member_id"],
                json.loads((adapter.runtime_data_dir / "exhentai-cookies.json").read_text(encoding="utf-8"))[
                    "ipb_member_id"
                ],
            )

    def test_preview_image_rejects_non_eh_hosts(self) -> None:
        adapter = EhentaiNativeAdapter()
        with self.assertRaisesRegex(ValueError, "ehgt.org"):
            adapter.fetch_preview_image_bytes("https://ehgt.org.evil.example/cover.jpg")

    def test_favorite_add_update_and_remove_are_verified_after_write(self) -> None:
        session = FavoriteSession()
        client = EhentaiClient(session=session)
        target = parse_ehentai_target(GALLERY_URL)
        added = client.set_favorite(target, 3, "测试 note")
        self.assertTrue(added["changed"])
        self.assertEqual((added["category"], added["note"]), (3, "测试 note"))
        self.assertEqual(added["categories"][3], "自定义分类 3")
        self.assertEqual(session.last_form["apply"], "Add to Favorites")
        unchanged = client.set_favorite(target, 3, "测试 note")
        self.assertFalse(unchanged["changed"])
        removed = client.set_favorite(target, None)
        self.assertTrue(removed["changed"])
        self.assertFalse(removed["favorited"])
        self.assertEqual(session.last_form["favcat"], "favdel")

    def test_favorite_rejects_unverified_write_and_oversized_utf8_note(self) -> None:
        session = FavoriteSession()
        client = EhentaiClient(session=session)
        target = parse_ehentai_target(GALLERY_URL)
        with self.assertRaisesRegex(ValueError, "200 字节"):
            client.set_favorite(target, 0, "中" * 67)
        session.ignore_writes = True
        with self.assertRaisesRegex(RuntimeError, "状态未生效"):
            client.set_favorite(target, 2, "note")

    def test_favorite_target_cannot_cross_table_and_inner_sessions(self) -> None:
        session = FavoriteSession()
        with self.assertRaisesRegex(ValueError, "会话一致"):
            EhentaiClient(session=session, site="e-hentai").favorite_state(
                parse_ehentai_target("https://exhentai.org/g/1234567/abcdef1234/")
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from software_app.core.browser_cookie_capture import (
    COOKIE_CAPTURE_PLATFORMS,
    _browser_session_is_valid,
    convert_header_capture_to_cookie_json,
    cookie_capture_spec,
    host_is_allowed,
    import_cookie_file,
    save_captured_cookies,
)
from software_app.core.ehentai_cookie import (
    convert_eh_header_capture_to_json,
    import_ehentai_cookie_file,
)
from software_app.core.jmcomic_plain_browser import (
    _decrypt_chrome_cookie_value,
    _enable_session_restore,
    capture_jmcomic_plain_chrome,
    chrome_request_headers,
    filter_jm_cookies,
    headers_from_performance_logs,
    parse_devtools_active_port,
    read_chrome_profile_cookies,
    safe_request_headers,
)


class BrowserCookieCaptureTests(unittest.TestCase):
    def test_jmcomic_uses_its_plain_chrome_flow(self) -> None:
        self.assertNotIn("JMComic", COOKIE_CAPTURE_PLATFORMS)
        self.assertEqual(parse_devtools_active_port("9222\n/devtools/browser/id\n"), "127.0.0.1:9222")
        with self.assertRaises(ValueError):
            parse_devtools_active_port("invalid")

    def test_platform_specs_require_the_sessions_used_by_adapters(self) -> None:
        self.assertEqual(cookie_capture_spec("Twitter/X").required_names, {"auth_token", "ct0"})
        self.assertEqual(cookie_capture_spec("Pixiv").required_names, {"PHPSESSID"})
        self.assertEqual(cookie_capture_spec("FANBOX").required_names, {"FANBOXSESSID"})
        self.assertEqual(cookie_capture_spec("Instagram").required_names, {"sessionid"})
        table = cookie_capture_spec("E-Hentai 表站")
        inner = cookie_capture_spec("ExHentai 里站")
        self.assertEqual(table.required_names, {"ipb_member_id", "ipb_pass_hash"})
        self.assertEqual(inner.required_names, {"ipb_member_id", "ipb_pass_hash", "igneous"})
        self.assertEqual(table.destination.name, "e-hentai-cookies.json")
        self.assertEqual(inner.destination.name, "exhentai-cookies.json")
        self.assertNotEqual(table.destination, inner.destination)
        self.assertIn("E-Hentai 表站", COOKIE_CAPTURE_PLATFORMS)
        self.assertIn("ExHentai 里站", COOKIE_CAPTURE_PLATFORMS)
        jmcomic = cookie_capture_spec("JMComic", jmcomic_domain="https://18comic.vip", jmcomic_username="example")
        self.assertEqual(jmcomic.required_names, {"AVS"})
        self.assertEqual(jmcomic.allowed_hosts, ("18comic.vip",))
        self.assertEqual(jmcomic.validation_kind, "jmcomic_account")
        self.assertIn("/user/example/favorite/albums", jmcomic.validation_url)

    def test_host_check_accepts_real_subdomains_and_rejects_lookalikes(self) -> None:
        self.assertTrue(host_is_allowed("www.instagram.com", ("instagram.com",)))
        self.assertFalse(host_is_allowed("instagram.com.example.org", ("instagram.com",)))
        self.assertFalse(host_is_allowed("", ("instagram.com",)))

    def test_pixiv_and_fanbox_capture_merge_shared_cookie_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "cookies.json"
            destination.write_text(json.dumps({"FANBOXSESSID": "fanbox-old"}), encoding="utf-8")
            pixiv = replace(cookie_capture_spec("Pixiv"), destination=destination)
            result = save_captured_cookies(
                pixiv,
                [
                    {"name": "PHPSESSID", "value": "123_account", "domain": ".pixiv.net", "path": "/"},
                    {"name": "device_token", "value": "device"},
                ],
            )
            records = json.loads(destination.read_text(encoding="utf-8"))
            self.assertIsInstance(records, list)
            saved = {item["name"]: item["value"] for item in records}
            self.assertEqual(saved["FANBOXSESSID"], "fanbox-old")
            self.assertEqual(saved["PHPSESSID"], "123_account")
            self.assertEqual(result.cookie_count, 3)

            fanbox = replace(cookie_capture_spec("FANBOX"), destination=destination)
            save_captured_cookies(fanbox, [{"name": "FANBOXSESSID", "value": "fanbox-new"}])
            saved = {item["name"]: item["value"] for item in json.loads(destination.read_text(encoding="utf-8"))}
            self.assertEqual(saved["PHPSESSID"], "123_account")
            self.assertEqual(saved["FANBOXSESSID"], "fanbox-new")

    def test_twitter_capture_keeps_selenium_cookie_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "X_cookie.json"
            spec = replace(cookie_capture_spec("Twitter/X"), destination=destination)
            save_captured_cookies(
                spec,
                [
                    {"name": "auth_token", "value": "secret-a", "domain": ".x.com", "path": "/", "secure": True},
                    {"name": "ct0", "value": "secret-b", "domain": ".x.com", "path": "/", "httpOnly": False},
                ],
            )
            saved = json.loads(destination.read_text(encoding="utf-8"))
            self.assertIsInstance(saved, list)
            self.assertEqual({item["name"] for item in saved}, {"auth_token", "ct0"})
            self.assertEqual(saved[0]["domain"], ".x.com")

    def test_incomplete_login_is_not_saved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "cookies.json"
            spec = replace(cookie_capture_spec("Instagram"), destination=destination)
            with self.assertRaisesRegex(ValueError, "sessionid"):
                save_captured_cookies(spec, [{"name": "csrftoken", "value": "token"}])
            self.assertFalse(destination.exists())

    def test_twitter_cookie_header_import_creates_selenium_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "cookie.txt"
            source.write_text("Cookie: auth_token=first; ct0=second; lang=zh-cn", encoding="utf-8")
            destination = root / "X_cookie.json"
            spec = replace(cookie_capture_spec("Twitter/X"), destination=destination)
            result = import_cookie_file(spec, source)
            saved = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(result.cookie_count, 3)
            self.assertEqual({row["name"] for row in saved}, {"auth_token", "ct0", "lang"})
            self.assertTrue(all(row["domain"] == ".x.com" for row in saved))

    def test_browser_validation_rejects_guest_sessions(self) -> None:
        class Driver:
            def __init__(self, response):
                self.response = response

            def execute_async_script(self, _script, _url):
                return self.response

        pixiv = cookie_capture_spec("Pixiv")
        self.assertTrue(
            _browser_session_is_valid(
                Driver({"status": 200, "url": pixiv.validation_url, "text": '{"error":false,"body":{}}'}),
                pixiv,
                "https://www.pixiv.net/",
            )
        )
        self.assertFalse(
            _browser_session_is_valid(
                Driver({"status": 401, "url": pixiv.validation_url, "text": '{"error":true,"body":[]}'}),
                pixiv,
                "https://www.pixiv.net/",
            )
        )
        self.assertFalse(
            _browser_session_is_valid(
                Driver({"status": 200, "url": "https://accounts.pixiv.net/login", "text": ""}),
                pixiv,
                "https://accounts.pixiv.net/login",
            )
        )

    def test_eh_devtools_header_import_validates_before_saving(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "headers.txt"
            destination = root / "inner.json"
            source.write_text(
                "Cookie:\n"
                "ipb_member_id=member; ipb_pass_hash=hash; igneous=access; sk=session\n"
                "User-Agent:\nChrome Test UA\n",
                encoding="utf-8",
            )
            real_spec = cookie_capture_spec

            def spec_for(label: str):
                return replace(real_spec(label), destination=destination)

            module = "software_app.core.ehentai_cookie"
            with (
                patch(f"{module}.cookie_capture_spec", side_effect=spec_for),
                patch(f"{module}._validate_eh_session") as validate,
            ):
                result = import_ehentai_cookie_file("ExHentai 里站", source, validate_online=True)
            self.assertEqual(result.cookie_count, 4)
            self.assertEqual(
                {item["name"]: item["value"] for item in json.loads(destination.read_text(encoding="utf-8"))}[
                    "igneous"
                ],
                "access",
            )
            self.assertEqual(validate.call_args.kwargs["user_agent"], "Chrome Test UA")

    def test_eh_header_capture_converts_to_cookie_only_json_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "capture.txt"
            source.write_text(
                ":method:\nGET\n:path:\n/z/site.css\nCookie:\n"
                "ipb_member_id=member; ipb_pass_hash=hash; igneous=access; sk=session\n"
                "Content-Type:\ntext/css\nServer:\ncloudflare\n",
                encoding="utf-8",
            )
            first = convert_eh_header_capture_to_json("ExHentai 里站", source)
            second = convert_eh_header_capture_to_json("ExHentai 里站", source)
            self.assertNotEqual(first.destination, second.destination)
            payload = json.loads(first.destination.read_text(encoding="utf-8"))
            self.assertEqual(set(payload), {"ipb_member_id", "ipb_pass_hash", "igneous", "sk"})
            self.assertNotIn("Content-Type", payload)

    def test_eh_header_conversion_rejects_missing_inner_cookie_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "capture.txt"
            source.write_text("Cookie:\nipb_member_id=member; ipb_pass_hash=hash\n", encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "igneous"):
                convert_eh_header_capture_to_json("ExHentai 里站", source)
            self.assertEqual(list(root.glob("*.json")), [])

    def test_header_converter_supports_every_cookie_platform(self) -> None:
        cases = (
            ("Twitter/X", "auth_token=a; ct0=b", {}),
            ("Pixiv", "PHPSESSID=a", {}),
            ("FANBOX", "FANBOXSESSID=a", {}),
            ("Instagram", "sessionid=a", {}),
            ("E-Hentai 表站", "ipb_member_id=a; ipb_pass_hash=b", {}),
            ("ExHentai 里站", "ipb_member_id=a; ipb_pass_hash=b; igneous=c", {}),
            (
                "JMComic",
                "AVS=a; cf_clearance=b",
                {
                    "jmcomic_domain": "https://18comic.vip",
                    "jmcomic_username": "example",
                },
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (label, cookie_line, kwargs) in enumerate(cases):
                with self.subTest(platform=label):
                    source = root / f"capture-{index}.txt"
                    source.write_text(
                        f":method:\nGET\nCookie:\n{cookie_line}\nServer:\ncloudflare\n",
                        encoding="utf-8",
                    )
                    spec = cookie_capture_spec(label, **kwargs)
                    converted = convert_header_capture_to_cookie_json(spec, source)
                    payload = json.loads(converted.destination.read_text(encoding="utf-8"))
                    self.assertTrue(spec.required_names.issubset(payload))
                    self.assertNotIn("Server", payload)

    def test_jm_plain_browser_filters_cookies_and_request_headers(self) -> None:
        cookies = filter_jm_cookies(
            [
                {"name": "AVS", "value": "secret", "domain": ".18comic.vip"},
                {"name": "other", "value": "secret", "domain": ".example.com"},
            ],
            {"18comic.vip"},
        )
        self.assertEqual([item["name"] for item in cookies], ["AVS"])
        safe = safe_request_headers(
            {
                "User-Agent": "Chrome",
                "Accept-Language": "zh-CN",
                "Cookie": "AVS=secret",
                "Authorization": "secret",
            }
        )
        self.assertEqual(safe, {"user-agent": "Chrome", "accept-language": "zh-CN"})
        account_url = "https://18comic.vip/user/example/favorite/albums"
        event = {
            "message": json.dumps(
                {
                    "message": {
                        "method": "Network.requestWillBeSent",
                        "params": {
                            "request": {
                                "url": account_url,
                                "headers": {
                                    "User-Agent": "Chrome",
                                    "Cookie": "AVS=secret",
                                },
                            }
                        },
                    }
                }
            )
        }
        self.assertEqual(headers_from_performance_logs([event], account_url), {"user-agent": "Chrome"})

    def test_jm_plain_browser_reads_encrypted_avs_from_profile(self) -> None:
        from Crypto.Cipher import AES

        key = b"k" * 32
        host = ".18comic.vip"
        value = "session-value"
        nonce = b"n" * 12
        plaintext = hashlib.sha256(host.encode("utf-8")).digest() + value.encode("utf-8")
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        encrypted, tag = cipher.encrypt_and_digest(plaintext)
        payload = b"v10" + nonce + encrypted + tag
        self.assertEqual(_decrypt_chrome_cookie_value(payload, key, host, 24), value)

        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "中文资料"
            database = profile / "Default" / "Network" / "Cookies"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE meta (key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR)")
                connection.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("version", "24"))
                connection.execute(
                    "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, "
                    "path TEXT, is_secure INTEGER, is_httponly INTEGER, expires_utc INTEGER)"
                )
                connection.execute(
                    "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (host, "AVS", "", payload, "/", 1, 1, 0),
                )
                connection.commit()
            finally:
                connection.close()
            cookies = read_chrome_profile_cookies(profile, master_key=key)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["name"], "AVS")
        self.assertEqual(cookies[0]["value"], value)

    def test_jm_plain_profile_keeps_session_cookies_after_normal_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "专用资料"
            preferences = profile / "Default" / "Preferences"
            preferences.parent.mkdir(parents=True)
            preferences.write_text(
                json.dumps({"session": {"startup_urls": ["https://example.test/"]}}), encoding="utf-8"
            )
            _enable_session_restore(profile)
            saved = json.loads(preferences.read_text(encoding="utf-8"))
        self.assertEqual(saved["session"]["restore_on_startup"], 1)
        self.assertEqual(saved["session"]["startup_urls"], ["https://example.test/"])

    def test_jm_plain_capture_builds_matching_navigation_headers(self) -> None:
        user_agent = "Mozilla/5.0 Chrome/122.0.0.0 Safari/537.36"
        headers = chrome_request_headers("https://18comic.vip", user_agent)
        self.assertEqual(headers["user-agent"], user_agent)
        self.assertEqual(headers["referer"], "https://18comic.vip/")
        self.assertIn('"122"', headers["sec-ch-ua"])
        self.assertEqual(headers["sec-ch-ua-platform"], '"Windows"')
        self.assertNotIn("cookie", headers)

    def test_jm_plain_capture_updates_only_after_nonempty_avs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            profile.mkdir()
            destination = root / "cookies.json"
            destination.write_text(json.dumps({"AVS": "old-session"}), encoding="utf-8")
            spec = replace(
                cookie_capture_spec("JMComic", jmcomic_domain="https://18comic.vip", jmcomic_username="example"),
                destination=destination,
            )
            module = "software_app.core.jmcomic_plain_browser"
            with (
                patch(f"{module}.platform.system", return_value="Windows"),
                patch(f"{module}.cookie_capture_spec", return_value=spec),
                patch(f"{module}.JMCOMIC_DATA_DIR", root),
                patch(f"{module}._chrome_user_agent", return_value="Chrome UA"),
                patch(
                    f"{module}.read_chrome_profile_cookies",
                    return_value=[
                        {"name": "AVS", "value": "new-session", "domain": ".18comic.vip", "path": "/"},
                        {"name": "cf_clearance", "value": "clearance", "domain": ".18comic.vip", "path": "/"},
                    ],
                ),
            ):
                result = capture_jmcomic_plain_chrome(
                    "https://18comic.vip", "example", profile_dir=profile, wait_seconds=0.1
                )
            saved = {item["name"]: item["value"] for item in json.loads(destination.read_text(encoding="utf-8"))}
            self.assertEqual(saved["AVS"], "new-session")
            self.assertEqual(result.user_agent, "Chrome UA")

            destination.write_text(json.dumps({"AVS": "keep-session"}), encoding="utf-8")
            with (
                patch(f"{module}.platform.system", return_value="Windows"),
                patch(
                    f"{module}.read_chrome_profile_cookies",
                    return_value=[
                        {"name": "cf_clearance", "value": "clearance", "domain": ".18comic.vip", "path": "/"},
                    ],
                ),
            ):
                with self.assertRaisesRegex(PermissionError, "没有获取到当前域名的 AVS"):
                    capture_jmcomic_plain_chrome(
                        "https://18comic.vip", "example", profile_dir=profile, wait_seconds=0.1
                    )
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["AVS"], "keep-session")


if __name__ == "__main__":
    unittest.main()

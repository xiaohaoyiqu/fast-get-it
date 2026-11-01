from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import requests

from software_app.core import ehentai_cookie as eh
from software_app.core.browser_cookie_capture import capture_browser_cookies, cookie_capture_spec
from software_app.core.cookie_store import load_cookie_records


class ManualEhCookieTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.real_spec = cookie_capture_spec
        self.spec_patch = patch.object(eh, "cookie_capture_spec", side_effect=self.spec)
        self.spec_patch.start()
        self.addCleanup(self.spec_patch.stop)

    def spec(self, label):
        spec = self.real_spec(label)
        return replace(spec, destination=self.root / (spec.key + ".json"))

    def test_manual_save_is_offline_and_sites_remain_separate(self):
        with patch.object(eh, "_validate_eh_session", side_effect=AssertionError("network forbidden")):
            table = eh.save_ehentai_manual_cookies("E-Hentai 表站", {"ipb_member_id": "1", "ipb_pass_hash": "a"})
            inner = eh.save_ehentai_manual_cookies(
                "ExHentai 里站", {"ipb_member_id": "2", "ipb_pass_hash": "b", "igneous": "c"}
            )
        self.assertNotEqual(table.destination, inner.destination)
        for result, domain in ((table, ".e-hentai.org"), (inner, ".exhentai.org")):
            self.assertEqual({row["domain"] for row in load_cookie_records(result.destination, strict=True)}, {domain})

    def test_missing_or_malformed_fields_preserve_saved_cookie(self):
        result = eh.save_ehentai_manual_cookies("E-Hentai 表站", {"ipb_member_id": "1", "ipb_pass_hash": "old"})
        before = result.destination.read_bytes()
        for values in ({"ipb_member_id": ""}, {"ipb_member_id": "1", "ipb_pass_hash": "bad\nvalue"}):
            with self.assertRaises(ValueError):
                eh.save_ehentai_manual_cookies("E-Hentai 表站", values)
            self.assertEqual(result.destination.read_bytes(), before)

    def test_file_import_does_not_request_online_validation_by_default(self):
        source = self.root / "export.json"
        source.write_text('{"ipb_member_id":"1","ipb_pass_hash":"a"}', encoding="utf-8")
        with (
            patch.object(eh, "_validate_eh_session", side_effect=TimeoutError),
            patch.object(eh, "_chrome_user_agent", return_value="test"),
        ):
            result = eh.import_ehentai_cookie_file("E-Hentai 表站", source)
        self.assertEqual(result.cookie_count, 2)

    def test_explicit_validation_timeout_preserves_local_save_and_proxy(self):
        result = eh.save_ehentai_manual_cookies("E-Hentai 表站", {"ipb_member_id": "1", "ipb_pass_hash": "a"})
        before = result.destination.read_bytes()
        with patch.object(eh, "_validate_eh_session", side_effect=TimeoutError) as validate:
            with self.assertRaises(TimeoutError):
                eh.validate_saved_ehentai_cookies("E-Hentai 表站", proxy_url="http://127.0.0.1:7890")
        self.assertEqual(validate.call_args.kwargs["proxy_url"], "http://127.0.0.1:7890")
        self.assertEqual(result.destination.read_bytes(), before)

    def test_automatic_capture_rejected_before_browser_start(self):
        with patch(
            "software_app.core.browser_cookie_capture.ensure_chromedriver",
            side_effect=AssertionError("browser forbidden"),
        ):
            for platform in ("E-Hentai 表站", "ExHentai 里站"):
                with self.assertRaisesRegex(ValueError, "手动"):
                    capture_browser_cookies(self.real_spec(platform))

    def test_timeout_diagnostic_distinguishes_direct_route_without_echoing_secrets(self):
        message = eh.eh_validation_failure(requests.ConnectTimeout("sensitive-server-text"))
        self.assertIn("直连", message)
        self.assertIn("不能确认 Cookie", message)
        self.assertNotIn("sensitive-server-text", message)

    def test_proxy_failure_diagnostic_does_not_echo_proxy_credentials(self):
        message = eh.eh_validation_failure(
            requests.exceptions.ProxyError("secret"), proxy_url="http://user:password@localhost:9"
        )
        self.assertIn("代理连接失败", message)
        self.assertNotIn("password", message)
        self.assertNotIn("secret", message)
        response = requests.Response()
        response.status_code = 403
        denied = eh.eh_validation_failure(requests.HTTPError(response=response), proxy_url="http://localhost:9")
        self.assertIn("HTTP 403", denied)
        self.assertIn("登录或访问被拒绝", denied)

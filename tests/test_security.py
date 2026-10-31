import unittest

from software_app.core.security import redact_sensitive_data, redact_url_query, safe_error_message


class SecurityTests(unittest.TestCase):
    def test_authorization_schemes_and_multiple_cookies_are_fully_hidden(self):
        for header in (
            "Authorization: Bearer secret-one",
            "authorization=Basic secret-one",
            "Proxy-Authorization: Basic secret-one",
            "Cookie: first=secret-one; second=secret-two",
            "Set-Cookie: first=secret-one; Path=/; Secure",
            "x-auth-token: secret-one",
        ):
            with self.subTest(header=header):
                rendered = safe_error_message(header + "\nHTTP 403")
                self.assertNotIn("secret-", rendered)
                self.assertIn("[REDACTED]", rendered)
                self.assertIn("HTTP 403", rendered)

    def test_quoted_header_values_keep_other_diagnostic_fields(self):
        for text in (
            "{'Authorization': 'Bearer secret-one', 'Cookie': 'a=secret-two; b=secret-three', 'status': 403}",
            '{"Cookie": "a=secret-one; b=secret-two", "status": 403}',
        ):
            rendered = safe_error_message(text)
            self.assertNotIn("secret-", rendered)
            self.assertIn("status", rendered)
            self.assertIn("403", rendered)

    def test_folded_and_separate_line_header_values_are_hidden(self):
        for text in (
            "Authorization: Bearer secret-one\r\n secret-two\r\nHTTP 403",
            "Cookie:\na=secret-one; b=secret-two\nHTTP 403",
        ):
            rendered = safe_error_message(text)
            self.assertNotIn("secret-", rendered)
            self.assertIn("HTTP 403", rendered)

    def test_url_credentials_and_queries_are_hidden_together(self):
        url = "https://user:secret-password@example.com:8443/file?token=secret-token&page=2"
        for rendered in (redact_url_query(url), safe_error_message(ValueError("failed: " + url))):
            self.assertNotIn("user:", rendered)
            self.assertNotIn("secret-", rendered)
            self.assertIn("example.com:8443/file", rendered)
            self.assertIn("page=2", rendered)

    def test_url_userinfo_is_removed_without_query(self):
        self.assertEqual(redact_url_query("http://user:secret@example.com/file"), "http://example.com/file")

    def test_ipv6_and_malformed_urls_do_not_expose_credentials(self):
        for url in (
            "https://user:secret-password@[2606:4700::1111]/file?token=secret-token",
            "https://user:secret-password@[broken/file?token=secret-token",
        ):
            self.assertNotIn("secret-", safe_error_message(url))

    def test_plain_errors_and_non_sensitive_url_parameters_remain_readable(self):
        text = "HTTP 404 for https://example.com/file?page=2"
        self.assertEqual(safe_error_message(text), text)
        self.assertEqual(safe_error_message("cookie file missing"), "cookie file missing")

    def test_nested_options_still_hide_credentials(self):
        result = redact_sensitive_data({"proxy_url": "http://user:secret@example.com", "count": 3})
        self.assertNotIn("secret", str(result))
        self.assertEqual(result["count"], 3)

    def test_malformed_option_urls_and_free_text_do_not_expose_credentials(self):
        result = redact_sensitive_data(
            {
                "proxy_url": "https://user:secret-password@[broken",
                "details": [
                    "request failed https://files.example/item?token=secret-token&page=2",
                    "Cookie: first=secret-one; second=secret-two",
                ],
            }
        )
        self.assertNotIn("secret-", str(result))
        self.assertIn("page=2", result["details"][0])

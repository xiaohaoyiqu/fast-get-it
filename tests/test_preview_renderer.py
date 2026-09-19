import unittest

from software_app.ui.preview_renderer import sanitize_page_html


class StaticHtmlSanitizerTests(unittest.TestCase):
    def test_removes_executable_and_form_content(self):
        result = sanitize_page_html(
            '<h1 onclick="steal()">Title</h1><script>alert(1)</script>'
            '<form><input value="secret"></form><p>Body</p>',
            "https://example.test/page",
        )
        self.assertIn("Title", result)
        self.assertIn("Body", result)
        self.assertNotIn("steal", result)
        self.assertNotIn("alert", result)
        self.assertNotIn("secret", result)

    def test_rejects_javascript_urls(self):
        result = sanitize_page_html('<a href="javascript:alert(1)">bad</a><a href="https://ok.test">ok</a>')
        self.assertNotIn("javascript:", result)
        self.assertIn('href="https://ok.test"', result)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import Mock

import requests

from software_app.core.preview_http import read_preview_image


class PreviewHttpTests(unittest.TestCase):
    def response(self, chunks, headers=None):
        response = Mock(headers=headers or {"Content-Type": "image/png"})
        response.iter_content.return_value = iter(chunks)
        return response

    def test_stream_without_content_length_stops_at_limit_and_closes(self):
        response = self.response([b"1234", b"5678", b"must not consume"])
        with self.assertRaisesRegex(ValueError, "上限"):
            read_preview_image(response, 5)
        self.assertEqual(next(response.iter_content.return_value), b"must not consume")
        response.close.assert_called_once()

    def test_large_content_length_is_rejected_before_body_read(self):
        response = self.response([], {"Content-Type": "image/png", "Content-Length": "100"})
        with self.assertRaises(ValueError):
            read_preview_image(response, 5)
        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    def test_empty_non_image_and_http_error_responses_are_closed(self):
        for response in (self.response([]), self.response([], {"Content-Type": "text/html"})):
            with self.assertRaises(ValueError):
                read_preview_image(response, 5)
            response.close.assert_called_once()
        response = self.response([])
        response.raise_for_status.side_effect = requests.HTTPError("403", response=response)
        with self.assertRaises(requests.HTTPError):
            read_preview_image(response, 5)
        response.close.assert_called_once()

    def test_success_and_invalid_content_length_still_respect_actual_bytes(self):
        response = self.response([b"ab", b"cd"], {"Content-Type": "image/png", "Content-Length": "bad"})
        self.assertEqual(read_preview_image(response, 4), b"abcd")
        response.close.assert_called_once()

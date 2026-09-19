from __future__ import annotations

import hashlib
import unittest

from software_app.core.torrent_metadata import parse_torrent


def bencode(value) -> bytes:
    if isinstance(value, int):
        return f"i{value}e".encode("ascii")
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"
    raise TypeError(type(value).__name__)


class TorrentMetadataTests(unittest.TestCase):
    def test_single_file_summary_and_exact_info_hash(self) -> None:
        info = {
            b"length": 9,
            b"name": b"sample.bin",
            b"piece length": 4,
            b"pieces": b"a" * 60,
            b"private": 1,
        }
        payload = bencode({b"announce": b"https://tracker.example/announce", b"info": info})
        summary = parse_torrent(payload)
        self.assertEqual(summary.info_hash, hashlib.sha1(bencode(info)).hexdigest())
        self.assertEqual((summary.file_count, summary.total_size), (1, 9))
        self.assertEqual((summary.piece_length, summary.piece_count), (4, 3))
        self.assertEqual(summary.tracker_count, 1)
        self.assertTrue(summary.private)

    def test_multi_file_summary_validates_paths_and_sizes(self) -> None:
        payload = bencode({
            b"announce-list": [[b"https://one.example/a"], [b"https://two.example/a"]],
            b"info": {
                b"files": [
                    {b"length": 3, b"path": [b"chapter", b"001.jpg"]},
                    {b"length": 5, b"path": [b"chapter", b"002.jpg"]},
                ],
                b"name": b"gallery",
                b"piece length": 4,
                b"pieces": b"b" * 40,
            },
        })
        summary = parse_torrent(payload)
        self.assertEqual((summary.file_count, summary.total_size, summary.piece_count), (2, 8, 2))
        self.assertEqual(summary.tracker_count, 2)

    def test_rejects_path_traversal_piece_mismatch_and_trailing_data(self) -> None:
        base_info = {
            b"files": [{b"length": 4, b"path": [b"..", b"escape.bin"]}],
            b"name": b"unsafe",
            b"piece length": 4,
            b"pieces": b"c" * 20,
        }
        with self.assertRaisesRegex(ValueError, "不安全"):
            parse_torrent(bencode({b"info": base_info}))

        mismatch = {b"length": 9, b"name": b"bad", b"piece length": 4, b"pieces": b"d" * 20}
        with self.assertRaisesRegex(ValueError, "分片数量"):
            parse_torrent(bencode({b"info": mismatch}))

        valid = {b"length": 1, b"name": b"ok", b"piece length": 4, b"pieces": b"e" * 20}
        with self.assertRaisesRegex(ValueError, "尾随"):
            parse_torrent(bencode({b"info": valid}) + b"junk")

    def test_rejects_noncanonical_integer_and_unsafe_root_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "整数格式"):
            parse_torrent(b"d4:infod6:lengthi+4e4:name4:test12:piece lengthi4e6:pieces20:" + b"a" * 20 + b"ee")
        with self.assertRaisesRegex(ValueError, "不安全"):
            parse_torrent(b"d4:infod6:lengthi4e4:name2:..12:piece lengthi4e6:pieces20:" + b"a" * 20 + b"ee")


if __name__ == "__main__":
    unittest.main()

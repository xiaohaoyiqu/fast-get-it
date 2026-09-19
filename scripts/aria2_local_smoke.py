"""Run a lawful localhost-only aria2 torrent/web-seed smoke test."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from software_app.core.aria2_bt import aria2_status, download_torrent_with_aria2
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


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return


def main() -> int:
    status = aria2_status()
    if status.executable is None:
        raise RuntimeError("aria2 尚未安装")
    payload = (b"YuqiuDa localhost aria2 smoke test\n" * 128) + b"done\n"
    with tempfile.TemporaryDirectory(prefix="yuqiuda-aria2-smoke-") as directory:
        root = Path(directory)
        source = root / "source"
        output = root / "output"
        source.mkdir()
        (source / "payload.bin").write_bytes(payload)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, directory=str(source))
        )
        thread = threading.Thread(target=server.serve_forever, name="aria2-local-webseed", daemon=True)
        thread.start()
        try:
            port = int(server.server_address[1])
            info = {
                b"length": len(payload),
                b"name": b"payload.bin",
                b"piece length": len(payload),
                b"pieces": hashlib.sha1(payload).digest(),
            }
            torrent_bytes = bencode({
                b"info": info,
                b"url-list": f"http://127.0.0.1:{port}/payload.bin".encode("ascii"),
            })
            parse_torrent(torrent_bytes)
            torrent = root / "localhost.torrent"
            torrent.write_bytes(torrent_bytes)
            result = download_torrent_with_aria2(torrent, output, threading.Event())
            downloaded = next(path for path in result.files if path.name == "payload.bin")
            valid = hashlib.sha256(downloaded.read_bytes()).digest() == hashlib.sha256(payload).digest()
            print(json.dumps({
                "engine": status.version,
                "localhost_only": True,
                "file_count": len(result.files),
                "content_verified": valid,
                "temporary_data_removed_on_exit": True,
            }, ensure_ascii=False))
            return 0 if valid else 2
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())

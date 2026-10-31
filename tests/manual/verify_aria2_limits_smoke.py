"""Installed aria2 with generated local webseed: engine limit and cancellation."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import threading
import time


def encode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(encode(item) for item in value) + b"e"
    return b"d" + b"".join(encode(key) + encode(item) for key, item in sorted(value.items())) + b"e"


def main():
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    from software_app.core.adapter import TaskCancelled
    from software_app.core.aria2_bt import Aria2Limits, download_torrent_with_aria2, find_aria2c

    root = repo / "data/software_app/verification" / f"aria2-limits-{time.time_ns()}"
    root.mkdir(parents=True)
    payload = bytes(range(256)) * 8192
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path.startswith("/announce"):
                body = encode({b"interval": 60, b"peers": b""})
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path != "/payload.bin":
                self.send_error(404)
                return
            start, end = 0, len(payload) - 1
            header = self.headers.get("Range")
            if header:
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", header)
                if not match:
                    self.send_error(416)
                    return
                start, end = int(match[1]), int(match[2]) if match[2] else end
            if not 0 <= start <= end < len(payload):
                self.send_error(416)
                return
            requests.append((start, end))
            self.send_response(206 if header else 200)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.end_headers()
            try:
                self.wfile.write(payload[start : end + 1])
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    result = {"ok": False, "checks": []}
    stop = threading.Event()
    watchdog = threading.Timer(45, stop.set)
    watchdog.start()
    try:
        executable, _source = find_aria2c(repo / "data/software_app/tools/aria2/aria2c.exe")
        if executable is None:
            raise RuntimeError("Installed aria2 missing; this smoke does not install tools")
        base = f"http://127.0.0.1:{server.server_port}"
        piece = 16384
        torrent = root / "generated.torrent"
        torrent.write_bytes(
            encode(
                {
                    b"announce": (base + "/announce").encode(),
                    b"url-list": [(base + "/payload.bin").encode()],
                    b"info": {
                        b"name": b"payload.bin",
                        b"length": len(payload),
                        b"piece length": piece,
                        b"private": 1,
                        b"pieces": b"".join(
                            hashlib.sha1(payload[offset : offset + piece]).digest()
                            for offset in range(0, len(payload), piece)
                        ),
                    },
                }
            )
        )
        elapsed = {}
        for name, limits in (("limited", Aria2Limits(True, 256, 32)), ("unlimited", Aria2Limits())):
            started = time.monotonic()
            downloaded = download_torrent_with_aria2(
                torrent, root / name, stop, configured_path=executable, limits=limits
            )
            elapsed[name] = time.monotonic() - started
            assert downloaded.total_size == len(payload)
            assert hashlib.sha256(downloaded.files[0].read_bytes()).digest() == hashlib.sha256(payload).digest()
            result["checks"].append(name + " local webseed content matches generated payload")
        assert elapsed["limited"] >= len(payload) / (256 * 1024) * 0.65, elapsed
        assert elapsed["limited"] > elapsed["unlimited"] + 1.0, elapsed
        result["checks"].append("installed engine download limit changes observed transfer time")
        cancel = threading.Event()
        timer = threading.Timer(0.7, cancel.set)
        timer.start()
        try:
            try:
                download_torrent_with_aria2(
                    torrent, root / "cancelled", cancel, configured_path=executable, limits=Aria2Limits(True, 16, 8)
                )
            except TaskCancelled:
                result["checks"].append("limited production runner cancels and reaps child")
            else:
                raise AssertionError("limited transfer ignored cancellation")
        finally:
            timer.cancel()
            timer.join()
        result.update(
            ok=True,
            elapsed_seconds=elapsed,
            resource_requests=len(requests),
            upload_measurement="not measured; local tracker has no peers",
        )
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc))
    finally:
        watchdog.cancel()
        watchdog.join()
        server.shutdown()
        server.server_close()
        serving.join(3)
    (root / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "report.json"), **result}))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

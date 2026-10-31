"""Measure media pacing against a local generated HTTP payload; no platform accounts."""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    from software_app.core.adapter import TaskCancelled
    from software_app.core.bandwidth import MEDIA_BANDWIDTH
    from software_app.core.http_transfer import download_http_file
    import requests

    root = repo / "data/software_app/verification" / f"bandwidth-{time.time_ns()}"
    root.mkdir(parents=True, exist_ok=False)
    content = b"generated-test-media\n" * 6554
    content = content[:128 * 1024]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("ETag", '"local-test"')
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    url = f"http://127.0.0.1:{server.server_port}/generated.mp4"

    def download(name, stop=None):
        with requests.Session() as session:
            session.trust_env = False
            return download_http_file(session, url, root / name, stop if stop is not None else threading.Event(),
                                      timeout=(2, 3), chunk_size=8192,
                                      validate_url=lambda _: None, retries=0)

    def wait_for_budget():
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with MEDIA_BANDWIDTH._condition:
                if MEDIA_BANDWIDTH._waiting:
                    return
            time.sleep(0.005)
        raise AssertionError("HTTP worker did not reach shared bandwidth wait")

    checks = []
    try:
        MEDIA_BANDWIDTH.configure(True, 128 * 1024)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(download, ("one.mp4", "two.mp4")))
        elapsed = time.monotonic() - started
        if results != [len(content), len(content)] or elapsed < 1.75:
            raise AssertionError(f"shared budget not respected: {results}, {elapsed}")
        expected = hashlib.sha256(content).digest()
        for name in ("one.mp4", "two.mp4"):
            if hashlib.sha256((root / name).read_bytes()).digest() != expected:
                raise AssertionError("downloaded bytes changed")
        checks.append("two HTTP downloads share 128 KiB/s and keep exact bytes")

        MEDIA_BANDWIDTH.configure(True, 1024)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(download, "unlimited.mp4")
            wait_for_budget()
            MEDIA_BANDWIDTH.configure(False)
            if future.result(timeout=3) != len(content):
                raise AssertionError("disable failed")
        checks.append("disable releases existing HTTP waiter")

        MEDIA_BANDWIDTH.configure(True, 1024)
        stop = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(download, "cancelled.mp4", stop)
            wait_for_budget()
            stop.set()
            try:
                future.result(timeout=3)
            except TaskCancelled:
                pass
            else:
                raise AssertionError("cancel did not interrupt wait")
        if (root / "cancelled.mp4").exists():
            raise AssertionError("cancelled output published")
        checks.append("cancel interrupts HTTP limiter without publishing a partial file")
        report = {"ok": True, "checks": checks, "bytes": sum(results), "elapsed_seconds": round(elapsed, 3),
                  "average_kib_per_second": round(sum(results) / 1024 / elapsed, 3)}
    finally:
        MEDIA_BANDWIDTH.configure(False, 1024 * 1024)
        server.shutdown()
        server.server_close()
        serving.join(2)
    target = root / "report.json"
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), **report}, ensure_ascii=True))


if __name__ == "__main__":
    main()

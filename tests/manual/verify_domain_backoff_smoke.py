"""Check shared cooldowns against local generated HTTP responses; no platform access."""

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
    import requests
    from software_app.core.adapter import TaskCancelled
    from software_app.core.http_transfer import download_http_file
    from software_app.core.network_policy import HTTP_BACKOFF

    root = repo / "data/software_app/verification" / f"domain-backoff-{time.time_ns()}"
    root.mkdir(parents=True, exist_ok=False)
    payload = b"generated-domain-backoff-test\n" * 100
    hits, checks = [], []
    long_deadlines = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            with lock:
                hits.append({"port": self.server.server_port, "path": self.path, "time": time.monotonic()})
            limited = self.path in {"/limited", "/long-429", "/long-503"}
            self.send_response((503 if self.path == "/long-503" else 429) if limited else 200)
            if limited:
                self.send_header("Retry-After", "1" if self.path == "/limited" else "3600")
            content = b"" if limited else payload
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _index in range(2)]
    threads = []
    for server in servers:
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    first = f"http://127.0.0.1:{servers[0].server_port}"
    second = f"http://127.0.0.1:{servers[1].server_port}"
    HTTP_BACKOFF.clear()

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def download(url, name, control=None):
        with requests.Session() as session:
            session.trust_env = False
            path = root / name
            download_http_file(
                session,
                url,
                path,
                control or threading.Event(),
                timeout=(2, 2),
                chunk_size=1024,
                validate_url=lambda _url: None,
                retries=0,
            )
            return path

    try:
        try:
            download(first + "/limited", "limited.bin")
            raise AssertionError("expected HTTP 429")
        except requests.HTTPError:
            pass
        check("exhausted_request_publishes_cooldown", HTTP_BACKOFF.remaining(first) > 0.5)
        cancel = threading.Event()
        with ThreadPoolExecutor(3) as pool:
            one = pool.submit(download, first + "/one", "one.bin")
            two = pool.submit(download, first + "/two", "two.bin")
            cancelled = pool.submit(download, first + "/cancelled", "cancelled.bin", cancel)
            cancel.set()
            try:
                cancelled.result(2)
                raise AssertionError("cancelled request was sent")
            except TaskCancelled:
                pass
            check("cancellation_prevents_request", not any(hit["path"] == "/cancelled" for hit in hits))
            paths = [one.result(10), two.result(10)]
        origin = next(hit["time"] for hit in hits if hit["path"] == "/limited")
        media_hits = [hit for hit in hits if hit["path"] in {"/one", "/two"}]
        check(
            "same_host_requests_wait_for_shared_deadline",
            len(media_hits) == 2 and all(hit["time"] - origin >= 0.9 for hit in media_hits),
        )
        digest = hashlib.sha256(payload).hexdigest()
        check("payloads_match", all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path in paths))
        for status in (429, 503):
            HTTP_BACKOFF.clear()
            try:
                download(first + f"/long-{status}", f"long-{status}.bin")
                raise AssertionError("expected rate limit response")
            except requests.HTTPError as exc:
                check(f"long_{status}_status", exc.response.status_code == status)
            long_deadlines[str(status)] = HTTP_BACKOFF.remaining(first)
            # Subtracting real monotonic floats can yield 3600 + ~2e-12.
            # One microsecond tolerates arithmetic noise, not an early retry.
            check(f"long_{status}_full_deadline", 3500 < long_deadlines[str(status)] <= 3600 + 1e-6)
            if status == 429:
                # Exercise port isolation while the first host is still under a
                # long deadline; a one-second deadline can expire during fsync.
                with ThreadPoolExecutor(1) as pool:
                    other = pool.submit(download, second + "/other", "other.bin")
                    other.result(10)
                check("different_port_is_not_delayed", HTTP_BACKOFF.remaining(first) > 3500)
            stop = threading.Event()
            timer = threading.Timer(0.2, stop.set)
            timer.start()
            try:
                with ThreadPoolExecutor(1) as pool:
                    waiting = pool.submit(download, first + f"/blocked-{status}", f"blocked-{status}.bin", stop)
                    try:
                        waiting.result(2)
                        raise AssertionError("long cooldown request was sent")
                    except TaskCancelled:
                        pass
            finally:
                stop.set()
                timer.cancel()
                timer.join()
            check(
                f"long_{status}_cancel_without_request_or_output",
                not any(hit["path"] == f"/blocked-{status}" for hit in hits)
                and not (root / f"blocked-{status}.bin").exists()
                and HTTP_BACKOFF.remaining(first) > 3500,
            )
        result = {
            "ok": True,
            "checks": checks,
            "request_delays_seconds": [round(hit["time"] - origin, 4) for hit in media_hits],
            "long_deadlines_seconds": long_deadlines,
        }
    except Exception as exc:
        result = {
            "ok": False,
            "checks": checks,
            "error": f"{type(exc).__name__}: {exc}",
            "hits": hits,
            "long_deadlines_seconds": long_deadlines,
        }
    finally:
        HTTP_BACKOFF.clear()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(2)
    (root / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "report.json"), **result}, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

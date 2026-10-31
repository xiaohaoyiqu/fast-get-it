"""Check redirected resume identity using a real local HTTP server."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def main():
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    import requests
    from software_app.core.http_transfer import download_http_file
    from software_app.core.transfer_workspace import workspace_paths

    root = repo / "data/software_app/verification" / f"http-redirect-runtime-{time.time_ns()}"
    root.mkdir(parents=True)
    report = {"ok": False, "checks": []}
    state = {"target": "/a", "interrupt": True, "calls": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state["calls"].append((self.path, self.headers.get("Range")))
            if self.path == "/entry":
                self.send_response(302)
                self.send_header("Location", state["target"])
                self.end_headers()
                return
            old = self.path == "/a"
            payload = b"OLDold" if old else b"NEWnew"
            ranged = self.headers.get("Range") == "bytes=3-"
            self.send_response(206 if ranged else 200)
            self.send_header("ETag", '"shared"')
            self.send_header("Content-Length", str(3 if ranged else 6))
            if ranged:
                self.send_header("Content-Range", "bytes 3-5/6")
            self.end_headers()
            if state["interrupt"]:
                state["interrupt"] = False
                self.wfile.write(payload[:3])
                self.wfile.flush()
                self.close_connection = True
            else:
                self.wfile.write(payload[3:] if ranged else payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/entry"

        def validate(target):
            parsed = urlsplit(target)
            assert parsed.hostname == "127.0.0.1" and parsed.port == server.server_port

        with tempfile.TemporaryDirectory(dir=root) as directory, requests.Session() as session:
            session.trust_env = False
            for mode, target in (
                ("same_target", "/a"),
                ("path_change", "/b"),
                ("query_change", "/a?token=generated"),
                ("legacy_state", "/b"),
            ):
                destination = Path(directory) / f"{mode}.bin"
                state.update(target="/a", interrupt=True, calls=[])

                def download(destination=destination):
                    return download_http_file(
                        session,
                        url,
                        destination,
                        threading.Event(),
                        timeout=(2, 2),
                        chunk_size=3,
                        validate_url=validate,
                        retries=0,
                    )

                try:
                    download()
                except requests.RequestException:
                    pass
                else:
                    raise AssertionError("Truncated fixture unexpectedly succeeded")
                _folder, partial, metadata = workspace_paths(destination)
                assert partial.read_bytes() == b"OLD"
                text = metadata.read_text(encoding="utf-8")
                assert "/a" not in text and "127.0.0.1" not in text
                if mode == "legacy_state":
                    legacy = json.loads(text)
                    legacy["version"] = 1
                    legacy.pop("final_key")
                    metadata.write_text(json.dumps(legacy), encoding="utf-8")
                state.update(target=target, calls=[])
                download()
                expected = b"OLDold" if mode == "same_target" else b"NEWnew"
                assert destination.read_bytes() == expected
                calls = state["calls"]
                if mode in {"path_change", "query_change"}:
                    assert len(calls) == 4
                    assert calls[0][1] == "bytes=3-" and calls[2][1] is None
                elif mode == "legacy_state":
                    assert all(byte_range is None for _path, byte_range in calls)
                else:
                    assert len(calls) == 2 and calls[1][1] == "bytes=3-"
                assert not partial.exists() and not metadata.exists()
                report["checks"].append(
                    {"mode": mode, "requests": len(calls), "sha256": hashlib.sha256(expected).hexdigest()}
                )
        report["ok"] = True
    except Exception as error:
        report.update(error_type=type(error).__name__, error=str(error))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
    target = root / "report.json"
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), **report}, ensure_ascii=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

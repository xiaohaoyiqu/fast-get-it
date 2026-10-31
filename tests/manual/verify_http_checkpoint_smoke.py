"""Real HTTP and os._exit recovery of verified prefixes, no platform requests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--child-url")
    parser.add_argument("--child-output", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    import requests
    from software_app.core import http_checkpoint as checkpoint
    from software_app.core.http_transfer import download_http_file
    from software_app.core.transfer_workspace import workspace_paths

    def validate(url):
        assert urlsplit(url).hostname == "127.0.0.1"

    def transfer(session, url, destination):
        return download_http_file(
            session,
            url,
            destination,
            threading.Event(),
            timeout=(3, 5),
            chunk_size=128 * 1024,
            validate_url=validate,
            retries=0,
        )

    if args.child_url:
        original = checkpoint.CheckpointWriter.write

        def die_after_tail(writer, chunk):
            original(writer, chunk)
            if writer.size >= 5 * 1024 * 1024:
                writer.output.flush()
                os.fsync(writer.output.fileno())
                os._exit(23)  # No finally, no atexit: real abrupt process loss.

        with (
            requests.Session() as session,
            patch.object(checkpoint, "CHECKPOINT_SECONDS", 3600),
            patch.object(checkpoint.CheckpointWriter, "write", die_after_tail),
        ):
            session.trust_env = False
            transfer(session, args.child_url, args.child_output)
        return 1

    root = repo / "data/software_app/verification" / f"http-checkpoint-runtime-{time.time_ns()}"
    root.mkdir(parents=True)
    content = bytes(range(256)) * (8 * 1024 * 1024 // 256)
    expected = hashlib.sha256(content).hexdigest()
    requests_seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            byte_range = self.headers.get("Range")
            requests_seen.append(byte_range)
            offset = int(byte_range[6:-1]) if byte_range else 0
            self.send_response(206 if byte_range else 200)
            self.send_header("Content-Length", str(len(content) - offset))
            self.send_header("ETag", '"local-stable"')
            if byte_range:
                self.send_header("Content-Range", f"bytes {offset}-{len(content) - 1}/{len(content)}")
            self.end_headers()
            try:
                self.wfile.write(content[offset:])
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    report = {"ok": False, "checks": []}
    try:
        url = f"http://127.0.0.1:{server.server_port}/media?signature=generated"
        with tempfile.TemporaryDirectory(dir=root) as directory, requests.Session() as session:
            session.trust_env = False
            for mode in ("abrupt_exit_tail", "corrupted_prefix", "legacy_state"):
                destination = Path(directory) / f"{mode}.bin"
                child = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--child-url",
                        url,
                        "--child-output",
                        str(destination),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                assert child.returncode == 23, child.stderr.decode("utf-8", errors="replace")
                _folder, part, metadata = workspace_paths(destination)
                state = json.loads(metadata.read_text(encoding="utf-8"))
                assert state["checkpoint_size"] == 4 * 1024 * 1024
                assert part.stat().st_size == 5 * 1024 * 1024
                assert not destination.exists()
                assert "generated" not in metadata.read_text(encoding="utf-8")
                if mode == "corrupted_prefix":
                    with part.open("r+b") as stream:
                        stream.write(b"BAD")
                elif mode == "legacy_state":
                    state["version"] = 2
                    state.pop("sha256")
                    state.pop("checkpoint_size")
                    metadata.write_text(json.dumps(state), encoding="utf-8")
                requests_seen.clear()
                transfer(session, url, destination)
                assert hashlib.sha256(destination.read_bytes()).hexdigest() == expected
                assert requests_seen == (["bytes=4194304-"] if mode == "abrupt_exit_tail" else [None])
                assert not part.exists() and not metadata.exists()
                report["checks"].append({"mode": mode, "range": requests_seen[0], "sha256": expected})
        report["ok"] = True
    except Exception as error:
        report.update(error_type=type(error).__name__, error=str(error))
    finally:
        server.shutdown()
        server.server_close()
        serving.join(3)
    target = root / "report.json"
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), **report}, ensure_ascii=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

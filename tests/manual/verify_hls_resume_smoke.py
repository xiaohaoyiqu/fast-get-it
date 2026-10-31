"""Installed FFmpeg + local HTTP VOD: recovery and invalid-resource refusal."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from pathlib import Path
import sys
import threading
import time
from urllib.parse import unquote, urlsplit
from unittest.mock import patch


def main():
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    from PIL import Image
    from software_app.core.adapter import TaskCancelled
    from software_app.core.external_tools import find_ffmpeg
    from software_app.core.media_process import run_media_process
    from software_app.crawlers.bluesky import BlueskyClient
    from software_app.crawlers.pixiv.ugoira import _gif_to_mp4

    root = repo / "data/software_app/verification" / f"hls-runtime-{time.time_ns()}"
    root.mkdir(parents=True)
    sources = root / "sources"
    sources.mkdir()
    counts, counts_lock = {}, threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            relative = unquote(urlsplit(self.path).path).lstrip("/")
            path = (sources / relative).resolve()
            if sources.resolve() not in path.parents or not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
            etag = '"' + hashlib.sha256(data).hexdigest() + '"'
            cached = self.headers.get("If-None-Match") == etag
            with counts_lock:
                stats = counts.setdefault(relative, {"body": 0, "not_modified": 0})
                stats["not_modified" if cached else "body"] += 1
            self.send_response(304 if cached else 200)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(0 if cached else len(data)))
            self.end_headers()
            if not cached:
                self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    client, result = None, {"ok": False, "checks": []}
    try:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("FFmpeg missing")
        event = threading.Event()
        gif, mp4 = sources / "generated.gif", sources / "generated.mp4"
        with Image.new("RGB", (32, 32), "blue") as first, Image.new("RGB", (32, 32), "red") as second:
            first.save(gif, save_all=True, append_images=[second], duration=[1000, 1000], loop=0)
        _gif_to_mp4(gif, mp4, event)
        client = BlueskyClient()

        def validate(url):
            parsed = urlsplit(url)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != server.server_port:
                raise ValueError("fixture only permits its exact local HTTP origin")

        for kind in ("ts", "fmp4"):
            folder = sources / kind
            folder.mkdir()
            playlist = folder / "index.m3u8"
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(mp4),
                "-vf",
                "fps=10",
                "-c:v",
                "libx264",
                "-g",
                "10",
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_list_size",
                "0",
            ]
            if kind == "fmp4":
                command.extend(("-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", str(folder / "init.mp4")))
            command.append(str(playlist))
            assert run_media_process(command, event, timeout=30).returncode == 0
            if kind == "fmp4":
                assert (folder / "init.mp4").is_file()
                text = playlist.read_text(encoding="utf-8")
                playlist.write_text(re.sub(r'(#EXT-X-MAP:URI=")[^"]+(\")', r"\g<1>init.mp4\2", text), encoding="utf-8")
            (folder / "master.m3u8").write_text(
                "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nunused.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nindex.m3u8\n",
                encoding="utf-8",
            )
            url = f"http://127.0.0.1:{server.server_port}/{kind}/master.m3u8"
            destination = root / "outputs" / f"{kind}.mp4"
            threshold = 1 if kind == "ts" else 2

            def cancel_after_piece(done, _total, _reused, threshold=threshold):
                if done == threshold:
                    event.set()

            with patch("software_app.crawlers.bluesky.validate_external_url", side_effect=validate):
                try:
                    client._download_hls_result(url, destination, event, "overwrite", progress=cancel_after_piece)
                except TaskCancelled:
                    pass
                else:
                    raise AssertionError("cancellation was not observed")
                assert not destination.exists()
                result["checks"].append(kind + " cancellation keeps completed resources without publishing MP4")
                event.clear()
                progress = []
                client._download_hls_result(
                    url,
                    destination,
                    event,
                    "overwrite",
                    progress=lambda *args, progress=progress: progress.append(args),
                )
                assert progress[-1][2] == threshold
                result["checks"].append(kind + " resume validates completed resources using HTTP 304")
            decoded = run_media_process(
                [ffmpeg, "-v", "error", "-i", str(destination), "-f", "null", "-"], event, timeout=20
            )
            assert decoded.returncode == 0 and destination.stat().st_size > 0
            result["checks"].append(kind + " remuxed MP4 decodes with installed FFmpeg")
            # FFmpeg itself may silently omit a non-media TS response and still
            # return zero. Exercise the production guard with a real HTTP 200.
            segments = [
                line for line in playlist.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")
            ]
            last = folder / segments[-1]
            original = last.read_bytes()
            prior_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
            last.write_bytes(b"<html>temporary upstream error</html>" if kind == "ts" else original[:-1])
            try:
                with patch("software_app.crawlers.bluesky.validate_external_url", side_effect=validate):
                    try:
                        client._download_hls_result(url, destination, event, "overwrite")
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("invalid HTTP resource was accepted as video")
                assert hashlib.sha256(destination.read_bytes()).hexdigest() == prior_hash
                result["checks"].append(
                    kind + " invalid HTTP 200 resource refuses incomplete video and preserves previous output"
                )
            finally:
                last.write_bytes(original)
            repaired_progress = []
            with patch("software_app.crawlers.bluesky.validate_external_url", side_effect=validate):
                client._download_hls_result(
                    url,
                    destination,
                    event,
                    "overwrite",
                    progress=lambda *args, progress=repaired_progress: progress.append(args),
                )
            assert repaired_progress[-1][2] == threshold
            decoded = run_media_process(
                [ffmpeg, "-v", "error", "-i", str(destination), "-f", "null", "-"], event, timeout=20
            )
            assert decoded.returncode == 0
            result["checks"].append(kind + " repaired resource resumes cached pieces and final MP4 decodes")
        result.update(ok=True, requests=counts)
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc))
    finally:
        if client is not None:
            client.session.close()
        server.shutdown()
        server.server_close()
        serving.join(3)
    (root / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "report.json"), **result}))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Run installed FFmpeg on a generated GIF; HLS has a separate HTTP resume smoke."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time


def main():
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    from PIL import Image
    from software_app.core.external_tools import find_ffmpeg
    from software_app.core.media_process import run_media_process
    from software_app.crawlers.common import convert_media_file
    from software_app.crawlers.pixiv.ugoira import _gif_to_mp4

    root = repo / "data/software_app/verification" / f"media-engine-{time.time_ns()}"
    root.mkdir(parents=True)
    result = {"ok": False, "checks": []}
    try:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("FFmpeg is not installed")
        cancel = threading.Event()
        gif, converted = root / "generated.gif", root / "converted.mp4"
        with Image.new("RGB", (32, 32), "blue") as first, Image.new("RGB", (32, 32), "red") as second:
            first.save(gif, save_all=True, append_images=[second], duration=[1000, 1000], loop=0)
        _gif_to_mp4(gif, converted, cancel)
        assert converted.stat().st_size > 0
        result["checks"].append("production Pixiv GIF to MP4 with installed FFmpeg")
        decoded = run_media_process(
            [ffmpeg, "-v", "error", "-i", str(converted), "-f", "null", "-"], cancel, timeout=20
        )
        assert decoded.returncode == 0
        result["checks"].append("FFmpeg decode of published MP4")
        generic = root / "generic-converted.mp4"
        convert_media_file(gif, generic, ".mp4", cancel_event=cancel)
        assert generic.stat().st_size > 0 and not gif.exists()
        result["checks"].append("production common GIF to MP4 with installed FFmpeg")
        decoded = run_media_process([ffmpeg, "-v", "error", "-i", str(generic), "-f", "null", "-"], cancel, timeout=20)
        assert decoded.returncode == 0
        result["checks"].append("FFmpeg decode of common conversion MP4")
        result["ok"] = True
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
    (root / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "report.json"), **result}))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

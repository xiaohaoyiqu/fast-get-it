from __future__ import annotations

import os
import shutil
from pathlib import Path

from .settings import PROJECT_ROOT


def find_ffmpeg() -> str | None:
    configured = str(os.environ.get("YUQIUDA_FFMPEG") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
    resolved = shutil.which("ffmpeg")
    if resolved:
        return str(Path(resolved).resolve())
    tools = Path(PROJECT_ROOT) / "tools"
    if tools.is_dir():
        for candidate in sorted(tools.glob("ffmpeg*.exe")):
            if candidate.is_file():
                return str(candidate.resolve())
    return None


__all__ = ["find_ffmpeg"]

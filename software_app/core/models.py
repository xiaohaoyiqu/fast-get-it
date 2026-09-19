from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"}
TEXT_EXTENSIONS = {".txt", ".md", ".html", ".htm", ".json", ".csv"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | TEXT_EXTENSIONS

FINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


def classify_file(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".gif":
        return "animation"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    return "other"


@dataclass(frozen=True)
class ModuleInfo:
    module_id: str
    display_name: str
    description: str = ""
    stage: str = "ready"
    capabilities: tuple[str, ...] = ()
    script_root: Path | None = None


@dataclass(frozen=True)
class TargetPreview:
    module_id: str
    raw_target: str
    normalized_target: str
    title: str = ""
    description: str = ""
    status: str = "ok"
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DownloadTask:
    task_id: str
    module_id: str
    target: str
    output_dir: Path
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgressEvent:
    task_id: str
    module_id: str
    level: str
    message: str
    status: str = "running"
    current: int | None = None
    total: int | None = None
    percent: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FileRecord:
    path: Path
    module_id: str
    task_id: str | None = None
    media_type: str = "other"
    size: int = 0
    title: str = ""
    source_url: str = ""
    source_id: str = ""
    author_id: str = ""
    author_name: str = ""
    chapter: str = ""
    published_at: str = ""
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

from __future__ import annotations

from software_app.core.task_control import task_cancelled

import json
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

from PIL import Image

from software_app.core.adapter import TaskCancelled
from software_app.core.external_tools import find_ffmpeg
from software_app.core.media_process import run_media_process
from software_app.crawlers.common import download_file, normalize_output_formats


MAX_UGOIRA_FRAMES = 10_000
MAX_UGOIRA_UNCOMPRESSED = 2 * 1024 * 1024 * 1024


def normalize_ugoira_meta(body: object) -> dict:
    """Validate the small part of Pixiv's ugoira metadata used by the downloader."""
    if not isinstance(body, dict):
        raise ValueError("Pixiv 动图元数据格式异常")
    source_url = str(body.get("originalSrc") or body.get("src") or "").strip()
    raw_frames = body.get("frames")
    if not source_url or not isinstance(raw_frames, list) or not raw_frames:
        raise ValueError("Pixiv 动图缺少 ZIP 地址或帧信息")
    if len(raw_frames) > MAX_UGOIRA_FRAMES:
        raise ValueError(f"Pixiv 动图帧数超过安全上限 {MAX_UGOIRA_FRAMES}")
    frames: list[dict] = []
    seen: set[str] = set()
    for item in raw_frames:
        if not isinstance(item, dict):
            raise ValueError("Pixiv 动图帧信息格式异常")
        filename = Path(str(item.get("file") or "")).name
        if not filename or filename in seen:
            raise ValueError("Pixiv 动图包含无效或重复帧名")
        try:
            delay = max(1, min(int(item.get("delay") or 1), 60_000))
        except (TypeError, ValueError) as exc:
            raise ValueError("Pixiv 动图包含无效帧延迟") from exc
        seen.add(filename)
        frames.append({"file": filename, "delay": delay})
    return {"source_url": source_url, "frames": frames}


def _extract_frames(zip_path: Path, destination: Path, frames: list[dict], cancel_event: threading.Event) -> list[Path]:
    wanted = {str(item["file"]): item for item in frames}
    extracted: dict[str, Path] = {}
    total_size = 0
    with zipfile.ZipFile(zip_path) as archive:
        members = {Path(info.filename).name: info for info in archive.infolist() if not info.is_dir()}
        for filename in wanted:
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")
            info = members.get(filename)
            if info is None:
                raise ValueError(f"Pixiv 动图 ZIP 缺少帧：{filename}")
            total_size += max(0, int(info.file_size))
            if total_size > MAX_UGOIRA_UNCOMPRESSED:
                raise ValueError("Pixiv 动图解压大小超过 2 GiB 安全上限")
            output = destination / filename
            with archive.open(info) as source, output.open("wb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            extracted[filename] = output
    return [extracted[str(item["file"])] for item in frames]


def _write_gif(frame_paths: list[Path], frames: list[dict], destination: Path, cancel_event: threading.Event) -> None:
    images: list[Image.Image] = []
    temporary = destination.with_name(destination.name + ".convert.part")
    try:
        for frame_path in frame_paths:
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")
            with Image.open(frame_path) as image:
                images.append(image.convert("RGBA").copy())
        if not images:
            raise ValueError("Pixiv 动图没有可转换的帧")
        images[0].save(
            temporary,
            format="GIF",
            save_all=True,
            append_images=images[1:],
            duration=[int(item["delay"]) for item in frames],
            disposal=2,
            loop=0,
            optimize=False,
        )
        temporary.replace(destination)
    finally:
        for image in images:
            image.close()
        temporary.unlink(missing_ok=True)


def _gif_to_mp4(source: Path, destination: Path, cancel_event: threading.Event) -> None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("需要 ffmpeg 才能把 Pixiv 动图转换为 MP4")
    destination.unlink(missing_ok=True)
    try:
        result = run_media_process(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(destination),
            ],
            cancel_event,
            timeout=1800,
        )
        if result.returncode != 0 or not destination.is_file():
            message = result.stderr.strip().splitlines()
            raise RuntimeError(message[-1] if message else "Pixiv 动图 MP4 转换失败")
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def download_ugoira(
    session,
    body: object,
    work_dir: Path,
    work_id: str,
    options: dict,
    cancel_event: threading.Event,
) -> list[tuple[Path, str, str]]:
    from software_app.core.file_conflict import media_output

    selected = normalize_output_formats(options)["animation_format"]
    requested = work_dir / (f"{work_id}.ugoira.zip" if selected == "original" else f"{work_id}.{selected}")
    with media_output(
        requested,
        options.get("file_conflict", "reuse"),
        cancel_event,
        related_paths=(work_dir / f".{work_id}.ugoira-output",),
        companions=lambda path: (path.with_suffix(".json"),) if selected == "original" else (),
    ) as destination:
        return _download_ugoira(
            session, body, work_dir, work_id, {**options, "_ugoira_destination": destination}, cancel_event
        )


def _download_ugoira(
    session,
    body: object,
    work_dir: Path,
    work_id: str,
    options: dict,
    cancel_event: threading.Event,
) -> list[tuple[Path, str, str]]:
    """Download an ugoira ZIP and emit GIF/MP4 or the original archive plus metadata."""
    meta = normalize_ugoira_meta(body)
    from software_app.core.file_conflict import reuse_existing

    policy = options.get("file_conflict", "reuse")
    source_url = str(meta["source_url"])
    frames = list(meta["frames"])
    selected = normalize_output_formats(options)["animation_format"]
    work_dir.mkdir(parents=True, exist_ok=True)
    if selected == "original":
        archive_path = Path(options.get("_ugoira_destination") or work_dir / f"{work_id}.ugoira.zip")
        metadata_path = archive_path.with_suffix(".json")
        if not reuse_existing(archive_path, policy):
            download_file(
                session,
                source_url,
                archive_path,
                cancel_event,
                overwrite=policy == "overwrite",
                public_referer="https://www.pixiv.net/",
            )
        metadata_path.write_text(
            json.dumps({"source_url": source_url, "frames": frames}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return [(archive_path, "animation", source_url), (metadata_path, "metadata", source_url)]

    final_path = Path(options.get("_ugoira_destination") or work_dir / f"{work_id}.{selected}")
    if reuse_existing(final_path, policy):
        return [(final_path, "animation", source_url)]
    with tempfile.TemporaryDirectory(prefix="ugoira-", dir=work_dir) as temp_name:
        temp_dir = Path(temp_name)
        archive_path = temp_dir / "source.zip"
        download_file(session, source_url, archive_path, cancel_event, public_referer="https://www.pixiv.net/")
        frame_paths = _extract_frames(archive_path, temp_dir, frames, cancel_event)
        gif_path = temp_dir / "animation.gif"
        _write_gif(frame_paths, frames, gif_path, cancel_event)
        if selected == "mp4":
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")
            converted = temp_dir / "animation.mp4"
            _gif_to_mp4(gif_path, converted, cancel_event)
        else:
            converted = gif_path
        converted.replace(final_path)
    if not final_path.is_file() or not final_path.stat().st_size:
        raise RuntimeError("Pixiv 动图转换后没有生成有效文件")
    return [(final_path, "animation", source_url)]


__all__ = ["download_ugoira", "normalize_ugoira_meta"]

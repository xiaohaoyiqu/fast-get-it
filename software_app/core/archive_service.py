from __future__ import annotations

import os
import re
import shutil
import stat
import threading
import time
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from .adapter import TaskCancelled
from .models import DownloadTask, FileRecord, classify_file


ProgressCallback = Callable[[int, int, str], None]
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul", *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


@dataclass
class ArchivePostprocessResult:
    records: list[FileRecord] = field(default_factory=list)
    removed_sources: list[Path] = field(default_factory=list)


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TaskCancelled("归档后处理已取消")


def _safe_label(value: str, fallback: str = "下载内容") -> str:
    label = unicodedata.normalize("NFKC", str(value or "")).strip()
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label).strip(" ._")
    if not label:
        label = fallback
    if label.casefold() in _WINDOWS_RESERVED:
        label = f"_{label}"
    return label[:72].rstrip(" .") or fallback


def _existing_sources(source_files: Iterable[Path | str], destination: Path, base_dir: Path) -> list[Path]:
    root = base_dir.expanduser().resolve()
    final = destination.expanduser().resolve()
    sources: list[Path] = []
    seen: set[str] = set()
    for value in source_files:
        path = Path(value).expanduser().resolve()
        key = os.path.normcase(str(path))
        if key in seen or path == final:
            continue
        if not path.is_relative_to(root):
            raise ValueError(f"不能归档保存目录之外的文件：{path.name}")
        if path.is_symlink() or not path.is_file():
            continue
        seen.add(key)
        sources.append(path)
    if not sources:
        raise ValueError("没有可压缩的已下载文件")
    return sources


def create_verified_zip(
    source_files: Iterable[Path | str],
    destination: Path | str,
    *,
    base_dir: Path | str,
    cleanup_sources: bool = False,
    cancel_event: threading.Event | None = None,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Create an atomic ZIP, verify every member, then optionally remove exact sources."""
    final = Path(destination).expanduser().resolve()
    root = Path(base_dir).expanduser().resolve()
    sources = _existing_sources(source_files, final, root)
    final.parent.mkdir(parents=True, exist_ok=True)
    partial = final.with_name(f"{final.name}.partial-{uuid.uuid4().hex[:8]}")
    try:
        _check_cancel(cancel_event)
        with zipfile.ZipFile(
            partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
        ) as archive:
            for index, source in enumerate(sources, 1):
                _check_cancel(cancel_event)
                arcname = source.relative_to(root).as_posix()
                archive.write(source, arcname)
                if on_progress is not None:
                    on_progress(index, len(sources), f"正在压缩 {source.name}")
        _check_cancel(cancel_event)
        with zipfile.ZipFile(partial, "r") as archive:
            broken = archive.testzip()
            if broken:
                raise ValueError(f"压缩包校验失败：{broken}")
            if len([item for item in archive.infolist() if not item.is_dir()]) != len(sources):
                raise ValueError("压缩包文件数量校验失败")
        for attempt in range(5):
            try:
                partial.replace(final)
                break
            except PermissionError as exc:
                if getattr(exc, "winerror", None) not in {32, 33} or attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
        if cleanup_sources:
            # Cancellation is honored before cleanup starts. Once deletion begins, finish the
            # exact verified source set so a late click cannot leave a half-cleaned directory.
            _check_cancel(cancel_event)
            for source in sources:
                source.unlink()
        return final
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _validated_members(
    archive: zipfile.ZipFile,
    *,
    max_files: int,
    max_uncompressed_bytes: int,
) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    files = [item for item in members if not item.is_dir()]
    if len(files) > max_files:
        raise ValueError(f"压缩包文件过多，最多允许 {max_files} 个")
    if sum(max(0, int(item.file_size)) for item in files) > max_uncompressed_bytes:
        raise ValueError("压缩包解压后总体积超过安全限制")
    for item in members:
        normalized = item.filename.replace("\\", "/")
        path = PurePosixPath(normalized)
        invalid_windows_part = any(
            re.search(r'[<>:"|?*\x00-\x1f]', part)
            or part.rstrip(" .") != part
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
            for part in path.parts
        )
        mode = int(item.external_attr) >> 16
        if (
            not normalized
            or normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized)
            or any(part in {"", ".", ".."} for part in path.parts)
            or invalid_windows_part
            or stat.S_ISLNK(mode)
            or bool(item.flag_bits & 0x1)
            or (
                item.file_size > 100 * 1024 * 1024
                and item.compress_size > 0
                and item.file_size / item.compress_size > 1000
            )
        ):
            raise ValueError(f"压缩包包含不安全或不支持的成员：{item.filename}")
    return members


def extract_zip_safely(
    archive_path: Path | str,
    destination: Path | str,
    *,
    cancel_event: threading.Event | None = None,
    on_progress: ProgressCallback | None = None,
    max_files: int = 10_000,
    max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024,
) -> list[Path]:
    """Extract a ZIP into a new directory without traversal, links, or partial results."""
    source = Path(archive_path).expanduser().resolve()
    final_root = Path(destination).expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise ValueError("待解压文件不存在或不是普通文件")
    if final_root.exists():
        raise FileExistsError(f"解压目录已存在：{final_root}")
    final_root.parent.mkdir(parents=True, exist_ok=True)
    partial_root = final_root.with_name(f"{final_root.name}.partial-{uuid.uuid4().hex[:8]}")
    extracted_relative: list[Path] = []
    try:
        _check_cancel(cancel_event)
        with zipfile.ZipFile(source, "r") as archive:
            members = _validated_members(
                archive, max_files=max_files, max_uncompressed_bytes=max_uncompressed_bytes
            )
            partial_root.mkdir(parents=True)
            file_count = len([item for item in members if not item.is_dir()])
            current = 0
            for item in members:
                _check_cancel(cancel_event)
                relative = Path(*PurePosixPath(item.filename.replace("\\", "/")).parts)
                target = partial_root / relative
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item, "r") as input_stream, target.open("wb") as output_stream:
                    while True:
                        _check_cancel(cancel_event)
                        chunk = input_stream.read(1024 * 1024)
                        if not chunk:
                            break
                        output_stream.write(chunk)
                current += 1
                extracted_relative.append(relative)
                if on_progress is not None:
                    on_progress(current, file_count, f"正在解压 {relative.name}")
        _check_cancel(cancel_event)
        for attempt in range(10):
            try:
                partial_root.replace(final_root)
                break
            except PermissionError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32} or attempt == 9:
                    raise
                _check_cancel(cancel_event)
                time.sleep(0.1)
        return [(final_root / relative).resolve() for relative in extracted_relative]
    except Exception:
        if partial_root.exists():
            shutil.rmtree(partial_root)
        raise


def _unique_extract_destination(archive_path: Path) -> Path:
    base = archive_path.with_suffix("")
    if not base.exists():
        return base
    for index in range(1, 10_000):
        candidate = base.with_name(f"{base.name}_解压_{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("无法为解压内容创建不冲突的目录")


def run_archive_postprocess(
    task: DownloadTask,
    title: str,
    records: Iterable[FileRecord],
    *,
    cancel_event: threading.Event,
    on_progress: ProgressCallback | None = None,
) -> ArchivePostprocessResult:
    """Apply opt-in extraction and ZIP creation to records from any platform adapter."""
    result = ArchivePostprocessResult()
    output_root = task.output_dir.expanduser().resolve()
    current_records = [record for record in records if record.path.exists() and record.path.is_file()]

    if bool(task.options.get("extract_archives", False)):
        for record in list(current_records):
            path = record.path.expanduser().resolve()
            if path.suffix.casefold() != ".zip" or path.name.casefold().endswith(".ugoira.zip"):
                continue
            if not path.is_relative_to(output_root):
                continue
            extracted = extract_zip_safely(
                path, _unique_extract_destination(path), cancel_event=cancel_event, on_progress=on_progress
            )
            for extracted_path in extracted:
                extracted_record = FileRecord(
                    path=extracted_path,
                    module_id=task.module_id,
                    task_id=task.task_id,
                    media_type=classify_file(extracted_path),
                    size=extracted_path.stat().st_size,
                    title=extracted_path.name,
                    source_url=record.source_url,
                    source_id=record.source_id,
                    author_id=record.author_id,
                    author_name=record.author_name,
                    chapter=record.chapter,
                    tags=record.tags,
                    metadata={"archive_action": "extract", "source_archive": str(path)},
                )
                result.records.append(extracted_record)
                current_records.append(extracted_record)

    mode = str(task.options.get("archive_mode") or "none").strip().casefold()
    if mode not in {"task", "folder"}:
        return result
    cleanup_sources = bool(task.options.get("archive_cleanup_sources", False))
    eligible = [
        record for record in current_records
        if record.path.expanduser().resolve().is_relative_to(output_root)
        and record.path.suffix.casefold() != ".zip"
    ]
    if not eligible:
        return result
    groups: list[tuple[Path, list[FileRecord], str]] = []
    if mode == "folder":
        by_parent: dict[Path, list[FileRecord]] = {}
        for record in eligible:
            by_parent.setdefault(record.path.expanduser().resolve().parent, []).append(record)
        groups = [(parent, rows, parent.name) for parent, rows in by_parent.items()]
    else:
        parents = [record.path.expanduser().resolve().parent for record in eligible]
        common_parent = Path(os.path.commonpath([str(path) for path in parents]))
        groups = [(common_parent, eligible, title)]

    for index, (base_dir, group, group_label) in enumerate(groups, 1):
        _check_cancel(cancel_event)
        archive_name = f"{_safe_label(group_label)}_归档_{task.task_id}.zip"
        destination = base_dir / archive_name
        archive_path = create_verified_zip(
            [record.path for record in group],
            destination,
            base_dir=base_dir,
            cleanup_sources=cleanup_sources,
            cancel_event=cancel_event,
            on_progress=on_progress,
        )
        result.records.append(
            FileRecord(
                path=archive_path,
                module_id=task.module_id,
                task_id=task.task_id,
                media_type="archive",
                size=archive_path.stat().st_size,
                title=archive_path.name,
                source_url=task.target,
                metadata={
                    "archive_action": "compress",
                    "archive_scope": mode,
                    "source_count": len(group),
                    "cleanup_sources": cleanup_sources,
                    "group": index,
                    "group_count": len(groups),
                },
            )
        )
        if cleanup_sources:
            result.removed_sources.extend(record.path.expanduser().resolve() for record in group)
    return result


__all__ = [
    "ArchivePostprocessResult",
    "create_verified_zip",
    "extract_zip_safely",
    "run_archive_postprocess",
]

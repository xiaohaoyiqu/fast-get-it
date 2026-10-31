from __future__ import annotations

from software_app.core.task_control import task_cancelled

import os
import re
import shutil
import stat
import threading
import time
import unicodedata
import uuid
import zipfile
import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from .adapter import TaskCancelled
from .models import DownloadTask, FileRecord, classify_file
from .file_conflict import media_output, reuse_existing, replace_output
from .output_lock import output_paths_lock


ProgressCallback = Callable[[int, int, str], None]
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul", *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


@dataclass
class ArchivePostprocessResult:
    records: list[FileRecord] = field(default_factory=list)
    removed_sources: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and task_cancelled(cancel_event):
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
        path = Path(os.path.abspath(Path(value).expanduser()))
        if path.is_symlink():
            continue
        resolved = path.resolve()
        if os.path.normcase(str(path)) != os.path.normcase(str(resolved)):
            continue  # Do not archive or delete through linked parent directories.
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


def _signature(value):
    # Windows stat/lstat may expose creation time as ctime, while fstat exposes
    # metadata change time (CPython #157671). Compare birth time across APIs;
    # older Windows Python builds use creation time for both ctime fields.
    timestamp = getattr(value, "st_birthtime_ns", value.st_ctime_ns) if os.name == "nt" else value.st_ctime_ns
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, timestamp)


def _stream_signature(value):
    # Within one API, retain change-time detection as well, including writes
    # which restore mtime. Never compare this field to a path stat on Windows.
    return (*_signature(value), value.st_ctime_ns)


def _source_snapshot(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or path.resolve() != path:
        raise ValueError(f"源文件已变为链接或非普通文件：{path.name}")
    return _signature(value)


def _digest_stream(stream, cancel_event):
    digest = hashlib.sha256()
    while True:
        _check_cancel(cancel_event)
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return digest.digest()
        digest.update(chunk)


def _hash_source(path, expected, cancel_event):
    if _source_snapshot(path) != expected:
        raise ValueError(f"源文件已变化：{path.name}")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if _signature(opened) != expected:
            raise ValueError(f"源文件已被替换：{path.name}")
        digest = _digest_stream(stream, cancel_event)
        if _stream_signature(os.fstat(stream.fileno())) != _stream_signature(opened):
            raise ValueError(f"源文件在读取时变化：{path.name}")
    if _source_snapshot(path) != expected:
        raise ValueError(f"源文件已被替换：{path.name}")
    return digest


def _cleanup_sources(sources, snapshots, digests, cancel_event, removed, warn):
    # Preflight the entire set before deleting any source. Stat identity plus a
    # content digest catches replacement and same-size/timestamp-preserving edits.
    try:
        for source in sources:
            if _hash_source(source, snapshots[source], cancel_event) != digests[source]:
                raise ValueError(f"源文件内容已变化：{source.name}")
    except (OSError, ValueError) as exc:
        warn(f"归档已保留；源文件复核失败，未执行本组清理：{exc}")
        return
    _check_cancel(cancel_event)
    # Once deletion begins, finish this exact set. Keep changed or busy files and
    # report only successful removals so the caller never drops their indexes.
    for source in sources:
        try:
            if _source_snapshot(source) != snapshots[source]:
                warn(f"源文件再次变化，已保留：{source.name}")
                continue
            source.unlink()
            removed.append(source)
        except (OSError, ValueError) as exc:
            warn(f"源文件未清理，已保留文件索引：{source.name}（{exc}）")


def create_verified_zip(
    source_files: Iterable[Path | str],
    destination: Path | str,
    *,
    base_dir: Path | str,
    cleanup_sources: bool = False,
    cancel_event: threading.Event | None = None,
    on_progress: ProgressCallback | None = None,
    file_conflict: str = "reuse",
    removed_sources: list[Path] | None = None,
    warnings: list[str] | None = None,
) -> Path:
    """Create an atomic ZIP, verify every member, then optionally remove exact sources."""
    requested = Path(destination).expanduser()
    final = requested.parent.resolve() / requested.name
    root = Path(base_dir).expanduser().resolve()
    sources = _existing_sources(source_files, final, root)
    final.parent.mkdir(parents=True, exist_ok=True)
    removed = removed_sources if removed_sources is not None else []

    def warn(message):
        if warnings is not None:
            warnings.append(message)
        elif on_progress is not None:
            on_progress(len(sources), len(sources), message)

    with media_output(final, file_conflict, cancel_event, related_paths=sources) as final:
        _check_cancel(cancel_event)
        snapshots = {source: _source_snapshot(source) for source in sources}
        if reuse_existing(final, file_conflict):
            with zipfile.ZipFile(final, "r") as archive:
                if archive.testzip():
                    raise ValueError("已有归档校验失败，未清理源文件")
                if cleanup_sources:
                    wanted = {source.relative_to(root).as_posix() for source in sources}
                    members = [info.filename for info in archive.infolist() if not info.is_dir()]
                    if len(members) != len(wanted) or set(members) != wanted:
                        warn("复用归档的文件列表与本次源文件不一致，未清理源文件")
                        return final
                    digests = {}
                    for source in sources:
                        with archive.open(source.relative_to(root).as_posix()) as stream:
                            digests[source] = _digest_stream(stream, cancel_event)
                    _cleanup_sources(sources, snapshots, digests, cancel_event, removed, warn)
            return final
        partial = final.with_name(f"{final.name}.partial-{uuid.uuid4().hex[:8]}")
        digests = {}
        try:
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
                for index, source in enumerate(sources, 1):
                    _check_cancel(cancel_event)
                    if _source_snapshot(source) != snapshots[source]:
                        raise ValueError(f"源文件在压缩期间变化：{source.name}")
                    info = zipfile.ZipInfo.from_file(source, source.relative_to(root).as_posix())
                    info.compress_type = zipfile.ZIP_DEFLATED
                    digest = hashlib.sha256()
                    with source.open("rb") as stream, archive.open(info, "w", force_zip64=True) as output:
                        opened = os.fstat(stream.fileno())
                        if _signature(opened) != snapshots[source]:
                            raise ValueError(f"源文件已被替换：{source.name}")
                        while True:
                            _check_cancel(cancel_event)
                            chunk = stream.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            digest.update(chunk)
                        if _stream_signature(os.fstat(stream.fileno())) != _stream_signature(opened):
                            raise ValueError(f"源文件在压缩期间变化：{source.name}")
                    digests[source] = digest.digest()
                    if on_progress is not None:
                        on_progress(index, len(sources), f"正在压缩 {source.name}")
            _check_cancel(cancel_event)
            with zipfile.ZipFile(partial, "r") as archive:
                if archive.testzip() or len(archive.infolist()) != len(sources):
                    raise ValueError("压缩包内容或数量校验失败")
            for source in sources:
                if _source_snapshot(source) != snapshots[source]:
                    raise ValueError(f"源文件在归档提交前变化：{source.name}")
            replace_output(partial, final, cancel_event)
            if cleanup_sources:
                _cleanup_sources(sources, snapshots, digests, cancel_event, removed, warn)
            return final
        finally:
            partial.unlink(missing_ok=True)


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
    source = Path(archive_path).expanduser()
    source = source.parent.resolve() / source.name
    target = Path(destination).expanduser()
    target = target.parent.resolve() / target.name
    with output_paths_lock((source, target), cancel_event):
        return _extract_zip_locked(source, target, cancel_event=cancel_event, on_progress=on_progress,
                                   max_files=max_files, max_uncompressed_bytes=max_uncompressed_bytes)


def _extract_zip_locked(
    source: Path, final_root: Path, *, cancel_event=None, on_progress=None,
    max_files=10_000, max_uncompressed_bytes=2 * 1024 * 1024 * 1024,
) -> list[Path]:
    """Extract a ZIP into a new directory without traversal, links, or partial results."""
    if not source.is_file() or source.is_symlink():
        raise ValueError("待解压文件不存在或不是普通文件")
    if final_root.exists() or final_root.is_symlink():
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
                if final_root.exists() or final_root.is_symlink():
                    raise FileExistsError(f"解压目录已出现，未覆盖：{final_root}")
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
            if (path.suffix.casefold() != ".zip" or record.metadata.get("pixiv_type") == "ugoira"
                    or re.search(r"\.ugoira(?: \(\d+\))?\.zip$", path.name, re.IGNORECASE)):
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
        removed_in_group = []
        archive_path = create_verified_zip(
            [record.path for record in group],
            destination,
            base_dir=base_dir,
            cleanup_sources=cleanup_sources,
            cancel_event=cancel_event,
            on_progress=on_progress,
            file_conflict=task.options.get("file_conflict", "reuse"),
            removed_sources=removed_in_group,
            warnings=result.warnings,
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
                    "removed_source_count": len(removed_in_group),
                    "group": index,
                    "group_count": len(groups),
                },
            )
        )
        result.removed_sources.extend(removed_in_group)
    return result


__all__ = [
    "ArchivePostprocessResult",
    "create_verified_zip",
    "extract_zip_safely",
    "run_archive_postprocess",
]

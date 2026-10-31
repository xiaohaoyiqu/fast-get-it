"""Full-index library filtering, CSV export and reviewed cleanup plans."""
from __future__ import annotations

import csv
import json
import os
import stat
import tempfile
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .exports import safe_csv_cell
from .output_lock import output_path_lock, output_paths_lock
from .security import redact_url_query
from .adapter import TaskCancelled


def _text(value) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


@dataclass(frozen=True)
class LibraryFilter:
    module_id: str = ""
    media_type: str = ""
    author: str = ""
    title: str = ""
    tag: str = ""
    query: str = ""
    start_date: str = ""
    end_date: str = ""
    date_basis: str = "created_at"

    def __post_init__(self):
        if self.date_basis not in {"created_at", "published_at"}:
            raise ValueError("日期依据必须为入库日期或发布日期")
        for value in (self.start_date, self.end_date):
            if value:
                try:
                    parsed = date.fromisoformat(value)
                    if parsed.isoformat() != value:
                        raise ValueError()
                except ValueError as exc:
                    raise ValueError("日期须为有效的 YYYY-MM-DD") from exc
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")

    def matches(self, row: dict) -> bool:
        if self.module_id and row.get("module_id") != self.module_id:
            return False
        if self.media_type and row.get("media_type") != self.media_type:
            return False
        if self.author and not (_text(self.author) == _text(row.get("author_id"))
                                or _text(self.author) in _text(row.get("author_name"))):
            return False
        if self.title and _text(self.title) not in _text(row.get("title")):
            return False
        tags = []
        if self.tag or self.query:
            try:
                tags = json.loads(row.get("tags_json") or "[]")
            except (ValueError, TypeError):
                tags = []
        tags = tags if isinstance(tags, list) else []
        if self.tag and _text(self.tag) not in {_text(tag) for tag in tags}:
            return False
        if self.start_date or self.end_date:
            value = str(row.get(self.date_basis) or "")[:10]
            try:
                if date.fromisoformat(value).isoformat() != value:
                    return False
            except ValueError:
                return False
            if (self.start_date and value < self.start_date) or (self.end_date and value > self.end_date):
                return False
        if not self.query:
            return True
        haystack = _text(" ".join(str(row.get(key) or "") for key in (
            "path", "module_id", "title", "media_type", "author_id", "author_name", "source_id", "chapter",
        )) + " " + " ".join(str(tag) for tag in tags))
        return all(token in haystack for token in _text(self.query).split())


def iter_library_rows(storage, filters: LibraryFilter, *, cancel_event=None):
    # The cursor reads one SQLite snapshot. Large metadata blobs and the UI's
    # display limits do not take part in export/cleanup selection.
    with storage.connect() as conn:
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelled("操作已取消")
        for row in conn.execute("""SELECT path, module_id, task_id, media_type, size, title,
                source_url, source_id, author_id, author_name, chapter, published_at,
                tags_json, created_at, updated_at, revision FROM files ORDER BY path"""):
            item = dict(row)
            if cancel_event is not None and cancel_event.is_set():
                raise TaskCancelled("操作已取消")
            if filters.matches(item):
                yield item


def export_library_csv(storage, filters: LibraryFilter, destination: Path | str, *, cancel_event=None) -> int:
    destination = Path(destination).expanduser().absolute()
    resolved_destination = destination.resolve()
    if destination.suffix.casefold() != ".csv":
        raise ValueError("下载库索引请导出为 CSV 文件")
    if destination.resolve() in {storage.db_path.resolve(), Path(str(storage.db_path.resolve()) + "-wal"),
                                  Path(str(storage.db_path.resolve()) + "-shm")}:
        raise ValueError("不能覆盖运行数据库")
    columns = ("module_id", "task_id", "path", "media_type", "size", "title", "source_url",
               "source_id", "author_id", "author_name", "chapter", "published_at", "tags_json",
               "created_at", "updated_at", "local_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    count = 0
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", delete=False,
                                         dir=destination.parent, prefix=".library-", suffix=".tmp") as output:
            temporary = Path(output.name)
            writer = csv.writer(output)
            writer.writerow(columns)
            for row in iter_library_rows(storage, LibraryFilter(), cancel_event=cancel_event):
                if cancel_event is not None and cancel_event.is_set():
                    raise TaskCancelled("导出已取消")
                if Path(row["path"]).resolve() == resolved_destination:
                    raise ValueError("导出位置已经是下载库中的资源，不能覆盖")
                if not filters.matches(row):
                    continue
                row["source_url"] = redact_url_query(str(row["source_url"] or ""))
                row["local_exists"] = str(Path(row["path"]).is_file()).lower()
                writer.writerow([safe_csv_cell(str(row.get(key) if row.get(key) is not None else "")) for key in columns])
                count += 1
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelled("导出已取消")
        os.replace(temporary, destination)
        return count
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fingerprint(path: Path):
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("路径包含链接或不是绝对路径")
    # Windows short (8.3) names can resolve to a different spelling without
    # being links. Inspect actual parent links/junctions instead of comparing
    # absolute and resolved strings.
    for parent in path.parents:
        try:
            parent_stat = parent.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(parent_stat.st_mode)
                or getattr(parent_stat, "st_reparse_tag", 0) == 0xA0000003):
            raise ValueError("路径包含符号链接或目录联接")
    try:
        file_stat = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file():
        raise ValueError("索引路径不是普通文件")
    return (file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns)


@dataclass(frozen=True)
class CleanupEntry:
    path: str
    revision: str
    fingerprint: tuple | None
    error: str = ""


@dataclass(frozen=True)
class CleanupPlan:
    entries: tuple[CleanupEntry, ...]
    size: int
    missing: int


def plan_library_cleanup(storage, filters: LibraryFilter, limit: int = 10000, *, cancel_event=None) -> CleanupPlan:
    return _plan_cleanup_rows(iter_library_rows(storage, filters, cancel_event=cancel_event), limit, cancel_event)


def plan_library_paths(storage, paths) -> CleanupPlan:
    """Snapshot exact selected index keys; never widen selection into a search."""
    paths = list(dict.fromkeys(str(path) for path in paths))
    if len(paths) > 10000:
        raise ValueError("一次最多选择 10000 个文件")
    rows = []
    with storage.connect() as conn:
        for path in paths:
            row = conn.execute("SELECT path, revision FROM files WHERE path=?", (path,)).fetchone()
            if row is None:
                raise ValueError("所选文件索引已变化，请刷新下载库后重新选择")
            rows.append(dict(row))
    return _plan_cleanup_rows(rows, 10000, None)


def _plan_cleanup_rows(rows, limit, cancel_event):
    entries = []
    size = missing = 0
    for row in rows:
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelled("预览已取消")
        if len(entries) >= limit:
            raise ValueError(f"匹配超过 {limit} 条，请缩小筛选范围后再预览")
        error = ""
        try:
            fingerprint = _fingerprint(Path(row["path"]))
            if fingerprint is None:
                missing += 1
            else:
                size += fingerprint[2]
        except (OSError, ValueError) as exc:
            fingerprint = None
            error = str(exc)
        entries.append(CleanupEntry(str(row["path"]), str(row["revision"]), fingerprint, error))
    return CleanupPlan(tuple(entries), size, missing)


def rename_library_file(manager, entry: CleanupEntry, destination: Path) -> bool:
    """Rename a reviewed file under the same maintenance and output guards."""
    source = Path(entry.path)
    destination = Path(destination).absolute()
    if entry.error or entry.fingerprint is None:
        raise ValueError("所选文件不存在或路径不适合重命名")
    if destination.parent.resolve() != source.parent.resolve():
        raise ValueError("只能修改文件名，不能移动到其他目录")
    with manager.library_maintenance(), output_paths_lock((source, destination), timeout=0):
        renamed = False
        try:
            with manager.storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running','paused','cancelling') LIMIT 1").fetchone():
                    raise ValueError("请先结束共享数据库中的下载任务（含暂停任务）")
                row = conn.execute("SELECT revision FROM files WHERE path=?", (entry.path,)).fetchone()
                if row is None or str(row["revision"]) != entry.revision or _fingerprint(source) != entry.fingerprint:
                    raise ValueError("文件或索引已变化，请刷新后重新选择")
                if str(source.absolute()) == str(destination):
                    return False
                same_path = os.path.normcase(str(source.resolve())) == os.path.normcase(str(destination.resolve()))
                if destination.is_symlink() or (destination.exists() and not same_path):
                    raise FileExistsError("目标文件名已经存在，未覆盖")
                indexed = conn.execute("SELECT path FROM files WHERE path=? COLLATE NOCASE", (str(destination),)).fetchone()
                if indexed is not None and str(indexed["path"]) != entry.path:
                    raise FileExistsError("目标文件名已有下载库索引，未覆盖")
                source.rename(destination)
                renamed = True
                changed = conn.execute("""UPDATE files SET path=?, title=?, revision=lower(hex(randomblob(16))),
                    updated_at=CURRENT_TIMESTAMP WHERE path=? AND revision=?""",
                    (str(destination), destination.name, entry.path, entry.revision))
                if changed.rowcount != 1:
                    raise ValueError("文件索引已变化")
            return True
        except Exception:
            if renamed:
                if source.exists() and not same_path:
                    raise RuntimeError(f"恢复路径被占用，重命名后的文件保留在：{destination}")
                destination.rename(source)
            raise


@dataclass
class CleanupResult:
    removed_indexes: int = 0
    recycled: int = 0
    skipped: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)


def execute_library_cleanup(manager, plan: CleanupPlan, *, mode: str, recycle=None,
                            cancel_event: threading.Event | None = None) -> CleanupResult:
    if mode not in {"index", "recycle"} or (mode == "recycle" and recycle is None):
        raise ValueError("清理方式无效")
    stop = cancel_event if cancel_event is not None else threading.Event()
    result = CleanupResult()
    with manager.library_maintenance():
        for entry in plan.entries:
            if stop.is_set():
                result.cancelled = True
                break
            removed = 0
            try:
                # A zero timeout skips files held by another cooperating process.
                with output_path_lock(entry.path, stop, timeout=0), manager.storage.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running','paused','cancelling') LIMIT 1").fetchone():
                        result.errors.append("检测到新的下载任务，已停止后续清理")
                        break
                    row = conn.execute("SELECT revision FROM files WHERE path=?", (entry.path,)).fetchone()
                    if row is None or str(row["revision"]) != entry.revision:
                        result.skipped += 1
                        continue
                    if mode == "recycle":
                        if entry.error:
                            result.skipped += 1
                            result.errors.append(f"{Path(entry.path).name}: {entry.error}")
                            continue
                        if _fingerprint(Path(entry.path)) != entry.fingerprint:
                            result.skipped += 1
                            continue
                        if entry.fingerprint is not None:
                            recycle(Path(entry.path))
                            if Path(entry.path).exists():
                                raise OSError("回收操作返回后文件仍在原位置，已保留索引")
                            result.recycled += 1
                    cursor = conn.execute("DELETE FROM files WHERE path=? AND revision=?", (entry.path, entry.revision))
                    removed = cursor.rowcount
                result.removed_indexes += removed
            except Exception as exc:  # Keep failed files indexed; continue independent items.
                if stop.is_set():
                    result.cancelled = True
                    break
                result.skipped += 1
                result.errors.append(f"{Path(entry.path).name}: {exc}")
    return result

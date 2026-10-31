"""Explicit local baselines and read-only verification of a selected library root."""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import tempfile
from urllib.parse import urlsplit, urlunsplit

from .adapter import TaskCancelled
from .library_service import iter_library_rows
from .output_lock import output_path_lock
from .task_control import task_cancelled


MAX_ENTRIES = 10000
MAX_JSON_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
METADATA_KEYS = ("module_id", "media_type", "source_id", "title", "author_id", "task_id")
BASELINE_STATES = {"recorded", "missing", "changed", "error", "busy"}


class _Changed(ValueError):
    pass


def _checkpoint(event):
    if event is not None and task_cancelled(event):
        raise TaskCancelled("成品清单操作已停止")


def _root(directory):
    root = Path(directory).expanduser().absolute()
    if not root.is_dir():
        raise ValueError("请选择实际保存文件的根目录")
    _no_links(root)
    return root


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("清单相对路径无效")
    parts = value.split("/")
    windows = PureWindowsPath(value)
    if (
        windows.drive
        or windows.root
        or "\\" in value
        or any(
            part in {"", ".", ".."}
            or ":" in part
            or "\x00" in part
            or part.endswith((" ", "."))
            or PureWindowsPath(part).is_reserved()
            for part in parts
        )
    ):
        raise ValueError("清单只接受根目录内的普通相对路径")
    return value


def _no_links(path):
    # Never follow a file link or a parent junction when hashing. Compare path
    # stat with fstat using identity/size/mtime (Windows ctime semantics differ).
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            raise ValueError("路径包含链接或重解析点，未读取")


def _signature(path):
    _no_links(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("路径不是普通文件")
    return _stat_key(info)


def _stat_key(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _hash(path, event):
    _checkpoint(event)
    before = _signature(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if _stat_key(os.fstat(stream.fileno())) != before:
            raise _Changed("读取前文件已变化")
        remaining = before[2]
        while remaining:
            _checkpoint(event)
            chunk = stream.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                raise _Changed("读取期间文件截短")
            digest.update(chunk)
            remaining -= len(chunk)
        _checkpoint(event)
        if stream.read(1) or _stat_key(os.fstat(stream.fileno())) != before or _signature(path) != before:
            raise _Changed("读取期间文件已变化")
    return before[2], digest.hexdigest()


def _inspect(path, event):
    try:
        with output_path_lock(path, event, timeout=0):
            size, digest = _hash(path, event)
        return {"status": "recorded", "size": size, "sha256": digest}
    except TaskCancelled:
        raise
    except FileNotFoundError:
        status = "missing"
    except _Changed:
        status = "changed"
    except TimeoutError:
        status = "busy"
    except (OSError, ValueError):
        status = "error"
    return {"status": status, "size": None, "sha256": None}


def _source_page(value):
    # Metadata whitelist: no raw engine metadata, credentials, signed queries
    # or fragments. This is a locator, not a reusable download credential.
    try:
        url = urlsplit(str(value or ""))
        if url.scheme not in {"http", "https"} or not url.hostname:
            return ""
        host = url.hostname
        host = f"[{host}]" if ":" in host else host
        if url.port:
            host += f":{url.port}"
        return urlunsplit((url.scheme, host, url.path, "", ""))[:4096]
    except ValueError:
        return ""


def _destination(storage, destination):
    path = Path(destination).expanduser().absolute()
    if path.suffix.casefold() != ".json":
        raise ValueError("清单和核验报告请使用 .json 文件")
    if os.path.lexists(path):
        raise FileExistsError("请选择尚不存在的文件名；清单和报告不会覆盖旧文件")
    key = os.path.normcase(str(path.resolve()))
    if key in {
        os.path.normcase(str(storage.db_path.resolve()) + suffix) for suffix in ("", "-wal", "-shm", "-journal")
    }:
        raise ValueError("不能覆盖运行数据库")
    with storage.connect() as conn:
        if conn.execute(
            "SELECT 1 FROM files WHERE path COLLATE NOCASE IN (?, ?)", (str(path), str(path.resolve()))
        ).fetchone():
            raise ValueError("输出位置已有下载库索引，不能用作清单或报告")
        # Legacy index keys may use a short parent name while the save dialog
        # supplies a long name. Narrow by basename before normalizing aliases.
        pattern = "%" + path.name.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        for row in conn.execute("SELECT path FROM files WHERE path LIKE ? ESCAPE '!'", (pattern,)):
            if os.path.normcase(str(Path(row["path"]).resolve())) == key:
                raise ValueError("输出位置已有下载库索引，不能用作清单或报告")
    return path


def _publish(destination, document, event):
    data = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("清单或报告超过 16 MiB，请缩小筛选范围")
    _checkpoint(event)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".integrity-", suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        _checkpoint(event)
        # An exclusive hard-link publication is atomic and cannot overwrite a
        # newly created file. On unsupported filesystems fail, never fall back
        # to replacing an existing resource. The temporary name is then removed.
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_integrity_manifest(storage, filters, directory, destination, *, cancel_event=None):
    root = _root(directory)
    output = _destination(storage, destination)
    entries = []
    seen = set()
    byte_budget = 0
    # Close the SQLite read snapshot before potentially lengthy file hashing,
    # so concurrent downloads can checkpoint their WAL normally.
    rows = []
    with closing(iter_library_rows(storage, filters, cancel_event=cancel_event)) as matches:
        for row in matches:
            _checkpoint(cancel_event)
            if len(rows) >= MAX_ENTRIES:
                raise ValueError("成品清单最多 10000 项，请缩小筛选范围")
            rows.append(row)
    with output_path_lock(output, cancel_event, timeout=0):
        for row in rows:
            _checkpoint(cancel_event)
            try:
                indexed = Path(row["path"]).absolute()
                try:
                    relative = indexed.relative_to(root).as_posix()
                except ValueError:
                    # Windows 8.3 aliases and long names can refer to the same
                    # parent. Normalize only after ruling out links/junctions.
                    _no_links(indexed)
                    relative = indexed.resolve().relative_to(root.resolve()).as_posix()
                relative = _relative(relative)
            except ValueError:
                raise ValueError("匹配文件位于所选根目录之外，请缩小筛选范围或选择共同根目录") from None
            key = os.path.normcase(relative)
            if key in seen:
                raise ValueError("匹配记录包含重复文件路径")
            seen.add(key)
            item = {"path": relative, **_inspect(root / relative, cancel_event)}
            item.update({name: str(row.get(name) or "")[:4096] for name in METADATA_KEYS})
            item["source_page"] = _source_page(row.get("source_url"))
            byte_budget += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            if byte_budget > MAX_JSON_BYTES:
                raise ValueError("清单超过 16 MiB，请缩小筛选范围")
            entries.append(item)
        if not entries:
            raise ValueError("筛选范围内没有下载库记录")
        _publish(
            output,
            {
                "format": "yuqiuda-library-manifest",
                "version": 1,
                "algorithm": "sha256",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "root_hint": root.name,
                "entries": entries,
            },
            cancel_event,
        )
    return dict(Counter(item["status"] for item in entries))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("清单包含重复字段")
        result[key] = value
    return result


def _load_manifest(source, event):
    before = _signature(source)
    if before[2] > MAX_JSON_BYTES:
        raise ValueError("清单超过 16 MiB")
    _checkpoint(event)
    with source.open("rb") as stream:
        if _stat_key(os.fstat(stream.fileno())) != before:
            raise ValueError("清单在读取前发生变化")
        data = stream.read(MAX_JSON_BYTES + 1)
        if _stat_key(os.fstat(stream.fileno())) != before or _signature(source) != before:
            raise ValueError("清单在读取期间发生变化")
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("清单超过 16 MiB")
    document = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    fields = {"format", "version", "algorithm", "created_at", "root_hint", "entries"}
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document["format"] != "yuqiuda-library-manifest"
        or type(document["version"]) is not int
        or document["version"] != 1
        or document["algorithm"] != "sha256"
        or not all(isinstance(document[key], str) and len(document[key]) <= 4096 for key in ("created_at", "root_hint"))
    ):
        raise ValueError("不支持的成品清单格式或版本")
    entries = document["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES:
        raise ValueError("成品清单必须包含 1–10000 项")
    seen = set()
    fields = {"path", "status", "size", "sha256", "source_page", *METADATA_KEYS}
    for item in entries:
        _checkpoint(event)
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("清单文件记录格式无效")
        key = os.path.normcase(_relative(item["path"]))
        if key in seen:
            raise ValueError("清单包含重复文件路径")
        seen.add(key)
        if not isinstance(item["status"], str) or item["status"] not in BASELINE_STATES:
            raise ValueError("清单校验基线状态无效")
        if item["status"] == "recorded":
            if (
                type(item["size"]) is not int
                or item["size"] < 0
                or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            ):
                raise ValueError("清单摘要或大小无效")
        elif item["size"] is not None or item["sha256"] is not None:
            raise ValueError("没有有效基线的记录不能携带摘要")
        if not all(isinstance(item[name], str) and len(item[name]) <= 4096 for name in (*METADATA_KEYS, "source_page")):
            raise ValueError("清单元数据格式无效")
    return entries


def verify_integrity_manifest(storage, source, directory, destination, *, cancel_event=None):
    root = _root(directory)  # Explicit selection, never taken from untrusted JSON.
    output = _destination(storage, destination)
    entries = _load_manifest(Path(source).absolute(), cancel_event)
    if any(
        os.path.normcase(str((root / item["path"]).resolve())) == os.path.normcase(str(output.resolve()))
        for item in entries
    ):
        raise ValueError("报告位置是清单中的资源路径，请选择其他文件名")
    results = []
    with output_path_lock(output, cancel_event, timeout=0):
        for item in entries:
            _checkpoint(cancel_event)
            current = _inspect(root / item["path"], cancel_event)
            status = current["status"]
            if status == "recorded":
                status = (
                    "no_baseline"
                    if item["status"] != "recorded"
                    else "matched"
                    if (current["size"], current["sha256"]) == (item["size"], item["sha256"])
                    else "changed"
                )
            results.append(
                {"path": item["path"], "status": status, "size": current["size"], "sha256": current["sha256"]}
            )
        counts = dict(Counter(item["status"] for item in results))
        _publish(
            output,
            {
                "format": "yuqiuda-library-integrity-report",
                "version": 1,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "root_hint": root.name,
                "counts": counts,
                "entries": results,
            },
            cancel_event,
        )
    return counts

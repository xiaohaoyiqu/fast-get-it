"""Owned same-volume HTTP scratch space, separate from legitimate attachments."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat


_RESERVED = re.compile(r"\.yuqiuda-(?:http|hls)-[0-9a-f]{64}", re.IGNORECASE)


def is_transfer_workspace_name(name):
    return bool(_RESERVED.fullmatch(name))


def workspace_paths(destination, *, kind="http"):
    if kind not in {"http", "hls"}:
        raise ValueError("未知内部下载目录类型")
    destination = Path(destination)
    if any(is_transfer_workspace_name(part) for part in destination.parts):
        raise ValueError("目标位于下载器内部续传目录，请更换保存路径")
    key = hashlib.sha256(os.path.normcase(str(destination.resolve())).encode("utf-8")).hexdigest()
    folder = destination.parent / (f".yuqiuda-{kind}-" + key)
    return folder, folder / "payload.part", folder / "resume.json"


def _linked(path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    # Path.is_junction is absent on Python 3.10/3.11. Windows reparse flags
    # also prevent cleanup from following junctions on those supported runtimes.
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


@contextmanager
def owned_workspace(destination, *, kind="http"):
    """Caller holds the destination lock. Never adopt an unmarked existing folder."""
    folder, _part, _state = workspace_paths(destination, kind=kind)
    marker = folder / "owner.json"
    identity = {"version": 1, "destination_key": folder.name.rsplit("-", 1)[1]}
    if _linked(folder):
        raise FileExistsError("内部续传目录是链接，未写入")
    try:
        folder.mkdir()
    except FileExistsError:
        if not folder.is_dir() or _linked(marker) or not marker.is_file() or marker.stat().st_size > 4096:
            raise FileExistsError("续传目录已被其他文件占用，未覆盖") from None
        try:
            owner = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise FileExistsError("续传目录归属记录无效，未覆盖") from None
        if owner != identity:
            raise FileExistsError("续传目录归属不匹配，未覆盖") from None
    else:
        with marker.open("x", encoding="utf-8") as output:
            json.dump(identity, output)
    try:
        yield folder
    finally:
        # Remove only our empty workspace; unexpected contents are always kept.
        try:
            if not _linked(folder) and set(folder.iterdir()) == {marker}:
                marker.unlink()
                folder.rmdir()
        except OSError:
            pass


@contextmanager
def transfer_workspace(destination):
    with owned_workspace(destination) as folder:
        part, state = folder / "payload.part", folder / "resume.json"
        if any(_linked(path) or (path.exists() and not path.is_file()) for path in (part, state)):
            raise FileExistsError("内部续传文件不是普通文件，未覆盖")
        yield part, state

"""Local HTTP prefix checkpoints, separate from server resource validators."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .adapter import TaskCancelled
from .task_control import task_cancelled


CHECKPOINT_BYTES = 4 * 1024 * 1024
CHECKPOINT_SECONDS = 2.0
HASH_CHUNK_BYTES = 1024 * 1024
MAX_STATE_BYTES = 16 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _digest(value):
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def read_checkpoint(part: Path, state_path: Path, url_key: str) -> dict:
    """Read bounded metadata only; it does not establish content validity."""
    try:
        with state_path.open("rb") as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            return {}
        state = json.loads(raw)
        if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 3:
            return {}
        size, total = state.get("checkpoint_size"), state.get("total")
        etag, modified = state.get("etag"), state.get("last_modified")
        if (
            state.get("url_key") != url_key
            or not _digest(state.get("final_key"))
            or not _digest(state.get("sha256"))
            or type(size) is not int
            or type(total) is not int
            or not 0 < size <= total
            or part.stat().st_size < size
            or not isinstance(etag, str)
            or not isinstance(modified, str)
            or not (etag or modified)
            or etag.startswith("W/")
            or any(character in etag + modified for character in "\r\n")
        ):
            return {}
        return state
    except (OSError, ValueError, TypeError):
        return {}


def file_signature(info):
    # Windows fstat and path stat can expose different ctime semantics. Compare
    # their common identity/size/mtime fields; the prefix digest checks content.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@dataclass
class RestoredCheckpoint:
    state: dict
    digest: object
    signature: tuple


def restore_checkpoint(part, state_path, url_key, event):
    state = read_checkpoint(part, state_path, url_key)
    if not state:
        return None
    digest = hashlib.sha256()
    remaining = state["checkpoint_size"]
    try:
        with part.open("rb") as stream:
            before = file_signature(os.fstat(stream.fileno()))
            while remaining:
                if task_cancelled(event):
                    raise TaskCancelled("校验续传缓存期间任务已取消")
                chunk = stream.read(min(remaining, HASH_CHUNK_BYTES))
                if not chunk:
                    return None
                digest.update(chunk)
                remaining -= len(chunk)
            if task_cancelled(event):
                raise TaskCancelled("校验续传缓存期间任务已取消")
            if before != file_signature(os.fstat(stream.fileno())) or before != file_signature(part.stat()):
                return None
    except FileNotFoundError:
        return None
    if digest.hexdigest() != state["sha256"]:
        return None
    return RestoredCheckpoint(state, digest, before)


def save_checkpoint(path, state):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CheckpointWriter:
    def __init__(self, output, state_path, state, *, offset=0, digest=None, enabled=True, save=save_checkpoint):
        self.output, self.state_path, self.state, self.save = output, state_path, state, save
        self.digest = digest if digest is not None else hashlib.sha256()
        self.size = self.saved_size = offset
        self.enabled = (
            enabled and type(state.get("total")) is int and bool(state.get("etag") or state.get("last_modified"))
        )
        self.saved_at = time.monotonic()

    def write(self, chunk):
        if self.output.write(chunk) != len(chunk):
            raise OSError("续传临时文件未完整写入")
        self.size += len(chunk)
        if self.enabled:
            self.digest.update(chunk)
        self.checkpoint()

    def checkpoint(self, *, force=False):
        if not self.enabled or not self.size or self.size == self.saved_size:
            return
        now = time.monotonic()
        if not force and self.size - self.saved_size < CHECKPOINT_BYTES and now - self.saved_at < CHECKPOINT_SECONDS:
            return
        # Commit metadata only after the prefix has reached the OS. A crash may
        # leave extra bytes, but they can never expand the trusted checkpoint.
        self.output.flush()
        os.fsync(self.output.fileno())
        self.save(self.state_path, {**self.state, "checkpoint_size": self.size, "sha256": self.digest.hexdigest()})
        self.saved_size, self.saved_at = self.size, now

"""Single-connection HTTP transfers with validated, persistent resume state."""
from __future__ import annotations

from software_app.core.task_control import task_cancelled, task_wait

import hashlib
import os
import re
import threading
from pathlib import Path

import requests

from .adapter import TaskCancelled
from .output_lock import output_path_lock
from .bandwidth import limited_chunks
from .network_policy import retry_after_seconds
from .error_policy import classify_error
from .redirect_policy import get_with_redirects
from .transfer_workspace import transfer_workspace
from .http_checkpoint import (
    CheckpointWriter, file_signature, restore_checkpoint,
    read_checkpoint as _resume_state, save_checkpoint as _save_state,
)


def _retry_delay(response, attempt: int) -> float:
    value = response.headers.get("Retry-After", "") if response is not None else ""
    return retry_after_seconds(value, min(2.0 ** attempt, 15.0))


def _open_response(session, url, headers, timeout, validate_url, cancel_event=None, attempt=0):
    """Validate every redirect before sending another request."""
    return get_with_redirects(session, url, headers=headers, timeout=timeout, validate_url=validate_url,
                              cancel_event=cancel_event, backoff_fallback=min(2.0 ** attempt, 15.0))


def download_http_file(session, url: str, destination: Path, cancel_event: threading.Event,
                       *, timeout, chunk_size: int, validate_url, retries: int = 2,
                       resume: bool = True, overwrite: bool = False, commit=None) -> int:
    """Preserve a part only when URL, validator and full length are known.

    The default rejects an existing nonempty destination. Callers that deliberately
    replace a file must opt in to overwrite. Existing zero-byte placeholders can
    be replaced, matching the platform clients' missing-file checks.
    """
    if task_cancelled(cancel_event):
        raise TaskCancelled("任务已取消")
    validate_url(url)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    url_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    with output_path_lock(destination, cancel_event), transfer_workspace(destination) as (part, state_path):
        if destination.exists() and (not destination.is_file() or destination.stat().st_size > 0) and not overwrite:
            raise FileExistsError(f"目标文件已存在，未覆盖：{destination.name}")
        attempt = 0
        restarted = False
        complete = False
        try:
            while True:
                if task_cancelled(cancel_event):
                    raise TaskCancelled("任务已取消")
                restored = restore_checkpoint(part, state_path, url_key, cancel_event) if resume else None
                state = restored.state if restored is not None else {}
                offset = state["checkpoint_size"] if state else 0
                if not state:
                    state_path.unlink(missing_ok=True)
                headers = {"Accept-Encoding": "identity"}
                if offset:
                    headers.update({"Range": f"bytes={offset}-", "If-Range": state.get("etag") or state["last_modified"]})
                response = None
                try:
                    response = _open_response(session, url, headers, timeout, validate_url, cancel_event, attempt)
                    final_url = str(getattr(response, "url", None) or url)
                    final_key = hashlib.sha256(final_url.encode("utf-8")).hexdigest()
                    status = response.status_code
                    if status == 416 and offset:
                        # Even an exactly-sized part must be revalidated. A clean
                        # request handles changed resources and bogus server ranges.
                        if restarted:
                            response.raise_for_status()
                        restarted = True
                        part.unlink(missing_ok=True)
                        state_path.unlink(missing_ok=True)
                        continue
                    response.raise_for_status()
                    if status not in {200, 206}:
                        raise ValueError(f"下载响应状态无效：{status}")
                    encoding = response.headers.get("Content-Encoding", "identity").lower()
                    if encoding not in {"", "identity"}:
                        raise ValueError("下载服务器未遵守 identity 编码，无法校验文件长度")
                    raw_length = response.headers.get("Content-Length")
                    length = int(raw_length) if raw_length is not None else None
                    if length is not None and length < 0:
                        raise ValueError("下载响应长度无效")
                    total = length
                    if status == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        valid_range = False
                        if match and offset:
                            start, end, total = map(int, match.groups())
                            validator = "ETag" if state.get("etag") else "Last-Modified"
                            expected = state.get("etag") or state.get("last_modified")
                            valid_range = (final_key == state["final_key"]
                                           and start == offset and start <= end < total
                                           and end == total - 1 and total == state["total"]
                                           and (length is None or length == end - start + 1)
                                           and response.headers.get(validator) == expected)
                        if not valid_range:
                            if restarted:
                                raise ValueError("服务器返回了不匹配的续传范围")
                            restarted = True
                            part.unlink(missing_ok=True)
                            state_path.unlink(missing_ok=True)
                            continue
                    else:
                        # 200 after If-Range means the resource changed or Range
                        # is unsupported. Replace the partial bytes, never append.
                        offset = 0
                    etag = str(response.headers.get("ETag") or "")
                    if etag.startswith("W/"):
                        etag = ""
                    modified = str(response.headers.get("Last-Modified") or "")
                    new_state = {"version": 3, "url_key": url_key, "final_key": final_key, "total": total,
                                 "etag": etag, "last_modified": modified}
                    with part.open("r+b" if offset else "wb") as output:
                        if offset:
                            if file_signature(os.fstat(output.fileno())) != restored.signature:
                                raise ValueError("续传缓存校验后发生变化，未追加；请重新读取后重试")
                            output.seek(offset)
                            output.truncate()  # Drop only the uncheckpointed tail.
                        else:
                            state_path.unlink(missing_ok=True)
                        writer = CheckpointWriter(output, state_path, new_state, offset=offset,
                                                  digest=restored.digest if offset else None, enabled=resume, save=_save_state)
                        try:
                            for chunk in limited_chunks(response, cancel_event, chunk_size=chunk_size):
                                if task_cancelled(cancel_event):
                                    raise TaskCancelled("任务已取消")
                                if chunk:
                                    if total is not None and writer.size + len(chunk) > total:
                                        state_path.unlink(missing_ok=True)
                                        raise ValueError("下载内容超过服务器声明的长度")
                                    writer.write(chunk)
                        except (TaskCancelled, requests.RequestException):
                            writer.checkpoint(force=True)
                            raise
                        writer.checkpoint(force=True)
                        size = writer.size
                    if total is not None and size != total:
                        raise requests.exceptions.ChunkedEncodingError("下载内容长度不足")
                    if task_cancelled(cancel_event):
                        raise TaskCancelled("任务已取消")
                    complete = True
                    (commit or os.replace)(part, destination)
                    state_path.unlink(missing_ok=True)
                    return destination.stat().st_size
                except requests.RequestException as exc:
                    if not classify_error(exc).retryable or attempt >= max(0, retries):
                        raise
                    delay = _retry_delay(response, attempt)
                    attempt += 1
                    if response is not None:
                        response.close()
                        response = None
                    if task_wait(cancel_event, delay):
                        raise TaskCancelled("重试等待期间任务已取消") from exc
                finally:
                    if response is not None:
                        response.close()
        finally:
            if not resume or not _resume_state(part, state_path, url_key):
                if not complete or not resume:
                    part.unlink(missing_ok=True)
                state_path.unlink(missing_ok=True)

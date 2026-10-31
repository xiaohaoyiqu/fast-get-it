"""Validated HTTP HLS segments with persistent cache and local-only remuxing."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import uuid

import requests

from .adapter import TaskCancelled
from .bandwidth import limited_chunks
from .error_policy import classify_error
from .file_conflict import replace_output
from .hls_playlist import MAX_PLAYLIST_BYTES, parse_playlist
from .media_process import run_media_process
from .redirect_policy import anonymous_session, get_with_redirects
from .response_read import read_response_bytes
from .task_control import task_cancelled, task_wait
from .transfer_workspace import owned_workspace, _linked


MAX_SEGMENT_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024 * 1024
_OWNED = re.compile(
    r"(?:r[0-9]{6}\.(?:ts|m4s|mp4|aac|mp3)(?:\.part|\.json)?|state\.json|local\.m3u8|output\.mp4|\.hls-[0-9a-f]{32}\.tmp)"
)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _checkpoint(event):
    if task_cancelled(event):
        raise TaskCancelled("任务已取消")


def _regular(path):
    if _linked(path) or (path.exists() and not path.is_file()):
        raise FileExistsError("HLS 内部路径不是普通文件，未覆盖")


def _hash_file(path, event):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            _checkpoint(event)
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _atomic_text(path, text):
    _regular(path)
    temporary = path.parent / f".hls-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _purge(folder):
    for path in folder.iterdir():
        if _OWNED.fullmatch(path.name):
            _regular(path)
            path.unlink()


def _get(session, url, event, validate_url, headers=None):
    return get_with_redirects(
        session,
        url,
        validate_url=validate_url,
        timeout=(10, 45),
        headers={"Accept-Encoding": "identity", "Accept": "*/*", **(headers or {})},
        cancel_event=event,
    )


def resolve_plan(session, url, event, validate_url):
    seen = set()
    root = url
    for _depth in range(4):
        _checkpoint(event)
        if url in seen:
            raise ValueError("HLS 主列表循环")
        seen.add(url)
        response = _get(session, url, event, validate_url)
        try:
            text = read_response_bytes(response, MAX_PLAYLIST_BYTES, cancel_event=event).decode("utf-8")
            final = str(response.url or url)
        finally:
            response.close()
        plan = parse_playlist(text, final)
        if "variant" in plan:
            url = plan["variant"]
            continue
        serialized = json.dumps({"root": root, "selected": final, "plan": plan}, sort_keys=True)
        plan["fingerprint"] = _digest(serialized)
        return plan
    raise ValueError("HLS 主列表嵌套超过上限")


def _resources(plan):
    resources, lookup = [], {}
    for segment in plan["segments"]:
        for role, uri in (("map", segment["map"]), ("segment", segment["url"])):
            if uri is None or (role, uri) in lookup:
                continue
            # Names written into the local playlist never come from remote text.
            extension = ".mp4" if role == "map" else (".m4s" if segment["map"] else ".ts")
            name = f"r{len(resources):06d}{extension}"
            lookup[role, uri] = name
            resources.append((name, uri))
    return resources, lookup


def _state(folder, fingerprint):
    path = folder / "state.json"
    _regular(path)
    try:
        with path.open("rb") as source:
            raw = source.read(4097)
        state = json.loads(raw) if len(raw) <= 4096 else None
        if isinstance(state, dict) and state.get("version") == 1 and state.get("fingerprint") == fingerprint:
            return state
    except (FileNotFoundError, ValueError, RecursionError):
        pass
    _purge(folder)
    return {"version": 1, "fingerprint": fingerprint}


def _resource_state(path):
    _regular(path)
    try:
        with path.open("rb") as source:
            raw = source.read(4097)
        value = json.loads(raw) if len(raw) <= 4096 else None
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, ValueError, RecursionError):
        return None


def _save_state(folder, state):
    _atomic_text(folder / "state.json", json.dumps(state, sort_keys=True))


def _valid_file(path, record, event):
    _regular(path)
    if (
        not isinstance(record, dict)
        or type(record.get("size")) is not int
        or not 0 < record["size"] <= MAX_SEGMENT_BYTES
        or not isinstance(record.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
        or not path.is_file()
        or path.stat().st_size != record["size"]
    ):
        return False
    if _hash_file(path, event)[1] != record["sha256"]:
        return False
    try:
        _validate_resource(path, event, path.suffix)
    except ValueError:
        return False
    return True


def _validate_resource(path, event, extension):
    """Reject obvious error bodies and truncated containers before remuxing.

    This is a bounded framing check, not a codec, sample-table or duration audit.
    FFmpeg can silently discard a non-TS response between otherwise valid pieces.
    """
    with path.open("rb") as source:
        if extension == ".ts":
            size = 0
            while chunk := source.read(188 * 1024):
                _checkpoint(event)
                if len(chunk) % 188 or chunk[::188] != b"\x47" * (len(chunk) // 188):
                    raise ValueError("HLS TS 分片包结构无效，未提交可能缺片的视频")
                size += len(chunk)
            if not size:
                raise ValueError("HLS TS 分片为空")
            return
        size = os.fstat(source.fileno()).st_size
        offset, count, types = 0, 0, set()
        pending_media = False
        while offset < size:
            _checkpoint(event)
            source.seek(offset)
            header = source.read(8)
            if len(header) != 8:
                raise ValueError("HLS MP4 分片盒头不完整")
            length, kind = int.from_bytes(header[:4], "big"), header[4:]
            header_size = 8
            if length == 1:
                extended = source.read(8)
                if len(extended) != 8:
                    raise ValueError("HLS MP4 分片扩展盒头不完整")
                length, header_size = int.from_bytes(extended, "big"), 16
            elif length == 0:
                length = size - offset
            count += 1
            if length < header_size or length > size - offset or count > 100000:
                raise ValueError("HLS MP4 分片盒长度无效或超限")
            if kind == b"moof":
                if pending_media or length == header_size:
                    raise ValueError("HLS MP4 分片缺少媒体数据")
                pending_media = True
            elif kind == b"mdat":
                if not pending_media or length == header_size:
                    raise ValueError("HLS MP4 分片缺少片段头或媒体数据")
                pending_media = False
            elif kind == b"moov" and (b"ftyp" not in types or length == header_size):
                raise ValueError("HLS MP4 初始化片段结构无效")
            elif kind == b"ftyp" and length < header_size + 8:
                raise ValueError("HLS MP4 初始化类型盒不完整")
            types.add(kind)
            offset += length
        required = {b"ftyp", b"moov"} if extension == ".mp4" else {b"moof", b"mdat"}
        if not required.issubset(types) or pending_media:
            raise ValueError("HLS MP4 分片缺少必要的初始化或媒体盒")


def _header(value):
    return value if isinstance(value, str) and len(value) <= 256 and not any(c in value for c in "\r\n\x00") else ""


class _ResourceChanged(Exception):
    pass


def _fetch_resource(session, url, path, old, event, validate_url):
    valid = _valid_file(path, old, event)
    headers = {}
    if valid:
        etag, modified = _header(old.get("etag")), _header(old.get("modified"))
        if etag and not etag.startswith("W/"):
            headers["If-None-Match"] = etag
        elif modified:
            headers["If-Modified-Since"] = modified
    temporary = path.with_name(path.name + ".part")
    _regular(temporary)
    response = None
    try:
        response = _get(session, url, event, validate_url, headers)
        final = _digest(str(response.url or url))
        if response.status_code == 304:
            if headers:
                same_etag = not response.headers.get("ETag") or response.headers["ETag"] == old.get("etag")
                same_modified = not response.headers.get("Last-Modified") or response.headers[
                    "Last-Modified"
                ] == old.get("modified")
                if final == old.get("final") and same_etag and same_modified:
                    return old, True
            response.close()
            response = _get(session, url, event, validate_url)
            final = _digest(str(response.url or url))
        response.raise_for_status()
        if response.status_code != 200 or response.headers.get("Content-Encoding", "identity").lower() not in {
            "",
            "identity",
        }:
            raise ValueError("HLS 分片响应状态或编码无效")
        length = response.headers.get("Content-Length")
        length = int(length) if length is not None else None
        if length is not None and not 0 < length <= MAX_SEGMENT_BYTES:
            raise ValueError("HLS 分片声明大小无效或超限")
        size, digest = 0, hashlib.sha256()
        with temporary.open("wb") as output:
            for chunk in limited_chunks(response, event, chunk_size=64 * 1024):
                _checkpoint(event)
                size += len(chunk)
                if size > MAX_SEGMENT_BYTES or (length is not None and size > length):
                    raise ValueError("HLS 分片大小超限")
                output.write(chunk)
                digest.update(chunk)
        if not size or (length is not None and size != length):
            raise requests.exceptions.ChunkedEncodingError("HLS 分片长度不足")
        if (
            isinstance(old, dict)
            and isinstance(old.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", old["sha256"])
            and digest.hexdigest() != old["sha256"]
        ):
            raise _ResourceChanged()
        _checkpoint(event)
        _validate_resource(temporary, event, path.suffix)
        os.replace(temporary, path)
        etag = _header(response.headers.get("ETag"))
        return {
            "size": size,
            "sha256": digest.hexdigest(),
            "final": final,
            "etag": etag if not etag.startswith("W/") else "",
            "modified": _header(response.headers.get("Last-Modified")),
        }, False
    finally:
        if response is not None:
            response.close()
        temporary.unlink(missing_ok=True)


def _local_playlist(plan, lookup):
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{plan['target']}",
        f"#EXT-X-MEDIA-SEQUENCE:{plan['sequence']}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{plan['discontinuity_sequence']}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    initialization = None
    for segment in plan["segments"]:
        if segment["discontinuity"]:
            lines.append("#EXT-X-DISCONTINUITY")
        if segment["map"] != initialization:
            initialization = segment["map"]
            if initialization:
                lines.append(f'#EXT-X-MAP:URI="{lookup["map", initialization]}"')
        lines.extend((f"#EXTINF:{segment['duration']:.9f},", lookup["segment", segment["url"]]))
    return "\n".join((*lines, "#EXT-X-ENDLIST", ""))


def download_hls(session, url, destination, event, *, ffmpeg, validate_url, progress=None):
    """Caller owns the output lock; only complete validated segments are reusable."""
    session = anonymous_session(session)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with owned_workspace(destination, kind="hls") as folder:
        for generation in range(2):
            plan = resolve_plan(session, url, event, validate_url)
            state = _state(folder, plan["fingerprint"])
            _save_state(folder, state)
            resources, lookup = _resources(plan)
            reused, total_bytes = 0, 0
            try:
                for done, (name, source) in enumerate(resources, 1):
                    record_path = folder / (name + ".json")
                    previous = _resource_state(record_path)
                    for attempt in range(3):
                        try:
                            record, cached = _fetch_resource(
                                session, source, folder / name, previous, event, validate_url
                            )
                            break
                        except requests.RequestException as exc:
                            if attempt == 2 or not classify_error(exc).retryable:
                                raise
                            if task_wait(event, 0.5 * (2**attempt)):
                                raise TaskCancelled("任务已取消") from exc
                    if not isinstance(previous, dict) or previous.get("sha256") != record["sha256"]:
                        if state.pop("assembled", None) is not None:
                            _save_state(folder, state)
                    total_bytes += record["size"]
                    if total_bytes > MAX_TOTAL_BYTES:
                        raise ValueError("HLS 总大小超过 64 GiB 上限")
                    _atomic_text(record_path, json.dumps(record))
                    reused += int(cached)
                    if progress:
                        progress(done, len(resources), reused)
                break
            except _ResourceChanged:
                _purge(folder)
                if generation:
                    raise ValueError("HLS 分片反复变化，未拼接旧内容") from None
        output = folder / "output.mp4"
        _regular(output)
        assembled = state.get("assembled")
        # A failed final rename can be retried without another remux after all
        # resources were validated again. Output size can exceed one segment.
        ready = (
            isinstance(assembled, dict)
            and type(assembled.get("size")) is int
            and 0 < assembled["size"] <= MAX_TOTAL_BYTES
            and output.is_file()
            and _hash_file(output, event) == (assembled.get("size"), assembled.get("sha256"))
        )
        if not ready:
            output.unlink(missing_ok=True)
            playlist = folder / "local.m3u8"
            _atomic_text(playlist, _local_playlist(plan, lookup))
            result = run_media_process(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-protocol_whitelist",
                    "file",
                    "-i",
                    str(playlist),
                    "-c",
                    "copy",
                    str(output),
                ],
                event,
            )
            if result.returncode != 0 or not output.is_file() or not output.stat().st_size:
                output.unlink(missing_ok=True)
                raise RuntimeError((result.stderr.strip().splitlines() or ["HLS 本地封装失败"])[-1])
            size, sha = _hash_file(output, event)
            state["assembled"] = {"size": size, "sha256": sha}
            _save_state(folder, state)
        replace_output(output, destination, event)
        _purge(folder)
        return {"resources": len(resources), "reused": reused, "bytes": total_bytes}

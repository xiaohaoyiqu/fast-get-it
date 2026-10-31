"""Bound decoded response bodies and check task control between chunks."""

from __future__ import annotations

import json

from .network_policy import request_checkpoint

JSON_LIMIT = 32 * 1024 * 1024
HTML_LIMIT = 16 * 1024 * 1024


class ResponseLimitError(ValueError):
    """A body was refused before parsing rather than treated as partial data."""


def read_response_bytes(response, maximum, *, cancel_event=None):
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("响应读取上限必须是正整数")
    request_checkpoint(cancel_event)
    response.raise_for_status()
    content_encoding = str(response.headers.get("Content-Encoding") or "").lower()
    try:
        declared = int(response.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if content_encoding in {"", "identity"} and declared > maximum:
        raise ResponseLimitError(f"响应超过 {maximum // (1024 * 1024)} MiB 读取上限")
    content = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        request_checkpoint(cancel_event)
        if not chunk:
            continue
        if len(content) + len(chunk) > maximum:
            raise ResponseLimitError(f"解压后响应超过 {maximum // (1024 * 1024)} MiB 读取上限")
        content.extend(chunk)
    request_checkpoint(cancel_event)
    return bytes(content)


def read_response_json(response, maximum=JSON_LIMIT, *, cancel_event=None):
    return json.loads(read_response_bytes(response, maximum, cancel_event=cancel_event))


def read_response_text(response, maximum=HTML_LIMIT, *, cancel_event=None):
    data = read_response_bytes(response, maximum, cancel_event=cancel_event)
    encoding = response.encoding or "utf-8"
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")

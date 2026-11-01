"""Bounded streaming reads for small public preview images."""

from __future__ import annotations


def read_preview_image(response, maximum: int) -> bytes:
    try:
        response.raise_for_status()
        content_type = str(
            response.headers.get("Content-Type") or response.headers.get("content-type") or ""
        ).casefold()
        if not content_type.startswith("image/"):
            raise ValueError("预览地址没有返回图片；可能是登录页或拦截页")
        try:
            length = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > maximum:
            raise ValueError("预览图片超过大小上限")
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > maximum:
                raise ValueError("预览图片超过大小上限")
            chunks.append(chunk)
        if not size:
            raise ValueError("预览图片为空")
        return b"".join(chunks)
    finally:
        response.close()

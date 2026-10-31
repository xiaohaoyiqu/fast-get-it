"""Bounded parser for closed, unencrypted, single-rendition HLS playlists."""

from __future__ import annotations

import math
import re
from urllib.parse import urljoin, urlsplit


MAX_SEGMENTS = 10000
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024


class UnsupportedHlsError(ValueError):
    pass


def attributes(text):
    if not text or text.endswith(","):
        raise ValueError("HLS 属性列表不能为空或以逗号结尾")
    result, offset = {}, 0
    pattern = re.compile(r'([A-Z0-9-]+)=("[^"\r\n]*"|[^,\s]+)(?:,|$)')
    while offset < len(text):
        match = pattern.match(text, offset)
        if not match or match[1] in result:
            raise ValueError("HLS 属性列表格式无效")
        result[match[1]] = match[2].strip('"')
        offset = match.end()
    return result


def media_url(base, value):
    if not value or any(c in value for c in '\r\n\x00\\"') or "{$" in value:
        raise ValueError("HLS 资源地址无效")
    result = urljoin(base, value)
    parts = urlsplit(result)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or (urlsplit(base).scheme == "https" and parts.scheme != "https")
    ):
        raise ValueError("HLS 仅接受无内嵌账号的公开 HTTP/HTTPS 地址，且不能降级 HTTPS")
    return result


def parse_playlist(text, base):
    if len(text.encode("utf-8")) > MAX_PLAYLIST_BYTES or text.startswith("\ufeff") or "\x00" in text:
        raise ValueError("HLS 播放列表编码或长度无效")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise ValueError("服务器未返回 HLS 播放列表")
    variants, renditions, segments = [], [], []
    pending_variant = None
    duration, initialization, target, sequence, discontinuity_sequence = None, None, None, 0, 0
    discontinuity, ended = False, False
    seen = set()
    for line in lines[1:]:
        tag, _, value = line.partition(":")
        if not line.startswith("#"):
            uri = media_url(base, line)
            if ended:
                raise ValueError("HLS 结束标记后仍有资源")
            if pending_variant is not None:
                variants.append({**pending_variant, "url": uri})
                pending_variant = None
            elif duration is not None:
                segments.append(
                    {"url": uri, "duration": duration, "map": initialization, "discontinuity": discontinuity}
                )
                duration, discontinuity = None, False
            else:
                raise ValueError("HLS 资源缺少分片时长或变体信息")
            if len(segments) > MAX_SEGMENTS or len(variants) > 100:
                raise ValueError("HLS 播放列表项目过多")
        elif tag == "#EXT-X-STREAM-INF":
            if pending_variant is not None or duration is not None:
                raise ValueError("HLS 变体地址缺失")
            attrs = attributes(value)
            bandwidth = attrs.get("BANDWIDTH", "")
            if (
                not bandwidth.isascii()
                or not bandwidth.isdigit()
                or len(bandwidth) > 12
                or not 0 < int(bandwidth) < 10**12
            ):
                raise ValueError("HLS 变体带宽无效")
            pending_variant = {"bandwidth": int(bandwidth), "audio": attrs.get("AUDIO"), "video": attrs.get("VIDEO")}
        elif tag == "#EXT-X-MEDIA":
            renditions.append(attributes(value))
        elif tag == "#EXTINF":
            if duration is not None or pending_variant is not None:
                raise ValueError("HLS 分片地址缺失")
            duration = float(value.partition(",")[0])
            if not math.isfinite(duration) or not 0 < duration <= 86400:
                raise ValueError("HLS 分片时长无效")
        elif tag in {"#EXT-X-TARGETDURATION", "#EXT-X-MEDIA-SEQUENCE", "#EXT-X-DISCONTINUITY-SEQUENCE"}:
            if (
                tag in seen
                or segments
                or not value.isascii()
                or not value.isdigit()
                or len(value) > 19
                or int(value) >= 2**63
            ):
                raise ValueError("HLS 时长或序号无效")
            seen.add(tag)
            if tag == "#EXT-X-TARGETDURATION":
                target = int(value)
                if not 0 < target <= 86400:
                    raise ValueError("HLS 目标时长无效")
            elif tag == "#EXT-X-MEDIA-SEQUENCE":
                sequence = int(value)
            else:
                discontinuity_sequence = int(value)
        elif tag == "#EXT-X-MAP":
            attrs = attributes(value)
            if "BYTERANGE" in attrs:
                raise UnsupportedHlsError("HLS 暂不支持字节范围初始化片段")
            initialization = media_url(base, attrs.get("URI", ""))
        elif tag in {"#EXT-X-KEY", "#EXT-X-SESSION-KEY"}:
            if attributes(value).get("METHOD") != "NONE":
                raise UnsupportedHlsError("HLS 分片恢复暂不支持加密流")
        elif tag == "#EXT-X-DISCONTINUITY":
            if discontinuity:
                raise ValueError("HLS 不连续标记重复")
            discontinuity = True
        elif tag == "#EXT-X-ENDLIST":
            if ended or duration is not None or pending_variant is not None:
                raise ValueError("HLS 结束标记位置无效")
            ended = True
        elif tag in {
            "#EXT-X-BYTERANGE",
            "#EXT-X-I-FRAMES-ONLY",
            "#EXT-X-PART",
            "#EXT-X-SKIP",
            "#EXT-X-DEFINE",
            "#EXT-X-GAP",
        }:
            raise UnsupportedHlsError("HLS 暂不支持此分片布局／增量或变量扩展")
        elif tag == "#EXT-X-PLAYLIST-TYPE":
            if value not in {"VOD", "EVENT"}:
                raise ValueError("HLS 播放列表类型无效")
        elif tag.startswith("#EXT") and tag not in {
            "#EXT-X-VERSION",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            "#EXT-X-PROGRAM-DATE-TIME",
            "#EXT-X-DATERANGE",
            "#EXT-X-START",
            "#EXT-X-I-FRAME-STREAM-INF",
            "#EXT-X-SESSION-DATA",
        }:
            raise UnsupportedHlsError("HLS 含有尚未支持的扩展")
    if pending_variant is not None or duration is not None:
        raise ValueError("HLS 最后一个资源地址缺失")
    if variants:
        if segments or target is not None or ended:
            raise ValueError("HLS 主列表和媒体列表混合")
        selected = max(variants, key=lambda row: row["bandwidth"])
        if any(
            row.get("URI")
            and row.get("GROUP-ID") in {selected["audio"], selected["video"]}
            and row.get("TYPE") in {"AUDIO", "VIDEO"}
            for row in renditions
        ):
            raise UnsupportedHlsError("HLS 分片恢复暂不支持独立音轨／视频轨")
        return {"variant": selected["url"]}
    if not ended:
        raise UnsupportedHlsError("HLS 分片恢复仅支持已有结束标记的点播列表")
    if not segments or target is None:
        raise ValueError("HLS 媒体列表缺少分片或目标时长")
    return {
        "target": target,
        "sequence": sequence,
        "discontinuity_sequence": discontinuity_sequence,
        "segments": segments,
    }

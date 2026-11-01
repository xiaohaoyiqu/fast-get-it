from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass


MAX_TORRENT_BYTES = 10 * 1024 * 1024
MAX_DEPTH = 64
MAX_ITEMS = 100_000


@dataclass(frozen=True)
class TorrentSummary:
    info_hash: str
    file_count: int
    total_size: int
    piece_length: int
    piece_count: int
    tracker_count: int
    private: bool


class _BencodeDecoder:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.position = 0
        self.items = 0
        self.info_span: tuple[int, int] | None = None

    def decode(self):
        value = self._value(0, top_level=True)
        if self.position != len(self.payload):
            raise ValueError("种子文件包含尾随数据")
        return value

    def _count(self) -> None:
        self.items += 1
        if self.items > MAX_ITEMS:
            raise ValueError("种子元数据项目数量过多")

    def _value(self, depth: int, *, top_level: bool = False):
        if depth > MAX_DEPTH or self.position >= len(self.payload):
            raise ValueError("种子 bencode 结构截断或嵌套过深")
        self._count()
        marker = self.payload[self.position:self.position + 1]
        if marker == b"i":
            return self._integer()
        if marker == b"l":
            self.position += 1
            values = []
            while self._peek() != b"e":
                values.append(self._value(depth + 1))
            self.position += 1
            return values
        if marker == b"d":
            self.position += 1
            values: dict[bytes, object] = {}
            while self._peek() != b"e":
                key = self._bytes()
                if key in values:
                    raise ValueError("种子 bencode 字典包含重复字段")
                start = self.position
                values[key] = self._value(depth + 1)
                if top_level and key == b"info":
                    self.info_span = (start, self.position)
            self.position += 1
            return values
        if marker.isdigit():
            return self._bytes()
        raise ValueError("种子 bencode 包含无效类型标记")

    def _peek(self) -> bytes:
        if self.position >= len(self.payload):
            raise ValueError("种子 bencode 结构意外结束")
        return self.payload[self.position:self.position + 1]

    def _integer(self) -> int:
        end = self.payload.find(b"e", self.position + 1)
        if end < 0:
            raise ValueError("种子 bencode 整数未结束")
        raw = self.payload[self.position + 1:end]
        if (
            not raw
            or raw == b"-0"
            or raw.startswith(b"+")
            or (raw.startswith(b"0") and len(raw) > 1)
            or raw.startswith(b"-0")
            or (raw.startswith(b"-") and not raw[1:].isdigit())
            or (not raw.startswith(b"-") and not raw.isdigit())
        ):
            raise ValueError("种子 bencode 整数格式无效")
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("种子 bencode 整数格式无效") from exc
        self.position = end + 1
        return value

    def _bytes(self) -> bytes:
        colon = self.payload.find(b":", self.position)
        if colon < 0:
            raise ValueError("种子 bencode 字节串缺少长度分隔符")
        raw_length = self.payload[self.position:colon]
        if not raw_length or not raw_length.isdigit() or (raw_length.startswith(b"0") and len(raw_length) > 1):
            raise ValueError("种子 bencode 字节串长度无效")
        length = int(raw_length)
        start = colon + 1
        end = start + length
        if end > len(self.payload):
            raise ValueError("种子 bencode 字节串被截断")
        self.position = end
        return self.payload[start:end]


def _positive_integer(value: object, label: str, *, allow_zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < (0 if allow_zero else 1):
        raise ValueError(f"种子 {label} 无效")
    return value


def _safe_path_parts(value: object) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError("种子多文件路径为空或格式无效")
    for raw in value:
        if not isinstance(raw, bytes):
            raise ValueError("种子多文件路径字段无效")
        text = raw.decode("utf-8", errors="replace")
        device_name = text.rstrip(" .").split(".", 1)[0].casefold()
        if (
            not text
            or text in {".", ".."}
            or "/" in text
            or "\\" in text
            or ":" in text
            or "\x00" in text
            or len(text) > 255
            or text.endswith((" ", "."))
            or any(ord(character) < 32 for character in text)
            or device_name in {"con", "prn", "aux", "nul", "clock$"}
            or re.fullmatch(r"(?:com|lpt)[1-9]", device_name) is not None
        ):
            raise ValueError("种子包含不安全的文件路径")


def _safe_name(value: object) -> None:
    if not isinstance(value, bytes):
        raise ValueError("种子 info 缺少有效名称")
    _safe_path_parts([value])


def parse_torrent(payload: bytes) -> TorrentSummary:
    data = bytes(payload)
    if not data or len(data) > MAX_TORRENT_BYTES:
        raise ValueError("种子文件为空或超过 10 MiB")
    decoder = _BencodeDecoder(data)
    root = decoder.decode()
    if not isinstance(root, dict) or not isinstance(root.get(b"info"), dict) or decoder.info_span is None:
        raise ValueError("种子缺少有效 info 字典")
    info = root[b"info"]
    name = info.get(b"name.utf-8", info.get(b"name"))
    if not isinstance(name, bytes) or not name or len(name) > 255:
        raise ValueError("种子 info 缺少有效名称")
    _safe_name(name)
    if b"name" in info:
        _safe_name(info[b"name"])
    if b"name.utf-8" in info:
        _safe_name(info[b"name.utf-8"])
    piece_length = _positive_integer(info.get(b"piece length"), "piece length")
    if piece_length > 64 * 1024 * 1024:
        raise ValueError("种子分片大小异常")
    pieces = info.get(b"pieces")
    if not isinstance(pieces, bytes) or not pieces or len(pieces) % 20:
        raise ValueError("种子 pieces 字段无效")
    piece_count = len(pieces) // 20

    files = info.get(b"files")
    if files is None:
        total_size = _positive_integer(info.get(b"length"), "文件大小", allow_zero=True)
        file_count = 1
    else:
        if not isinstance(files, list) or not files:
            raise ValueError("种子多文件列表为空或格式无效")
        total_size = 0
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("种子多文件项目格式无效")
            total_size += _positive_integer(item.get(b"length"), "文件大小", allow_zero=True)
            path_value = item.get(b"path.utf-8", item.get(b"path"))
            _safe_path_parts(path_value)
            if b"path" in item:
                _safe_path_parts(item[b"path"])
            if b"path.utf-8" in item:
                _safe_path_parts(item[b"path.utf-8"])
        file_count = len(files)
    expected_pieces = math.ceil(total_size / piece_length) if total_size else 1
    if piece_count != expected_pieces:
        raise ValueError("种子分片数量与总大小不一致")

    trackers: set[bytes] = set()
    announce = root.get(b"announce")
    if isinstance(announce, bytes) and announce:
        trackers.add(announce)
    announce_list = root.get(b"announce-list")
    if isinstance(announce_list, list):
        for tier in announce_list:
            values = tier if isinstance(tier, list) else [tier]
            trackers.update(value for value in values if isinstance(value, bytes) and value)

    start, end = decoder.info_span
    return TorrentSummary(
        info_hash=hashlib.sha1(data[start:end]).hexdigest(),
        file_count=file_count,
        total_size=total_size,
        piece_length=piece_length,
        piece_count=piece_count,
        tracker_count=len(trackers),
        private=info.get(b"private") == 1,
    )


__all__ = ["TorrentSummary", "parse_torrent"]

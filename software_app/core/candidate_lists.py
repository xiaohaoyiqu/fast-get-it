from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from urllib.parse import urlparse

from .exports import _atomic_write_text, safe_csv_cell


_MAX_LIST_BYTES = 5 * 1024 * 1024
_MAX_ROWS = 10000
_FIELDS = (
    "module_id", "target", "input_kind", "id", "title", "author_id", "author_name",
    "page_count", "chapter_count", "image_count", "bookmarked", "likes", "bookmarks",
    "views", "popularity", "published_at", "bookmarked_at", "tags", "source",
)


def _platform_for_url(target: str) -> str:
    parsed = urlparse(target)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"名单中存在无效网址：{target[:100]}")
    host = parsed.hostname.casefold().rstrip(".")
    if host in {"pixiv.net", "fanbox.cc"} or host.endswith((".pixiv.net", ".fanbox.cc")):
        return "pixiv"
    if host in {"x.com", "twitter.com"} or host.endswith((".x.com", ".twitter.com")):
        return "twitter"
    if "jmcomic" in host or host.endswith("18comic.vip"):
        return "jmcomic"
    return ""


def _normalize_row(value: object, module_id: str) -> dict | None:
    row = {"target": value} if isinstance(value, str) else value
    if not isinstance(row, dict):
        return None
    declared_platform = str(row.get("module_id") or row.get("platform") or "").strip().lower()
    if declared_platform and declared_platform != module_id:
        raise ValueError(f"名单包含其他平台 {declared_platform} 的目标")
    target = str(row.get("target") or row.get("url") or row.get("下载地址") or row.get("id") or "").strip()
    if not target or target.startswith("#"):
        return None
    if any(character in target for character in "\r\n\x00"):
        raise ValueError("名单目标含有无效控制字符")
    if target.startswith(("http://", "https://")):
        detected = _platform_for_url(target)
        if module_id != "website" and detected and detected != module_id:
            raise ValueError(f"名单包含 {detected} 的网址，请切换平台后导入")
        if module_id in {"pixiv", "twitter"} and detected != module_id:
            raise ValueError(f"{module_id} 名单包含非本站网址，请切换到网页平台后导入")
    elif module_id == "website":
        raise ValueError("网页平台名单必须使用完整 HTTP(S) 网址")
    normalized = {str(key): item for key, item in row.items() if isinstance(key, str)}
    tags = normalized.get("tags")
    if isinstance(tags, str):
        normalized["tags"] = [part.strip() for part in tags.replace("，", ",").split(",") if part.strip()]
    normalized.update({
        "module_id": module_id,
        "target": target,
        "url": target,
        "title": str(row.get("title") or row.get("name") or row.get("名字") or target),
        "source": "file_import",
        "import_source": "candidate_list",
    })
    return normalized


def import_candidate_list(path: Path | str, module_id: str) -> list[dict]:
    """Read a local list into preview candidates; never start downloads."""
    source = Path(path).expanduser()
    if source.stat().st_size > _MAX_LIST_BYTES:
        raise ValueError("名单超过 5 MiB，请拆分后导入")
    content = source.read_text(encoding="utf-8-sig")
    suffix = source.suffix.casefold()
    if suffix == ".json":
        payload = json.loads(content)
        values = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(values, list):
            raise ValueError("JSON 名单需要数组，或包含 items 数组")
    elif suffix == ".csv":
        values = list(csv.DictReader(io.StringIO(content)))
        for row in values:
            for key in ("target", "url", "下载地址"):
                value = row.get(key)
                if isinstance(value, str) and len(value) > 1 and value[0] == "'" and value[1] in "=+-@":
                    row[key] = value[1:]
    elif suffix == ".txt":
        values = [line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    else:
        raise ValueError("名单仅支持 TXT、CSV、JSON")
    if len(values) > _MAX_ROWS:
        raise ValueError("单次最多导入 10000 个目标")
    result: list[dict] = []
    seen: set[str] = set()
    for value in values:
        row = _normalize_row(value, module_id)
        if row and row["target"].casefold() not in seen:
            seen.add(row["target"].casefold())
            result.append(row)
    return result


def export_candidate_list(rows: list[dict], destination: Path | str, module_id: str) -> int:
    """Export current candidates with a shared schema and empty cells for unknown metrics."""
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        target = str(row.get("target") or row.get("url") or "").strip()
        if not target or target.casefold() in seen:
            continue
        seen.add(target.casefold())
        item = {key: str(row.get(key) if row.get(key) is not None else "") for key in _FIELDS}
        item["module_id"] = module_id
        item["target"] = target
        item["id"] = str(row.get("id") or row.get("handle") or "")
        item["title"] = str(row.get("title") or row.get("name") or target)
        tags = row.get("tags") or []
        item["tags"] = ", ".join(map(str, tags)) if isinstance(tags, (tuple, list)) else str(tags)
        normalized.append(item)
    path = Path(destination).expanduser()
    suffix = path.suffix.casefold()
    if suffix == ".json":
        content = json.dumps({"version": 1, "count": len(normalized), "items": normalized}, ensure_ascii=False, indent=2)
        _atomic_write_text(path, content + "\n", encoding="utf-8")
    elif suffix == ".txt":
        content = "\n".join(item["target"] for item in normalized)
        _atomic_write_text(path, content + ("\n" if content else ""), encoding="utf-8")
    elif suffix == ".csv":
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=_FIELDS)
        writer.writeheader()
        writer.writerows({key: safe_csv_cell(item[key]) for key in _FIELDS} for item in normalized)
        _atomic_write_text(path, output.getvalue(), encoding="utf-8-sig")
    else:
        raise ValueError("候选导出仅支持 TXT、CSV、JSON")
    return len(normalized)


__all__ = ["import_candidate_list", "export_candidate_list"]

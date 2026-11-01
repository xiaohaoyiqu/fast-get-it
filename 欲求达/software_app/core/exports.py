from __future__ import annotations

import csv
import io
import json
import uuid
from pathlib import Path
from urllib.parse import urlparse


def safe_csv_cell(value: object) -> str:
    """Prevent spreadsheet programs from evaluating untrusted exported text."""
    text = str(value or "")
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _atomic_write_text(path: Path, text: str, *, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding=encoding)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def export_url_candidates(rows: list[dict], destination: Path | str) -> int:
    """Export unique HTTP(S) URL candidates as CSV, JSON, or plain text."""
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        url = str(row.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or url in seen:
            continue
        seen.add(url)
        normalized.append(
            {
                "title": str(row.get("title") or url),
                "site": str(row.get("site") or parsed.hostname or ""),
                "url": url,
                "source_image": str(row.get("source_image") or ""),
                "source_image_path": str(row.get("source_image_path") or ""),
                "availability": str(row.get("availability") or "unknown"),
                "status_code": str(row.get("status_code") or ""),
                "blocked": str(bool(row.get("blocked"))).lower(),
            }
        )

    path = Path(destination).expanduser()
    suffix = path.suffix.lower()
    if suffix == ".json":
        text = json.dumps({"version": 1, "count": len(normalized), "items": normalized}, ensure_ascii=False, indent=2)
        _atomic_write_text(path, text + "\n", encoding="utf-8")
    elif suffix == ".txt":
        text = "\n".join(row["url"] for row in normalized)
        _atomic_write_text(path, text + ("\n" if text else ""), encoding="utf-8")
    else:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            ["index", "title", "site", "url", "source_image", "source_image_path", "availability", "status_code", "blocked"]
        )
        for index, row in enumerate(normalized, 1):
            writer.writerow(
                [
                    index,
                    safe_csv_cell(row["title"]),
                    safe_csv_cell(row["site"]),
                    safe_csv_cell(row["url"]),
                    safe_csv_cell(row["source_image"]),
                    safe_csv_cell(row["source_image_path"]),
                    safe_csv_cell(row["availability"]),
                    safe_csv_cell(row["status_code"]),
                    safe_csv_cell(row["blocked"]),
                ]
            )
        _atomic_write_text(path, output.getvalue(), encoding="utf-8-sig")
    return len(normalized)


__all__ = ["export_url_candidates", "safe_csv_cell"]

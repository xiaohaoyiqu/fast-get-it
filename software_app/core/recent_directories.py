"""Bounded directory history shared by the workbench and settings."""

from __future__ import annotations

import os
from pathlib import Path


def recent_directories(values: object, limit: int = 10) -> list[str]:
    if not isinstance(values, list):
        return []
    result, seen = [], set()
    for value in values[:100]:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            continue
        try:
            value.encode("utf-16-le", errors="strict")
            path = str(Path(value).expanduser().resolve())
        except (OSError, RuntimeError, UnicodeError, ValueError):
            continue
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
        if len(result) >= max(1, limit):
            break
    return result

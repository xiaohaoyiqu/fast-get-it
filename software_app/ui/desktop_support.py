from __future__ import annotations

import ctypes
import os
import re
import unicodedata
from pathlib import Path

from software_app.core.models import IMAGE_EXTENSIONS


GOOGLE_BATCH_IMAGE_LIMIT = 20
LIBRARY_RENDER_LIMIT = 500


def select_treeview_row_at_event(tree, event) -> str:
    """Select only the Treeview row under a pointer event; return its item id."""
    item_id = str(tree.identify_row(event.y) or "")
    if not item_id or not tree.exists(item_id):
        return ""
    tree.selection_set(item_id)
    tree.focus(item_id)
    tree.see(item_id)
    return item_id


def normalized_search_text(*values: object) -> str:
    """Build a Unicode-insensitive search key while preserving original display text."""
    text = " ".join(str(value or "") for value in values)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


def library_query_tokens(query: object) -> tuple[str, ...]:
    return tuple(token for token in normalized_search_text(query).split(" ") if token)


def library_text_matches(search_text: str, tokens: tuple[str, ...]) -> bool:
    """AND-match normalized tokens; linear and predictable for an in-memory file index."""
    return all(token in search_text for token in tokens)


def matches_search(search_text: str, query: object) -> bool:
    return library_text_matches(search_text, library_query_tokens(query))


class _ShellFileOperation(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def send_path_to_recycle_bin(path: Path) -> None:
    """Move one exact Windows path to the Recycle Bin."""
    resolved = path.expanduser().resolve(strict=True)
    operation = _ShellFileOperation()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = str(resolved) + "\0\0"
    operation.fFlags = 0x40 | 0x10 | 0x400  # ALLOWUNDO | NOCONFIRMATION | NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(result, f"移动到回收站失败: {resolved}")


def collect_image_files(folder: Path, limit: int = GOOGLE_BATCH_IMAGE_LIMIT) -> tuple[list[Path], bool]:
    """Collect a stable bounded image batch without following directory links."""
    found: list[Path] = []
    for current, directories, filenames in os.walk(folder, followlinks=False):
        directories[:] = sorted(directories, key=str.casefold)
        for filename in sorted(filenames, key=str.casefold):
            path = Path(current) / filename
            if path.suffix.lower() not in IMAGE_EXTENSIONS or path.is_symlink() or not path.is_file():
                continue
            found.append(path)
            if len(found) > limit:
                return found[:limit], True
    return found, False


def bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def download_module_for_target(module_id: str, target: str) -> str:
    """Keep similarity hits as page tasks instead of invoking a site crawler."""
    del target
    return "website" if module_id == "google_image" else module_id


def download_types_for_discovered_target(
    source_module_id: str,
    target_module_id: str,
    selected_types: str,
) -> str:
    """Choose media types according to how a target was discovered."""
    del source_module_id, target_module_id
    return str(selected_types or "").strip()


def retry_route_for_task(module_id: str, target: str, options: dict) -> tuple[str, dict]:
    """Preserve discovery semantics when recreating old and current tasks."""
    del target
    updated = dict(options or {})
    is_discovered_page = updated.get("discovery_source") == "google_image"
    # Older candidate tasks had these page limits but no discovery marker and
    # were sometimes persisted under a platform downloader.
    is_legacy_discovered_page = "max_files" in updated and "page_read_timeout" in updated
    if is_discovered_page or is_legacy_discovered_page:
        updated["discovery_source"] = "google_image"
        updated["browser_render"] = True
        return "website", updated
    return module_id, updated

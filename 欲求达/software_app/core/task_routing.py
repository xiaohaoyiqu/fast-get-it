"""Compatibility routing shared by task retries and the desktop UI."""

from __future__ import annotations


def retry_route_for_task(module_id: str, target: str, options: dict) -> tuple[str, dict]:
    del target
    updated = dict(options or {})
    updated.pop("_batch_id", None)
    updated.pop("_batch_position", None)
    discovered = updated.get("discovery_source") == "google_image"
    legacy = not updated.get("discovery_source") and "max_files" in updated and "page_read_timeout" in updated
    if discovered or legacy:
        updated.update(discovery_source="google_image", browser_render=True)
        return "website", updated
    return module_id, updated

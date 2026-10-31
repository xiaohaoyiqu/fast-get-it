from __future__ import annotations

from typing import Any

import requests

from software_app.crawlers.common import make_session
from software_app.crawlers.webpage.client import normalize_page_url, _request_public_url
from software_app.core.adapter import TaskCancelled
from software_app.core.security import safe_error_message
from software_app.core.task_control import task_cancelled


AVAILABILITY_LABELS = {
    "available": "可访问",
    "missing": "已失效",
    "restricted": "需登录/受限",
    "temporary": "网站暂时异常",
    "unknown": "无法确认",
}


def check_page_availability(
    raw_url: str,
    *,
    session: requests.Session | None = None,
    proxy_url: str = "",
    timeout: tuple[int, int] = (8, 15),
    cancel_event=None,
) -> dict[str, Any]:
    """Classify definite HTTP absence without treating login/rate limits as deletion."""

    if cancel_event is not None and task_cancelled(cancel_event):
        raise TaskCancelled("候选链接检查已取消")
    url = normalize_page_url(raw_url)
    client = session if session is not None else make_session(proxy_url=proxy_url)
    response = None
    try:
        response = _request_public_url(client, url, timeout=timeout, cancel_event=cancel_event)
        status_code = int(response.status_code)
        final_url = str(response.url or url)
        if status_code in {404, 410}:
            state = "missing"
        elif status_code in {401, 403, 407, 429}:
            state = "restricted"
        elif 200 <= status_code < 400:
            state = "available"
        elif status_code in {408, 425} or status_code >= 500:
            state = "temporary"
        else:
            state = "unknown"
        return {
            "availability": state,
            "state": state,
            "label": AVAILABILITY_LABELS[state],
            "status_code": status_code,
            "url": url,
            "final_url": final_url,
            "error": "",
        }
    except (requests.RequestException, ValueError) as exc:
        return {
            "availability": "unknown",
            "state": "unknown",
            "label": AVAILABILITY_LABELS["unknown"],
            "status_code": None,
            "url": url,
            "final_url": "",
            "error": safe_error_message(exc),
        }
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            if session is None:
                client.close()

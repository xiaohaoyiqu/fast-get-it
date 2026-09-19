from __future__ import annotations

from typing import Any

import requests

from software_app.crawlers.common import make_session
from software_app.crawlers.webpage.client import normalize_page_url


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
) -> dict[str, Any]:
    """Classify definite HTTP absence without treating login/rate limits as deletion."""

    url = normalize_page_url(raw_url)
    client = session or make_session(proxy_url=proxy_url)
    response = None
    try:
        response = client.get(url, stream=True, allow_redirects=True, timeout=timeout)
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
    except requests.RequestException as exc:
        return {
            "availability": "unknown",
            "state": "unknown",
            "label": AVAILABILITY_LABELS["unknown"],
            "status_code": None,
            "url": url,
            "final_url": "",
            "error": str(exc),
        }
    finally:
        if response is not None:
            response.close()

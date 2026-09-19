"""Software-bundled crawler for media embedded in selected web pages."""

from .client import WebPageCrawler, extract_media_candidates, normalize_page_url
from .availability import AVAILABILITY_LABELS, check_page_availability

__all__ = [
    "AVAILABILITY_LABELS",
    "WebPageCrawler",
    "check_page_availability",
    "extract_media_candidates",
    "normalize_page_url",
]

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "ct0",
)


def redact_sensitive_data(value: Any, key: str = "") -> Any:
    """Return a display-safe copy of nested task options."""
    lowered_key = key.lower()
    if lowered_key and any(part in lowered_key for part in SENSITIVE_KEY_PARTS):
        return "[已隐藏]"
    if isinstance(value, dict):
        return {str(item_key): redact_sensitive_data(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)
    if isinstance(value, str) and "://" in value:
        try:
            parsed = urlparse(value)
        except ValueError:
            return value
        if parsed.username or parsed.password:
            return "[含凭据的 URL 已隐藏]"
    return value


__all__ = ["redact_sensitive_data"]

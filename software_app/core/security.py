from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


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
    "sig",
    "signature",
    "auth",
    "access_key",
    "secret_key",
    "session",
    "sessionid",
)

# Matches http/https URLs in arbitrary text for query-param redaction.
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)
_HEADER_RE = re.compile(
    r"(?<![\w-])(?P<quote>['\"]?)(?P<name>proxy-authorization|authorization|set-cookie|cookie|x-auth-token)"
    r"(?P=quote)[ \t]*[:=][ \t]*(?:\r?\n[ \t]*)?"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*)",
    re.IGNORECASE,
)


def redact_url_query(url: str) -> str:
    """Remove URL userinfo and redact sensitive query parameters."""
    # Remove userinfo before parsing, including on malformed URLs.
    url = re.sub(r"(?i)^(https?://)[^/?#]*@", r"\1", url)
    try:
        parsed = urlparse(url)
    except ValueError:
        return "[无效 URL 已隐藏]"
    if not parsed.query:
        return url
    cleaned = []
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        if any(part in name.lower() for part in SENSITIVE_KEY_PARTS):
            cleaned.append((name, "[REDACTED]"))
        else:
            cleaned.append((name, value))
    return urlunparse(parsed._replace(query=urlencode(cleaned)))


def redact_urls_in_text(text: str) -> str:
    """Find all http(s) URLs in text and redact sensitive query parameters.

    Used before storing exception messages or user-facing logs so that
    signed CDN links, OAuth callbacks, and token-bearing URLs do not end up
    in the SQLite database or UI detail panel.
    """
    if not isinstance(text, str) or "://" not in text:
        return text
    return _URL_RE.sub(lambda match: redact_url_query(match.group(0)), text)


def safe_error_message(error: BaseException | str) -> str:
    """Return a display- and storage-safe error string.

    Exception messages from requests/urllib often contain the full request URL,
    including query-string tokens.  This redacts them before the message is
    written to tasks.error or shown in the UI.
    """
    message = str(error) if not isinstance(error, str) else error
    message = redact_urls_in_text(message)
    # Header values can include schemes, spaces, multiple cookies and folded lines.
    # Quoted values (e.g. a headers dict repr) stop at their matching quote;
    # raw values are hidden through the end of the header line.
    message = _HEADER_RE.sub(lambda match: f"{match.group('name')}=[REDACTED]", message)
    return message


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
    if isinstance(value, str):
        if "://" in value:
            try:
                parsed = urlparse(value)
            except ValueError:
                return safe_error_message(value)
            if parsed.username or parsed.password:
                return "[含凭据的 URL 已隐藏]"
        return safe_error_message(value)
    return value


__all__ = ["redact_sensitive_data", "redact_urls_in_text", "safe_error_message", "redact_url_query"]

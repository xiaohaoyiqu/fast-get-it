"""Isolate credentials on manual redirects without mutating a shared Session."""

from __future__ import annotations

import copy
from http.cookiejar import CookieJar
from urllib.parse import urljoin, urlsplit

import requests
from requests.cookies import RequestsCookieJar

from .network_policy import HTTP_BACKOFF, retry_after_seconds, request_checkpoint
from .cookie_store import copied_cookie_policy


SENSITIVE_REDIRECT_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "host",
    "referer",
    "origin",
    "x-csrf-token",
    "x-csrftoken",
    "x-api-key",
    "x-auth-token",
    "x-access-token",
    "token",
    "tokenparam",
}


class _NoOriginAuth(requests.auth.AuthBase):
    def __call__(self, request):
        # A truthy auth object also prevents Requests from loading target netrc auth.
        return request


def url_origin(url):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("下载地址必须是没有内嵌账号的 HTTP/HTTPS 地址")
    return parts.scheme, parts.hostname.casefold(), parts.port or (443 if parts.scheme == "https" else 80)


def isolated_redirect_session(session):
    """Share transport pools, but never headers, auth or unscoped cookies.

    Do not close this shallow view: the caller owns its shared transport adapters.
    Proxy routing and TLS settings stay intact; proxy credentials authenticate to
    the configured proxy rather than becoming an origin request header.
    """
    if not isinstance(session, requests.Session):
        factory = getattr(type(session), "for_redirect", None)
        if factory is not None:
            return factory(session)
        raise ValueError("此传输会话未提供跨主机凭据隔离能力")
    view = copy.copy(session)
    view.headers = requests.structures.CaseInsensitiveDict(
        (name, value) for name, value in session.headers.items() if name.casefold() not in SENSITIVE_REDIRECT_HEADERS
    )
    view.auth = _NoOriginAuth()
    view.cert = None
    view.params = {}
    view.cookies = RequestsCookieJar()
    if isinstance(session.cookies, CookieJar):
        view.cookies.set_policy(copied_cookie_policy(session.cookies))
        for cookie in session.cookies:
            if cookie.domain:
                view.cookies.set_cookie(copy.copy(cookie))
    return view


def redirect_headers(headers):
    # None suppresses Session defaults, including those of thin request wrappers.
    result = {key: value for key, value in headers.items() if key.casefold() not in SENSITIVE_REDIRECT_HEADERS}
    result.update({name: None for name in SENSITIVE_REDIRECT_HEADERS})
    return result


def anonymous_session(session):
    """A view for explicitly public assets; no login cookies, even scoped ones."""
    if not isinstance(session, requests.Session):
        raise ValueError("公开资源请求需要支持凭据隔离的 Requests 会话")
    view = isolated_redirect_session(session)
    view.cookies.clear()
    return view


class _OwnedResponse:
    """A fresh transport view lives until its final streamed response closes."""

    def __init__(self, response, owner):
        self._response = response
        self._owner = owner
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._response, name)

    @property
    def encoding(self):
        return self._response.encoding

    @encoding.setter
    def encoding(self, value):
        self._response.encoding = value

    def close(self):
        if self._closed:
            return
        self._closed = True
        owner, self._owner = self._owner, None
        try:
            self._response.close()
        finally:
            if owner is not None:
                owner.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def get_with_redirects(
    session, url, *, validate_url, timeout, headers=None, params=None, cancel_event=None, backoff_fallback=1.0
):
    """A streamed GET; caller owns the final response, redirects are closed here.

    Request params apply only to the first URL. Cross-origin requests use an
    isolated credential view for the rest of the chain, including return hops.
    This does not follow or replay POST requests.
    """
    original = url_origin(url)
    current, active, isolated = url, session, False
    request_headers = dict(headers or {})
    owner = None
    try:
        for hop in range(6):
            request_checkpoint(cancel_event)
            validate_url(current)
            HTTP_BACKOFF.wait(current, cancel_event)
            destination = url_origin(current)
            if original[0] == "https" and destination[0] != "https":
                raise ValueError("不允许从 HTTPS 重定向到 HTTP")
            if destination != original and not isolated:
                active, isolated = isolated_redirect_session(session), True
                if getattr(active, "_close_after_redirect", False):
                    owner = active
            effective_headers = redirect_headers(request_headers) if isolated else request_headers
            arguments = {"headers": effective_headers, "timeout": timeout, "stream": True, "allow_redirects": False}
            if hop == 0 and params is not None:
                arguments["params"] = params
            response = active.get(current, **arguments)
            try:
                if response.status_code in {429, 503}:
                    delay = retry_after_seconds(response.headers.get("Retry-After"), backoff_fallback)
                    HTTP_BACKOFF.defer(url, delay)
                    HTTP_BACKOFF.defer(current, delay)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    if owner is not None:
                        response, owner = _OwnedResponse(response, owner), None
                    return response
                location = str(response.headers.get("Location") or "").strip()
                if not location:
                    raise requests.HTTPError("重定向缺少目标地址", response=response)
                if hop == 5:
                    raise requests.TooManyRedirects("重定向超过 5 次", response=response)
                current = urljoin(current, location)
            except BaseException:
                response.close()
                raise
            response.close()
        raise requests.TooManyRedirects("重定向过多")
    finally:
        if owner is not None:
            owner.close()

"""Cookie records retain scope; old name/value files remain readable."""

from __future__ import annotations

import copy
import json
import math
import re
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from pathlib import Path

import requests
from requests.cookies import RequestsCookieJar, cookiejar_from_dict, merge_cookies

MAX_COOKIE_FILE_BYTES = 10 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9_!#$%&'*+.^`|~-]+")


def normalize_record(item):
    if not isinstance(item, dict):
        raise ValueError("Cookie 记录必须是对象")
    name, value = str(item.get("name") or ""), item.get("value")
    if not _NAME.fullmatch(name) or value is None or isinstance(value, (dict, list)):
        raise ValueError("Cookie 名称或值无效")
    value = str(value)
    if any(char in value for char in "\r\n\x00") or len(value) > 65536:
        raise ValueError("Cookie 值包含非法字符或过长")
    domain = str(item.get("domain") or "").lower()
    if domain and (any(char in domain for char in "/\\:@ \t\r\n\x00") or domain in {".", ".."}):
        raise ValueError("Cookie 域无效")
    path = str(item.get("path") or "/")
    if not path.startswith("/") or any(char in path for char in "\r\n\x00"):
        raise ValueError("Cookie 路径无效")
    record = {"name": name, "value": value, "domain": domain, "path": path}
    for field in ("secure", "httpOnly", "hostOnly"):
        if field in item and type(item[field]) is not bool:
            raise ValueError("Cookie 属性必须是布尔值")
    record["secure"] = item.get("secure", False)
    record["httpOnly"] = item.get("httpOnly", False)
    record["hostOnly"] = item.get("hostOnly", bool(domain and not domain.startswith(".")))
    expires = item.get("expiry", item.get("expires", item.get("expirationDate")))
    if expires is not None:
        if isinstance(expires, bool):
            raise ValueError("Cookie 到期时间无效")
        try:
            expires = float(expires)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Cookie 到期时间无效") from None
        if not math.isfinite(expires):
            raise ValueError("Cookie 到期时间无效")
        if expires >= 0:
            record["expiry"] = int(expires)
    if item.get("sameSite") is not None:
        record["sameSite"] = str(item["sameSite"])
    return record


def parse_cookie_records(raw):
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
        rows = []
        for line in raw.splitlines():
            line = line.rstrip("\r\n")
            if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            columns = line.split("\t", 6)
            if len(columns) == 7:
                domain, subdomains, path, secure, expiry, name, value = columns
                if subdomains.upper() not in {"TRUE", "FALSE"} or secure.upper() not in {"TRUE", "FALSE"}:
                    raise ValueError("Netscape Cookie 标志无效") from None
                rows.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": domain.removeprefix("#HttpOnly_"),
                        "path": path,
                        "secure": secure.upper() == "TRUE",
                        "expiry": expiry,
                        "hostOnly": subdomains.upper() != "TRUE",
                        "httpOnly": domain.startswith("#HttpOnly_"),
                    }
                )
                if expiry == "0":
                    rows[-1].pop("expiry")  # Netscape uses zero for a session cookie.
        if rows:
            payload = rows
        else:
            lines = raw.splitlines()
            text = raw.strip()
            for index, line in enumerate(lines):
                if line.strip().lower().startswith("cookie:"):
                    text = line.strip().partition(":")[2].strip()
                    if not text and index + 1 < len(lines):
                        text = lines[index + 1].strip()
                    break
            if "\n" in text or "=" not in text:
                raise ValueError("不是可识别的 Cookie 文件") from None
            payload = []
            for part in text.split(";"):
                name, separator, value = part.strip().partition("=")
                if separator:
                    payload.append({"name": name, "value": value})
    if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
        payload = payload["cookies"]
    if isinstance(payload, dict):
        payload = [{"name": name, "value": value} for name, value in payload.items() if value is not None]
    if not isinstance(payload, list):
        raise ValueError("Cookie 文件必须包含记录列表或名称字典")
    if len(payload) > 20000:
        raise ValueError("Cookie 文件超过 20000 条记录")
    return [normalize_record(item) for item in payload]


def load_cookie_records(path, *, strict=False):
    if not path:
        return []
    try:
        with Path(path).expanduser().open("rb") as source:
            raw = source.read(MAX_COOKIE_FILE_BYTES + 1)
        if len(raw) > MAX_COOKIE_FILE_BYTES:
            raise ValueError("Cookie 文件超过 10 MiB")
        return parse_cookie_records(raw.decode("utf-8-sig"))
    except (OSError, ValueError, TypeError):
        if strict:
            raise
        return []


def merge_cookie_records(existing, incoming):
    incoming = [normalize_record(row) for row in incoming]
    # A newly scoped browser record supersedes legacy unscoped values of its name.
    names = {row["name"] for row in incoming}
    result = {
        (row["name"], row["domain"], row["path"]): row for row in existing if row["domain"] or row["name"] not in names
    }
    result.update({(row["name"], row["domain"], row["path"]): row for row in incoming})
    return list(result.values())


def cookie_jar(records, *, default_domain="", cookie_domains=None, default_secure=True):
    jar = RequestsCookieJar(policy=DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
    for item in records:
        record = normalize_record(item)
        domain = record["domain"] or (cookie_domains or {}).get(record["name"], default_domain)
        scoped_default = not record["domain"] and bool(domain)
        host_only = record["hostOnly"] if not scoped_default else not domain.startswith(".")
        rest = {"HttpOnly": None} if record["httpOnly"] else {}
        if "sameSite" in record:
            rest["SameSite"] = record["sameSite"]
        jar.set_cookie(
            Cookie(
                0,
                record["name"],
                record["value"],
                None,
                False,
                domain,
                bool(domain and not host_only),
                domain.startswith("."),
                record["path"],
                True,
                record["secure"] or (scoped_default and default_secure),
                record.get("expiry"),
                "expiry" not in record,
                None,
                None,
                rest,
            )
        )
    return jar


def copied_cookie_policy(jar):
    # CookieJar has a public setter but no public getter. curl_cffi exposes the
    # standard jar, while RequestsCookieJar adds get_policy(); both own _policy.
    return copy.copy(jar._policy)


class CookieSession(requests.Session):
    """Requests merges jars into a new default jar; restore the scope policy."""

    def prepare_request(self, request):
        prepared = super().prepare_request(request)
        explicit = requests.structures.CaseInsensitiveDict(self.headers)
        explicit.update(request.headers or {})
        if explicit.get("Cookie") is not None:
            return prepared
        merged = merge_cookies(RequestsCookieJar(), self.cookies)
        if request.cookies:
            cookies = (
                request.cookies if hasattr(request.cookies, "set_cookie") else cookiejar_from_dict(request.cookies)
            )
            merge_cookies(merged, cookies)
        if isinstance(self.cookies, CookieJar):
            merged.set_policy(copied_cookie_policy(self.cookies))
        prepared.headers.pop("Cookie", None)
        prepared.prepare_cookies(merged)
        return prepared

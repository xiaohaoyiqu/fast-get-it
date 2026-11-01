"""Opt-in EH login check. Reports contain no cookie values or account IDs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path("dist"))
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument(
        "--system-proxy", action="store_true", help="Test with the system HTTPS proxy without saving it"
    )
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    home = args.home.resolve()
    os.environ["YUQIUDA_HOME"] = str(home)
    from software_app.core.cookie_store import load_cookie_records
    from software_app.core.ehentai_cookie import validate_saved_ehentai_cookies
    from software_app.core.error_policy import classify_error

    data = home / "data/software_app"
    with sqlite3.connect((data / "content_downloader.db").as_uri() + "?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT value_json FROM settings WHERE key=?", ("proxy_url",)).fetchone()
    saved_proxy = str(json.loads(row[0]) or "") if row else ""
    proxy_reader = getattr(urllib.request, "getproxies_registry", urllib.request.getproxies)
    system_proxy = str(proxy_reader().get("https") or "")

    def endpoint(value):
        if not value:
            return None
        parsed = urlsplit(value if "://" in value else "http://" + value)
        return {"scheme": parsed.scheme, "host": parsed.hostname, "port": parsed.port}

    result = {"saved_proxy": endpoint(saved_proxy), "system_https_proxy": endpoint(system_proxy), "sites": []}
    for filename, label in (("e-hentai-cookies.json", "E-Hentai 表站"), ("exhentai-cookies.json", "ExHentai 里站")):
        cookie = data / "ehentai" / filename
        item = {"site": filename.removesuffix("-cookies.json"), "cookie_file_exists": cookie.is_file()}
        result["sites"].append(item)
        if not cookie.is_file():
            continue
        before = hashlib.sha256(cookie.read_bytes()).hexdigest()
        records = load_cookie_records(cookie, strict=True)
        item["cookie_names"] = sorted({r["name"] for r in records})
        item["expired_records"] = sum(r.get("expiry", time.time() + 1) <= time.time() for r in records)
        if args.inspect_only:
            continue
        started = time.monotonic()
        try:
            validate_saved_ehentai_cookies(label, proxy_url=system_proxy if args.system_proxy else saved_proxy)
            item["validation_passed"] = True
        except Exception as exc:
            item.update(
                validation_passed=False,
                error_type=type(exc).__name__,
                error_code=classify_error(exc).code,
                http_status=getattr(getattr(exc, "response", None), "status_code", None),
            )
        item["elapsed_seconds"] = round(time.monotonic() - started, 3)
        item["cookie_unchanged"] = before == hashlib.sha256(cookie.read_bytes()).hexdigest()
    result["proxy_source"] = "system" if args.system_proxy else "saved"
    root = repo / "data/software_app/verification" / f"eh-live-{time.time_ns()}"
    root.mkdir(parents=True)
    (root / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "report.json"), **result}, ensure_ascii=True))


if __name__ == "__main__":
    main()

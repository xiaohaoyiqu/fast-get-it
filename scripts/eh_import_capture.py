"""Validate and import an EH browser export without echoing secrets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from software_app.core.ehentai_plain_browser import import_ehentai_cookie_file  # noqa: E402
from software_app.core.storage import AppStorage  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="在线验证后导入 EH Cookie，不输出 Cookie 或代理")
    parser.add_argument("--site", choices=("e-hentai", "exhentai"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    label = "ExHentai 里站" if args.site == "exhentai" else "E-Hentai 表站"
    proxy_url = str(AppStorage().get_setting("proxy_url", "") or "")
    try:
        result = import_ehentai_cookie_file(label, args.source, proxy_url=proxy_url)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "site": args.site,
            "validated": False,
            "saved": False,
            "error_type": type(exc).__name__,
        }, ensure_ascii=False))
        return 1
    print(json.dumps({
        "site": args.site,
        "validated": True,
        "saved": True,
        "cookie_count": result.cookie_count,
        "destination_name": result.destination.name,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from software_app.core.browser_driver import (  # noqa: F401
    BrowserDriverError,
    BrowserDriverStatus,
    chrome_version,
    chromedriver_status,
    driver_name,
    driver_platform_name,
    ensure_chromedriver,
    find_existing_driver,
    global_driver_path,
)


if __name__ == "__main__":
    status = chromedriver_status(auto_download=True)
    print(status.driver_path or "")

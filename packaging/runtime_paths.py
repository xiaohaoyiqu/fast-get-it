"""Prepare bundled native tools before application imports."""

from __future__ import annotations

import os
import sys
from pathlib import Path


bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
tools_dir = bundle_root / "tools"
if tools_dir.is_dir():
    current = str(os.environ.get("PATH") or "")
    os.environ["PATH"] = str(tools_dir) + (os.pathsep + current if current else "")

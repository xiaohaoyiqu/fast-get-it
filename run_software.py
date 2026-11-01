"""Compatibility entry point for the application now stored in 欲求达/."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


if __name__ == "__main__":
    app_root = Path(__file__).resolve().parent / "欲求达"
    sys.path.insert(0, str(app_root))
    runpy.run_path(str(app_root / "run_software.py"), run_name="__main__")

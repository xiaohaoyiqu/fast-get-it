"""Stage tool payloads beside the destination without touching an existing tool."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def staged_tool_payload(target: Path, payload: bytes, *, executable=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    # Keep .exe for Windows execution, and use the same filesystem for replace.
    handle, name = tempfile.mkstemp(prefix=f".{target.stem}.install-", suffix=target.suffix, dir=target.parent)
    candidate = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        if executable and os.name != "nt":
            candidate.chmod(candidate.stat().st_mode | 0o755)
        yield candidate
    finally:
        candidate.unlink(missing_ok=True)

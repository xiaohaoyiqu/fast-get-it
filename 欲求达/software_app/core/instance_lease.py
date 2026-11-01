"""A process-held lease identifies live task owners without PID reuse or timeouts."""
from __future__ import annotations

import errno
import hashlib
import os
import re
import uuid
import weakref
from pathlib import Path

from .output_lock import _try_kernel_lock, _unlock_kernel_lock


def _lease_path(db_path: Path, owner_id: str) -> Path:
    key = os.path.normcase(str(Path(db_path).resolve())) + ":" + owner_id
    # All instances using this database must share the same lease namespace,
    # even when launched with different TEMP settings or from a frozen bundle.
    root = Path(db_path).resolve().parent / ".instance-leases"
    root.mkdir(parents=True, exist_ok=True)
    return root / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock")


def _open_lease(db_path, owner_id):
    handle = _lease_path(db_path, owner_id).open("a+b")
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b"\0")
        handle.flush()
    return handle


class InstanceLease:
    def __init__(self, db_path: Path, *, acquire: bool = True):
        self.owner_id = uuid.uuid4().hex
        self.db_path = db_path
        self._finalizer = None
        if acquire:
            self.acquire()

    def acquire(self):
        if self._finalizer is not None and self._finalizer.alive:
            return
        handle = _open_lease(self.db_path, self.owner_id)
        try:
            _try_kernel_lock(handle)
        except BaseException:
            handle.close()
            raise
        # Closing the handle releases the OS lock, including after process death.
        # Unlike output_path_lock this is not reentrant or thread-owned, so a
        # second manager on the same thread still sees the first manager as live.
        self._finalizer = weakref.finalize(self, handle.close)

    def close(self):
        if self._finalizer is not None:
            self._finalizer()


def owner_is_alive(db_path: Path, owner_id: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{32}", owner_id):
        # Unknown owner formats cannot safely be declared dead.
        return True
    with _open_lease(db_path, owner_id) as handle:
        try:
            _try_kernel_lock(handle)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return True
            raise
        _unlock_kernel_lock(handle)
        return False

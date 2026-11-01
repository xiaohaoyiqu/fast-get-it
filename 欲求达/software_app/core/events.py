from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .models import FileRecord, ProgressEvent


def _noop_progress(_event: ProgressEvent) -> None:
    return None


def _noop_file(_record: FileRecord) -> None:
    return None


def _noop_done(_task_id: str, _status: str) -> None:
    return None


@dataclass
class CallbackSet:
    on_progress: Callable[[ProgressEvent], None] = _noop_progress
    on_file: Callable[[FileRecord], None] = _noop_file
    on_done: Callable[[str, str], None] = _noop_done


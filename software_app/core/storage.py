from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import (
    FINAL_TASK_STATUSES,
    MEDIA_EXTENSIONS,
    FileRecord,
    ModuleInfo,
    classify_file,
)
from .settings import DB_PATH, ensure_data_dirs


SCHEMA = """
CREATE TABLE IF NOT EXISTS modules (
    module_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'ready',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    script_root TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    module_id TEXT NOT NULL,
    target TEXT NOT NULL,
    normalized_target TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    module_id TEXT NOT NULL,
    task_id TEXT,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    author_id TEXT NOT NULL DEFAULT '',
    author_name TEXT NOT NULL DEFAULT '',
    chapter TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_module_status ON tasks(module_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_files_module_type ON files(module_id, media_type);
CREATE INDEX IF NOT EXISTS idx_files_updated ON files(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id, id DESC);
"""

UPSERT_FILE_SQL = """
INSERT INTO files(
    path, module_id, task_id, media_type, size, title, source_url,
    source_id, author_id, author_name, chapter, published_at,
    tags_json, metadata_json
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(path) DO UPDATE SET
    module_id=CASE WHEN excluded.task_id IS NOT NULL THEN excluded.module_id ELSE files.module_id END,
    task_id=COALESCE(excluded.task_id, files.task_id),
    media_type=excluded.media_type,
    size=excluded.size,
    title=CASE WHEN excluded.title != '' THEN excluded.title ELSE files.title END,
    source_url=CASE WHEN excluded.source_url != '' THEN excluded.source_url ELSE files.source_url END,
    source_id=CASE WHEN excluded.source_id != '' THEN excluded.source_id ELSE files.source_id END,
    author_id=CASE WHEN excluded.author_id != '' THEN excluded.author_id ELSE files.author_id END,
    author_name=CASE WHEN excluded.author_name != '' THEN excluded.author_name ELSE files.author_name END,
    chapter=CASE WHEN excluded.chapter != '' THEN excluded.chapter ELSE files.chapter END,
    published_at=CASE WHEN excluded.published_at != '' THEN excluded.published_at ELSE files.published_at END,
    tags_json=CASE WHEN excluded.tags_json != '[]' THEN excluded.tags_json ELSE files.tags_json END,
    metadata_json=CASE WHEN excluded.metadata_json != '{}' THEN excluded.metadata_json ELSE files.metadata_json END,
    updated_at=CURRENT_TIMESTAMP
"""


def _file_record_values(record: FileRecord) -> tuple:
    return (
        str(record.path),
        record.module_id,
        record.task_id,
        record.media_type,
        record.size,
        record.title,
        record.source_url,
        record.source_id,
        record.author_id,
        record.author_name,
        record.chapter,
        record.published_at,
        json.dumps(list(record.tags), ensure_ascii=False),
        json.dumps(record.metadata or {}, ensure_ascii=False),
    )


class AppStorage:
    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        ensure_data_dirs()
        self.db_path = Path(db_path)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_columns(
                conn,
                "files",
                {
                    "source_id": "TEXT NOT NULL DEFAULT ''",
                    "author_id": "TEXT NOT NULL DEFAULT ''",
                    "author_name": "TEXT NOT NULL DEFAULT ''",
                    "chapter": "TEXT NOT NULL DEFAULT ''",
                    "published_at": "TEXT NOT NULL DEFAULT ''",
                    "tags_json": "TEXT NOT NULL DEFAULT '[]'",
                },
            )

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def register_module(self, info: ModuleInfo, config: dict | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO modules(
                    module_id, display_name, description, stage,
                    capabilities_json, script_root, config_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(module_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    description=excluded.description,
                    stage=excluded.stage,
                    capabilities_json=excluded.capabilities_json,
                    script_root=excluded.script_root,
                    config_json=excluded.config_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    info.module_id,
                    info.display_name,
                    info.description,
                    info.stage,
                    json.dumps(list(info.capabilities), ensure_ascii=False),
                    str(info.script_root or ""),
                    json.dumps(config or {}, ensure_ascii=False),
                ),
            )

    def list_modules(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM modules ORDER BY module_id"))

    def create_task(self, task_id: str, module_id: str, target: str, output_dir: str, options: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, module_id, target, status, output_dir, options_json)
                VALUES (?, ?, ?, 'queued', ?, ?)
                """,
                (task_id, module_id, target, output_dir, json.dumps(options, ensure_ascii=False)),
            )

    def update_task_status(
        self,
        task_id: str,
        status: str,
        error: str = "",
        normalized_target: str = "",
    ) -> None:
        finished = status in FINAL_TASK_STATUSES
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status=?,
                    error=?,
                    normalized_target=CASE WHEN ? != '' THEN ? ELSE normalized_target END,
                    started_at=CASE
                        WHEN ? = 'running' AND started_at IS NULL THEN CURRENT_TIMESTAMP
                        ELSE started_at
                    END,
                    finished_at=CASE
                        WHEN ? THEN CURRENT_TIMESTAMP
                        ELSE finished_at
                    END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE task_id=?
                """,
                (
                    status,
                    error,
                    normalized_target,
                    normalized_target,
                    status,
                    1 if finished else 0,
                    task_id,
                ),
            )

    def add_log(
        self,
        task_id: str,
        module_id: str,
        level: str,
        message: str,
        metadata: dict | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO logs(task_id, module_id, level, message, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, module_id, level, message, json.dumps(metadata or {}, ensure_ascii=False)),
            )

    def upsert_file(self, record: FileRecord) -> None:
        with self.connect() as conn:
            conn.execute(UPSERT_FILE_SQL, _file_record_values(record))

    def list_tasks(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)))

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()

    def list_files(
        self,
        module_id: str | None = None,
        limit: int = 500,
        task_id: str | None = None,
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if task_id:
                if module_id:
                    return list(
                        conn.execute(
                            "SELECT * FROM files WHERE task_id=? AND module_id=? ORDER BY updated_at DESC LIMIT ?",
                            (task_id, module_id, limit),
                        )
                    )
                return list(
                    conn.execute(
                        "SELECT * FROM files WHERE task_id=? ORDER BY updated_at DESC LIMIT ?",
                        (task_id, limit),
                    )
                )
            if module_id:
                return list(
                    conn.execute(
                        "SELECT * FROM files WHERE module_id=? ORDER BY updated_at DESC LIMIT ?",
                        (module_id, limit),
                    )
                )
            return list(conn.execute("SELECT * FROM files ORDER BY updated_at DESC LIMIT ?", (limit,)))

    def list_library_files(self, limit: int = 100000) -> list[sqlite3.Row]:
        """Load only fields needed by the local library index, excluding large metadata blobs."""
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT path, module_id, media_type, title, author_name, author_id,
                           source_id, chapter, tags_json, updated_at
                    FROM files
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def rename_file_record(self, old_path: Path | str, new_path: Path | str) -> int:
        """Move one indexed path after the caller has renamed the local file."""
        old_value = str(Path(old_path).expanduser().resolve())
        new_value = str(Path(new_path).expanduser().resolve())
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE files
                SET path=?, title=?, updated_at=CURRENT_TIMESTAMP
                WHERE path=?
                """,
                (new_value, Path(new_value).name, old_value),
            )
            return cursor.rowcount if cursor.rowcount is not None else 0

    def delete_file_record(self, path: Path | str) -> int:
        """Delete only a SQLite file index row; never touches local media."""
        value = str(Path(path).expanduser().resolve())
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM files WHERE path=?", (value,))
            return cursor.rowcount if cursor.rowcount is not None else 0

    def list_logs(self, task_id: str, limit: int = 500) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM logs WHERE task_id=? ORDER BY id DESC LIMIT ?",
                    (task_id, limit),
                )
            )

    def recent_logs(self, limit: int = 500) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)))

    def delete_task_logs(self, task_ids: list[str] | tuple[str, ...] | set[str]) -> int:
        """Delete logs for selected tasks without changing tasks or file indexes."""
        identifiers = sorted({str(task_id).strip() for task_id in task_ids if str(task_id).strip()})
        if not identifiers:
            return 0
        placeholders = ",".join("?" for _ in identifiers)
        with self.connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM logs WHERE task_id IN ({placeholders})",
                identifiers,
            )
            return cursor.rowcount if cursor.rowcount is not None else 0

    def set_setting(self, key: str, value: object) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value_json)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def get_setting(self, key: str, default: object | None = None) -> object | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return default


    def clear_history(self, module_id: str | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.connect() as conn:
            if module_id:
                for table in ("logs", "files", "tasks"):
                    cursor = conn.execute(f"DELETE FROM {table} WHERE module_id=?", (module_id,))
                    counts[table] = cursor.rowcount if cursor.rowcount is not None else 0
            else:
                for table in ("logs", "files", "tasks"):
                    cursor = conn.execute(f"DELETE FROM {table}")
                    counts[table] = cursor.rowcount if cursor.rowcount is not None else 0
        return counts

    def delete_task_records(self, task_ids: list[str] | tuple[str, ...] | set[str]) -> dict[str, int]:
        """Delete exact task/log/file index rows without touching local media files."""
        identifiers = sorted({str(task_id).strip() for task_id in task_ids if str(task_id).strip()})
        if not identifiers:
            return {"logs": 0, "files": 0, "tasks": 0}
        placeholders = ",".join("?" for _ in identifiers)
        counts: dict[str, int] = {}
        with self.connect() as conn:
            for table in ("logs", "files", "tasks"):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE task_id IN ({placeholders})",
                    identifiers,
                )
                counts[table] = cursor.rowcount if cursor.rowcount is not None else 0
        return counts

    def recover_interrupted_tasks(self) -> int:
        """Mark tasks left active by a previous abnormal exit as cancelled."""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status='cancelled',
                    error='上次软件退出时任务未能继续，已恢复为取消状态',
                    finished_at=COALESCE(finished_at, CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE status IN ('queued', 'running', 'cancelling')
                """
            )
            return cursor.rowcount if cursor.rowcount is not None else 0

    def scan_media_files(
        self,
        root: Path | str,
        module_id: str,
        task_id: str | None = None,
        modified_after: float | None = None,
        on_progress=None,
        count_only: bool = False,
    ) -> list[FileRecord] | int:
        root_path = Path(root).expanduser()
        if not root_path.exists():
            return 0 if count_only else []

        records: list[FileRecord] = []
        found_count = 0
        skip_dirs = {".git", "__pycache__", ".venv", "venv", "build", "dist", ".idea"}
        pending_values: list[tuple] = []
        with self.connect() as conn:
            for current, directories, filenames in os.walk(root_path, followlinks=False):
                directories[:] = [
                    name
                    for name in directories
                    if name not in skip_dirs and not (Path(current) / name).is_symlink()
                ]
                for filename in filenames:
                    path = Path(current) / filename
                    if path.suffix.lower() not in MEDIA_EXTENSIONS:
                        continue
                    try:
                        if path.is_symlink() or not path.is_file():
                            continue
                        stat = path.stat()
                    except OSError:
                        continue
                    if modified_after is not None and stat.st_mtime < modified_after:
                        continue
                    record = FileRecord(
                        path=path.resolve(),
                        module_id=module_id,
                        task_id=task_id,
                        media_type=classify_file(path),
                        size=stat.st_size,
                        title=path.name,
                    )
                    found_count += 1
                    if not count_only:
                        records.append(record)
                    pending_values.append(_file_record_values(record))
                    if len(pending_values) >= 500:
                        conn.executemany(UPSERT_FILE_SQL, pending_values)
                        pending_values.clear()
                        if on_progress:
                            on_progress(found_count)
            if pending_values:
                conn.executemany(UPSERT_FILE_SQL, pending_values)
                if on_progress:
                    on_progress(found_count)
        return found_count if count_only else records




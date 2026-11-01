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
from .instance_lease import owner_is_alive
from .security import redact_sensitive_data, safe_error_message
from .collection_plan import validated_collection_plan
from .transfer_workspace import is_transfer_workspace_name


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
    owner_id TEXT NOT NULL DEFAULT '',
    recovery_status TEXT NOT NULL DEFAULT '',
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
    revision TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_media (
    task_id TEXT NOT NULL,
    media_key TEXT NOT NULL,
    descriptor_json TEXT NOT NULL,
    state TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(task_id, media_key)
);

CREATE TABLE IF NOT EXISTS task_collections (
    task_id TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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

CREATE TABLE IF NOT EXISTS person_configs (
    module_id TEXT NOT NULL,
    account_key TEXT NOT NULL,
    label TEXT NOT NULL,
    options_json TEXT NOT NULL,
    output_dir TEXT NOT NULL DEFAULT '',
    revision TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(module_id, account_key)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    output_dir TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    module_id TEXT NOT NULL,
    target TEXT NOT NULL,
    task_id TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(batch_id, position)
);
CREATE INDEX IF NOT EXISTS idx_batch_items_task ON batch_items(task_id);

CREATE INDEX IF NOT EXISTS idx_tasks_module_status ON tasks(module_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_files_module_type ON files(module_id, media_type);
CREATE INDEX IF NOT EXISTS idx_files_updated ON files(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_files_task_updated ON files(task_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id, id DESC);
"""

UPSERT_FILE_SQL = """
INSERT INTO files(
    path, module_id, task_id, media_type, size, title, source_url,
    source_id, author_id, author_name, chapter, published_at,
    tags_json, metadata_json, revision
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, lower(hex(randomblob(16))))
ON CONFLICT(path) DO UPDATE SET
    revision=excluded.revision,
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
    # Soft cap on total log rows.  Oldest rows beyond this are pruned
    # periodically so the SQLite file does not grow without bound.
    MAX_LOG_ROWS = 20000

    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        ensure_data_dirs()
        self.db_path = Path(db_path)
        self._log_write_counter = 0
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            # WAL 大幅提升高并发写入吞吐：写不再阻塞读，且 synchronous=NORMAL 时
            # 每次提交不必 fsync。WAL 是数据库级持久设置，重复执行是幂等的。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            pass  # 只读介质或网络盘可能不支持 WAL，回退默认模式不影响功能
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_columns(conn, "person_configs", {"updated_at": "TEXT NOT NULL DEFAULT ''"})
            self._ensure_columns(
                conn, "tasks", {"owner_id": "TEXT NOT NULL DEFAULT ''",
                                "recovery_status": "TEXT NOT NULL DEFAULT ''"},
            )
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
                    "revision": "TEXT NOT NULL DEFAULT ''",
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

    def create_task(self, task_id: str, module_id: str, target: str, output_dir: str, options: dict, *, owner_id: str = "", retry_of: str = "", collection_plan: dict | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, module_id, target, status, output_dir, options_json, owner_id)
                VALUES (?, ?, ?, 'queued', ?, ?, ?)
                """,
                (task_id, module_id, target, output_dir, json.dumps(options, ensure_ascii=False), owner_id),
            )
            if options.get("_batch_id"):
                changed = conn.execute("""UPDATE batch_items SET task_id=?, state='queued'
                    WHERE batch_id=? AND position=? AND module_id=? AND target=? AND task_id IS NULL AND state='pending'""",
                    (task_id, options["_batch_id"], options.get("_batch_position"), module_id, target))
                if changed.rowcount != 1:
                    raise ValueError("批次条目已变化或已经入队")
            if retry_of:
                conn.execute("UPDATE tasks SET recovery_status='' WHERE task_id=? AND status='cancelled'",
                             (retry_of,))
            if collection_plan is not None:
                self._write_collection_plan(conn, task_id, collection_plan)

    @staticmethod
    def _write_collection_plan(conn, task_id, plan):
        task = conn.execute("SELECT module_id,target,options_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("集合任务不存在，未保存发现计划")
        plan = validated_collection_plan(plan, task["module_id"], task["target"], json.loads(task["options_json"]))
        encoded = json.dumps(plan, sort_keys=True, ensure_ascii=False)
        existing = conn.execute("SELECT plan_json FROM task_collections WHERE task_id=?", (task_id,)).fetchone()
        if existing:
            if json.loads(existing["plan_json"]) != plan:
                raise ValueError("任务已有不同的集合计划，未替换原作品范围")
            return
        conn.execute("INSERT INTO task_collections(task_id,plan_json) VALUES (?,?)", (task_id, encoded))

    def record_collection_plan(self, task_id: str, plan: dict) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._write_collection_plan(conn, task_id, plan)

    def collection_plan(self, task_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("""SELECT p.plan_json,t.module_id,t.target,t.options_json
                FROM task_collections p JOIN tasks t ON t.task_id=p.task_id WHERE p.task_id=?""", (task_id,)).fetchone()
        if row is None:
            return None
        return validated_collection_plan(json.loads(row["plan_json"]), row["module_id"], row["target"], json.loads(row["options_json"]))

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
            conn.execute("UPDATE batch_items SET state=?, error=? WHERE task_id=?", (status, error, task_id))

    def record_media_result(self, task_id: str, item: dict) -> None:
        self.record_media_results(task_id, [item])

    def record_media_results(self, task_id: str, items: list[dict]) -> None:
        values = []
        for item in items:
            if item.get("state") not in {"pending", "saved", "reused", "failed"}:
                raise ValueError("无效的媒体结果")
            descriptor = {name: item[name] for name in ("key", "target", "index", "kind", "identity", "folder")}
            if item.get("path"):
                descriptor["path"] = str(item["path"])
            values.append((task_id, item["key"], json.dumps(descriptor, ensure_ascii=False), item["state"], item.get("error", ""), task_id))
        with self.connect() as conn:
            conn.executemany("""INSERT INTO task_media(task_id,media_key,descriptor_json,state,error)
                SELECT ?,?,?,?,? WHERE EXISTS (SELECT 1 FROM tasks WHERE task_id=?)
                ON CONFLICT(task_id,media_key) DO UPDATE SET
                descriptor_json=excluded.descriptor_json,state=excluded.state,error=excluded.error,
                updated_at=CURRENT_TIMESTAMP""",
                values)

    def media_results(self, task_id: str, *, unfinished: bool = False) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT descriptor_json,state,error FROM task_media WHERE task_id=?"
                                + (" AND state IN ('failed','pending')" if unfinished else "")
                                + " ORDER BY media_key", (task_id,))
            return [{**json.loads(row["descriptor_json"]), "state": row["state"], "error": row["error"]} for row in rows]

    def media_page(self, task_id: str, *, state: str = "", offset: int = 0, limit: int = 100):
        if state not in {"", "failed", "pending", "saved", "reused", "repaired"}:
            raise ValueError("无效的媒体筛选状态")
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 200))
        predicate = "task_id=?" + (" AND state=?" if state else "")
        values = (task_id, state) if state else (task_id,)
        with self.connect() as conn:
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM task_media WHERE " + predicate, values).fetchone()[0]
            rows = conn.execute("SELECT descriptor_json,state,error FROM task_media WHERE " + predicate
                                + " ORDER BY CASE state WHEN 'failed' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,media_key LIMIT ? OFFSET ?",
                                (*values, limit, offset))
            items = [{**json.loads(row["descriptor_json"]), "state": row["state"], "error": row["error"]} for row in rows]
        return total, items

    def media_summary(self, task_id: str) -> dict[str, int]:
        with self.connect() as conn:
            return {row["state"]: row["count"] for row in conn.execute(
                "SELECT state,COUNT(*) AS count FROM task_media WHERE task_id=? GROUP BY state", (task_id,))}

    def task_file_records(self, task_id: str) -> list[FileRecord]:
        with self.connect() as conn:
            rows = list(conn.execute("SELECT * FROM files WHERE task_id=? ORDER BY path LIMIT 100001", (task_id,)))
        if len(rows) > 100000:
            raise ValueError("本任务索引超过 100000 项，未执行不完整的公共后处理")
        result = []
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            tags = json.loads(row["tags_json"])
            result.append(FileRecord(
                path=Path(row["path"]), module_id=row["module_id"], task_id=task_id,
                media_type=row["media_type"], size=row["size"], title=row["title"],
                source_url=row["source_url"], source_id=row["source_id"], author_id=row["author_id"],
                author_name=row["author_name"], chapter=row["chapter"], published_at=row["published_at"],
                tags=tuple(tags) if isinstance(tags, list) else (),
                metadata=metadata if isinstance(metadata, dict) else {},
            ))
        return result

    def resolve_media_result(self, task_id: str, key: str, retry_id: str) -> None:
        with self.connect() as conn:
            seen = set()
            while task_id and task_id not in seen and len(seen) < 1000:
                seen.add(task_id)
                conn.execute("""UPDATE task_media SET state='repaired',error=?,updated_at=CURRENT_TIMESTAMP
                    WHERE task_id=? AND media_key=? AND state IN ('failed','pending')""",
                    (f"已由补下任务 {retry_id} 完成", task_id, key))
                row = conn.execute("SELECT options_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if row is None:
                    break
                try:
                    options = json.loads(row["options_json"])
                    task_id = str(options.get("_media_retry_of") or "") if isinstance(options, dict) else ""
                except (ValueError, TypeError):
                    break

    def create_batch(self, batch_id: str, output_dir: str, items) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO batches(batch_id,output_dir) VALUES (?,?)", (batch_id, output_dir))
            conn.executemany("INSERT INTO batch_items(batch_id,position,module_id,target) VALUES (?,?,?,?)",
                             [(batch_id, index, module_id, target) for index, module_id, target in items])

    def fail_batch_item(self, batch_id: str, position: int, error: str) -> None:
        with self.connect() as conn:
            conn.execute("""UPDATE batch_items SET state='enqueue_failed', error=?
                WHERE batch_id=? AND position=? AND task_id IS NULL""", (error, batch_id, position))

    def batch_snapshot(self, batch_id: str):
        with self.connect() as conn:
            batch = conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                raise KeyError("找不到批次")
            rows = conn.execute("""SELECT i.position,i.module_id,i.target,i.task_id,
                COALESCE(t.status,i.state) AS status,
                CASE WHEN t.error != '' THEN t.error ELSE i.error END AS error
                FROM batch_items i LEFT JOIN tasks t ON i.task_id=t.task_id
                WHERE i.batch_id=? ORDER BY i.position""", (batch_id,))
            return dict(batch), [dict(row) for row in rows]

    def unfinished_batch_ids(self) -> list[str]:
        with self.connect() as conn:
            return [str(row[0]) for row in conn.execute("""SELECT DISTINCT batch_id FROM batch_items
                WHERE state IN ('pending','queued','running','paused','cancelling')""")]

    def request_task_cancel(self, task_id: str) -> bool:
        """Accept cancellation only for active, including paused, tasks."""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks SET status='cancelling', error='正在停止任务',
                    updated_at=CURRENT_TIMESTAMP
                WHERE task_id=? AND status IN ('queued', 'running', 'paused')
                """,
                (task_id,),
            )
            return cursor.rowcount > 0

    def transition_active_task(self, task_id: str, status: str, expected: tuple[str, ...]) -> bool:
        """Compare-and-set prevents delayed pause/resume requests reopening terminal tasks."""
        if (status not in {"queued", "running", "paused"} or not expected
                or not set(expected).issubset({"queued", "running", "paused"})):
            raise ValueError("无效的任务状态转换")
        placeholders = ",".join("?" for _ in expected)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""UPDATE tasks SET status=?, error='', updated_at=CURRENT_TIMESTAMP,
                    started_at=CASE WHEN ?='running' AND started_at IS NULL
                        THEN CURRENT_TIMESTAMP ELSE started_at END
                    WHERE task_id=? AND status IN ({placeholders})""",
                (status, status, task_id, *expected),
            )
            return cursor.rowcount > 0

    def update_task_normalized_target(self, task_id: str, normalized_target: str) -> None:
        if not normalized_target:
            return
        with self.connect() as conn:
            conn.execute(
                "UPDATE tasks SET normalized_target=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                (normalized_target, task_id),
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
                (task_id, module_id, level, safe_error_message(message),
                 json.dumps(redact_sensitive_data(metadata or {}), ensure_ascii=False)),
            )
        self._log_write_counter += 1
        # Prune roughly every 200 writes so the check itself is cheap.
        if self._log_write_counter % 200 == 0:
            self.prune_logs()

    def prune_logs(self, max_rows: int | None = None) -> int:
        """Delete the oldest log rows beyond *max_rows* (default MAX_LOG_ROWS).

        Returns the number of deleted rows.  Safe to call at any time; a no-op
        when the table is already under the limit.
        """
        limit = max_rows if max_rows is not None else self.MAX_LOG_ROWS
        with self.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
            if count <= limit:
                return 0
            excess = count - limit
            conn.execute(
                """
                DELETE FROM logs WHERE id IN (
                    SELECT id FROM logs ORDER BY id ASC LIMIT ?
                )
                """,
                (excess,),
            )
            return excess

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
                SET path=?, title=?, revision=lower(hex(randomblob(16))), updated_at=CURRENT_TIMESTAMP
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
        self.set_settings({key: value})

    def set_settings(self, values: dict[str, object]) -> None:
        # Serialize first so invalid values cannot leave partially saved preferences.
        rows = [(key, json.dumps(value, ensure_ascii=False)) for key, value in values.items()]
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO settings(key, value_json)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                rows,
            )

    def get_settings(self) -> dict[str, object]:
        with self.connect() as conn:
            rows = list(conn.execute("SELECT key, value_json FROM settings"))
        values = {}
        for row in rows:
            try:
                values[row["key"]] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                continue
        return values

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
            conn.execute("BEGIN IMMEDIATE")
            rows = list(conn.execute("SELECT task_id, owner_id FROM tasks WHERE status IN ('queued','running','paused','cancelling')"
                                     + (" AND module_id=?" if module_id else ""), (module_id,) if module_id else ()))
            if self._live_task_ids(rows):
                raise ValueError("请先在所属实例结束下载任务，再清理历史")
            conn.execute("""UPDATE batch_items SET state='cancelled', error='未完成任务记录已清理'
                WHERE task_id IN (SELECT task_id FROM tasks WHERE status IN ('queued','running','paused','cancelling')"""
                + (" AND module_id=?" if module_id else "") + ")", (module_id,) if module_id else ())
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
            conn.execute("BEGIN IMMEDIATE")
            rows = list(conn.execute(f"SELECT task_id, owner_id FROM tasks WHERE task_id IN ({placeholders})"
                                     " AND status IN ('queued','running','paused','cancelling')", identifiers))
            protected = self._live_task_ids(rows)
            identifiers = [task_id for task_id in identifiers if task_id not in protected]
            if not identifiers:
                return {"logs": 0, "files": 0, "tasks": 0}
            placeholders = ",".join("?" for _ in identifiers)
            conn.execute(f"""UPDATE batch_items SET state='cancelled', error='未完成任务记录已清理'
                WHERE task_id IN (SELECT task_id FROM tasks WHERE task_id IN ({placeholders})
                AND status IN ('queued','running','paused','cancelling'))""", identifiers)
            conn.execute(f"DELETE FROM task_media WHERE task_id IN ({placeholders})", identifiers)
            conn.execute(f"DELETE FROM task_collections WHERE task_id IN ({placeholders})", identifiers)
            for table in ("logs", "files", "tasks"):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE task_id IN ({placeholders})",
                    identifiers,
                )
                counts[table] = cursor.rowcount if cursor.rowcount is not None else 0
        return counts

    def _live_task_ids(self, rows) -> set[str]:
        states = {}
        protected = set()
        for row in rows:
            owner = str(row["owner_id"] or "")
            if not owner:
                continue
            if owner not in states:
                try:
                    states[owner] = owner_is_alive(self.db_path, owner)
                except OSError:
                    states[owner] = True
            if states[owner]:
                protected.add(str(row["task_id"]))
        return protected

    def recover_interrupted_tasks(self) -> int:
        """Recover dead owners only; live instances and uncertain owners stay untouched."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = list(conn.execute("SELECT task_id, owner_id FROM tasks WHERE status IN ('queued','running','cancelling','paused')"))
            owners = {}
            for row in rows:
                owner = str(row["owner_id"] or "")
                if owner and owner not in owners:
                    try:
                        owners[owner] = owner_is_alive(self.db_path, owner)
                    except OSError:
                        owners[owner] = True
            live_owner_present = any(owners.values())
            recovered = 0
            for row in rows:
                owner = str(row["owner_id"] or "")
                if (owner and owners[owner]) or (not owner and live_owner_present):
                    continue
                cursor = conn.execute("""UPDATE tasks
                SET recovery_status=CASE WHEN status IN ('queued','running','paused') THEN status ELSE recovery_status END,
                    status='cancelled',
                    error='上次软件退出时任务未能继续，已恢复为取消状态',
                    finished_at=COALESCE(finished_at, CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE task_id=? AND owner_id=? AND status IN ('queued','running','cancelling','paused')""",
                    (row["task_id"], owner))
                recovered += cursor.rowcount
                if cursor.rowcount:
                    conn.execute("UPDATE batch_items SET state='cancelled', error='所属实例已退出' WHERE task_id=?",
                                 (row["task_id"],))
            return recovered

    def interrupted_tasks(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM tasks WHERE status='cancelled' AND recovery_status IN ('queued','running','paused') ORDER BY created_at, task_id"
            )]

    def mark_shutdown_tasks(self, owner_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET recovery_status=status WHERE owner_id=? AND status IN ('queued','running','paused')",
                         (owner_id,))

    def claim_interrupted_task(self, task_id: str, owner_id: str, *, options: dict | None = None) -> bool:
        """Only one instance may restart a marked task; cancellation is not recovery."""
        options_json = json.dumps(options, ensure_ascii=False) if options is not None else None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row and row["owner_id"] and owner_is_alive(self.db_path, row["owner_id"]):
                return False
            cursor = conn.execute("""UPDATE tasks SET
                status=CASE WHEN recovery_status='paused' THEN 'paused' ELSE 'queued' END,
                recovery_status='', owner_id=?, error='', started_at=NULL, finished_at=NULL,
                updated_at=CURRENT_TIMESTAMP
                WHERE task_id=? AND status='cancelled' AND recovery_status IN ('queued','running','paused')""",
                (owner_id, task_id))
            if cursor.rowcount:
                if options_json is not None:
                    conn.execute("UPDATE tasks SET options_json=? WHERE task_id=?", (options_json, task_id))
                conn.execute("UPDATE batch_items SET state=(SELECT status FROM tasks WHERE task_id=?), error='' WHERE task_id=?",
                             (task_id, task_id))
            return bool(cursor.rowcount)

    def scan_media_files(
        self,
        root: Path | str,
        module_id: str,
        task_id: str | None = None,
        modified_after: float | None = None,
        on_progress=None,
        count_only: bool = False,
        max_depth: int = 6,
    ) -> list[FileRecord] | int:
        """Walk *root* and upsert every media file into the library.

        *max_depth* caps directory recursion so a failure-recovery scan does not
        wander into unrelated deep trees. When *modified_after* is set, each
        file's mtime is checked; old parent directories are still traversed.
        """
        root_path = Path(root).expanduser()
        if not root_path.exists():
            return 0 if count_only else []

        root_parts = len(root_path.parts)
        records: list[FileRecord] = []
        found_count = 0
        skip_dirs = {".git", "__pycache__", ".venv", "venv", "build", "dist", ".idea"}
        pending_values: list[tuple] = []
        with self.connect() as conn:
            for current, directories, filenames in os.walk(root_path, followlinks=False):
                # Depth limit: prune directories beyond max_depth.
                current_depth = len(Path(current).parts) - root_parts
                if current_depth >= max_depth:
                    directories[:] = []
                directories[:] = [
                    name
                    for name in directories
                    if name not in skip_dirs and not is_transfer_workspace_name(name)
                    and not (Path(current) / name).is_symlink()
                ]
                # Directory mtime does not cover existing files or descendants.
                # Filter individual files below instead.
                for filename in filenames:
                    path = Path(current) / filename
                    if (path.suffix.lower() not in MEDIA_EXTENSIONS
                            or path.stem.lower().endswith(".part")
                            or (filename.startswith(".") and (".source" in filename or ".convert" in filename))):
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




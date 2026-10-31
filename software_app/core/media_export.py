"""Bounded snapshots and atomic TXT/CSV exports of unfinished media."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path

from .adapter import TaskCancelled
from .exports import safe_csv_cell
from .output_lock import output_path_lock
from .security import redact_url_query, safe_error_message
from .task_control import task_cancelled


MAX_EXPORT_ITEMS = 10000
COLUMNS = ("task_id", "module_id", "page_url", "media_index", "media_type", "state", "error")


def _checkpoint(cancel_event):
    if cancel_event is not None and task_cancelled(cancel_event):
        raise TaskCancelled("失败清单导出已取消")


def unfinished_media_snapshot(storage, task_ids, *, cancel_event=None):
    identifiers = sorted({str(value) for value in task_ids if str(value)})
    results = []
    with storage.connect() as conn:
        # All chunks see the same database snapshot, including when a download
        # completes or a task is deleted while this background export runs.
        conn.execute("BEGIN")
        for offset in range(0, len(identifiers), 200):
            _checkpoint(cancel_event)
            chunk = identifiers[offset : offset + 200]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""SELECT m.task_id,t.module_id,m.descriptor_json,m.state,m.error
                FROM task_media m JOIN tasks t ON m.task_id=t.task_id
                WHERE m.task_id IN ({placeholders}) AND m.state IN ('failed','pending')
                ORDER BY m.task_id,m.media_key LIMIT ?""",
                (*chunk, MAX_EXPORT_ITEMS + 1 - len(results)),
            )
            for row in rows:
                _checkpoint(cancel_event)
                item = json.loads(row["descriptor_json"])
                results.append(
                    {
                        "task_id": row["task_id"],
                        "module_id": row["module_id"],
                        "page_url": redact_url_query(str(item["target"])),
                        "media_index": item["index"],
                        "media_type": item["kind"],
                        "state": row["state"],
                        "error": safe_error_message(row["error"] or "任务中断或尚未处理"),
                    }
                )
                if len(results) > MAX_EXPORT_ITEMS:
                    raise ValueError("一次最多导出 10000 项，请减少所选任务")
    return results


def _validate_destination(storage, destination):
    if destination.suffix.casefold() not in {".txt", ".csv"}:
        raise ValueError("失败清单仅支持 TXT 或 CSV")
    key = os.path.normcase(str(destination.resolve()))
    database = str(storage.db_path.resolve())
    if key in {os.path.normcase(database + suffix) for suffix in ("", "-wal", "-shm", "-journal")}:
        raise ValueError("不能用导出清单覆盖运行数据库")
    with storage.connect() as conn:
        collation = " COLLATE NOCASE" if os.name == "nt" else ""
        if conn.execute("SELECT 1 FROM files WHERE path=?" + collation, (str(destination),)).fetchone():
            raise ValueError("导出位置已经是下载库中的资源，请选择其他文件名")


def export_unfinished_media(storage, task_ids, destination, *, cancel_event=None, on_progress=None):
    destination = Path(destination).expanduser().resolve()
    _checkpoint(cancel_event)
    _validate_destination(storage, destination)
    rows = unfinished_media_snapshot(storage, task_ids, cancel_event=cancel_event)
    if not rows:
        raise ValueError("所选任务没有已登记的未完成媒体；支持 Bluesky／Instagram 静态下载、Pixiv 单作品图片及作品收藏")
    _checkpoint(cancel_event)
    temporary = None
    with output_path_lock(destination, cancel_event):
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8-sig",
                newline="",
                delete=False,
                dir=destination.parent,
                prefix=".media-export-",
                suffix=".tmp",
            ) as output:
                temporary = Path(output.name)
                writer = csv.writer(output) if destination.suffix.casefold() == ".csv" else None
                if writer:
                    writer.writerow(COLUMNS)
                else:
                    output.write(
                        "欲求达 · 未完成媒体清单\n只含导出快照中的已发现媒体；补下时会重新读取页面核对资源。\n\n"
                    )
                for index, row in enumerate(rows, 1):
                    _checkpoint(cancel_event)
                    if writer:
                        writer.writerow([safe_csv_cell(row[column]) for column in COLUMNS])
                    else:
                        output.write(
                            f"任务：{row['task_id']} · 平台：{row['module_id']}\n"
                            f"页面：{row['page_url']}\n媒体序号：{row['media_index']} · 类型：{row['media_type']}\n"
                            f"状态：{'失败' if row['state'] == 'failed' else '待处理'}\n原因：{row['error']}\n\n"
                        )
                    if on_progress and (index == len(rows) or index % 100 == 0):
                        on_progress(index, len(rows))
            _checkpoint(cancel_event)
            _validate_destination(storage, destination)
            os.replace(temporary, destination)
            return len(rows)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

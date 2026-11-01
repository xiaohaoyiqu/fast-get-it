"""Exact account bindings and immutable per-task configuration snapshots."""

from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .blocklist import account_from_url


FORMAT_VALUES = {
    "image_format": {"original", "png", "jpg", "webp"},
    "video_format": {"original", "mp4"},
    "animation_format": {"gif", "mp4"},
    "audio_format": {"mp3", "m4a"},
}
ALLOWED_OPTIONS = {*FORMAT_VALUES, "types", "max_works", "filter_ai"}


def settings_source(options):
    profile = options.get("_person_config")
    source = "当前设置"
    if profile:
        source = (
            f"人物配置：{profile['label']}"
            if isinstance(profile, dict) and profile.get("label")
            else "人物配置快照损坏"
        )
    return source + " + 本次任务覆盖" if options.get("_task_override") else source


def person_key(module_id: str, target: str, input_kind: str = "") -> tuple[str, str] | None:
    """Only direct supported account targets qualify; never infer an artwork's author."""
    value = str(target or "").strip()
    if module_id not in {"twitter", "pixiv", "bluesky", "instagram"}:
        return None
    if value.startswith(("http://", "https://")):
        try:
            parsed = urlparse(value)
        except ValueError:
            return None
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if module_id == "bluesky":
            if parsed.hostname not in {"bsky.app", "www.bsky.app"} or len(parts) != 2 or parts[0] != "profile":
                return None
            return person_key(module_id, parts[1])
        if module_id == "pixiv":
            if parts and parts[0] == "en":
                parts = parts[1:]
            if len(parts) != 2 or parts[0] != "users":
                return None
        key = account_from_url(value)
        return key if key and key[0] == module_id else None
    if module_id == "pixiv":
        return (module_id, value) if input_kind == "user" and value.isdigit() else None
    if module_id == "bluesky":
        value = value.lstrip("@")
        if re.fullmatch(r"did:(?:plc|web):[A-Za-z0-9.:%_-]+", value):
            return module_id, value
        if re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", value.casefold()):
            return module_id, value.casefold()
        return None
    host = "x.com" if module_id == "twitter" else "www.instagram.com"
    return person_key(module_id, f"https://{host}/{value.lstrip('@')}")


def validated_options(options: dict) -> dict:
    if not isinstance(options, dict) or set(options) - ALLOWED_OPTIONS:
        raise ValueError("人物配置包含不支持的参数；登录资料、代理和内部任务参数不能存入人物配置")
    result = copy.deepcopy(options)
    for key, values in FORMAT_VALUES.items():
        if key in result and result[key] not in values:
            raise ValueError(f"人物配置的 {key} 无效")
    if "types" in result:
        types = str(result["types"]).split(",")
        if not types or any(item not in {"1", "2", "3", "4"} for item in types):
            raise ValueError("下载类型应为 1、2、3、4，以英文逗号分隔")
        result["types"] = ",".join(sorted(set(types)))
    if "max_works" in result and (type(result["max_works"]) is not int or not 1 <= result["max_works"] <= 100):
        raise ValueError("最多作品应为 1–100")
    if "filter_ai" in result and type(result["filter_ai"]) is not bool:
        raise ValueError("跳过 AI 作品必须为开关值")
    return result


class PersonConfigs:
    def __init__(self, storage):
        self.storage = storage

    def get(self, key):
        if key is None:
            return None
        with self.storage.connect() as conn:
            row = conn.execute("SELECT * FROM person_configs WHERE module_id=? AND account_key=?", key).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["options"] = validated_options(json.loads(result.pop("options_json")))
        return result

    def metadata(self, key):
        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT module_id,account_key,label,output_dir,revision,updated_at FROM person_configs "
                "WHERE module_id=? AND account_key=?",
                key,
            ).fetchone()
        return dict(row) if row else None

    def list_page(self, query="", module_id="", offset=0, limit=100):
        offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
        clauses, parameters = [], []
        if module_id:
            clauses.append("module_id=?")
            parameters.append(module_id)
        if query.strip():
            clauses.append("(instr(lower(label),lower(?))>0 OR instr(lower(account_key),lower(?))>0)")
            parameters.extend([query.strip()] * 2)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.storage.connect() as conn:
            conn.execute("BEGIN")
            total = conn.execute("SELECT count(*) FROM person_configs" + where, parameters).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM person_configs" + where + " ORDER BY module_id,account_key LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            raw = item.pop("options_json")
            try:
                validated_options(json.loads(raw))
                item["state"] = "正常"
            except (ValueError, TypeError):
                item["state"] = "参数损坏，可重设或删除"
            result.append(item)
        return result, total

    @staticmethod
    def _check_revision(conn, key, expected_revision):
        if expected_revision is None:
            return
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT revision FROM person_configs WHERE module_id=? AND account_key=?", key).fetchone()
        if (row[0] if row else "") != expected_revision:
            raise ValueError("配置已被其他窗口或实例修改，请关闭后重新打开再操作")

    def save(self, key, label: str, options: dict, output_dir: str = "", *, expected_revision=None):
        if key is None or key[0] not in {"twitter", "pixiv", "bluesky", "instagram"}:
            raise ValueError("请使用明确的作者主页或账号目标保存人物配置")
        options = validated_options(options)
        label = label.strip()
        if not label or len(label) > 100:
            raise ValueError("配置名称应为 1–100 个字符")
        directory = str(Path(output_dir).expanduser().resolve()) if output_dir.strip() else ""
        if directory and Path(directory).exists() and not Path(directory).is_dir():
            raise ValueError("人物保存目录指向文件")
        with self.storage.connect() as conn:
            self._check_revision(conn, key, expected_revision)
            conn.execute(
                """INSERT INTO person_configs(module_id,account_key,label,options_json,output_dir,revision,updated_at)
                VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(module_id,account_key) DO UPDATE SET
                label=excluded.label,options_json=excluded.options_json,output_dir=excluded.output_dir,
                revision=excluded.revision,updated_at=CURRENT_TIMESTAMP""",
                (*key, label, json.dumps(options), directory, uuid.uuid4().hex),
            )

    def delete(self, key, *, expected_revision=None):
        with self.storage.connect() as conn:
            self._check_revision(conn, key, expected_revision)
            conn.execute("DELETE FROM person_configs WHERE module_id=? AND account_key=?", key)

    def prepare(self, module_id, target, output_dir, options):
        result = copy.deepcopy(options)
        keep_settings = result.pop("_keep_task_settings", False)
        directory = Path(output_dir).expanduser()
        key = person_key(module_id, target, str(result.get("input_kind") or ""))
        snapshot = result.get("_person_config")
        binding = result.get("_settings_binding")
        identity = {"module_id": module_id, "target": str(target)}
        if binding is not None and (not isinstance(binding, dict) or binding != identity):
            raise ValueError("已确认的设置与当前目标不匹配，请重新配置任务")
        if snapshot is not None:
            if key is None or not isinstance(snapshot, dict) or snapshot.get("key") != list(key):
                raise ValueError("人物配置与当前目标不匹配，任务未启动；请重新选择目标和配置")
            result.update(validated_options(snapshot["options"]))
        elif binding is None and not keep_settings:
            profile = self.get(key)
            if profile is not None:
                result.update(profile["options"])
                if profile["output_dir"] and not result.get("_batch_id") and "_retry_media" not in result:
                    directory = Path(profile["output_dir"])
                result["_person_config"] = {
                    "key": list(key),
                    "label": profile["label"],
                    "revision": profile["revision"],
                    "options": profile["options"],
                }
        override = result.get("_task_override")
        if override is not None:
            if not isinstance(override, dict) or set(override) - {"options", "output_dir"}:
                raise ValueError("本次任务覆盖参数无效")
            result.update(validated_options(override.get("options", {})))
            if binding is None and not keep_settings and override.get("output_dir"):
                if result.get("_batch_id") or "_retry_media" in result:
                    raise ValueError("批次和媒体补下不能覆盖原有输出目录")
                directory = Path(override["output_dir"]).expanduser()
        # Even an explicit absence of a person profile is a confirmed decision.
        result["_settings_binding"] = identity
        return directory, result

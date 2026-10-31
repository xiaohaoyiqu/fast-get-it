"""Immutable selection snapshots; these are not download-completion archives."""

from __future__ import annotations

import hashlib
import json

from .models import ProgressEvent


SELECTION_KEYS = (
    "input_kind",
    "search_mode",
    "max_works",
    "account_id",
    "pixiv_visibility",
    "bookmark_tag",
    "start_date",
    "end_date",
    "bookmark_date_basis",
)

ARTWORK_SEARCH_MODES = frozenset({"", "标签（全部）", "标签（插画）", "标签（漫画）", "标题 / 简介"})


def collection_label(kind):
    return {
        "bookmark": "收藏",
        "novel_bookmark": "收藏",
        "manga_series": "漫画系列",
        "search": "作品搜索",
        "ranking": "排行榜",
    }.get(kind, "集合")


def selection_key(options, kind=""):
    if not isinstance(options, dict):
        raise ValueError("集合任务选择参数损坏")
    # Existing v1 bookmark/series keys must remain valid, even when their
    # task options happen to contain filters only used by artwork search.
    keys = SELECTION_KEYS + (("age_mode", "minimum_bookmarks") if kind == "search" else ())
    selected = {key: options[key] for key in keys if key in options}
    return hashlib.sha256(json.dumps(selected, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _numeric_id(value):
    return isinstance(value, str) and value.isascii() and value.isdigit() and 1 <= len(value) <= 32


def validated_collection_plan(plan, module_id, target, options):
    if not isinstance(options, dict):
        raise ValueError("集合任务选择参数损坏")
    fields = {"version", "module_id", "target", "kind", "owner_id", "ids", "selection_key"}
    if (
        not isinstance(plan, dict)
        or set(plan) != fields
        or type(plan.get("version")) is not int
        or plan["version"] != 1
    ):
        raise ValueError("集合发现计划格式损坏，未重新选择其他作品")
    if module_id != "pixiv" or plan["module_id"] != module_id or plan["target"] != target:
        raise ValueError("集合发现计划与任务目标不匹配")
    kind = plan["kind"]
    if not isinstance(kind, str) or kind not in {"bookmark", "novel_bookmark", "manga_series", "search", "ranking"}:
        raise ValueError("集合发现计划的类型或身份无效")
    if kind in {"search", "ranking"}:
        if (
            not isinstance(target, str)
            or plan["owner_id"] != hashlib.sha256(target.strip().encode("utf-8")).hexdigest()
        ):
            raise ValueError("搜索或排行榜计划的目标身份不匹配")
        if kind == "search" and options.get("search_mode", "") not in ARTWORK_SEARCH_MODES:
            raise ValueError("当前搜索类型不支持作品集合计划")
    elif not _numeric_id(plan["owner_id"]):
        raise ValueError("集合发现计划的类型或身份无效")
    ids = plan["ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or not all(_numeric_id(value) for value in ids):
        raise ValueError("集合发现计划必须包含 1–100 个有效作品 ID")
    if len(set(ids)) != len(ids):
        raise ValueError("集合发现计划包含重复作品")
    limit = max(1, min(int(options.get("max_works") or 20), 100))
    if len(ids) > limit:
        raise ValueError("集合发现计划超过任务选择的作品上限")
    if plan["selection_key"] != selection_key(options, kind):
        raise ValueError("集合选择参数已变化；请按当前设置重试或重新创建任务")
    return {**plan, "ids": list(ids)}


def restored_collection_ids(task, kind, owner_id):
    if "_collection_plan" not in task.options:
        return None
    plan = validated_collection_plan(task.options["_collection_plan"], task.module_id, task.target, task.options)
    if plan["kind"] != kind or plan["owner_id"] != owner_id:
        raise ValueError("原集合类型或身份已变化，未下载其他作品")
    return plan["ids"]


def publish_collection_plan(task, kind, owner_id, ids, callbacks):
    plan = validated_collection_plan(
        {
            "version": 1,
            "module_id": task.module_id,
            "target": task.target,
            "kind": kind,
            "owner_id": owner_id,
            "ids": list(ids),
            "selection_key": selection_key(task.options, kind),
        },
        task.module_id,
        task.target,
        task.options,
    )
    # The manager commits this event before returning; transfer starts afterwards.
    callbacks.on_progress(
        ProgressEvent(
            task.task_id,
            task.module_id,
            "info",
            f"已固定本次{collection_label(kind)}范围：{len(ids)} 个作品",
            metadata={"collection_plan": plan},
        )
    )

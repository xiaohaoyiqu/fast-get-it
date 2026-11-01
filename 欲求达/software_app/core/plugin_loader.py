from __future__ import annotations

import importlib.util
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterable

from .adapter import CrawlerAdapter


PLUGIN_API_VERSION = 1
PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


@dataclass(frozen=True)
class PluginReport:
    plugin_id: str
    name: str
    source: str
    status: str
    message: str = ""
    replaces: str = ""


@dataclass(frozen=True)
class AdapterBundle:
    adapters: tuple[CrawlerAdapter, ...]
    reports: tuple[PluginReport, ...]


def _load_module(path: Path, plugin_id: str) -> ModuleType:
    module_name = f"software_app_external_plugin_{plugin_id.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法创建插件模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _external_plugin(folder: Path) -> tuple[list[CrawlerAdapter], PluginReport, str]:
    manifest_path = folder / "plugin.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("plugin.json 必须是对象")
    plugin_id = str(payload.get("id") or folder.name).strip().lower()
    name = str(payload.get("name") or plugin_id).strip()
    if not PLUGIN_ID_PATTERN.fullmatch(plugin_id):
        raise ValueError("插件 id 只能使用小写字母、数字、下划线和连字符")
    if int(payload.get("api_version") or 0) != PLUGIN_API_VERSION:
        raise ValueError(f"插件 API 版本不兼容，需要 {PLUGIN_API_VERSION}")
    replaces = str(payload.get("replaces") or "").strip().lower()
    if payload.get("enabled") is not True:
        return [], PluginReport(
            plugin_id, name, str(folder), "disabled", "清单未设置 enabled=true", replaces
        ), replaces
    module_name = str(payload.get("module") or "plugin.py").strip()
    module_path = (folder / module_name).resolve()
    folder_root = folder.resolve()
    if folder_root not in module_path.parents or not module_path.is_file() or module_path.suffix.lower() != ".py":
        raise ValueError("插件 module 必须是插件目录内存在的 Python 文件")
    factory_name = str(payload.get("factory") or "create_adapters").strip()
    module = _load_module(module_path, plugin_id)
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise ValueError(f"插件缺少可调用工厂：{factory_name}")
    created = factory()
    candidates = list(created) if isinstance(created, (list, tuple)) else [created]
    adapters: list[CrawlerAdapter] = []
    for adapter in candidates:
        if not isinstance(adapter, CrawlerAdapter):
            raise TypeError("插件工厂必须返回 CrawlerAdapter 或其列表")
        if not PLUGIN_ID_PATTERN.fullmatch(adapter.module_id):
            raise ValueError(f"插件返回了无效模块 ID：{adapter.module_id}")
        adapters.append(adapter)
    if not adapters:
        raise ValueError("插件没有返回任何 adapter")
    module_ids = ", ".join(adapter.module_id for adapter in adapters)
    return adapters, PluginReport(
        plugin_id, name, str(folder), "loaded", f"加载模块：{module_ids}", replaces
    ), replaces


def external_plugin_manifest(source: Path | str, plugin_root: Path | str) -> tuple[Path, dict]:
    """Return one external manifest after proving it is an immediate plugin-root child."""
    root = Path(plugin_root).expanduser().resolve()
    folder = Path(source).expanduser().resolve()
    if folder.parent != root:
        raise ValueError("只能修改插件目录中的直接子目录")
    manifest = folder / "plugin.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"插件清单不存在：{manifest}")
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("plugin.json 必须是对象")
    return manifest, payload


def set_external_plugin_enabled(
    source: Path | str,
    enabled: bool,
    plugin_root: Path | str,
) -> dict:
    """Atomically toggle an external manifest. The running registry is unchanged until restart."""
    manifest, payload = external_plugin_manifest(source, plugin_root)
    payload["enabled"] = bool(enabled)
    temporary = manifest.with_name(f".{manifest.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(manifest)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def load_adapter_plugins(
    builtin_factories: Iterable[tuple[str, str, Callable[[], CrawlerAdapter]]],
    plugin_root: Path | str,
) -> AdapterBundle:
    """Load built-ins and explicitly enabled local adapter plugins with duplicate protection."""
    adapters: dict[str, CrawlerAdapter] = {}
    reports: list[PluginReport] = []
    for plugin_id, name, factory in builtin_factories:
        try:
            adapter = factory()
            if not isinstance(adapter, CrawlerAdapter):
                raise TypeError("内置工厂没有返回 CrawlerAdapter")
            if adapter.module_id in adapters:
                raise ValueError(f"模块 ID 重复：{adapter.module_id}")
            adapters[adapter.module_id] = adapter
            reports.append(PluginReport(plugin_id, name, "builtin", "loaded", adapter.module_id))
        except Exception as exc:  # noqa: BLE001
            reports.append(PluginReport(plugin_id, name, "builtin", "error", str(exc)))

    root = Path(plugin_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    for folder in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.casefold()):
        manifest = folder / "plugin.json"
        if not manifest.is_file():
            continue
        try:
            loaded, report, replaces = _external_plugin(folder)
            duplicate_ids = [adapter.module_id for adapter in loaded if adapter.module_id in adapters]
            if duplicate_ids and replaces not in duplicate_ids:
                raise ValueError(f"模块 ID 已存在：{', '.join(duplicate_ids)}；替换内置模块需要清单 replaces")
            if replaces:
                if replaces not in adapters:
                    raise ValueError(f"要替换的模块不存在：{replaces}")
                if len(loaded) != 1 or loaded[0].module_id != replaces:
                    raise ValueError("替换插件必须只返回一个与 replaces 同 ID 的 adapter")
                adapters.pop(replaces)
                for index, old_report in enumerate(reports):
                    if old_report.source == "builtin" and old_report.plugin_id == replaces:
                        reports[index] = PluginReport(
                            old_report.plugin_id,
                            old_report.name,
                            old_report.source,
                            "replaced",
                            f"由外部插件 {report.plugin_id} 接管",
                        )
                        break
            for adapter in loaded:
                adapters[adapter.module_id] = adapter
            reports.append(report)
        except Exception as exc:  # noqa: BLE001
            reports.append(PluginReport(folder.name, folder.name, str(folder), "error", str(exc)))
    return AdapterBundle(tuple(adapters.values()), tuple(reports))


__all__ = [
    "AdapterBundle",
    "PLUGIN_API_VERSION",
    "PluginReport",
    "external_plugin_manifest",
    "load_adapter_plugins",
    "set_external_plugin_enabled",
]

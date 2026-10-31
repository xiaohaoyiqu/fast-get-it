"""Local, explicit checks; never log in, install tools or send network requests."""

from __future__ import annotations

import shutil
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .browser_driver import chromedriver_status
from .external_tools import find_ffmpeg
from .security import safe_error_message
from .aria2_bt import find_aria2c
from .person_config import settings_source


def _inspect_directory(output_dir, probe_write):
    lines = []
    directory = Path(output_dir).expanduser().resolve()
    lines.append(f"保存目录：{directory}")
    ancestor = directory
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        lines.append("保存目录异常：路径经过现有文件，请修改目录。")
    else:
        free = shutil.disk_usage(ancestor).free
        lines.append(f"可用磁盘空间：{free / (1024**3):.2f} GiB")
        lines.append("目录状态：已存在" if directory.is_dir() else "目录状态：尚未创建，任务启动时创建")
        if probe_write:
            try:
                with tempfile.TemporaryFile(prefix="yuqiuda-write-probe-", dir=ancestor) as probe:
                    probe.write(b"yuqiuda write probe\n")
                    probe.flush()
                    os.fsync(probe.fileno())
                location = "保存目录" if directory.is_dir() else f"最近现有父目录 {ancestor}"
                lines.append(f"试写通过：{location}（临时文件已关闭并清理）")
                if not directory.is_dir():
                    lines.append("目标尚不存在；父目录试写不保证后续逐层创建或任务执行时仍可写。")
            except Exception as exc:
                lines.append("试写未通过或清理未完成：" + safe_error_message(exc))
        else:
            lines.append("未执行写入探测。")
    return lines


def inspect_environment(output_dir, *, tasks=None, probe_write=False, cancel_event=None):
    lines = ["检查开始：" + datetime.now().astimezone().isoformat(timespec="seconds")]
    entries = tasks if tasks is not None else [("", "当前目录", output_dir, {})]
    if not entries:
        entries = [("", "未选择目标，仅检查当前目录", output_dir, {})]
    browser_needed = tasks is None
    ffmpeg_needed = tasks is None
    bt_needed = False
    checked = {}
    for module_id, target, directory, options in entries:
        if cancel_event is not None and cancel_event.is_set():
            lines.append("检查已停止；未开始的目标未检查。")
            return "\n".join(lines), ""
        lines.extend(("", f"目标：[{module_id or '本机'}] {target}", "设置来源：" + settings_source(options)))
        try:
            key = str(Path(directory).expanduser().resolve())
            if key not in checked:
                checked[key] = _inspect_directory(directory, probe_write)
            lines.extend(checked[key])
        except Exception as exc:
            lines.append("目录检查失败：" + safe_error_message(exc))
        browser_needed |= module_id in {"twitter", "google_image"} or (
            module_id == "website" and bool(options.get("browser_render"))
        )
        types = set(str(options.get("types", "1,2,3,4")).split(","))
        ffmpeg_needed |= (
            "4" in types
            or "3" in types
            or (
                "2" in types
                and (options.get("video_format", "original") != "original" or module_id in {"bluesky", "website"})
            )
        )
        bt_needed |= module_id == "ehentai" and bool(options.get("eh_bt_download_enabled"))
        if module_id == "ehentai" and options.get("eh_bt_download_enabled"):
            from .aria2_bt import aria2_limits_from_options

            limits = aria2_limits_from_options(options)
            lines.append(
                (
                    f"aria2 每进程上限：下载 {limits.download_kib} KiB/s，上传 {limits.upload_kib} KiB/s"
                    if limits.enabled
                    else "aria2 每进程限速：未启用"
                )
                + "；与 HTTP 预算分开，任务中不动态调整。"
            )
    driver = ""
    if browser_needed:
        try:
            status = chromedriver_status(auto_download=False)
            driver = (
                f"{status.message}\nChrome：{status.chrome_version or '未检测到'}\n"
                f"Driver：{status.driver_version or '未检测到'}\n路径：{status.driver_path or '未找到'}"
            )
            lines.append("\n当前任务可能需要浏览器：\n" + driver)
        except Exception as exc:
            driver = "浏览器检查失败：" + safe_error_message(exc)
            lines.append("\n" + driver)
    else:
        lines.append("\n当前任务未声明需要受管理浏览器，跳过 Chrome/Driver 检测。")
    try:
        if ffmpeg_needed:
            lines.append("\n按媒体类型／格式可能需要 FFmpeg：" + (find_ffmpeg() or "未找到"))
        else:
            lines.append("\n当前媒体类型／格式未声明需要 FFmpeg；实际发现 HLS 等媒体时可能仍需要。")
        if bt_needed:
            executable, source = find_aria2c()
            lines.append(f"\naria2：{executable or '未找到'}（{source}）")
    except Exception as exc:
        lines.append("工具检查失败：" + safe_error_message(exc))
    lines.append("\n检查结束：" + datetime.now().astimezone().isoformat(timespec="seconds"))
    lines.append("本次为本机检查；没有验证账号登录、网站可访问性或实际下载，也没有安装／更新工具。")
    return "\n".join(lines), driver

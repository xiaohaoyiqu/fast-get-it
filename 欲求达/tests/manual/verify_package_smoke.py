"""Check isolated one-file CLI startup and optionally open/close the packaged GUI."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", type=Path)
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()
    dist = args.dist.resolve()
    cli = next(dist.glob("*-cli.exe"))
    gui_path = next(path for path in dist.glob("*.exe") if path != cli)
    checks = []
    with tempfile.TemporaryDirectory(prefix="package-smoke-", dir=dist.parent) as temporary:
        runtime = Path(temporary)
        env = {**os.environ, "YUQIUDA_HOME": str(runtime), "YUQIUDA_OUTPUT_DIR": str(runtime / "media"),
               "PYTHONIOENCODING": "utf-8"}
        commands = (
            ("--help",), ("modules",),
            ("preview", "hentaipaw", "123"),
            ("preview", "hentaipaw", "https://zh.hentaipaw.com/tags"),
            ("search", "hentaipaw", "全部", "--scope", "软件收藏", "--limit", "1"),
        )
        for command in commands:
            result = subprocess.run([str(cli), *command], cwd=runtime, env=env, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=45)
            if result.returncode:
                raise RuntimeError(f"CLI {command}: {result.stderr[-2000:]}")
            if command == ("modules",) and not all(name in result.stdout for name in (
                "twitter", "pixiv", "jmcomic", "ehentai", "bluesky", "instagram", "google_image", "website", "hentaipaw",
            )):
                raise AssertionError("packaged modules missing")
            checks.append("cli " + " ".join(command))
        if not (runtime / "data/software_app/content_downloader.db").is_file():
            raise AssertionError("package did not respect isolated runtime directory")
        checks.append("isolated runtime")
        if args.gui:
            import psutil
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            process = subprocess.Popen([str(gui_path)], cwd=runtime, env=env)
            try:
                deadline = time.monotonic() + 35
                found = []
                while time.monotonic() < deadline and process.poll() is None:
                    owner = psutil.Process(process.pid)
                    pids = {owner.pid, *(child.pid for child in owner.children(recursive=True))}
                    found.clear()

                    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                    def inspect(hwnd, _lparam, pids=pids):
                        pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if pid.value in pids and user32.IsWindowVisible(hwnd):
                            title = ctypes.create_unicode_buffer(256)
                            user32.GetWindowTextW(hwnd, title, len(title))
                            if title.value == "欲求达":
                                found.append(hwnd)
                        return True

                    user32.EnumWindows(inspect, 0)
                    if found:
                        break
                    time.sleep(0.1)
                if not found:
                    raise AssertionError("packaged GUI did not create its visible main window")
                checks.append("GUI main window")
                user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                user32.PostMessageW(found[0], 0x0010, 0, 0)  # WM_CLOSE uses the app's normal exit handler.
                if process.wait(timeout=20) != 0:
                    raise AssertionError("packaged GUI did not exit normally")
                checks.append("GUI normal close")
            finally:
                if process.poll() is None:
                    try:
                        for child in psutil.Process(process.pid).children(recursive=True):
                            child.kill()
                    except psutil.Error:
                        pass
                    process.kill()
                    process.wait(timeout=5)
    report = {"ok": True, "checks": checks, "dist": str(dist)}
    target = dist.parent / "package-smoke.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(target), **report}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

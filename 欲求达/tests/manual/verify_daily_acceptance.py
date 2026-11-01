"""Run isolated native/local daily workflows, preserving each script's evidence."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


CASES = (
    ("desktop", "verify_desktop_smoke.py", ()),
    ("person_settings", "verify_person_config_smoke.py", ()),
    ("eh_form", "verify_eh_manual_smoke.py", ()),
    ("transfer_cache", "verify_transfer_cache_smoke.py", ()),
    ("library_integrity", "verify_library_integrity_smoke.py", ()),
    ("browser", "verify_runtime_smoke.py", ("browser",)),
    ("browser_failure", "verify_browser_failure_smoke.py", ()),
    ("browser_ownership", "verify_browser_orphan_smoke.py", ()),
    ("http_checkpoint", "verify_http_checkpoint_smoke.py", ()),
    ("http_redirect", "verify_http_redirect_resume_smoke.py", ()),
    ("http_bandwidth", "verify_bandwidth_smoke.py", ()),
    ("http_backoff", "verify_domain_backoff_smoke.py", ()),
    ("aria2_limits", "verify_aria2_limits_smoke.py", ()),
    ("hls_resume", "verify_hls_resume_smoke.py", ()),
    ("media_conversion", "verify_media_engine_smoke.py", ()),
    ("tool_install", "verify_tool_install_smoke.py", ()),
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--only", nargs="+", choices=[case[0] for case in CASES])
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    args.root.mkdir(parents=True, exist_ok=True)
    results = []
    cases = [case for case in CASES if args.only is None or case[0] in args.only]
    for name, filename, options in cases:
        started = time.monotonic()
        result = {"name": name, "ok": False}
        try:
            process = subprocess.run(
                [sys.executable, "-X", "utf8", str(Path(__file__).parent / filename), *options],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            log_name = f"{name}.rerun.log" if args.only else f"{name}.log"
            (args.root / log_name).write_text(process.stdout + "\n" + process.stderr, encoding="utf-8")
            report = None
            for line in process.stdout.splitlines():
                try:
                    candidate = json.loads(line)
                except ValueError:
                    continue
                if isinstance(candidate, dict) and "ok" in candidate:
                    report = candidate
            result.update(
                returncode=process.returncode,
                report=report,
                ok=process.returncode == 0 and report is not None and report.get("ok") is True,
            )
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["seconds"] = round(time.monotonic() - started, 3)
        results.append(result)
        summary = {
            "ok": all(item["ok"] for item in results),
            "completed": len(results),
            "planned": len(cases),
            "results": results,
        }
        (args.root / ("local-rerun-summary.json" if args.only else "local-summary.json")).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"name": name, "ok": result["ok"], "seconds": result["seconds"]}), flush=True)
    return 0 if all(item["ok"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

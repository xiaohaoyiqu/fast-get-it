from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


class CliHelpTests(unittest.TestCase):
    def test_following_script_help_runs_without_external_pythonpath(self):
        root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "following_collector.py", "--help"],
            cwd=root / "software_app" / "crawlers" / "twitter", env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--scroll-pause", result.stdout)

    def test_twitter_script_help_runs_without_external_pythonpath(self):
        root = Path(__file__).resolve().parents[1]
        script_dir = root / "software_app" / "crawlers" / "twitter"
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "twitter_Crawler_2.py", "--help"],
            cwd=script_dir,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--no-convert-gif", result.stdout)
        self.assertIn("默认转换为 .gif", result.stdout)
        self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()

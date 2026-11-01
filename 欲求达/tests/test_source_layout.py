from __future__ import annotations

import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from software_app.core import settings


APP_ROOT = Path(__file__).resolve().parents[1]


class SourceLayoutTests(unittest.TestCase):
    def test_root_launcher_from_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            result = subprocess.run(
                [sys.executable, str(APP_ROOT.parent / "run_software.py"), "--help"],
                cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preview", result.stdout)

    def test_legacy_data_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data/software_app").mkdir(parents=True)
            with patch.object(settings, "PROJECT_ROOT", root / "欲求达"):
                with patch.dict(os.environ, {"YUQIUDA_HOME": ""}):
                    self.assertEqual(settings.runtime_root(frozen=False), root)
                with patch.dict(os.environ, {"YUQIUDA_HOME": str(root / "custom")}):
                    self.assertEqual(settings.runtime_root(frozen=False), (root / "custom").resolve())

    def test_standalone_checkout_uses_own_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "standalone"
            with patch.object(settings, "PROJECT_ROOT", root), patch.dict(os.environ, {"YUQIUDA_HOME": ""}):
                self.assertEqual(settings.runtime_root(frozen=False), root)

    def test_packaging_inputs_resolve_relative_to_spec(self):
        analysis = Mock(return_value=Mock(pure=[], scripts=[], binaries=[], datas=[]))
        exe = Mock()
        runpy.run_path(str(APP_ROOT / "欲求达.spec"), init_globals={
            "SPECPATH": str(APP_ROOT), "Analysis": analysis, "PYZ": Mock(), "EXE": exe,
        })
        args, kwargs = analysis.call_args
        self.assertEqual(Path(args[0][0]), APP_ROOT / "run_software.py")
        self.assertEqual(kwargs["pathex"], [str(APP_ROOT)])
        for source, _destination in kwargs["datas"]:
            self.assertTrue(Path(source).is_dir())
        self.assertTrue(Path(exe.call_args.kwargs["icon"]).is_file())

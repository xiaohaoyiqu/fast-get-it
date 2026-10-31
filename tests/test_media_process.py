from __future__ import annotations

from pathlib import Path
import subprocess
import hashlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from software_app.crawlers.pixiv import ugoira
from software_app.crawlers.bluesky import BlueskyClient
from software_app.core.media_process import run_media_process, STDERR_LIMIT
from software_app.core.adapter import TaskCancelled
from software_app.crawlers import common


class MediaProcessTests(unittest.TestCase):
    def test_download_pipeline_passes_its_control_and_cleans_only_working_source(self):
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / ".source.webm", Path(directory) / "output.mp4"
            output.write_bytes(b"previous")
            event = threading.Event()

            def download(_session, _url, path, *_args, **_kwargs):
                path.write_bytes(b"working source")

            with (
                patch.object(common, "download_file", side_effect=download),
                patch.object(common, "convert_media_file", side_effect=TaskCancelled("generated cancel")) as convert,
                self.assertRaises(TaskCancelled),
            ):
                common.download_media_file(
                    Mock(), "https://example.com/video", source, output, event, ".mp4", conflict_policy="overwrite"
                )
            self.assertIs(convert.call_args.kwargs["cancel_event"], event)
            self.assertEqual(output.read_bytes(), b"previous")
            self.assertFalse(source.exists())

    def test_browser_payload_conversion_passes_control_and_preserves_old_output(self):
        from types import SimpleNamespace
        from software_app.core.models import DownloadTask
        from software_app.crawlers.webpage import client

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "001-clip.mp4"
            output.write_bytes(b"previous")
            event = threading.Event()
            task = DownloadTask("test", "website", "https://example.com", root, {"file_conflict": "overwrite"})
            payload = SimpleNamespace(
                data=b"video", content_type="video/webm", final_url="https://example.com/clip.webm"
            )
            with (
                patch.object(client, "convert_media_file", side_effect=TaskCancelled("generated cancel")) as convert,
                self.assertRaises(TaskCancelled),
            ):
                client.WebPageCrawler._download_browser_payload(
                    payload,
                    client.MediaCandidate(payload.final_url, "video"),
                    root,
                    1,
                    100,
                    task,
                    task.target,
                    cancel_event=event,
                )
            self.assertIs(convert.call_args.kwargs["cancel_event"], event)
            self.assertEqual(output.read_bytes(), b"previous")

    def test_image_conversion_cancel_after_render_preserves_source_and_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.png", Path(directory) / "output.jpg"
            with common.Image.new("RGB", (4, 4), "blue") as image:
                image.save(source)
            output.write_bytes(b"previous")
            event = threading.Event()
            save = common.Image.Image.save

            def render(image, *args, **kwargs):
                save(image, *args, **kwargs)
                event.set()

            with patch.object(common.Image.Image, "save", render), self.assertRaises(TaskCancelled):
                common.convert_media_file(source, output, ".jpg", cancel_event=event)
            self.assertTrue(source.exists())
            self.assertEqual(output.read_bytes(), b"previous")
            self.assertFalse(output.with_name(output.name + ".convert.part").exists())

    def test_common_conversion_cancel_before_launch_keeps_source_and_old_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.webm"
            output = Path(directory) / "output.mp4"
            source.write_bytes(b"source")
            output.write_bytes(b"previous")
            event = threading.Event()
            event.set()
            with patch("subprocess.Popen") as launch, self.assertRaises(TaskCancelled):
                common.convert_media_file(source, output, ".mp4", cancel_event=event)
            launch.assert_not_called()
            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(output.read_bytes(), b"previous")

    def test_common_conversion_real_process_cancel_and_noisy_success_or_failure(self):
        real_popen = subprocess.Popen
        for mode in ("success", "failure", "cancel"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                source, output = Path(directory) / "source.webm", Path(directory) / "output.mp4"
                source.write_bytes(b"source")
                output.write_bytes(b"previous")
                event, started = threading.Event(), threading.Event()
                processes = []

                def launch(command, mode=mode, processes=processes, started=started, **kwargs):
                    code = (
                        "import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_bytes(b'converted');"
                        "sys.stderr.buffer.write(b'x'*1048576+b'\\nconversion failed\\n');sys.stderr.flush();"
                        + (
                            "time.sleep(30)"
                            if mode == "cancel"
                            else "sys.exit(" + ("0" if mode == "success" else "7") + ")"
                        )
                    )
                    process = real_popen([sys.executable, "-c", code, command[-1]], **kwargs)
                    processes.append(process)
                    started.set()
                    return process

                def cancel(started=started, event=event):
                    if started.wait(3):
                        event.set()

                timer = threading.Timer(0.3 if mode == "cancel" else 5, cancel)
                timer.start()
                try:
                    with (
                        patch.object(common, "find_ffmpeg", return_value="generated-engine"),
                        patch("subprocess.Popen", side_effect=launch),
                    ):
                        if mode == "success":
                            common.convert_media_file(source, output, ".mp4", cancel_event=event)
                        else:
                            with self.assertRaises(TaskCancelled if mode == "cancel" else RuntimeError):
                                common.convert_media_file(source, output, ".mp4", cancel_event=event)
                    self.assertEqual(output.read_bytes(), b"converted" if mode == "success" else b"previous")
                    self.assertEqual(source.exists(), mode != "success")
                    self.assertFalse(list(Path(directory).glob(".*.convert.mp4")))
                    self.assertTrue(processes)
                    self.assertTrue(all(p.poll() is not None and p.stderr.closed for p in processes))
                finally:
                    event.set()
                    timer.cancel()
                    timer.join()
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=3)

    def test_common_conversion_cancellation_after_engine_success_does_not_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.webm", Path(directory) / "output.mp4"
            source.write_bytes(b"source")
            output.write_bytes(b"previous")
            event = threading.Event()

            def engine(command, *_args, **_kwargs):
                Path(command[-1]).write_bytes(b"converted")
                event.set()
                return subprocess.CompletedProcess(command, 0, stderr="")

            with (
                patch.object(common, "find_ffmpeg", return_value="generated-engine"),
                patch.object(common, "run_media_process", side_effect=engine, create=True),
                self.assertRaises(TaskCancelled),
            ):
                common.convert_media_file(source, output, ".mp4", cancel_event=event)
            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(output.read_bytes(), b"previous")
            self.assertFalse(list(Path(directory).glob(".*.convert.mp4")))

    def test_common_conversion_timeout_keeps_old_output_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.webm", Path(directory) / "output.mp4"
            source.write_bytes(b"source")
            output.write_bytes(b"previous")

            def engine(command, *_args, **_kwargs):
                Path(command[-1]).write_bytes(b"partial")
                raise TimeoutError("generated engine timeout")

            with (
                patch.object(common, "find_ffmpeg", return_value="generated-engine"),
                patch.object(common, "run_media_process", side_effect=engine, create=True),
                self.assertRaises(TimeoutError),
            ):
                common.convert_media_file(source, output, ".mp4", cancel_event=threading.Event())
            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(output.read_bytes(), b"previous")
            self.assertFalse(list(Path(directory).glob(".*.convert.mp4")))

    def test_diagnostics_are_bounded_and_non_utf8_does_not_crash_reader(self):
        result = run_media_process(
            [sys.executable, "-c", "import sys;sys.stderr.buffer.write(b'x'*1048576+b'\\xffEND');sys.exit(7)"],
            threading.Event(),
            timeout=5,
        )
        self.assertEqual(result.returncode, 7)
        self.assertLessEqual(len(result.stderr), STDERR_LIMIT)
        self.assertTrue(result.stderr.endswith("END"))

    def test_timeout_and_cancellation_reap_process_and_close_stderr(self):
        real_popen = subprocess.Popen
        for mode in ("timeout", "cancel"):
            with self.subTest(mode=mode):
                processes = []

                def create(*args, processes=processes, **kwargs):
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    return process

                event = threading.Event()
                timer = threading.Timer(0.3, event.set)
                if mode == "cancel":
                    timer.start()
                try:
                    with patch("subprocess.Popen", side_effect=create):
                        with self.assertRaises(TimeoutError if mode == "timeout" else TaskCancelled):
                            run_media_process(
                                [sys.executable, "-c", "import time;time.sleep(30)"],
                                event,
                                timeout=0.3 if mode == "timeout" else 5,
                            )
                    self.assertIsNotNone(processes[0].poll())
                    self.assertTrue(processes[0].stderr.closed)
                finally:
                    timer.cancel()
                    if mode == "cancel":
                        timer.join()
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=3)

    def test_cancel_before_launch_does_not_create_process(self):
        event = threading.Event()
        event.set()
        with patch("subprocess.Popen") as create, self.assertRaises(TaskCancelled):
            run_media_process(["unused"], event)
        create.assert_not_called()

    def test_production_wrappers_drain_noisy_engine_before_waiting_for_exit(self):
        real_popen = subprocess.Popen
        for platform, success in (("pixiv", True), ("bluesky", True), ("pixiv", False), ("bluesky", False)):
            with self.subTest(platform=platform, success=success), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output.mp4"
                cancel = threading.Event()
                processes = []

                def fake_engine(command, processes=processes, success=success, **kwargs):
                    code = (
                        "import sys,pathlib; sys.stderr.buffer.write(b'x'*1048576+b'\\nengine failed\\n'); sys.stderr.flush(); pathlib.Path(sys.argv[1]).write_bytes(b'complete'); sys.exit("
                        + ("0" if success else "7")
                        + ")"
                    )
                    kwargs.pop("text", None)
                    process = real_popen([sys.executable, "-c", code, command[-1]], **kwargs)
                    processes.append(process)
                    return process

                watchdog = threading.Timer(3, cancel.set)
                watchdog.start()
                try:
                    with patch("subprocess.Popen", side_effect=fake_engine), self.subTest(stage="wrapper"):

                        def run(platform=platform, output=output, cancel=cancel):
                            if platform == "pixiv":
                                with patch.object(ugoira, "find_ffmpeg", return_value="fake-ffmpeg"):
                                    ugoira._gif_to_mp4(output.with_suffix(".gif"), output, cancel)
                            else:
                                plan = {
                                    "target": 1,
                                    "sequence": 0,
                                    "discontinuity_sequence": 0,
                                    "fingerprint": "f" * 64,
                                    "segments": [
                                        {
                                            "url": "https://example.com/segment.ts",
                                            "duration": 1.0,
                                            "map": None,
                                            "discontinuity": False,
                                        }
                                    ],
                                }

                                def resource(_session, _url, path, _old, _event, _validate):
                                    path.write_bytes(b"generated segment")
                                    return {
                                        "size": 17,
                                        "sha256": hashlib.sha256(b"generated segment").hexdigest(),
                                    }, False

                                with (
                                    patch("software_app.crawlers.bluesky.find_ffmpeg", return_value="fake-ffmpeg"),
                                    patch("software_app.core.hls_transfer.resolve_plan", return_value=plan),
                                    patch("software_app.core.hls_transfer._fetch_resource", side_effect=resource),
                                ):
                                    client = BlueskyClient()
                                    try:
                                        client._download_hls_locked("https://example.com/video", output, cancel)
                                    finally:
                                        client.session.close()

                        if success:
                            run()
                        else:
                            with self.assertRaisesRegex(RuntimeError, "engine failed"):
                                run()
                    if success:
                        self.assertEqual(output.read_bytes(), b"complete")
                    else:
                        self.assertFalse(output.exists())
                        self.assertFalse(output.with_name(output.name + ".part.mp4").exists())
                    self.assertTrue(all(process.stderr.closed for process in processes))
                finally:
                    watchdog.cancel()
                    watchdog.join()
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=3)
                        if process.stderr:
                            process.stderr.close()

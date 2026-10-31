"""HTTP recovery must verify the persisted prefix and discard uncommitted tails."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from software_app.core.http_transfer import download_http_file
from software_app.core import http_checkpoint as checkpoint, http_transfer as transfer
from software_app.core.adapter import TaskCancelled
from software_app.core.transfer_workspace import workspace_paths
from test_http_transfer import Response, Session


class OriginSession(Session):
    def __init__(self, data=b"abcdef"):
        super().__init__()
        self.data = data

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        headers = {"ETag": '"stable"'}
        byte_range = kwargs["headers"].get("Range")
        if byte_range:
            offset = int(byte_range.removeprefix("bytes=").removesuffix("-"))
            headers.update(
                {
                    "Content-Range": f"bytes {offset}-{len(self.data) - 1}/{len(self.data)}",
                    "Content-Length": str(len(self.data) - offset),
                }
            )
            return Response([self.data[offset:]], status=206, headers=headers)
        headers["Content-Length"] = str(len(self.data))
        return Response([self.data], headers=headers)


class HttpCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "media.bin"
        self.url = "https://media.example/video?signature=private"
        self.stop = threading.Event()

    def download(self, session, **kwargs):
        return download_http_file(
            session,
            self.url,
            self.path,
            self.stop,
            timeout=(1, 1),
            chunk_size=3,
            retries=0,
            validate_url=lambda _: None,
            **kwargs,
        )

    def interrupt(self):
        with self.assertRaises(requests.ConnectionError):
            self.download(
                Session(
                    Response(
                        [b"abc", requests.ConnectionError("interrupted")],
                        headers={"Content-Length": "6", "ETag": '"stable"'},
                    )
                )
            )
        _folder, self.part, self.state_path = workspace_paths(self.path)
        self.assertEqual(self.part.read_bytes(), b"abc")

    def test_same_length_corrupted_prefix_is_not_appended(self):
        self.interrupt()
        self.part.write_bytes(b"BAD")
        session = OriginSession()
        self.download(session)
        self.assertEqual(self.path.read_bytes(), b"abcdef")
        self.assertNotIn("Range", session.calls[0][1]["headers"])

    def test_uncommitted_tail_is_removed_before_resuming_verified_prefix(self):
        self.interrupt()
        with self.part.open("ab") as output:
            output.write(b"ZZ")
        session = OriginSession()
        self.download(session)
        self.assertEqual(self.path.read_bytes(), b"abcdef")
        self.assertEqual(session.calls[0][1]["headers"]["Range"], "bytes=3-")

    def test_graceful_interruption_saves_exact_prefix_digest(self):
        self.interrupt()
        text = self.state_path.read_text(encoding="utf-8")
        state = json.loads(text)
        self.assertEqual(state["version"], 3)
        self.assertEqual(state["checkpoint_size"], 3)
        self.assertEqual(state["sha256"], hashlib.sha256(b"abc").hexdigest())
        self.assertNotIn("private", text)

    def test_legacy_without_content_checkpoint_is_not_trusted(self):
        self.interrupt()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["version"] = 2
        state.pop("sha256", None)
        state.pop("checkpoint_size", None)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        session = OriginSession()
        self.download(session)
        self.assertNotIn("Range", session.calls[0][1]["headers"])

    def test_intact_prefix_resumes_and_removes_checkpoint(self):
        self.interrupt()
        session = OriginSession()
        self.download(session)
        self.assertEqual(session.calls[0][1]["headers"]["Range"], "bytes=3-")
        self.assertEqual(self.path.read_bytes(), b"abcdef")
        self.assertFalse(self.part.exists())
        self.assertFalse(self.state_path.exists())

    def test_shorter_than_checkpoint_restarts_without_range(self):
        self.interrupt()
        self.part.write_bytes(b"ab")
        session = OriginSession()
        self.download(session)
        self.assertNotIn("Range", session.calls[0][1]["headers"])
        self.assertEqual(self.path.read_bytes(), b"abcdef")

    def test_wrong_digest_restarts_without_range(self):
        self.interrupt()
        state = json.loads(self.state_path.read_text())
        state["sha256"] = "0" * 64
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        session = OriginSession()
        self.download(session)
        self.assertNotIn("Range", session.calls[0][1]["headers"])

    def test_invalid_checkpoint_fields_are_not_trusted(self):
        cases = [("checkpoint_size", value) for value in (0, -1, True, "3", 7)]
        cases += [("total", True), ("sha256", "bad"), ("sha256", None), ("etag", True)]
        for key, value in cases:
            with self.subTest(field=key, value=value):
                self.path.unlink(missing_ok=True)
                self.interrupt()
                state = json.loads(self.state_path.read_text())
                state[key] = value
                self.state_path.write_text(json.dumps(state), encoding="utf-8")
                session = OriginSession()
                self.download(session)
                self.assertNotIn("Range", session.calls[0][1]["headers"])
                self.assertEqual(self.path.read_bytes(), b"abcdef")

    def test_cancel_during_hash_makes_no_request_and_preserves_checkpoint(self):
        self.interrupt()
        before = self.state_path.read_bytes()
        session = OriginSession()

        def stop(_event):
            self.stop.set()
            return True

        with patch.object(checkpoint, "task_cancelled", side_effect=stop):
            with self.assertRaises(TaskCancelled):
                self.download(session)
        self.assertEqual(session.calls, [])
        self.assertEqual(self.part.read_bytes(), b"abc")
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_cache_changed_during_request_is_not_appended_or_published(self):
        self.interrupt()
        self.path.write_bytes(b"previous final")
        session = OriginSession()
        original = session.get

        def get(url, **kwargs):
            self.part.write_bytes(b"BAD")
            return original(url, **kwargs)

        session.get = get
        with self.assertRaisesRegex(ValueError, "缓存校验后发生变化"):
            self.download(session, overwrite=True)
        self.assertEqual(self.path.read_bytes(), b"previous final")
        self.assertEqual(self.part.read_bytes(), b"BAD")

    def test_failed_checkpoint_save_preserves_last_committed_prefix(self):
        original_save = transfer._save_state

        def save(path, state):
            if state["checkpoint_size"] > 3:
                raise PermissionError("state temporarily locked")
            original_save(path, state)

        with patch.object(checkpoint, "CHECKPOINT_BYTES", 3), patch.object(transfer, "_save_state", side_effect=save):
            with self.assertRaises(PermissionError):
                self.download(
                    Session(
                        Response(
                            [b"abc", b"de", requests.ConnectionError("interrupted")],
                            headers={"Content-Length": "6", "ETag": '"stable"'},
                        )
                    )
                )
        _folder, self.part, self.state_path = workspace_paths(self.path)
        self.assertEqual(json.loads(self.state_path.read_text())["checkpoint_size"], 3)
        self.assertEqual(self.part.read_bytes(), b"abcde")
        session = OriginSession()
        self.download(session)
        self.assertEqual(session.calls[0][1]["headers"]["Range"], "bytes=3-")
        self.assertEqual(self.path.read_bytes(), b"abcdef")

    def test_cancel_after_transfer_preserves_latest_prefix_digest(self):
        def chunks():
            yield b"abc"
            self.stop.set()
            yield b"def"

        with self.assertRaises(TaskCancelled):
            self.download(Session(Response(chunks(), headers={"Content-Length": "6", "ETag": '"stable"'})))
        _folder, part, state_path = workspace_paths(self.path)
        state = json.loads(state_path.read_text())
        self.assertEqual(state["checkpoint_size"], 3)
        self.assertEqual(state["sha256"], hashlib.sha256(part.read_bytes()).hexdigest())
        self.assertFalse(self.path.exists())

    def test_complete_checkpoint_survives_failed_final_commit(self):
        with self.assertRaises(PermissionError):
            self.download(
                Session(Response([b"abcdef"], headers={"Content-Length": "6", "ETag": '"stable"'})),
                commit=lambda *_args: (_ for _ in ()).throw(PermissionError("final busy")),
            )
        _folder, part, state_path = workspace_paths(self.path)
        state = json.loads(state_path.read_text())
        self.assertEqual(state["checkpoint_size"], 6)
        self.assertEqual(state["sha256"], hashlib.sha256(part.read_bytes()).hexdigest())
        self.assertFalse(self.path.exists())

    def test_abrupt_exit_keeps_only_last_periodic_checkpoint(self):
        class AbruptExit(BaseException):
            pass

        def chunks():
            yield b"abc"
            yield b"de"
            raise AbruptExit()

        with patch.object(checkpoint, "CHECKPOINT_BYTES", 3):
            with self.assertRaises(AbruptExit):
                self.download(Session(Response(chunks(), headers={"Content-Length": "6", "ETag": '"stable"'})))
        _folder, self.part, self.state_path = workspace_paths(self.path)
        self.assertEqual(self.part.read_bytes(), b"abcde")
        self.assertEqual(json.loads(self.state_path.read_text())["checkpoint_size"], 3)
        session = OriginSession()
        self.download(session)
        self.assertEqual(session.calls[0][1]["headers"]["Range"], "bytes=3-")
        self.assertEqual(self.path.read_bytes(), b"abcdef")

    def test_time_checkpoint_can_preserve_small_prefix_before_abrupt_exit(self):
        class AbruptExit(BaseException):
            pass

        def chunks():
            yield b"abc"
            raise AbruptExit()

        with patch.object(checkpoint, "CHECKPOINT_SECONDS", 0):
            with self.assertRaises(AbruptExit):
                self.download(Session(Response(chunks(), headers={"Content-Length": "6", "ETag": '"stable"'})))
        _folder, part, state_path = workspace_paths(self.path)
        self.assertEqual(json.loads(state_path.read_text())["checkpoint_size"], 3)
        self.assertEqual(part.read_bytes(), b"abc")

    def test_zero_received_bytes_does_not_create_a_resume_claim(self):
        with self.assertRaises(requests.ConnectionError):
            self.download(
                Session(
                    Response(
                        [requests.ConnectionError("interrupted")], headers={"Content-Length": "6", "ETag": '"stable"'}
                    )
                )
            )
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_oversized_metadata_is_not_loaded_as_trusted_state(self):
        self.interrupt()
        self.state_path.write_text(" " * (checkpoint.MAX_STATE_BYTES + 1), encoding="utf-8")
        session = OriginSession()
        self.download(session)
        self.assertNotIn("Range", session.calls[0][1]["headers"])

    def test_large_uncommitted_tail_does_not_discard_valid_prefix(self):
        self.interrupt()
        with self.part.open("ab") as output:
            output.write(b"uncommitted" * 20)
        session = OriginSession()
        self.download(session)
        self.assertEqual(session.calls[0][1]["headers"]["Range"], "bytes=3-")
        self.assertEqual(self.path.read_bytes(), b"abcdef")

    def test_corrupt_cache_is_not_kept_as_trusted_when_new_request_fails(self):
        self.interrupt()
        self.part.write_bytes(b"BAD")
        with self.assertRaises(requests.HTTPError):
            self.download(Session(Response(status=503)))
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.part.exists())

    def test_atomic_metadata_failure_keeps_previous_record(self):
        self.interrupt()
        before = self.state_path.read_bytes()
        state = json.loads(before)
        state["checkpoint_size"] = 4
        with patch.object(checkpoint.os, "replace", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                checkpoint.save_checkpoint(self.state_path, state)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(list(self.state_path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

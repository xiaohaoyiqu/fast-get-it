from __future__ import annotations

import multiprocessing
import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import requests

from software_app.core.adapter import TaskCancelled
from software_app.core.http_transfer import download_http_file
from software_app.core.transfer_workspace import workspace_paths
from software_app.core.output_lock import output_path_lock, output_paths_lock
from software_app.crawlers.common import download_media_file, convert_media_file
from software_app.core.storage import AppStorage
from software_app.core.bandwidth import MEDIA_BANDWIDTH
from software_app.core.network_policy import HTTP_BACKOFF
from software_app.crawlers.twitter import download_method as twitter_download


def _process_hold_lock(path, acquired, release):
    with output_path_lock(path):
        acquired.set()
        release.wait(15)


class Response:
    def __init__(self, chunks=(), *, status=200, headers=None):
        self.chunks = chunks
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def iter_content(self, **_kwargs):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return self.responses.pop(0)


class HttpTransferTests(unittest.TestCase):
    def setUp(self):
        HTTP_BACKOFF.clear()
        self.addCleanup(HTTP_BACKOFF.clear)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "video.mp4"
        self.cancel = threading.Event()
        self.url = "https://media.example/video?token=secret"

    def download(self, session, **kwargs):
        return download_http_file(session, self.url, self.path, self.cancel,
                                  timeout=(1, 1), chunk_size=3, validate_url=lambda _url: None,
                                  retries=0, **kwargs)

    def interrupted(self, **kwargs):
        response = Response([b"abc", requests.ConnectionError("interrupted")],
                            headers={"Content-Length": "6", "ETag": '"v1"', **kwargs})
        with self.assertRaises(requests.ConnectionError):
            self.download(Session(response))
        self.assertTrue(response.closed)
        self.assertEqual(workspace_paths(self.path)[1].read_bytes(), b"abc")
        self.assertNotIn("secret", workspace_paths(self.path)[2].read_text())

    def test_interrupted_download_resumes_with_if_range_and_cleans_state(self):
        self.interrupted()
        response = Response([b"def"], status=206, headers={
            "Content-Range": "bytes 3-5/6", "Content-Length": "3", "ETag": '"v1"',
        })
        session = Session(response)
        self.assertEqual(self.download(session), 6)
        self.assertEqual(self.path.read_bytes(), b"abcdef")
        self.assertEqual(session.calls[0][1]["headers"]["Range"], "bytes=3-")
        self.assertEqual(session.calls[0][1]["headers"]["If-Range"], '"v1"')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_http_media_chunks_are_charged_to_the_process_shared_budget(self):
        with patch.object(MEDIA_BANDWIDTH, "consume") as consume:
            self.download(Session(Response([b"abc", b"def"])))
        self.assertEqual([call.args[0] for call in consume.call_args_list], [3, 3])
        self.assertTrue(all(call.args[1] is self.cancel for call in consume.call_args_list))

    def interrupt_at(self, final_url):
        response = Response([b"old", requests.ConnectionError("interrupted")],
                            headers={"Content-Length": "6", "ETag": '"same"'})
        response.url = final_url
        with self.assertRaises(requests.ConnectionError):
            self.download(Session(response))

    def test_changed_final_resource_does_not_splice_matching_etag(self):
        for target in ("https://other.example/a", "https://cdn.example/b", "https://cdn.example/a?token=new"):
            with self.subTest(target=target):
                self.path.unlink(missing_ok=True)
                self.interrupt_at("https://cdn.example/a")
                partial = Response([b"NEW"], status=206, headers={
                    "Content-Range": "bytes 3-5/6", "Content-Length": "3", "ETag": '"same"'})
                partial.url = target
                full = Response([b"newNEW"], headers={"Content-Length": "6", "ETag": '"same"'})
                full.url = target
                session = Session(partial, full)
                self.download(session)
                self.assertEqual(self.path.read_bytes(), b"newNEW")
                self.assertEqual(len(session.calls), 2)
                self.assertNotIn("Range", session.calls[1][1]["headers"])
                self.assertTrue(partial.closed)

    def test_same_final_resource_still_resumes(self):
        target = "https://cdn.example/a?token=sensitive"
        self.interrupt_at(target)
        response = Response([b"NEW"], status=206, headers={
            "Content-Range": "bytes 3-5/6", "Content-Length": "3", "ETag": '"same"'})
        response.url = target
        session = Session(response)
        self.download(session)
        self.assertEqual(self.path.read_bytes(), b"oldNEW")
        self.assertEqual(session.calls[0][1]["headers"]["Range"], "bytes=3-")

    def test_final_resource_is_stored_as_fingerprint_without_signed_url(self):
        target = "https://cdn.example/a?token=sensitive"
        self.interrupt_at(target)
        content = workspace_paths(self.path)[2].read_text(encoding="utf-8")
        state = json.loads(content)
        self.assertEqual(state.get("final_key"), hashlib.sha256(target.encode()).hexdigest())
        self.assertNotIn("sensitive", content)
        self.assertNotIn("cdn.example", content)

    def test_owned_legacy_state_without_final_identity_restarts(self):
        self.interrupted()
        state_path = workspace_paths(self.path)[2]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.pop("final_key", None)
        state["version"] = 1
        state_path.write_text(json.dumps(state), encoding="utf-8")
        session = Session(Response([b"newNEW"], headers={"Content-Length": "6"}))
        self.download(session)
        self.assertNotIn("Range", session.calls[0][1]["headers"])
        self.assertEqual(self.path.read_bytes(), b"newNEW")

    def test_invalid_final_identity_is_not_used_for_range(self):
        for fingerprint in (None, 12, "bad", "z" * 64):
            with self.subTest(fingerprint=fingerprint):
                self.path.unlink(missing_ok=True)
                self.interrupted()
                state_path = workspace_paths(self.path)[2]
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["final_key"] = fingerprint
                state_path.write_text(json.dumps(state), encoding="utf-8")
                session = Session(Response([b"newNEW"], headers={"Content-Length": "6"}))
                self.download(session)
                self.assertNotIn("Range", session.calls[0][1]["headers"])

    def test_changed_final_target_returning_full_response_replaces_prefix(self):
        self.interrupt_at("https://cdn.example/a")
        full = Response([b"newNEW"], headers={"Content-Length": "6", "ETag": '"same"'})
        full.url = "https://cdn.example/b"
        session = Session(full)
        self.download(session)
        self.assertEqual(self.path.read_bytes(), b"newNEW")
        self.assertEqual(len(session.calls), 1)

    def test_repeated_unsolicited_partial_after_target_change_is_rejected(self):
        self.interrupt_at("https://cdn.example/a")
        responses = [Response([b"NEW"], status=206, headers={
            "Content-Range": "bytes 3-5/6", "Content-Length": "3", "ETag": '"same"'}) for _ in range(2)]
        for response in responses:
            response.url = "https://cdn.example/b"
        with self.assertRaises(ValueError):
            self.download(Session(*responses))
        self.assertFalse(self.path.exists())
        self.assertTrue(all(response.closed for response in responses))

    def test_final_target_change_within_network_retry_restarts_cleanly(self):
        first = Response([b"old", requests.ConnectionError("interrupted")], headers={"Content-Length": "6", "ETag": '"same"'})
        second = Response([b"NEW"], status=206, headers={"Content-Range": "bytes 3-5/6", "Content-Length": "3", "ETag": '"same"'})
        third = Response([b"newNEW"], headers={"Content-Length": "6", "ETag": '"same"'})
        first.url, second.url, third.url = "https://cdn.example/a", "https://cdn.example/b", "https://cdn.example/b"
        session = Session(first, second, third)
        with patch("software_app.core.http_transfer._retry_delay", return_value=0):
            download_http_file(session, self.url, self.path, self.cancel, timeout=(1, 1),
                               chunk_size=3, validate_url=lambda _: None, retries=1)
        self.assertEqual(self.path.read_bytes(), b"newNEW")
        self.assertNotIn("Range", session.calls[2][1]["headers"])

    def test_changed_resource_or_unsupported_range_restarts_without_appending(self):
        self.interrupted()
        self.download(Session(Response([b"NEW"], headers={"Content-Length": "3", "ETag": '"v2"'})))
        self.assertEqual(self.path.read_bytes(), b"NEW")

    def test_bad_range_or_validator_and_416_fall_back_to_full_request(self):
        cases = [
            Response(status=416),
            Response([b"def"], status=206, headers={"Content-Range": "bytes 0-2/6", "ETag": '"v1"'}),
            Response([b"def"], status=206, headers={"Content-Range": "bytes 3-5/6", "ETag": '"v2"'}),
        ]
        for response in cases:
            with self.subTest(status=response.status_code, headers=response.headers):
                self.path.unlink(missing_ok=True)
                self.interrupted()
                session = Session(response, Response([b"abcdef"], headers={"Content-Length": "6"}))
                self.download(session)
                self.assertEqual(self.path.read_bytes(), b"abcdef")
                self.assertNotIn("Range", session.calls[1][1]["headers"])
                self.assertTrue(response.closed)

    def test_unvalidated_or_disabled_partial_is_removed(self):
        for resume, headers in ((True, {}), (False, {"Content-Length": "6", "ETag": '"v1"'})):
            with self.subTest(resume=resume):
                with self.assertRaises(requests.ConnectionError):
                    self.download(Session(Response([b"abc", requests.ConnectionError()], headers=headers)), resume=resume)
                self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_short_body_is_not_published_and_can_resume(self):
        with self.assertRaises(requests.exceptions.ChunkedEncodingError):
            self.download(Session(Response([b"abc"], headers={"Content-Length": "6", "ETag": '"v1"'})))
        self.assertFalse(self.path.exists())
        self.assertTrue(workspace_paths(self.path)[1].exists())

    def test_oversize_body_does_not_leave_trusted_resume_state(self):
        with self.assertRaises(ValueError):
            self.download(Session(Response([b"abcdefg"], headers={"Content-Length": "6", "ETag": '"v1"'})))
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_cancel_keeps_validated_partial_and_does_not_publish(self):
        def chunks():
            yield b"abc"
            self.cancel.set()
            yield b"def"

        with self.assertRaises(TaskCancelled):
            self.download(Session(Response(chunks(), headers={"Content-Length": "6", "ETag": '"v1"'})))
        self.assertFalse(self.path.exists())
        self.assertEqual(workspace_paths(self.path)[1].read_bytes(), b"abc")

    def test_existing_file_requires_explicit_overwrite(self):
        self.path.write_bytes(b"original")
        session = Session(Response([b"replacement"]))
        with self.assertRaises(FileExistsError):
            self.download(session)
        self.assertFalse(session.calls)
        self.assertEqual(self.path.read_bytes(), b"original")
        self.download(session, overwrite=True)
        self.assertEqual(self.path.read_bytes(), b"replacement")

    def test_real_attachments_named_like_sidecars_are_preserved(self):
        part = self.path.with_name(self.path.name + ".part")
        state = self.path.with_name(self.path.name + ".resume")
        part.write_bytes(b"real attachment part")
        state.write_bytes(b"real attachment resume")
        self.download(Session(Response([b"downloaded"])))
        self.assertEqual(part.read_bytes(), b"real attachment part")
        self.assertEqual(state.read_bytes(), b"real attachment resume")

    def test_legacy_partial_pair_is_left_untouched_without_implicit_adoption(self):
        part = self.path.with_name(self.path.name + ".part")
        state = self.path.with_name(self.path.name + ".resume")
        part.write_bytes(b"abc")
        state.write_text(json.dumps({"version": 1, "url_key": hashlib.sha256(self.url.encode()).hexdigest(),
                                     "total": 6, "etag": '"v1"'}), encoding="utf-8")
        before = state.read_bytes()
        session = Session(Response([b"NEW"]))
        self.download(session)
        self.assertNotIn("Range", session.calls[0][1]["headers"])
        self.assertEqual(part.read_bytes(), b"abc")
        self.assertEqual(state.read_bytes(), before)

    def test_unowned_or_corrupted_workspace_is_never_overwritten(self):
        folder, part, state = workspace_paths(self.path)
        folder.mkdir()
        part.write_bytes(b"unrelated file")
        session = Session(Response([b"NEW"]))
        with self.assertRaises(FileExistsError):
            self.download(session)
        self.assertEqual(part.read_bytes(), b"unrelated file")
        self.assertFalse(session.calls)
        (folder / "owner.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.download(session)
        self.assertEqual(part.read_bytes(), b"unrelated file")

    def test_real_sidecar_target_can_download_concurrently_without_interference(self):
        barrier = threading.Barrier(2)

        def work(path, payload):
            def chunks():
                barrier.wait(timeout=5)
                yield payload
            return download_http_file(Session(Response(chunks())), self.url, path, threading.Event(),
                                      timeout=(1, 1), chunk_size=3, validate_url=lambda _url: None, retries=0)

        sibling = self.path.with_name(self.path.name + ".part")
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(work, self.path, b"video"), pool.submit(work, sibling, b"attachment")]
            self.assertEqual([job.result(timeout=10) for job in jobs], [5, 10])
        self.assertEqual(self.path.read_bytes(), b"video")
        self.assertEqual(sibling.read_bytes(), b"attachment")

    def test_output_inside_internal_workspace_is_rejected(self):
        folder, part, _state = workspace_paths(self.path)
        with self.assertRaises(ValueError):
            workspace_paths(folder / "real.jpg")

    def test_redirect_destination_is_validated_before_request(self):
        response = Response(status=302, headers={"Location": "http://127.0.0.1/private"})
        session = Session(response)

        def validate(url):
            if "127.0.0.1" in url:
                raise ValueError("private address")

        with self.assertRaisesRegex(ValueError, "private address"):
            download_http_file(session, self.url, self.path, self.cancel, timeout=(1, 1),
                               chunk_size=3, validate_url=validate, retries=0)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(response.closed)

    def test_cross_origin_redirect_does_not_forward_explicit_authentication(self):
        session = requests.Session()
        self.addCleanup(session.close)
        session.headers.update({"Cookie": "explicit=origin", "Proxy-Authorization": "Basic origin-proxy"})
        session.auth = ("origin-user", "origin-password")
        session.cookies.set("unscoped", "origin-only")
        session.cookies.set("origin", "scoped", domain="media.example", path="/")
        session.cookies.set("cdn", "allowed", domain="cdn.example", path="/")
        responses = [Response(status=302, headers={"Location": "https://cdn.example/video"}), Response([b"video"])]
        with patch.object(requests.Session, "send", side_effect=responses) as send:
            self.download(session)
        first, second = [call.args[0] for call in send.call_args_list]
        self.assertIn("Authorization", first.headers)
        self.assertNotIn("Authorization", second.headers)
        self.assertNotIn("Proxy-Authorization", second.headers)
        self.assertEqual(second.headers.get("Cookie"), "cdn=allowed")
        self.assertEqual(session.headers["Cookie"], "explicit=origin")
        self.assertEqual(session.cookies.get("unscoped"), "origin-only")
        self.assertTrue(all(response.closed for response in responses))

    def test_transient_error_retries_but_404_does_not(self):
        for status, calls in ((429, 2), (503, 2), (404, 1)):
            with self.subTest(status=status):
                self.path.unlink(missing_ok=True)
                session = Session(Response(status=status), Response([b"ok"]))
                with patch("software_app.core.http_transfer._retry_delay", return_value=0):
                    if status == 404:
                        with self.assertRaises(requests.HTTPError):
                            download_http_file(session, self.url, self.path, self.cancel, timeout=(1, 1),
                                               chunk_size=3, validate_url=lambda _: None, retries=2)
                    else:
                        download_http_file(session, self.url, self.path, self.cancel, timeout=(1, 1),
                                           chunk_size=3, validate_url=lambda _: None, retries=2)
                self.assertEqual(len(session.calls), calls)

    def test_stream_failure_resumes_within_the_same_download_call(self):
        session = Session(
            Response([b"abc", requests.ConnectionError("interrupted")], headers={
                "Content-Length": "6", "ETag": '"v1"',
            }),
            Response([b"def"], status=206, headers={
                "Content-Range": "bytes 3-5/6", "Content-Length": "3", "ETag": '"v1"',
            }),
        )
        with patch("software_app.core.http_transfer._retry_delay", return_value=0):
            download_http_file(session, self.url, self.path, self.cancel, timeout=(1, 1),
                               chunk_size=3, validate_url=lambda _: None, retries=1)
        self.assertEqual(self.path.read_bytes(), b"abcdef")
        self.assertEqual(session.calls[1][1]["headers"]["Range"], "bytes=3-")

    def test_retry_after_wait_can_be_cancelled_without_another_request(self):
        session = Session(Response(status=429, headers={"Retry-After": "120"}))
        with patch.object(self.cancel, "wait", return_value=True) as wait:
            with self.assertRaises(TaskCancelled):
                download_http_file(session, self.url, self.path, self.cancel, timeout=(1, 1),
                                   chunk_size=3, validate_url=lambda _: None, retries=1)
        wait.assert_called_once_with(120.0)
        self.assertEqual(len(session.calls), 1)

    def test_changed_url_does_not_reuse_old_partial(self):
        self.interrupted()
        self.url += "-changed"
        session = Session(Response([b"new"], headers={"Content-Length": "3"}))
        self.download(session)
        self.assertNotIn("Range", session.calls[0][1]["headers"])
        self.assertEqual(self.path.read_bytes(), b"new")

    def test_last_modified_can_validate_resume_without_a_strong_etag(self):
        modified = "Wed, 28 Oct 2026 00:00:00 GMT"
        self.interrupted(ETag='W/"weak"', **{"Last-Modified": modified})
        session = Session(Response([b"def"], status=206, headers={
            "Content-Range": "bytes 3-5/6", "Content-Length": "3", "Last-Modified": modified,
        }))
        self.download(session)
        self.assertEqual(session.calls[0][1]["headers"]["If-Range"], modified)
        self.assertEqual(self.path.read_bytes(), b"abcdef")

    def test_completed_part_survives_failed_commit(self):
        with patch("software_app.core.http_transfer.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.download(Session(Response([b"complete"])))
        self.assertEqual(workspace_paths(self.path)[1].read_bytes(), b"complete")
        self.assertFalse(self.path.exists())

    def test_parallel_media_tasks_reuse_only_after_conversion_finishes(self):
        session = Session(Response([b"video"]))
        source = self.path.with_name(".source.mp4")
        start = threading.Barrier(2)

        def work():
            start.wait(5)
            return download_media_file(session, self.url, source, self.path, self.cancel, ".mp4")

        with patch("software_app.crawlers.common.validate_external_url"), ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(work) for _ in range(2)]
            results = [job.result(timeout=10) for job in jobs]
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(sorted(results), [(5, False), (5, True)])
        self.assertEqual(self.path.read_bytes(), b"video")
        self.assertFalse(source.exists())

    def test_failed_ffmpeg_conversion_preserves_previous_destination(self):
        source = self.path.with_suffix(".webm")
        source.write_bytes(b"source")
        self.path.write_bytes(b"previous")

        def fail(command, *_args, **_kwargs):
            Path(command[-1]).write_bytes(b"partial conversion")
            return SimpleNamespace(returncode=1, stderr="conversion failed", stdout="")

        with patch("software_app.crawlers.common.find_ffmpeg", return_value="ffmpeg"), patch(
            "software_app.crawlers.common.run_media_process", side_effect=fail,
        ):
            with self.assertRaisesRegex(RuntimeError, "conversion failed"):
                convert_media_file(source, self.path, ".mp4")
        self.assertEqual(self.path.read_bytes(), b"previous")
        self.assertEqual(source.read_bytes(), b"source")
        self.assertFalse(list(self.path.parent.glob(".*.convert.mp4")))

    def test_library_does_not_index_transfer_or_conversion_intermediates(self):
        media_dir = self.path.parent / "media"
        media_dir.mkdir()
        for name in ("done.mp4", "video.mp4.part", "video.mp4.resume", "video.part.mp4",
                     ".page.source.jpg", ".video.uuid.convert.mp4"):
            (media_dir / name).write_bytes(b"data")
        internal = workspace_paths(media_dir / "interrupted.mp4")[0]
        internal.mkdir()
        (internal / "unexpected.jpg").write_bytes(b"internal")
        storage = AppStorage(self.path.parent / "library.sqlite3")
        records = storage.scan_media_files(media_dir, "website")
        self.assertEqual([record.path.name for record in records], ["done.mp4"])

    def test_twitter_video_retry_uses_shared_resume_and_task_network_config(self):
        first = Response([b"abc", requests.ConnectionError("interrupted")], headers={
            "Content-Length": "6", "ETag": '"v1"',
        })
        second = Response([b"def"], status=206, headers={
            "Content-Range": "bytes 3-5/6", "Content-Length": "3", "ETag": '"v1"',
        })
        results = []
        config = twitter_download.configure_downloads(max_retries=1, proxy_url="http://127.0.0.1:7890")
        url = "https://video.twimg.com/example.mp4"
        with patch.object(twitter_download, "validate_external_url"), patch.object(
            twitter_download.requests, "get", side_effect=[first, second],
        ) as request:
            for _ in range(2):
                twitter_download.download_video(url, self.path.parent, filename_base="renamed",
                                                on_result=lambda *args: results.append(args), network_config=config)
        self.assertEqual([row[1] for row in results], ["failed", "completed"])
        self.assertEqual((self.path.parent / "renamed.mp4").read_bytes(), b"abcdef")
        self.assertEqual(request.call_args_list[1].kwargs["headers"]["Range"], "bytes=3-")
        self.assertEqual(request.call_args_list[1].kwargs["proxies"]["https"], "http://127.0.0.1:7890")

    def test_parallel_twitter_videos_do_not_share_an_active_part_file(self):
        start = threading.Barrier(2)
        results = []

        def work():
            start.wait(5)
            twitter_download.download_video("https://video.twimg.com/shared.mp4", self.path.parent,
                                            filename_base="shared", on_result=lambda *args: results.append(args))

        with patch.object(twitter_download, "validate_external_url"), patch.object(
            twitter_download.requests, "get", return_value=Response([b"complete"]),
        ) as request, ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(work) for _ in range(2)]
            for job in jobs:
                job.result(timeout=10)
        request.assert_called_once()
        self.assertEqual(sorted(row[1] for row in results), ["completed", "skipped"])
        self.assertEqual((self.path.parent / "shared.mp4").read_bytes(), b"complete")


class OutputLockTests(unittest.TestCase):
    def test_empty_lock_file_enforces_exclusion_without_initialization_writes(self):
        from software_app.core import output_lock

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "locks"
            target = Path(directory) / "media.mp4"
            with patch.object(output_lock, "_lock_root", return_value=root):
                with output_path_lock(target):
                    lock_path, = root.glob("*.lock")
                    self.assertEqual(lock_path.stat().st_size, 0)
                    with lock_path.open("r+b") as contender:
                        with self.assertRaises(OSError):
                            output_lock._try_kernel_lock(contender)
                # Closing/releasing the owner allows another acquisition while
                # the lock file remains empty and keeps its inode.
                with output_path_lock(target):
                    self.assertEqual(lock_path.stat().st_size, 0)

    def test_waiting_thread_can_cancel_without_releasing_owner_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            stop, started = threading.Event(), threading.Event()
            errors = []

            def waiter():
                started.set()
                try:
                    with output_path_lock(path, stop):
                        errors.append("unexpected acquisition")
                except TaskCancelled:
                    errors.append("cancelled")

            with output_path_lock(path):
                # Reentrant group locks also deduplicate aliases.
                with output_paths_lock([path, path.parent / "." / path.name]):
                    thread = threading.Thread(target=waiter)
                    thread.start()
                    self.assertTrue(started.wait(5))
                    stop.set()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, ["cancelled"])

    def test_kernel_releases_cross_process_lock_after_process_exit(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "media.mp4")
            acquired, release = context.Event(), context.Event()
            process = context.Process(target=_process_hold_lock, args=(path, acquired, release))
            process.start()
            try:
                self.assertTrue(acquired.wait(10))
                stop = threading.Event()
                timer = threading.Timer(0.2, stop.set)
                timer.start()
                try:
                    with self.assertRaises(TaskCancelled), output_path_lock(path, stop):
                        pass
                finally:
                    timer.join(5)
                process.terminate()
                process.join(5)
                self.assertFalse(process.is_alive())
                deadline = threading.Event()
                timer = threading.Timer(3, deadline.set)
                timer.start()
                try:
                    with output_path_lock(path, deadline):
                        pass
                finally:
                    timer.cancel()
            finally:
                if process.is_alive():
                    process.terminate()
                process.join(5)

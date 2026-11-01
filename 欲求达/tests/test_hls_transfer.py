from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests

from software_app.core.adapter import TaskCancelled
from software_app.core.hls_playlist import parse_playlist, UnsupportedHlsError
from software_app.core.hls_transfer import download_hls
from software_app.core.transfer_workspace import workspace_paths
from software_app.crawlers.common import validate_external_url


BASE = "https://video.example/playlist.m3u8"
MEDIA = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:3\n#EXTINF:1.5,\na.ts\n#EXTINF:1.5,\nb.ts\n#EXT-X-ENDLIST\n"


def ts_bytes(payload):
    # Generated framing fixture; actual codec data is exercised by the FFmpeg smoke.
    return b"\x47\x01\xff\x10" + payload.ljust(184, b"\xff")


def mp4_box(kind, payload=b"generated"):
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


class HlsParserTests(unittest.TestCase):
    def test_vod_map_sequence_and_discontinuity(self):
        text = MEDIA.replace("#EXTINF:1.5,\na.ts", '#EXT-X-MAP:URI="init.mp4"\n#EXTINF:1.5,\na.m4s')
        text = text.replace("#EXTINF:1.5,\nb.ts", "#EXT-X-DISCONTINUITY\n#EXTINF:1.5,\nb.m4s")
        plan = parse_playlist(text, BASE)
        self.assertEqual(plan["sequence"], 3)
        self.assertEqual(plan["segments"][0]["map"], "https://video.example/init.mp4")
        self.assertTrue(plan["segments"][1]["discontinuity"])

    def test_master_chooses_highest_bandwidth_and_handles_quoted_codec_comma(self):
        text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="avc1,mp4a"\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2\nhigh.m3u8\n'
        self.assertEqual(parse_playlist(text, BASE), {"variant": "https://video.example/high.m3u8"})

    def test_external_audio_is_not_silently_omitted(self):
        text = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=2,AUDIO="a"\nvideo.m3u8\n'
        with self.assertRaises(UnsupportedHlsError):
            parse_playlist(text, BASE)

    def test_live_encrypted_and_unsupported_layouts_are_rejected(self):
        cases = [
            MEDIA.replace("#EXT-X-ENDLIST", ""),
            MEDIA.replace("#EXTINF", '#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF', 1),
        ]
        cases.extend(
            MEDIA.replace("#EXTINF", tag + "\n#EXTINF", 1)
            for tag in (
                "#EXT-X-BYTERANGE:10@0",
                '#EXT-X-MAP:URI="init.mp4",BYTERANGE="10@0"',
                "#EXT-X-PART:DURATION=1,URI=part.ts",
                "#EXT-X-GAP",
                "#EXT-X-DEFINE:NAME=x,VALUE=y",
            )
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(UnsupportedHlsError):
                parse_playlist(text, BASE)

    def test_bad_structure_duration_and_unsafe_uris_are_rejected(self):
        cases = [
            "<html>not HLS</html>",
            MEDIA.replace("1.5", "nan"),
            MEDIA.replace("a.ts", "file:///secret"),
            MEDIA.replace("a.ts", "http://video.example/a.ts"),
            MEDIA.replace("a.ts", "https://user:pass@video.example/a.ts"),
            MEDIA.replace("a.ts", 'bad"name.ts'),
            MEDIA.replace("#EXT-X-ENDLIST", "#EXT-X-ENDLIST\nextra.ts"),
            MEDIA.replace("#EXT-X-TARGETDURATION:2", "#EXT-X-TARGETDURATION:0"),
            MEDIA.replace("#EXTINF:1.5,\na.ts", "#EXTINF:1.5,\n#EXTINF:1.5,\na.ts"),
        ]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_playlist(text, BASE)


class Response:
    def __init__(self, url, data=b"", status=200, headers=None):
        self.url, self.data, self.status_code = url, data, status
        self.headers = {"Content-Length": str(len(data)), **(headers or {})}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("generated HTTP error", response=self)

    def iter_content(self, **_kwargs):
        yield self.data

    def close(self):
        self.closed = True


class HlsTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "video.mp4"
        self.session = requests.Session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)
        self.event = threading.Event()
        self.contents = {
            BASE: MEDIA.encode(),
            "https://video.example/a.ts": ts_bytes(b"first-segment"),
            "https://video.example/b.ts": ts_bytes(b"second-segment"),
        }
        self.validators = True
        self.calls, self.responses, self.muxes = [], [], []
        self.blocked = set()
        self.mux_failure = False
        # The anonymous transport is a shallow Session view. Patch the type's
        # send rather than only this instance to retain public credential checks.
        self.send_patch = patch("requests.Session.send", side_effect=self.send)
        self.send_patch.start()
        self.addCleanup(self.send_patch.stop)
        mux = patch("software_app.core.hls_transfer.run_media_process", side_effect=self.mux)
        mux.start()
        self.addCleanup(mux.stop)

    def send(self, request, **_kwargs):
        self.calls.append((request.url, dict(request.headers)))
        self.assertNotIn("Cookie", request.headers)
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("X-Api-Key", request.headers)
        if request.url in self.blocked:
            response = Response(request.url, status=404)
        else:
            data = self.contents[request.url]
            etag = '"' + hashlib.sha256(data).hexdigest() + '"'
            cached = self.validators and request.headers.get("If-None-Match") == etag
            response = Response(
                request.url, b"" if cached else data, 304 if cached else 200, {"ETag": etag} if self.validators else {}
            )
        self.responses.append(response)
        return response

    def mux(self, command, _event, **_kwargs):
        self.muxes.append(command)
        playlist = Path(command[command.index("-i") + 1]).read_text(encoding="utf-8")
        self.assertNotIn("https://", playlist)
        self.assertIn("file", command)
        Path(command[-1]).write_bytes(b"generated mp4")
        return subprocess.CompletedProcess(
            command, 1 if self.mux_failure else 0, stderr="generated mux error" if self.mux_failure else ""
        )

    def download(self, **kwargs):
        return download_hls(
            self.session,
            BASE,
            self.destination,
            self.event,
            ffmpeg="fake-ffmpeg",
            validate_url=lambda _url: None,
            **kwargs,
        )

    def interrupt(self):
        def progress(done, _total, _reused):
            if done == 1:
                self.event.set()

        with self.assertRaises(TaskCancelled):
            self.download(progress=progress)
        self.event.clear()
        self.assertFalse(self.destination.exists())

    def test_cancel_then_resume_conditionally_reuses_completed_segment(self):
        self.interrupt()
        result = self.download()
        self.assertEqual(result["reused"], 1)
        self.assertEqual(len(self.muxes), 1)
        self.assertEqual(list(self.root.iterdir()), [self.destination])
        self.assertTrue(all(response.closed for response in self.responses))

    def test_corrupt_local_segment_is_downloaded_instead_of_trusted(self):
        self.interrupt()
        folder = workspace_paths(self.destination, kind="hls")[0]
        (folder / "r000000.ts").write_bytes(b"corrupted")
        self.assertEqual(self.download()["reused"], 0)
        self.assertNotIn("If-None-Match", [headers for url, headers in self.calls if url.endswith("/a.ts")][-1])

    def test_missing_validators_do_not_reuse_completed_bytes(self):
        self.validators = False
        self.interrupt()
        self.assertEqual(self.download()["reused"], 0)
        self.assertEqual(sum(url.endswith("/a.ts") for url, _headers in self.calls), 2)

    def test_changed_remote_segment_restarts_entire_generation(self):
        self.interrupt()
        self.contents["https://video.example/a.ts"] = ts_bytes(b"new generation")
        result = self.download()
        self.assertEqual(result["reused"], 0)
        self.assertEqual(sum(url == BASE for url, _headers in self.calls), 3)
        self.assertEqual(len(self.muxes), 1)

    def test_changed_playlist_or_signed_url_does_not_mix_old_cache(self):
        self.interrupt()
        self.contents[BASE] = MEDIA.replace("a.ts", "a.ts?token=new").encode()
        self.contents["https://video.example/a.ts?token=new"] = ts_bytes(b"first-segment")
        self.assertEqual(self.download()["reused"], 0)
        folder = workspace_paths(self.destination, kind="hls")[0]
        self.assertFalse(folder.exists())

    def test_network_error_keeps_prior_segment_and_does_not_publish(self):
        self.blocked.add("https://video.example/b.ts")
        with self.assertRaises(requests.HTTPError):
            self.download()
        self.assertFalse(self.destination.exists())
        self.blocked.clear()
        self.assertEqual(self.download()["reused"], 1)

    def test_mux_failure_retains_segments_and_previous_destination(self):
        self.destination.write_bytes(b"old complete file")
        self.mux_failure = True
        with self.assertRaisesRegex(RuntimeError, "mux error"):
            self.download()
        self.assertEqual(self.destination.read_bytes(), b"old complete file")
        self.mux_failure = False
        self.assertEqual(self.download()["reused"], 2)

    def test_failed_final_commit_reuses_assembled_mp4_after_resource_validation(self):
        with patch("software_app.core.hls_transfer.replace_output", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.download()
        self.assertEqual(self.download()["reused"], 2)
        self.assertEqual(len(self.muxes), 1)

    def test_public_session_drops_credentials_and_does_not_mutate_source(self):
        self.session.headers.update({"Authorization": "secret", "Cookie": "secret", "X-Api-Key": "secret"})
        self.session.auth = ("user", "password")
        self.session.cookies.set("secret", "value", domain="video.example")
        self.download()
        self.assertEqual(self.session.headers["Authorization"], "secret")
        self.assertTrue(list(self.session.cookies))

    def test_master_selection_and_map_are_downloaded_and_rewritten_locally(self):
        high = "https://video.example/high.m3u8"
        self.contents[BASE] = (
            b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2\nhigh.m3u8\n"
        )
        self.contents[high] = MEDIA.replace(
            "#EXTINF:1.5,\na.ts", '#EXT-X-MAP:URI="init.mp4"\n#EXTINF:1.5,\na.ts'
        ).encode()
        self.contents["https://video.example/init.mp4"] = mp4_box(b"ftyp", b"iso6\0\0\0\0") + mp4_box(b"moov")
        for name in ("a.ts", "b.ts"):
            self.contents["https://video.example/" + name] = mp4_box(b"moof") + mp4_box(b"mdat")
        self.assertEqual(self.download()["resources"], 3)
        self.assertFalse(any("low.m3u8" in url for url, _headers in self.calls))
        self.assertTrue(any("init.mp4" in url for url, _headers in self.calls))

    def test_private_segment_is_rejected_before_request(self):
        self.contents[BASE] = MEDIA.replace("a.ts", "https://127.0.0.1/a.ts").encode()
        with patch("software_app.crawlers.common._resolve_host_ips", return_value=["93.184.216.34"]):
            with self.assertRaises(ValueError):
                download_hls(
                    self.session, BASE, self.destination, self.event, ffmpeg="fake", validate_url=validate_external_url
                )
        self.assertEqual([url for url, _headers in self.calls], [BASE])

    def test_master_cycle_is_bounded(self):
        self.contents[BASE] = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nplaylist.m3u8\n"
        with self.assertRaisesRegex(ValueError, "循环"):
            self.download()
        self.assertEqual(len(self.calls), 1)

    def test_unknown_workspace_and_legacy_sidecar_are_not_overwritten(self):
        legacy = self.destination.with_name(self.destination.name + ".part.mp4")
        legacy.write_bytes(b"real attachment")
        self.download()
        self.assertEqual(legacy.read_bytes(), b"real attachment")
        folder = workspace_paths(self.destination, kind="hls")[0]
        folder.mkdir()
        (folder / "note.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.download()

    def test_resume_state_does_not_persist_signed_urls(self):
        self.contents[BASE] = MEDIA.replace("a.ts", "a.ts?token=do-not-store").encode()
        self.contents["https://video.example/a.ts?token=do-not-store"] = ts_bytes(b"first-segment")
        self.interrupt()
        folder = workspace_paths(self.destination, kind="hls")[0]
        text = (folder / "state.json").read_text(encoding="utf-8")
        self.assertNotIn("do-not-store", text)
        self.assertNotIn("https://", text)

    def test_changed_resource_with_corrupt_local_copy_also_resets_generation(self):
        self.interrupt()
        folder = workspace_paths(self.destination, kind="hls")[0]
        (folder / "r000000.ts").write_bytes(b"corrupt")
        self.contents["https://video.example/a.ts"] = ts_bytes(b"new generation")
        self.assertEqual(self.download()["reused"], 0)
        self.assertEqual(sum(url == BASE for url, _headers in self.calls), 3)

    def test_corrupt_assembled_output_is_remuxed_after_failed_commit(self):
        with patch("software_app.core.hls_transfer.replace_output", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.download()
        folder = workspace_paths(self.destination, kind="hls")[0]
        (folder / "output.mp4").write_bytes(b"corrupt output")
        self.assertEqual(self.download()["reused"], 2)
        self.assertEqual(len(self.muxes), 2)

    def test_segment_downloads_use_shared_http_bandwidth_budget(self):
        with patch("software_app.core.bandwidth.MEDIA_BANDWIDTH.consume") as consume:
            self.download()
        self.assertEqual(sum(call.args[0] for call in consume.call_args_list), 2 * 188)

    def test_oversized_resource_is_refused_before_publication(self):
        original = self.send

        def send(request, **kwargs):
            response = original(request, **kwargs)
            if request.url.endswith("a.ts"):
                response.headers["Content-Length"] = str(512 * 1024 * 1024 + 1)
            return response

        with patch("requests.Session.send", side_effect=send), self.assertRaisesRegex(ValueError, "超限"):
            self.download()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.muxes, [])

    def test_redirect_to_private_host_is_validated_before_request(self):
        original = self.send

        def send(request, **kwargs):
            if request.url.endswith("a.ts"):
                self.calls.append((request.url, dict(request.headers)))
                response = Response(request.url, status=302, headers={"Location": "https://127.0.0.1/private.ts"})
                self.responses.append(response)
                return response
            return original(request, **kwargs)

        with (
            patch("requests.Session.send", side_effect=send),
            patch("software_app.crawlers.common._resolve_host_ips", return_value=["93.184.216.34"]),
            self.assertRaises(ValueError),
        ):
            download_hls(
                self.session, BASE, self.destination, self.event, ffmpeg="fake", validate_url=validate_external_url
            )
        self.assertFalse(any("127.0.0.1" in url for url, _headers in self.calls))

    def test_unsolicited_not_modified_response_never_creates_empty_segment(self):
        original = self.send

        def send(request, **kwargs):
            if request.url.endswith("a.ts"):
                self.calls.append((request.url, dict(request.headers)))
                response = Response(request.url, status=304)
                self.responses.append(response)
                return response
            return original(request, **kwargs)

        with patch("requests.Session.send", side_effect=send), self.assertRaises(ValueError):
            self.download()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.muxes, [])

    def test_unsolicited_304_with_validators_retries_without_attribute_error(self):
        original = self.send
        for validators in ({"ETag": '"unexpected"'}, {"Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"}):
            with self.subTest(validators=validators):
                sent = []

                def send(request, sent=sent, validators=validators, **kwargs):
                    if request.url.endswith("a.ts"):
                        sent.append(dict(request.headers))
                        if len(sent) == 1:
                            response = Response(request.url, status=304, headers=validators)
                            self.responses.append(response)
                            return response
                    return original(request, **kwargs)

                with patch("requests.Session.send", side_effect=send):
                    self.download()
                self.assertEqual(len(sent), 2)
                self.assertTrue(
                    all("If-None-Match" not in headers and "If-Modified-Since" not in headers for headers in sent)
                )
                self.assertTrue(all(response.closed for response in self.responses))

    def test_http_200_error_body_and_truncated_ts_never_publish_partial_video(self):
        for payload in (b"<html>upstream unavailable</html>", ts_bytes(b"truncated")[:-1], b"x" * 188):
            with self.subTest(payload=payload[:20]):
                self.destination.write_bytes(b"previous complete video")
                self.contents["https://video.example/b.ts"] = payload
                with self.assertRaisesRegex(ValueError, "TS 分片"):
                    self.download()
                self.assertEqual(self.destination.read_bytes(), b"previous complete video")
                self.assertEqual(self.muxes, [])
                folder = workspace_paths(self.destination, kind="hls")[0]
                self.assertTrue((folder / "r000000.ts").is_file())
                self.assertFalse((folder / "r000001.ts").exists())
                self.assertFalse(list(folder.glob("*.part")))

    def test_bad_fmp4_initialization_and_truncated_boxes_are_rejected(self):
        self.contents[BASE] = MEDIA.replace("#EXTINF", '#EXT-X-MAP:URI="init.mp4"\n#EXTINF', 1).encode()
        initialization = mp4_box(b"ftyp", b"iso6\0\0\0\0") + mp4_box(b"moov")
        fragment = mp4_box(b"moof") + mp4_box(b"mdat")
        cases = (
            (b"<html>error</html>", fragment),
            (mp4_box(b"ftyp", b"iso6\0\0\0\0"), fragment),
            (initialization, fragment[:-1]),
            (initialization, mp4_box(b"moof")),
            (initialization, mp4_box(b"mdat")),
        )
        for init, segment in cases:
            with self.subTest(initialization=init, segment=segment):
                self.contents["https://video.example/init.mp4"] = init
                self.contents["https://video.example/a.ts"] = segment
                self.contents["https://video.example/b.ts"] = fragment
                with self.assertRaisesRegex(ValueError, "MP4"):
                    self.download()
                self.assertFalse(self.destination.exists())
                self.assertEqual(self.muxes, [])

    def test_matching_hash_for_legacy_bad_cache_does_not_make_it_reusable(self):
        self.interrupt()
        folder = workspace_paths(self.destination, kind="hls")[0]
        payload = b"<html>old error response</html>"
        self.contents["https://video.example/a.ts"] = payload
        (folder / "r000000.ts").write_bytes(payload)
        metadata = folder / "r000000.ts.json"
        record = json.loads(metadata.read_text(encoding="utf-8"))
        record.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        metadata.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "TS 分片"):
            self.download()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.muxes, [])
        headers = [headers for url, headers in self.calls if url.endswith("/a.ts")][-1]
        self.assertNotIn("If-None-Match", headers)

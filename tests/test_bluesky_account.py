from __future__ import annotations

import unittest

from software_app.core.bluesky_account import BlueskyAccountManager


class Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(str(self.status_code))

    def json(self) -> dict:
        return self.payload


class Session:
    def __init__(self, following_uri: str = "") -> None:
        self.following_uri = following_uri
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs) -> Response:
        self.calls.append((method, url, kwargs))
        if url.endswith("com.atproto.server.createSession"):
            return Response({
                "did": "did:plc:owner",
                "accessJwt": "temporary-token",
                "didDoc": {"service": [{
                    "type": "AtprotoPersonalDataServer",
                    "serviceEndpoint": "https://bsky.social",
                }]},
            })
        if url.endswith("app.bsky.actor.getProfile"):
            return Response({"did": "did:plc:target", "handle": "target.bsky.social"})
        if url.endswith("app.bsky.graph.getRelationships"):
            return Response({"relationships": [{
                "did": "did:plc:target", "following": self.following_uri,
            }]})
        if url.endswith("com.atproto.repo.createRecord"):
            return Response({"uri": "at://did:plc:owner/app.bsky.graph.follow/new", "cid": "cid"})
        if url.endswith("com.atproto.repo.deleteRecord"):
            return Response({})
        raise AssertionError(url)


class BlueskyAccountTests(unittest.TestCase):
    def test_follow_uses_ephemeral_login_and_official_follow_record(self) -> None:
        session = Session()
        result = BlueskyAccountManager("owner.bsky.social", "app-password", session=session).set_following(
            "target.bsky.social", True
        )
        self.assertTrue(result["changed"])
        create = next(call for call in session.calls if call[1].endswith("com.atproto.repo.createRecord"))
        self.assertEqual(create[2]["json"]["collection"], "app.bsky.graph.follow")
        self.assertEqual(create[2]["json"]["record"]["subject"], "did:plc:target")
        self.assertEqual(create[2]["headers"]["Authorization"], "Bearer temporary-token")

    def test_unfollow_deletes_only_the_existing_follow_record(self) -> None:
        uri = "at://did:plc:owner/app.bsky.graph.follow/existing-rkey"
        session = Session(uri)
        result = BlueskyAccountManager("owner.bsky.social", "app-password", session=session).set_following(
            "did:plc:target", False
        )
        self.assertTrue(result["changed"])
        delete = next(call for call in session.calls if call[1].endswith("com.atproto.repo.deleteRecord"))
        self.assertEqual(delete[2]["json"]["rkey"], "existing-rkey")

    def test_noop_does_not_create_duplicate_follow_record(self) -> None:
        uri = "at://did:plc:owner/app.bsky.graph.follow/existing-rkey"
        session = Session(uri)
        result = BlueskyAccountManager("owner.bsky.social", "app-password", session=session).set_following(
            "did:plc:target", True
        )
        self.assertFalse(result["changed"])
        self.assertFalse(any(call[1].endswith("repo.createRecord") for call in session.calls))


if __name__ == "__main__":
    unittest.main()

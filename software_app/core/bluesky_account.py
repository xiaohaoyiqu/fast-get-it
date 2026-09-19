from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from software_app.crawlers.common import make_session


PUBLIC_APPVIEW = "https://public.api.bsky.app"
DEFAULT_PDS = "https://bsky.social"


class BlueskyAccountManager:
    """Ephemeral authenticated Bluesky follow management; credentials are never persisted."""

    def __init__(self, identifier: str, app_password: str, *, proxy_url: str = "",
                 session: requests.Session | None = None) -> None:
        self.identifier = str(identifier or "").strip()
        self.app_password = str(app_password or "").strip()
        if not self.identifier or not self.app_password:
            raise ValueError("请输入 Bluesky 账号和应用密码")
        self.session = session or make_session(referer="https://bsky.app/", proxy_url=proxy_url)
        self.did = ""
        self.token = ""
        self.pds = DEFAULT_PDS

    def _json(self, method: str, url: str, **kwargs) -> dict:
        response = self.session.request(method, url, timeout=(10, 30), **kwargs)
        if response.status_code in {401, 403}:
            raise PermissionError("Bluesky 认证失败；请检查账号、应用密码或登录权限")
        if response.status_code == 429:
            raise RuntimeError("Bluesky 请求过于频繁，请稍后重试")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Bluesky 接口没有返回对象")
        if payload.get("error"):
            raise RuntimeError(str(payload.get("message") or payload["error"]))
        return payload

    def login(self) -> None:
        payload = self._json(
            "POST",
            f"{DEFAULT_PDS}/xrpc/com.atproto.server.createSession",
            json={"identifier": self.identifier, "password": self.app_password},
            headers={"Accept": "application/json"},
        )
        self.did = str(payload.get("did") or "")
        self.token = str(payload.get("accessJwt") or "")
        if not self.did.startswith(("did:plc:", "did:web:")) or not self.token:
            raise PermissionError("Bluesky 登录未返回有效 DID 或访问令牌")
        did_doc = payload.get("didDoc") if isinstance(payload.get("didDoc"), dict) else {}
        for service in did_doc.get("service") or []:
            if not isinstance(service, dict) or service.get("type") != "AtprotoPersonalDataServer":
                continue
            endpoint = str(service.get("serviceEndpoint") or "").rstrip("/")
            parsed = urlparse(endpoint)
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                self.pds = endpoint
                break

    def _auth_headers(self) -> dict[str, str]:
        if not self.token:
            self.login()
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def resolve_did(self, actor: str) -> str:
        value = str(actor or "").strip().lstrip("@")
        if value.startswith(("did:plc:", "did:web:")):
            return value
        payload = self._json(
            "GET", f"{PUBLIC_APPVIEW}/xrpc/app.bsky.actor.getProfile", params={"actor": value}
        )
        did = str(payload.get("did") or "")
        if not did.startswith(("did:plc:", "did:web:")):
            raise ValueError("Bluesky 目标账号没有返回有效 DID")
        return did

    def following_uri(self, target_did: str) -> str:
        payload = self._json(
            "GET",
            f"{self.pds}/xrpc/app.bsky.graph.getRelationships",
            params={"actor": self.did, "others": target_did},
            headers=self._auth_headers(),
        )
        relationships = payload.get("relationships")
        if not isinstance(relationships, list) or not relationships:
            return ""
        row = relationships[0] if isinstance(relationships[0], dict) else {}
        uri = str(row.get("following") or "")
        return uri if uri.startswith(f"at://{self.did}/app.bsky.graph.follow/") else ""

    def set_following(self, actor: str, desired: bool) -> dict[str, object]:
        self.login()
        target_did = self.resolve_did(actor)
        if target_did == self.did:
            raise ValueError("不能关注自己的 Bluesky 账号")
        existing = self.following_uri(target_did)
        if desired and existing:
            return {"changed": False, "following": True, "target_did": target_did}
        if not desired and not existing:
            return {"changed": False, "following": False, "target_did": target_did}
        if desired:
            self._json(
                "POST",
                f"{self.pds}/xrpc/com.atproto.repo.createRecord",
                json={
                    "repo": self.did,
                    "collection": "app.bsky.graph.follow",
                    "record": {
                        "$type": "app.bsky.graph.follow",
                        "subject": target_did,
                        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    },
                },
                headers=self._auth_headers(),
            )
        else:
            rkey = existing.rsplit("/", 1)[-1]
            self._json(
                "POST",
                f"{self.pds}/xrpc/com.atproto.repo.deleteRecord",
                json={"repo": self.did, "collection": "app.bsky.graph.follow", "rkey": rkey},
                headers=self._auth_headers(),
            )
        return {"changed": True, "following": desired, "target_did": target_did}

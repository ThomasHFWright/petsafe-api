"""Client for the local SmartDoor setup API."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional
from uuid import UUID

import httpx

MAX_RESPONSE_BYTES = 16 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*\Z")
_FIELDS = {
    "session_id", "device_id", "mode", "endpoint", "expires_at", "state",
    "provisioned", "online", "reported",
}


class SetupAPIError(Exception):
    """A setup API request returned an unexpected HTTP status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"setup API returned HTTP {status_code}")


class InvalidSetupResponse(Exception):
    """The setup API returned data that cannot be trusted."""


@dataclass(frozen=True)
class SetupSession:
    session_id: str
    device_id: str
    mode: str
    endpoint: str
    expires_at: float
    state: str
    provisioned: bool
    online: bool
    reported: bool

    @property
    def confirmed(self) -> bool:
        """Whether this response contains all mode-specific completion evidence."""
        if self.state != "complete" or not self.online or not self.reported:
            return False
        if self.mode == "provision":
            return self.provisioned
        return self.mode == "reconnect" and not self.provisioned


def _canonical_uuid(value: Any, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{name} must be a canonical UUID") from error
    if str(parsed) != value:
        raise ValueError(f"{name} must be a canonical UUID")
    return value


def _url(value: Any, *, origin_only: bool = False) -> tuple[httpx.URL, tuple[str, str, int]]:
    if type(value) is not str or len(value) > 2048 or "?" in value or "#" in value:
        raise ValueError("URL must be HTTPS without credentials, query, or fragment")
    try:
        url = httpx.URL(value)
    except (TypeError, httpx.InvalidURL) as error:
        raise ValueError("URL must be HTTPS without credentials, query, or fragment") from error
    if (url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment
            or (origin_only and url.path != "/")):
        raise ValueError("URL must be HTTPS without credentials, query, or fragment")
    return url, (url.scheme, url.host, url.port or 443)


class SetupClient:
    """Minimal async client for creating and confirming setup sessions."""

    def __init__(self, base_url: str, api_token: str, *,
                 client: Optional[httpx.AsyncClient] = None,
                 timeout: float = 10.0) -> None:
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout <= 0):
            raise ValueError("timeout must be a finite positive number")
        if (type(api_token) is not str or not api_token or len(api_token) > 4096
                or not _TOKEN.fullmatch(api_token)):
            raise ValueError("api_token must be a valid bearer token")
        url, self._origin = _url(base_url, origin_only=True)
        self._base_url = str(url).rstrip("/")
        self._api_token = api_token
        self._timeout = float(timeout)
        self._owned = client is None
        self._client = (client if client is not None else
                        httpx.AsyncClient(timeout=self._timeout, follow_redirects=False))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self._base_url!r})"

    async def __aenter__(self) -> "SetupClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _request(self, method: str, path: str, expected: int,
                       payload: Optional[dict[str, str]] = None) -> bytes:
        headers = {"Authorization": f"Bearer {self._api_token}", "Accept": "application/json"}
        async with self._client.stream(
            method, self._base_url + path, headers=headers, json=payload,
            timeout=self._timeout, follow_redirects=False,
        ) as response:
            if response.status_code != expected:
                raise SetupAPIError(response.status_code)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise InvalidSetupResponse("setup API response is too large")
            if expected != 204:
                media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                if media_type != "application/json":
                    raise InvalidSetupResponse("setup API response is not JSON")
            elif body:
                raise InvalidSetupResponse("cancel response must be empty")
            return bytes(body)

    def _session(self, body: bytes, *, device_id: str, mode: str,
                 session_id: Optional[str] = None) -> SetupSession:
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise InvalidSetupResponse("setup API response is malformed") from error
        if type(value) is not dict or set(value) != _FIELDS:
            raise InvalidSetupResponse("setup API response has an invalid shape")
        try:
            returned_session = _canonical_uuid(value["session_id"], "session_id")
            returned_device = _canonical_uuid(value["device_id"], "device_id")
            endpoint, endpoint_origin = _url(value["endpoint"])
        except ValueError as error:
            raise InvalidSetupResponse("setup API response has an invalid identity") from error
        expires_at = value["expires_at"]
        if (type(expires_at) not in (int, float) or not math.isfinite(expires_at)
                or expires_at <= 0 or value["mode"] not in ("provision", "reconnect")
                or value["state"] not in ("pending", "complete")
                or any(type(value[name]) is not bool for name in ("provisioned", "online", "reported"))):
            raise InvalidSetupResponse("setup API response contains invalid values")
        if (returned_device != device_id or value["mode"] != mode
                or (session_id is not None and returned_session != session_id)):
            raise InvalidSetupResponse("setup API response identity does not match the request")
        if endpoint_origin != self._origin or endpoint.path != f"/provision/{returned_session}":
            raise InvalidSetupResponse("setup endpoint does not match the session origin and identity")
        return SetupSession(returned_session, returned_device, value["mode"], str(endpoint),
                            expires_at, value["state"], value["provisioned"],
                            value["online"], value["reported"])

    async def create(self, device_id: str, mode: str) -> SetupSession:
        device_id = _canonical_uuid(device_id, "device_id")
        if mode not in ("provision", "reconnect"):
            raise ValueError("mode must be 'provision' or 'reconnect'")
        body = await self._request("POST", "/setup/sessions", 201,
                                   {"device_id": device_id, "mode": mode})
        return self._session(body, device_id=device_id, mode=mode)

    async def status(self, session: SetupSession) -> SetupSession:
        if not isinstance(session, SetupSession):
            raise TypeError("session must be a SetupSession")
        session_id = _canonical_uuid(session.session_id, "session_id")
        device_id = _canonical_uuid(session.device_id, "device_id")
        if session.mode not in ("provision", "reconnect"):
            raise ValueError("session mode is invalid")
        body = await self._request("GET", f"/setup/sessions/{session_id}", 200)
        return self._session(body, device_id=device_id, mode=session.mode,
                             session_id=session_id)

    def confirmation(self, session: SetupSession) -> Callable[[], Awaitable[bool]]:
        """Return the bound async hook accepted by ``provision_smartdoor``."""
        if not isinstance(session, SetupSession):
            raise TypeError("session must be a SetupSession")

        async def confirmed() -> bool:
            return (await self.status(session)).confirmed

        return confirmed

    async def cancel(self, session: SetupSession) -> None:
        if not isinstance(session, SetupSession):
            raise TypeError("session must be a SetupSession")
        session_id = _canonical_uuid(session.session_id, "session_id")
        await self._request("DELETE", f"/setup/sessions/{session_id}", 204)

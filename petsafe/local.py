"""Client for a PetSafe local-control server."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

import httpx

from .devices import DeviceSmartDoor
from .setup import _TOKEN

MAX_RESPONSE_BYTES = 1024 * 1024


class LocalPetSafeClient:
    """Minimal async client exposing the cloud-compatible local API surface."""

    def __init__(self, server_url: str, api_token: str, *,
                 client: Optional[httpx.AsyncClient] = None) -> None:
        if (type(api_token) is not str or not api_token or len(api_token) > 4096
                or not _TOKEN.fullmatch(api_token)):
            raise ValueError("api_token must be a valid bearer token")
        try:
            url = httpx.URL(server_url)
        except (TypeError, httpx.InvalidURL) as error:
            raise ValueError("server_url must be an HTTPS origin") from error
        if (type(server_url) is not str or len(server_url) > 2048 or url.scheme != "https"
                or not url.host or url.userinfo or url.path != "/" or url.query or url.fragment):
            raise ValueError("server_url must be an HTTPS origin")
        self._origin = (url.scheme, url.host, url.port or 443)
        self._base_url = str(url).rstrip("/") + "/local/"
        self._api_token = api_token
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(server_url={self._base_url[:-7]!r})"

    async def __aenter__(self) -> "LocalPetSafeClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    def _request_url(self, path: str) -> str:
        if type(path) is not str or len(path) > 2048 or "\\" in path:
            raise ValueError("path must be a relative API path")
        parsed = urlsplit(path)
        decoded = unquote(unquote(parsed.path))
        if (parsed.scheme or parsed.netloc or parsed.fragment or decoded.startswith("/")
                or "\\" in decoded or any(part in (".", "..") for part in decoded.split("/"))):
            raise ValueError("path must be a relative API path without traversal")
        url = httpx.URL(self._base_url + path)
        if (url.scheme, url.host, url.port or 443) != self._origin:
            raise ValueError("path must remain on the configured server")
        return str(url)

    async def _request(self, method: str, path: str, data: Optional[dict] = None) -> httpx.Response:
        request = self._client.build_request(
            method, self._request_url(path),
            headers={"Authorization": f"Bearer {self._api_token}", "Accept": "application/json"},
            json=data, timeout=10,
        )
        response = await self._client.send(request, stream=True, follow_redirects=False)
        try:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise httpx.HTTPError("local API response is too large")
            return httpx.Response(
                response.status_code, headers=response.headers, content=bytes(body),
                request=request, extensions=response.extensions,
            )
        finally:
            await response.aclose()

    async def api_get(self, path: str = "") -> httpx.Response:
        return await self._request("GET", path)

    async def api_patch(self, path: str = "", data: Optional[dict] = None) -> httpx.Response:
        return await self._request("PATCH", path, data)

    async def api_post(self, path: str = "", data: Optional[dict] = None) -> httpx.Response:
        return await self._request("POST", path, data)

    async def api_delete(self, path: str = "") -> httpx.Response:
        return await self._request("DELETE", path)

    async def get_smartdoors(self) -> list[DeviceSmartDoor]:
        response = await self.api_get("smartdoor/product/product")
        payload = json.loads(response.content.decode("UTF-8"))
        doors = [DeviceSmartDoor(self, data) for data in payload.get("data", payload)]
        preferences = await asyncio.gather(*(door.get_preferences() for door in doors))
        for door, prefs in zip(doors, preferences):
            for name in ("friendlyName", "tz"):
                if name in prefs:
                    door.data[name] = prefs[name]
        return doors

    async def get_feeders(self) -> list:
        return []

    async def get_litterboxes(self) -> list:
        return []

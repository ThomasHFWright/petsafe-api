"""Client for a PetSafe local-control server."""
from __future__ import annotations

import asyncio
import json
import math
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

import httpx

from .devices import DeviceSmartDoor
from .setup import _TOKEN

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_EVENT_BYTES = 4 * 1024 * 1024


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
        self._client = client or httpx.AsyncClient(timeout=60, follow_redirects=False)

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
            json=data, timeout=60,
        )
        response = await self._client.send(request, stream=True, follow_redirects=False)
        try:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise httpx.HTTPError("local API response is too large")
            headers = response.headers.copy()
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            return httpx.Response(
                response.status_code, headers=headers, content=bytes(body),
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

    async def iter_events(self):
        """Yield validated full snapshots from the local server's SSE stream."""
        request = self._client.build_request(
            "GET", self._request_url("events"),
            headers={"Authorization": f"Bearer {self._api_token}",
                     "Accept": "text/event-stream"},
            timeout=httpx.Timeout(60, read=45),
        )
        response = await self._client.send(request, stream=True, follow_redirects=False)
        event_type, event_data, event_bytes = None, [], 0
        try:
            response.raise_for_status()
            if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                raise ValueError("Unexpected local events content type")
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    event_bytes += len(line) + 1
                    if event_bytes > MAX_EVENT_BYTES:
                        raise ValueError("SSE event is too large")
                    line = line.removesuffix(b"\r")
                    if line:
                        if line.startswith(b":"):
                            continue
                        try:
                            field, separator, value = line.decode("UTF-8").partition(":")
                        except UnicodeDecodeError as error:
                            raise ValueError("Invalid UTF-8 in SSE event") from error
                        if separator and value.startswith(" "):
                            value = value[1:]
                        if field == "event":
                            event_type = value
                        elif field == "data":
                            event_data.append(value)
                        continue
                    event = self._snapshot(event_type, event_data)
                    event_type, event_data, event_bytes = None, [], 0
                    if event is not None:
                        yield event
                if event_bytes + len(buffer) > MAX_EVENT_BYTES:
                    raise ValueError("SSE event is too large")
            # A disconnected peer may leave a partial frame; reconnect policy belongs to the caller.
        finally:
            await response.aclose()

    @classmethod
    def _snapshot(cls, event_type: Optional[str], data: list[str]):
        if event_type != "snapshot":
            return None
        if not data:
            raise ValueError("Snapshot event has no data")
        try:
            payload = json.loads("\n".join(data), parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("Invalid JSON number")))
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError("Invalid snapshot JSON") from error
        keys = {"version", "revision", "brokerConnected", "door", "pets",
                "links", "activity", "override"}
        try:
            finite = cls._finite(payload)
        except RecursionError as error:
            raise ValueError("Invalid snapshot schema") from error
        if (not isinstance(payload, dict) or set(payload) != keys
                or type(payload["version"]) is not int or payload["version"] != 1
                or type(payload["revision"]) is not int
                or payload["revision"] < 0
                or type(payload["brokerConnected"]) is not bool
                or not isinstance(payload["door"], dict)
                or not isinstance(payload["pets"], list)
                or any(not isinstance(item, dict) for item in payload["pets"])
                or not isinstance(payload["links"], dict)
                or any(not isinstance(key, str) or not isinstance(items, list)
                       or any(not isinstance(item, dict) for item in items)
                       for key, items in payload["links"].items())
                or not isinstance(payload["activity"], list)
                or any(not isinstance(item, dict) for item in payload["activity"])
                or payload["override"] is not None and not isinstance(payload["override"], dict)
                or not finite):
            raise ValueError("Invalid snapshot schema")
        return payload

    @staticmethod
    def _finite(value: Any) -> bool:
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, dict):
            return all(isinstance(key, str) and LocalPetSafeClient._finite(item)
                       for key, item in value.items())
        if isinstance(value, list):
            return all(LocalPetSafeClient._finite(item) for item in value)
        return True

    async def get_smartdoors(self) -> list[DeviceSmartDoor]:
        response = await self.api_get("smartdoor/product/product")
        payload = json.loads(response.content.decode("UTF-8"))
        devices = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(devices, list) or any(not isinstance(data, dict) for data in devices):
            raise ValueError("Unexpected response payload for SmartDoor devices")
        doors = [DeviceSmartDoor(self, data) for data in devices]
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

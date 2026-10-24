"""Client for a PetSafe local-control server."""
from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote, quote_plus, unquote, urlsplit

import httpx

from .devices import DeviceSmartDoor
from .setup import _TOKEN

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = MAX_RESPONSE_BYTES
EMPTY_COMMANDS = {"REPORT_STATUS", "REBOOT", "RESET_FALSE_TRIGGERS",
                  "RESET_CONCURRENT_TRIGGERS", "CALIBRATE_FLAP_CENTER_POSITION",
                  "CALIBRATE_RFID_SCANNER"}
COMMAND_STATES = {"submitted", "response_received", "completed", "unconfirmed"}


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

    async def _request(self, method: str, path: str, data: Any = None) -> httpx.Response:
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

    async def api_put(self, path: str = "", data: Any = None) -> httpx.Response:
        return await self._request("PUT", path, data)

    async def api_delete(self, path: str = "") -> httpx.Response:
        return await self._request("DELETE", path)

    async def iter_events(self) -> AsyncIterator[dict]:
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
        return cls._validate_snapshot(payload)

    @classmethod
    def _validate_snapshot(cls, payload: Any) -> dict:
        required = {"version", "revision", "brokerConnected", "door", "pets",
                    "links", "activity", "override"}
        try:
            finite = cls._finite(payload)
        except RecursionError as error:
            raise ValueError("Invalid snapshot schema") from error
        if (not isinstance(payload, dict) or not required <= set(payload)
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
                or "rfidScan" in payload and payload["rfidScan"] is not None
                    and not isinstance(payload["rfidScan"], dict)
                or "setupSession" in payload and payload["setupSession"] is not None
                    and not isinstance(payload["setupSession"], dict)
                or "command" in payload and payload["command"] is not None
                    and not isinstance(payload["command"], dict)
                or any(key in payload and not isinstance(payload[key], dict)
                       for key in ("account", "notifications", "capabilities", "firmware"))
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

    @staticmethod
    def _data(response: httpx.Response) -> Any:
        try:
            payload = json.loads(response.content.decode("UTF-8"))
        except (UnicodeError, ValueError, RecursionError) as error:
            raise ValueError("Invalid local API JSON") from error
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    @staticmethod
    def _segment(value: str, name: str) -> str:
        if (type(value) is not str or not value or "/" in value or "\\" in value
                or value in (".", "..")):
            raise ValueError(f"{name} must be provided")
        return quote(value, safe="")

    @staticmethod
    def _query(value: str, name: str) -> str:
        if type(value) is not str or not value:
            raise ValueError(f"{name} must be provided")
        return quote_plus(value, safe="")

    async def get_state(self) -> dict:
        """Return the server's offline-readable full local snapshot."""
        return self._validate_snapshot(self._data(await self.api_get("state")))

    async def get_product_summaries(self) -> list[dict]:
        """Return local SmartDoor directory summaries."""
        data = self._data(await self.api_get("directory/products"))
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("Unexpected local product summaries response")
        return data

    async def list_pets(self) -> list[dict]:
        """Return active local pet profiles."""
        data = self._data(await self.api_get("pets/pets"))
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("Unexpected local pets response")
        return data

    async def create_pet(self, pet: dict) -> dict:
        """Create a local pet from the server's profile/timezone request object."""
        if not isinstance(pet, dict):
            raise TypeError("pet must be a dict")
        data = self._data(await self.api_post("pets/pets", pet))
        if not isinstance(data, dict):
            raise ValueError("Unexpected local pet response")
        return data

    async def update_pet(self, pet_id: str, changes: dict) -> dict:
        """Update a local pet profile and return the updated pet."""
        if not isinstance(changes, dict):
            raise TypeError("changes must be a dict")
        data = self._data(await self.api_patch(
            "pets/pets/" + self._segment(pet_id, "pet_id"), changes))
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, dict):
            raise ValueError("Unexpected local pet response")
        return data

    async def delete_pet(self, pet_id: str) -> Any:
        """Delete a local pet and its door access."""
        return self._data(await self.api_delete(
            "pets/pets/" + self._segment(pet_id, "pet_id")))

    async def set_pet_identifier(self, pet_id: str, thing_name: str,
                                 tag_id: str, technology: str) -> dict:
        """Assign or replace one SmartDoor RFID technology for a pet."""
        path = "pets/pets/{}/{}/identifier".format(
            self._segment(pet_id, "pet_id"), self._segment(thing_name, "thing_name"))
        data = self._data(await self.api_put(
            path, {"tagId": tag_id, "technology": technology}))
        if not isinstance(data, dict):
            raise ValueError("Unexpected local identifier response")
        return data

    async def delete_pet_identifiers(self, pet_id: str, thing_name: str) -> Any:
        """Remove all SmartDoor identifiers for a pet."""
        path = "pets/pets/{}/{}/identifier".format(
            self._segment(pet_id, "pet_id"), self._segment(thing_name, "thing_name"))
        return self._data(await self.api_delete(path))

    async def get_pet_links(self, pet_id: str) -> list[dict]:
        """Return active product links for one pet."""
        data = self._data(await self.api_get(
            "directory/petProduct?petId=" + self._query(pet_id, "pet_id")))
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("Unexpected local pet links response")
        return data

    async def get_door_links(self, thing_name: str) -> list[dict]:
        """Return active pet links for one SmartDoor."""
        data = self._data(await self.api_get(
            "directory/petProduct?productId="
            + self._query(thing_name, "thing_name")))
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("Unexpected local door links response")
        return data

    async def link_pet(self, pet_id: str, thing_name: str, product_type: str) -> list[dict]:
        """Grant a pet use of one local SmartDoor."""
        link = {"petId": pet_id, "productId": thing_name,
                "productType": product_type, "relationType": "use"}
        return await self.link_pets([link])

    async def link_pets(self, links: list[dict]) -> list[dict]:
        """Atomically add or revive local pet-product links."""
        if not isinstance(links, list) or any(not isinstance(item, dict) for item in links):
            raise TypeError("links must be a list of dicts")
        data = self._data(await self.api_put("directory/petProduct", links))
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("Unexpected local pet links response")
        return data

    async def unlink_pet(self, pet_id: str, thing_name: str) -> Any:
        """Revoke a pet's access to one local SmartDoor."""
        path = "directory/petProduct?productId={}&petId={}".format(
            self._query(thing_name, "thing_name"), self._query(pet_id, "pet_id"))
        return self._data(await self.api_delete(path))

    async def get_account(self) -> dict:
        """Return the local owner account-shaped record."""
        data = self._data(await self.api_get("directory/account"))
        if not isinstance(data, dict):
            raise ValueError("Unexpected local account response")
        return data

    async def update_account(self, changes: dict) -> dict:
        """Update supported local account preferences."""
        if not isinstance(changes, dict):
            raise TypeError("changes must be a dict")
        data = self._data(await self.api_patch("directory/account", changes))
        if not isinstance(data, dict):
            raise ValueError("Unexpected local account response")
        return data

    async def get_notifications(self, thing_name: str) -> dict:
        """Return persisted notification flags for one SmartDoor."""
        data = self._data(await self.api_get(
            "directory/product-preferences/" + self._segment(thing_name, "thing_name")))
        preferences = data.get("productPreferences") if isinstance(data, dict) else None
        preferences = preferences.get("preferences") if isinstance(preferences, dict) else None
        if not isinstance(preferences, dict):
            raise ValueError("Unexpected local notification response")
        return preferences

    async def update_notifications(self, thing_name: str, preferences: dict) -> dict:
        """Replace persisted notification flags for one SmartDoor."""
        if not isinstance(preferences, dict):
            raise TypeError("preferences must be a dict")
        data = self._data(await self.api_put(
            "directory/product-preferences",
            {"thingName": thing_name, "preferences": preferences}))
        result = data.get("preferences") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise ValueError("Unexpected local notification response")
        return result

    async def get_capabilities(self) -> dict:
        """Return the local server's supported feature contract."""
        data = self._data(await self.api_get("capabilities"))
        if not isinstance(data, dict):
            raise ValueError("Unexpected local capabilities response")
        return data

    @staticmethod
    def _command(data: Any) -> dict:
        required = {"id", "code", "state", "confirmed", "responseReceived",
                    "submittedAt", "deadline"}
        if (not isinstance(data, dict) or not required <= set(data)
                or any(not isinstance(data[key], str) or not data[key]
                       for key in ("id", "code"))
                or data["state"] not in COMMAND_STATES
                or type(data["confirmed"]) is not bool
                or type(data["responseReceived"]) is not bool
                or any(type(data[key]) not in (int, float) or not math.isfinite(data[key])
                       for key in ("submittedAt", "deadline"))):
            raise ValueError("Unexpected local command response")
        return data

    async def get_last_command(self, thing_name: str) -> Optional[dict]:
        """Return the latest native command submission record, if any."""
        data = self._data(await self.api_get(
            "smartdoor/product/product/{}/commands".format(
                self._segment(thing_name, "thing_name"))))
        return None if data is None else self._command(data)

    async def submit_command(self, thing_name: str, code: str,
                             data: Optional[dict] = None) -> dict:
        """Submit an allowlisted native maintenance command."""
        data = {} if data is None else data
        if code in EMPTY_COMMANDS:
            valid = data == {}
        elif code == "MEMFAULT_CONFIG":
            valid = (isinstance(data, dict) and set(data) == {"mask", "time"}
                     and all(type(data[key]) is int and 0 <= data[key] <= 0xffffffff
                             for key in data))
        elif code == "CLI":
            value = data.get("TESTCMD") if isinstance(data, dict) and set(data) == {"TESTCMD"} else None
            valid = (isinstance(value, str) and 1 <= len(value) <= 64
                     and all(32 <= ord(character) <= 126 for character in value))
        else:
            valid = False
        if not valid:
            raise ValueError("Invalid native maintenance command")
        result = self._data(await self.api_post(
            "smartdoor/product/product/{}/commands".format(
                self._segment(thing_name, "thing_name")), {"code": code, "data": data}))
        return self._command(result)

    @staticmethod
    def _sha256(value: str) -> str:
        if type(value) is not str or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("sha256 must be 64 hexadecimal characters")
        return value.lower()

    async def get_firmware(self, thing_name: str) -> dict:
        """Return public firmware artifacts and the current installation job."""
        data = self._data(await self.api_get(
            "smartdoor/product/product/{}/firmware".format(
                self._segment(thing_name, "thing_name"))))
        if (not isinstance(data, dict) or not {"artifacts", "job"} <= set(data)
                or not isinstance(data["artifacts"], list)
                or any(not isinstance(item, dict) for item in data["artifacts"])
                or data["job"] is not None and not isinstance(data["job"], dict)):
            raise ValueError("Unexpected local firmware response")
        return data

    async def stage_firmware(self, thing_name: str, artifact_id: str,
                             version: str, sha256: str) -> dict:
        """Stage one server-owned firmware inbox artifact after hash verification."""
        if type(artifact_id) is not str or not artifact_id or type(version) is not str or not version:
            raise ValueError("artifact_id and version must be provided")
        data = self._data(await self.api_post(
            "smartdoor/product/product/{}/firmware/artifacts".format(
                self._segment(thing_name, "thing_name")),
            {"artifactId": artifact_id, "version": version, "sha256": self._sha256(sha256)}))
        if not isinstance(data, dict):
            raise ValueError("Unexpected local firmware artifact response")
        return data

    async def install_firmware(self, thing_name: str, sha256: str) -> dict:
        """Queue installation of one previously staged firmware hash."""
        data = self._data(await self.api_post(
            "smartdoor/product/product/{}/firmware/jobs".format(
                self._segment(thing_name, "thing_name")), {"sha256": self._sha256(sha256)}))
        if not isinstance(data, dict):
            raise ValueError("Unexpected local firmware job response")
        return data

    async def delete_firmware_artifact(self, thing_name: str, sha256: str) -> None:
        """Delete one inactive staged firmware artifact by hash."""
        path = "smartdoor/product/product/{}/firmware/artifacts/{}".format(
            self._segment(thing_name, "thing_name"), self._sha256(sha256))
        await self.api_delete(path)

    async def cancel_firmware_job(self, thing_name: str, job_id: str) -> Any:
        """Cancel one firmware installation job."""
        path = "smartdoor/product/product/{}/firmware/jobs/{}".format(
            self._segment(thing_name, "thing_name"), self._segment(job_id, "job_id"))
        response = await self.api_delete(path)
        return self._data(response) if response.content else None

    @staticmethod
    def _scan_path(thing_name: str, scan_id: Optional[str] = None) -> str:
        path = "smartdoor/product/product/{}/rfid-scans".format(
            LocalPetSafeClient._segment(thing_name, "thing_name"))
        return path if scan_id is None else path + "/" + LocalPetSafeClient._segment(scan_id, "scan_id")

    async def start_rfid_scan(self, thing_name: str, technology: str) -> dict:
        """Start one bounded native RFID scan."""
        data = self._data(await self.api_post(
            self._scan_path(thing_name), {"technology": technology}))
        if not isinstance(data, dict):
            raise ValueError("Unexpected local RFID scan response")
        return data

    async def get_rfid_scan(self, thing_name: str,
                            scan_id: Optional[str] = None) -> Optional[dict]:
        """Return a current or identified managed RFID scan."""
        data = self._data(await self.api_get(self._scan_path(thing_name, scan_id)))
        if data is not None and not isinstance(data, dict):
            raise ValueError("Unexpected local RFID scan response")
        return data

    async def cancel_rfid_scan(self, thing_name: str, scan_id: str) -> dict:
        """Cancel a managed RFID scan and confirm mode restoration."""
        data = self._data(await self.api_delete(self._scan_path(thing_name, scan_id)))
        if not isinstance(data, dict):
            raise ValueError("Unexpected local RFID scan response")
        return data

    async def assign_rfid_scan(self, thing_name: str, scan_id: str, pet_id: str) -> dict:
        """Assign a captured RFID candidate to a pet."""
        data = self._data(await self.api_post(
            self._scan_path(thing_name, scan_id) + "/assign", {"petId": pet_id}))
        if not isinstance(data, dict):
            raise ValueError("Unexpected local RFID scan response")
        return data

    async def scan_rfid(self, thing_name: str, technology: str) -> AsyncIterator[dict]:
        """Start a scan and yield push updates through capture and mode restoration."""
        current = await self.start_rfid_scan(thing_name, technology)
        yield current
        if current.get("state") in {"captured", "assigned", "cancelled", "timed_out",
                                     "failed", "expired"} and current.get("restored") is True:
            return
        async for snapshot in self.iter_events():
            if "rfidScan" not in snapshot:
                raise ValueError("Local server snapshots do not expose RFID scan state")
            scan = snapshot["rfidScan"]
            if not isinstance(scan, dict) or scan.get("id") != current.get("id"):
                raise ValueError("Managed RFID scan is no longer available")
            if scan == current:
                continue
            current = scan
            yield current
            if current.get("state") in {"captured", "assigned", "cancelled", "timed_out",
                                         "failed", "expired"} and current.get("restored") is True:
                return

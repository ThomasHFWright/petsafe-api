"""Bounded async BLE provisioning for PetSafe SmartDoor devices."""
from __future__ import annotations

import asyncio
import base64
import io
import inspect
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

PREFIX = "3113a187-4b9f-4f9a-aa83-c614e11bff"
APPLICATION_MTU = 23
MAX_RESPONSE = 4096
SUPPORTED_SECURITY = 3


class ProvisioningError(Exception):
    def __init__(self, message: str, phase: str = "validation") -> None:
        super().__init__(message)
        self.phase = phase


class InvalidResponseError(ProvisioningError):
    pass


class WifiRejectedError(ProvisioningError):
    pass


class ProvisioningTimeoutError(ProvisioningError):
    pass


class ProvisioningState(str, Enum):
    COMPLETE = "complete"
    PENDING = "pending"


@dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    password: str
    bssid: bytes
    group: Any
    security: int = SUPPORTED_SECURITY


@dataclass(frozen=True)
class ProvisioningTimeouts:
    overall: float = 300.0
    connect: float = 30.0
    phase: float = 20.0
    scan: float = 6.0
    confirmation: float = 120.0
    connection_attempts: int = 3
    connection_backoff: float = 0.25


@dataclass(frozen=True)
class ProvisioningResult:
    state: ProvisioningState
    phase: str
    wifi_retried: bool = False
    finish_retried: bool = False


ConfirmationHook = Callable[[], Awaitable[bool]]
ClientFactory = Callable[[Any], Any]


def _imports():
    try:
        import cbor2
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ImportError as error:
        raise RuntimeError("BLE provisioning requires petsafe-api[ble]") from error
    return cbor2, rsa, PKCS1v15, load_pem_public_key


def _frames(payload: bytes, mtu: int = APPLICATION_MTU) -> list[bytes]:
    if not 23 <= mtu <= 517:
        raise ProvisioningError("invalid negotiated MTU")
    size = mtu - 5
    return [b"\xff\xfe" + payload[i:i + size] for i in range(0, len(payload), size)] + [b"\xff\xff"]


def _decode(payload: bytes) -> dict[str, Any]:
    cbor2, _, _, _ = _imports()
    try:
        stream = io.BytesIO(payload)
        value = cbor2.CBORDecoder(stream).decode()
    except Exception as error:
        raise InvalidResponseError("invalid CBOR response") from error
    if stream.read(1) or not isinstance(value, dict):
        raise InvalidResponseError("response must be one CBOR map")
    return value


def _validate(endpoint: str, network: WifiNetwork, timeouts: ProvisioningTimeouts) -> None:
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as error:
        raise ProvisioningError("invalid endpoint port") from error
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or port not in (None, 443) or not parsed.path.startswith("/")
            or len(endpoint) > 2048):
        raise ProvisioningError("endpoint must be HTTPS without credentials, query, fragment, or non-standard port")
    try:
        ssid, password = network.ssid.encode(), network.password.encode()
    except (AttributeError, UnicodeEncodeError) as error:
        raise ProvisioningError("Wi-Fi credentials must be UTF-8 strings") from error
    if not 1 <= len(ssid) <= 32:
        raise ProvisioningError("SSID must be 1 to 32 UTF-8 bytes")
    if not 8 <= len(password) <= 63:
        raise ProvisioningError("Wi-Fi password must be 8 to 63 UTF-8 bytes")
    if not isinstance(network.bssid, bytes) or len(network.bssid) != 6:
        raise ProvisioningError("BSSID must be six bytes")
    if network.security != SUPPORTED_SECURITY:
        raise ProvisioningError("unsupported Wi-Fi security")
    cbor2, _, _, _ = _imports()
    try:
        encoded_group = cbor2.dumps(network.group, canonical=True)
    except Exception as error:
        raise ProvisioningError("Wi-Fi group must be CBOR-encodable") from error
    if len(encoded_group) > 256:
        raise ProvisioningError("Wi-Fi group is too large")
    if min(timeouts.overall, timeouts.connect, timeouts.phase, timeouts.confirmation) <= 0:
        raise ProvisioningError("timeouts must be positive")
    if timeouts.scan < 0 or not 1 <= timeouts.connection_attempts <= 3:
        raise ProvisioningError("invalid scan timeout or connection budget")


class _Sequence:
    def __init__(self, device: Any, endpoint: str, network: WifiNetwork,
                 factory: ClientFactory, confirm: Optional[ConfirmationHook],
                 timeouts: ProvisioningTimeouts) -> None:
        self.device, self.endpoint, self.network = device, endpoint, network
        self.factory, self.confirm, self.timeouts = factory, confirm, timeouts
        self.deadline = time.monotonic() + timeouts.overall
        self.phase, self.connections = "connecting", 0

    def remaining(self, limit: float) -> float:
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise ProvisioningTimeoutError("overall deadline exhausted", self.phase)
        return min(limit, value)

    async def wait(self, awaitable: Awaitable[Any], limit: Optional[float] = None) -> Any:
        try:
            timeout = self.remaining(limit or self.timeouts.phase)
        except BaseException:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise
        try:
            return await asyncio.wait_for(awaitable, timeout)
        except asyncio.TimeoutError as error:
            raise ProvisioningTimeoutError("provisioning phase timed out", self.phase) from error

    async def connect(self):
        last = None
        while self.connections < self.timeouts.connection_attempts:
            self.connections += 1
            client = self.factory(self.device)
            if inspect.isawaitable(client):
                client = await self.wait(client, self.timeouts.connect)
            try:
                await self.wait(client.connect(), self.timeouts.connect)
                return client
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                last = error
                try:
                    await client.disconnect()
                except BaseException:
                    pass
                if self.connections < self.timeouts.connection_attempts:
                    await self.wait(asyncio.sleep(self.timeouts.connection_backoff), self.timeouts.connect)
        raise ProvisioningError("BLE connection budget exhausted", self.phase) from last

    async def disconnect(self, client: Any, original: Optional[BaseException] = None) -> None:
        try:
            if getattr(client, "is_connected", True):
                await asyncio.wait_for(client.disconnect(), self.timeouts.phase)
        except BaseException:
            if original is None:
                raise

    def characteristic(self, client: Any, suffix: str):
        value = client.services.get_characteristic(PREFIX + suffix)
        if value is None:
            raise ProvisioningError("required BLE characteristic is missing", self.phase)
        return value

    async def write(self, client: Any, suffix: str, value: dict[str, Any], mtu: int) -> None:
        cbor2, _, _, _ = _imports()
        characteristic = self.characteristic(client, suffix)
        for frame in _frames(cbor2.dumps(value, canonical=True), mtu):
            await self.wait(client.write_gatt_char(characteristic, frame, response=True))

    async def response(self, client: Any, suffix: str,
                       request: Optional[dict[str, Any]] = None,
                       mtu: int = APPLICATION_MTU) -> dict[str, Any]:
        characteristic = self.characteristic(client, suffix)
        future = asyncio.get_running_loop().create_future()
        buffer, active = bytearray(), True

        def notified(_sender: Any, data: bytearray) -> None:
            if not active or future.done():
                return
            packet = bytes(data)
            if packet == b"\xff\xff":
                try:
                    future.set_result(_decode(bytes(buffer)))
                except BaseException as error:
                    future.set_exception(error)
            elif (len(packet) <= 2 or packet[:2] not in (b"\xff\xfe", b"\xff\xfd")
                  or len(buffer) + len(packet) - 2 > MAX_RESPONSE):
                future.set_exception(InvalidResponseError("invalid or oversized response frame", self.phase))
            else:
                buffer.extend(packet[2:])

        await self.wait(client.start_notify(characteristic, notified))
        original = None
        try:
            if request is None:
                await self.wait(client.read_gatt_char(characteristic))
            else:
                await self.write(client, suffix, request, mtu)
            return await self.wait(future)
        except BaseException as error:
            original = error
            raise
        finally:
            active = False
            try:
                await asyncio.wait_for(client.stop_notify(characteristic), self.timeouts.phase)
            except BaseException:
                if not future.done():
                    future.cancel()
                if original is None:
                    raise

    async def ack(self, client: Any, suffix: str, value: dict[str, Any], mtu: int) -> None:
        status = (await self.response(client, suffix, value, mtu)).get("s")
        if not isinstance(status, int) or isinstance(status, bool) or status != 1:
            error = WifiRejectedError if suffix == "09" else ProvisioningError
            raise error("door rejected provisioning value", self.phase)

    async def status(self, client: Any) -> bool:
        self.phase = "wifi_status"
        return (await self.response(client, "06")).get("w") == "PROFILE_CONNECTED_GOOD"

    async def confirmation(self) -> bool:
        if self.confirm is None:
            return False
        self.phase = "server_confirmation"
        try:
            return bool(await self.wait(self.confirm(), self.timeouts.confirmation))
        except ProvisioningTimeoutError:
            return False

    async def full(self, client: Any) -> None:
        _, rsa, PKCS1v15, load_pem_public_key = _imports()
        self.phase = "mtu"
        raw_mtu = await self.wait(client.read_gatt_char(self.characteristic(client, "03")))
        try:
            mtu = min(int(bytes(raw_mtu)), APPLICATION_MTU)
        except (TypeError, ValueError) as error:
            raise InvalidResponseError("invalid advertised MTU", self.phase) from error
        if mtu < 23:
            raise InvalidResponseError("advertised MTU is below 23", self.phase)
        await self.write(client, "90", {"a": mtu}, APPLICATION_MTU)

        self.phase = "device_key"
        key_value = (await self.response(client, "08", {"k": 2}, mtu)).get("v")
        if not isinstance(key_value, str):
            raise InvalidResponseError("device key response is missing", self.phase)
        try:
            key = load_pem_public_key(key_value.encode("ascii"))
        except (ValueError, TypeError, UnicodeEncodeError) as error:
            raise InvalidResponseError("invalid device public key", self.phase) from error
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise InvalidResponseError("device key must be RSA with at least 2048 bits", self.phase)

        self.phase = "firmware"
        if not isinstance((await self.response(client, "10")).get("n"), str):
            raise InvalidResponseError("firmware response is missing a version", self.phase)

        self.phase = "wifi_scan"
        characteristic = self.characteristic(client, "01")
        networks: list[dict[str, Any]] = []
        buffer, active = bytearray(), True
        scan_error = asyncio.get_running_loop().create_future()

        def scanned(_sender: Any, data: bytearray) -> None:
            if not active:
                return
            packet = bytes(data)
            if packet == b"\xff\xff":
                try:
                    networks.append(_decode(bytes(buffer)))
                except BaseException as error:
                    if not scan_error.done():
                        scan_error.set_exception(error)
                buffer.clear()
            elif (len(packet) <= 2 or packet[:2] not in (b"\xff\xfe", b"\xff\xfd")
                  or len(buffer) + len(packet) - 2 > MAX_RESPONSE):
                if not scan_error.done():
                    scan_error.set_exception(InvalidResponseError("invalid Wi-Fi scan frame", self.phase))
            else:
                buffer.extend(packet[2:])

        await self.wait(client.start_notify(characteristic, scanned))
        original = None
        try:
            await self.write(client, "01", {"h": 15, "t": 5}, mtu)
            sleeper = asyncio.create_task(asyncio.sleep(self.timeouts.scan))
            done, _ = await self.wait(asyncio.wait((sleeper, scan_error),
                                                    return_when=asyncio.FIRST_COMPLETED),
                                      self.timeouts.scan + .1)
            if scan_error in done:
                sleeper.cancel()
                await asyncio.gather(sleeper, return_exceptions=True)
                scan_error.result()
        except BaseException as error:
            original = error
            raise
        finally:
            active = False
            try:
                await asyncio.wait_for(client.stop_notify(characteristic), self.timeouts.phase)
            except BaseException:
                if original is None:
                    raise
        candidates = [item for item in networks
                      if item.get("r") == self.network.ssid
                      and item.get("q") == SUPPORTED_SECURITY
                      and isinstance(item.get("b"), bytes) and len(item["b"]) == 6
                      and isinstance(item.get("p"), int) and -127 <= item["p"] <= 0]
        if not candidates:
            raise ProvisioningError("configured secured network was not visible", self.phase)
        bssid = max(candidates, key=lambda item: (item["b"] == self.network.bssid, item["p"]))["b"]
        encrypted = base64.encodebytes(key.encrypt(self.network.password.encode(), PKCS1v15())).decode()

        self.phase = "url"
        await self.ack(client, "05", {"u": self.endpoint}, mtu)
        self.phase = "wifi"
        await self.ack(client, "09", {"g": self.network.group, "r": self.network.ssid,
                       "b": bssid.replace(b"\x00", b"\x55"), "m": encrypted,
                       "q": self.network.security}, mtu)
        if not await self.status(client):
            raise ProvisioningTimeoutError("Wi-Fi did not reach connected state", self.phase)

    async def finish(self, client: Any) -> None:
        self.phase = "finish"
        await self.write(client, "07", {"o": True, "c": 1}, APPLICATION_MTU)

    async def run(self) -> ProvisioningResult:
        wifi_retried = False
        finish_uncertain = False
        while True:
            client, original = await self.connect(), None
            try:
                await self.full(client)
                try:
                    await self.finish(client)
                except ProvisioningTimeoutError as error:
                    original = error
                    finish_uncertain = True
                except ProvisioningError:
                    raise
                except Exception as error:
                    original = error
                    finish_uncertain = True
            except WifiRejectedError as error:
                original = error
                if wifi_retried:
                    raise
                wifi_retried = True
                continue
            except BaseException as error:
                original = error
                raise
            finally:
                await self.disconnect(client, original)

            if finish_uncertain:
                break
            if await self.confirmation():
                return ProvisioningResult(ProvisioningState.COMPLETE, "server_confirmed", wifi_retried)
            return ProvisioningResult(ProvisioningState.PENDING, "server_confirmation", wifi_retried)

        if await self.confirmation():
            return ProvisioningResult(ProvisioningState.COMPLETE, "server_confirmed", wifi_retried)
        client, original = await self.connect(), None
        recovery_uncertain = False
        try:
            if not await self.status(client):
                return ProvisioningResult(ProvisioningState.PENDING, "finish_not_permitted", wifi_retried)
            try:
                await self.finish(client)
            except ProvisioningTimeoutError as error:
                original = error
                recovery_uncertain = True
            except ProvisioningError:
                raise
            except Exception as error:
                original = error
                recovery_uncertain = True
        except BaseException as error:
            original = error
            raise
        finally:
            await self.disconnect(client, original)
        if await self.confirmation():
            return ProvisioningResult(ProvisioningState.COMPLETE, "server_confirmed", wifi_retried, True)
        phase = "finish_uncertain" if recovery_uncertain else "server_confirmation"
        return ProvisioningResult(ProvisioningState.PENDING, phase, wifi_retried, True)


async def provision_smartdoor(device: Any, endpoint: str, network: WifiNetwork, *,
                              confirm: Optional[ConfirmationHook] = None,
                              client_factory: Optional[ClientFactory] = None,
                              timeouts: ProvisioningTimeouts = ProvisioningTimeouts()) -> ProvisioningResult:
    """Provision a discovered BLE device; success requires fresh server evidence."""
    _validate(endpoint, network, timeouts)
    if client_factory is None:
        try:
            from bleak import BleakClient
        except ImportError as error:
            raise RuntimeError("BLE provisioning requires petsafe-api[ble]") from error
        client_factory = BleakClient
    sequence = _Sequence(device, endpoint, network, client_factory, confirm, timeouts)
    return await sequence.wait(sequence.run(), timeouts.overall)

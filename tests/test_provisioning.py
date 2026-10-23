import asyncio

import cbor2
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from petsafe.provisioning import (
    PREFIX,
    InvalidResponseError,
    ProvisioningError,
    ProvisioningState,
    ProvisioningTimeoutError,
    ProvisioningTimeouts,
    WifiNetwork,
    provision_smartdoor,
)


KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_KEY = KEY.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
ENDPOINT = "https://setup.example.test/provision/session"
NETWORK = WifiNetwork("Test Network", "synthetic-password", b"\x10\x20\x30\x40\x50\x60", 7)
FAST = ProvisioningTimeouts(overall=.5, connect=.05, phase=.05, scan=.001,
                            confirmation=.05, connection_backoff=0)


class Services:
    def get_characteristic(self, uuid):
        return uuid if uuid.startswith(PREFIX) else None


class FakeGatt:
    def __init__(self, *, wifi_ack=1, finish_timeout=False, malformed_key=False,
                 connect_error=False, stall=False, status=True):
        self.services = Services()
        self.is_connected = False
        self.callbacks = {}
        self.buffers = {}
        self.wifi_ack = wifi_ack
        self.finish_timeout = finish_timeout
        self.malformed_key = malformed_key
        self.connect_error = connect_error
        self.stall = stall
        self.status = status
        self.disconnected = False
        self.finished = 0
        self.key_requests = 0

    async def connect(self):
        if self.connect_error:
            raise ConnectionError("synthetic connect failure")
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        self.disconnected = True

    async def start_notify(self, characteristic, callback):
        self.callbacks[characteristic[-2:]] = callback

    async def stop_notify(self, characteristic):
        self.callbacks.pop(characteristic[-2:], None)

    def notify(self, suffix, value, fragmented=True):
        callback = self.callbacks.get(suffix)
        if not callback:
            return
        raw = cbor2.dumps(value, canonical=True)
        if fragmented:
            cut = max(1, len(raw) // 2)
            callback(suffix, b"\xff\xfd" + raw[:cut])
            callback(suffix, b"\xff\xfe" + raw[cut:])
        else:
            callback(suffix, b"\x00\x00" + raw)
        callback(suffix, b"\xff\xff")

    async def read_gatt_char(self, characteristic):
        suffix = characteristic[-2:]
        if suffix == "03":
            return b"512"
        if suffix == "10":
            self.notify("10", {"n": "synthetic-firmware"})
        if suffix == "06":
            self.notify("06", {"w": "PROFILE_CONNECTED_GOOD" if self.status else "CONNECTING"})
        return b""

    async def write_gatt_char(self, characteristic, frame, response=True):
        suffix = characteristic[-2:]
        if self.stall:
            await asyncio.Event().wait()
        if suffix == "07" and self.finish_timeout:
            await asyncio.Event().wait()
        if frame == b"\xff\xff":
            payload = cbor2.loads(bytes(self.buffers.pop(suffix, b"")))
            if suffix == "08":
                self.key_requests += 1
                if self.malformed_key:
                    callback = self.callbacks[suffix]
                    callback(suffix, b"\x01\x02bad")
                else:
                    self.notify(suffix, {"v": PUBLIC_KEY})
            elif suffix == "01":
                self.notify(suffix, {"r": NETWORK.ssid, "q": 3, "b": NETWORK.bssid, "p": -40})
            elif suffix == "05":
                self.notify(suffix, {"s": 1})
            elif suffix == "09":
                assert payload["m"] != NETWORK.password
                self.notify(suffix, {"s": self.wifi_ack})
            elif suffix == "07":
                self.finished += 1
        else:
            assert frame[:2] == b"\xff\xfe"
            self.buffers.setdefault(suffix, bytearray()).extend(frame[2:])


def run(factory, confirm=None, timeouts=FAST):
    return asyncio.run(provision_smartdoor(object(), ENDPOINT, NETWORK,
                                           client_factory=factory,
                                           confirm=confirm, timeouts=timeouts))


def test_success_requires_server_confirmation_and_uses_uuid_characteristics():
    client = FakeGatt()

    async def confirmed():
        return True

    result = run(lambda device: client, confirmed)
    assert result.state is ProvisioningState.COMPLETE
    assert client.finished == 1 and client.disconnected and client.key_requests == 1


def test_wifi_rejection_retries_once_with_fresh_connection_and_key():
    clients = [FakeGatt(wifi_ack=0), FakeGatt()]

    async def confirmed():
        return True

    result = run(lambda device: clients.pop(0), confirmed)
    assert result.wifi_retried and result.state is ProvisioningState.COMPLETE
    assert not clients


def test_lost_finish_ack_accepts_delayed_fresh_server_confirmation():
    client = FakeGatt(finish_timeout=True)

    async def confirmed():
        await asyncio.sleep(.002)
        return True

    result = run(lambda device: client, confirmed)
    assert result.state is ProvisioningState.COMPLETE
    assert not result.finish_retried


def test_finish_only_recovery_checks_fresh_status_first():
    clients = [FakeGatt(finish_timeout=True), FakeGatt()]
    confirmations = iter((False, True))

    async def confirmed():
        return next(confirmations)

    result = run(lambda device: clients.pop(0), confirmed)
    assert result.state is ProvisioningState.COMPLETE and result.finish_retried
    assert not clients


def test_fragment_validation_and_input_validation():
    with pytest.raises(InvalidResponseError):
        run(lambda device: FakeGatt(malformed_key=True))
    with pytest.raises(ProvisioningError):
        asyncio.run(provision_smartdoor(object(), "http://user:pass@example.test/x",
                                        NETWORK, client_factory=FakeGatt))
    with pytest.raises(ProvisioningError):
        asyncio.run(provision_smartdoor(object(), ENDPOINT,
                                        WifiNetwork("x", "long-enough", b"123456", 1, 0),
                                        client_factory=FakeGatt))


def test_stale_reply_cannot_satisfy_new_attempt_and_disconnect_is_bounded():
    first, second = FakeGatt(wifi_ack=0), FakeGatt()
    stale = []
    original_stop = first.stop_notify

    async def save_callback(characteristic):
        if characteristic[-2:] == "09":
            stale.append(first.callbacks.get("09"))
        await original_stop(characteristic)

    first.stop_notify = save_callback

    async def confirmed():
        if stale and stale[0]:
            stale[0]("09", b"\xff\xfe" + cbor2.dumps({"s": 1}))
            stale[0]("09", b"\xff\xff")
        return True

    result = run(lambda device: first if not first.disconnected else second, confirmed)
    assert result.wifi_retried and second.finished == 1

    failures = [FakeGatt(connect_error=True) for _ in range(3)]
    with pytest.raises(ProvisioningError, match="connection budget"):
        run(lambda device: failures.pop(0))
    assert not failures


def test_cancellation_cleans_up_and_deadline_exhaustion_is_finite():
    async def cancelled():
        client = FakeGatt(stall=True)
        task = asyncio.create_task(provision_smartdoor(
            object(), ENDPOINT, NETWORK, client_factory=lambda device: client, timeouts=FAST))
        await asyncio.sleep(.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.disconnected

    asyncio.run(cancelled())
    tiny = ProvisioningTimeouts(overall=.01, connect=.05, phase=.05, scan=.001,
                                confirmation=.05, connection_backoff=0)
    with pytest.raises(ProvisioningTimeoutError):
        run(lambda device: FakeGatt(stall=True), timeouts=tiny)


def test_acknowledged_finish_without_confirmation_is_pending():
    result = run(lambda device: FakeGatt())
    assert result.state is ProvisioningState.PENDING
    assert result.phase == "server_confirmation"

import asyncio
import time

import cbor2
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from petsafe.provisioning import (
    PREFIX,
    MTU_UUID,
    InvalidResponseError,
    ProvisioningError,
    ProvisioningState,
    ProvisioningTimeoutError,
    ProvisioningTimeouts,
    WifiNetwork,
    WifiConnectionError,
    provision_smartdoor,
)


KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_KEY = KEY.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
ENDPOINT = "https://setup.example.test/provision/session"
NETWORK = WifiNetwork("Test Network", "synthetic-password", b"\x10\x20\x30\x40\x50\x60", 7)
FAST = ProvisioningTimeouts(overall=.5, connect=.05, phase=.05, scan=.001,
                            confirmation=.05, connection_backoff=0)


class Services:
    # Independent firmware service map: a typo in production UUIDs must fail.
    UUIDS = {"8a7f1168-48af-4efb-83b5-e679f932ff03",
             *("3113a187-4b9f-4f9a-aa83-c614e11bff" + suffix for suffix in
                         ("90", "08", "10", "01", "05", "09", "06", "07"))}

    def __init__(self):
        self.requested = []

    def get_characteristic(self, uuid):
        self.requested.append(uuid)
        return uuid if uuid in self.UUIDS else None


class FakeGatt:
    def __init__(self, *, wifi_ack=1, finish_timeout=False, malformed_key=False,
                 connect_error=False, stall=False, statuses=None,
                 partial_connect=False, hanging_disconnect=False,
                 notify_stall=None):
        self.services = Services()
        self.is_connected = False
        self.callbacks = {}
        self.buffers = {}
        self.wifi_ack = wifi_ack
        self.finish_timeout = finish_timeout
        self.malformed_key = malformed_key
        self.connect_error = connect_error
        self.stall = stall
        self.statuses = list(statuses or ["PROFILE_CONNECTED_GOOD"])
        self.status_reads = 0
        self.partial_connect = partial_connect
        self.hanging_disconnect = hanging_disconnect
        self.notify_stall = notify_stall
        self.disconnected = False
        self.disconnect_calls = 0
        self.finished = 0
        self.finish_payload_frames = 0
        self.key_requests = 0

    async def connect(self):
        if self.connect_error:
            raise ConnectionError("synthetic connect failure")
        self.is_connected = True
        if self.partial_connect:
            await asyncio.Event().wait()

    async def disconnect(self):
        self.disconnect_calls += 1
        if self.hanging_disconnect:
            await asyncio.Event().wait()
        self.is_connected = False
        self.disconnected = True

    async def start_notify(self, characteristic, callback):
        suffix = characteristic[-2:]
        self.callbacks[suffix] = callback
        if suffix == self.notify_stall:
            await asyncio.Event().wait()

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
            self.status_reads += 1
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            self.notify("06", {"w": status})
        return b""

    async def write_gatt_char(self, characteristic, frame, response=True):
        suffix = characteristic[-2:]
        if self.stall:
            await asyncio.Event().wait()
        if suffix == "07" and frame != b"\xff\xff":
            self.finish_payload_frames += 1
        if suffix == "07" and frame == b"\xff\xff" and self.finish_timeout:
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
    assert MTU_UUID in client.services.requested


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
    assert client.finish_payload_frames > 0


def test_finish_only_recovery_checks_fresh_status_first():
    first, recovery = FakeGatt(finish_timeout=True), FakeGatt()
    clients = [first, recovery]
    first_check = None

    async def confirmed():
        nonlocal first_check
        if first_check is None:
            first_check = time.monotonic()
        if recovery.finished:
            assert time.monotonic() - first_check >= FAST.confirmation
            return True
        return False

    result = run(lambda device: clients.pop(0), confirmed)
    assert result.state is ProvisioningState.COMPLETE and result.finish_retried
    assert not clients


def test_status_polls_protocol_pending_state_until_good():
    client = FakeGatt(statuses=["UNKNOWN", "UNPROVISIONED", "PROFILE_CONNECTED_GOOD"])

    async def confirmed():
        return True

    assert run(lambda device: client, confirmed).state is ProvisioningState.COMPLETE
    assert client.status_reads == 3


def test_status_rejects_protocol_failure_and_malformed_value():
    for status, error in (("WRONG_PASSWORD", WifiConnectionError), (7, InvalidResponseError)):
        with pytest.raises(error):
            run(lambda device, status=status: FakeGatt(statuses=[status]))


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
    for value in (float("nan"), float("inf"), -1):
        invalid = ProvisioningTimeouts(connection_backoff=value)
        with pytest.raises(ProvisioningError):
            asyncio.run(provision_smartdoor(object(), ENDPOINT, NETWORK,
                                            client_factory=FakeGatt, timeouts=invalid))
    assert "synthetic-password" not in repr(NETWORK)


@pytest.mark.parametrize("status", (None, True, 2, "0"))
def test_malformed_wifi_ack_is_not_retried(status):
    clients = [FakeGatt(wifi_ack=status), FakeGatt()]
    with pytest.raises(InvalidResponseError):
        run(lambda device: clients.pop(0))
    assert len(clients) == 1


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


def test_partial_connect_cancellation_attempts_bounded_disconnect():
    async def check(hanging_disconnect):
        client = FakeGatt(partial_connect=True, hanging_disconnect=hanging_disconnect)
        task = asyncio.create_task(provision_smartdoor(
            object(), ENDPOINT, NETWORK, client_factory=lambda device: client, timeouts=FAST))
        await asyncio.sleep(.005)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, .15)
        assert client.disconnect_calls == 1
        assert time.monotonic() - started < .15

    asyncio.run(check(False))
    asyncio.run(check(True))


def test_notification_and_scan_cancellation_clean_owned_state():
    async def cancel_when(client, ready, timeouts=FAST):
        task = asyncio.create_task(provision_smartdoor(
            object(), ENDPOINT, NETWORK, client_factory=lambda device: client,
            timeouts=timeouts))
        for _ in range(100):
            if ready(client):
                break
            await asyncio.sleep(.001)
        else:
            raise AssertionError("fake did not reach cancellation point")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, .15)
        assert not client.callbacks and client.disconnect_calls == 1

    partial = FakeGatt(notify_stall="08")
    asyncio.run(cancel_when(partial, lambda client: "08" in client.callbacks))
    scanning = FakeGatt()
    slow_scan = ProvisioningTimeouts(overall=.5, connect=.05, phase=.25, scan=.2,
                                     confirmation=.05, connection_backoff=0)
    asyncio.run(cancel_when(scanning, lambda client: "01" in client.callbacks, slow_scan))


def test_acknowledged_finish_without_confirmation_is_pending():
    result = run(lambda device: FakeGatt())
    assert result.state is ProvisioningState.PENDING
    assert result.phase == "server_confirmation"


def test_uncertain_finish_without_confirmation_does_not_reconnect_or_replay():
    clients = [FakeGatt(finish_timeout=True)]
    result = run(lambda device: clients.pop(0))
    assert result.state is ProvisioningState.PENDING
    assert result.phase == "finish_uncertain" and not result.finish_retried
    assert not clients


def test_confirmation_requires_literal_true():
    async def truthy_but_not_confirmation():
        return {"connected": True}

    assert run(lambda device: FakeGatt(), truthy_but_not_confirmation).state is ProvisioningState.PENDING


def test_negative_server_checks_wait_for_late_success_without_finish_replay():
    client = FakeGatt(finish_timeout=True)
    first_check = None
    checks = 0

    async def confirmed():
        nonlocal first_check, checks
        checks += 1
        if first_check is None:
            first_check = time.monotonic()
        return time.monotonic() - first_check >= .015

    result = run(lambda device: client, confirmed)
    assert result.state is ProvisioningState.COMPLETE
    assert checks > 1 and not result.finish_retried
    assert client.disconnect_calls == 1

import asyncio
import gzip
import json

import httpx
import pytest

import petsafe
from petsafe import pets
from petsafe.devices import DeviceSmartDoor
from petsafe.local import MAX_EVENT_BYTES, MAX_RESPONSE_BYTES


BASE = "https://local.example.test"
TOKEN = "synthetic-token"


def run(awaitable):
    return asyncio.run(awaitable)


def test_routes_devices_pets_and_shared_client_lifecycle():
    requests = []

    async def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.extensions["timeout"] == {
            "connect": 60, "read": 60, "write": 60, "pool": 60,
        }
        path = request.url.path
        if path == "/local/smartdoor/product/product":
            return httpx.Response(200, json={"data": [{"thingName": "door-test"}]})
        if path == "/local/preferences/product/smartdoor/door-test":
            return httpx.Response(200, json={"data": {"preferenceData": {
                "friendlyName": "Test Door", "tz": "Etc/UTC"}}})
        if path == "/local/pets/pets":
            return httpx.Response(200, json={"data": [{"id": "pet-test"}]})
        if path == "/local/directory/petProduct":
            assert request.url.params["petId"] == "pet test"
            return httpx.Response(200, json={"data": [{"thingName": "door-test"}]})
        if path.endswith("/shadow"):
            assert json.loads(request.content) == {"door": {"mode": "MANUAL_LOCKED"}}
            return httpx.Response(204)
        raise AssertionError(str(request.url))

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = petsafe.LocalPetSafeClient(BASE, TOKEN, client=http)
            doors = await client.get_smartdoors()
            assert len(doors) == 1 and isinstance(doors[0], DeviceSmartDoor)
            assert doors[0].friendly_name == "Test Door" and doors[0].timezone == "Etc/UTC"
            assert await client.get_feeders() == [] and await client.get_litterboxes() == []
            assert await pets.list_pets(client) == [{"id": "pet-test"}]
            assert await pets.list_pet_products(client, "pet test") == [{"thingName": "door-test"}]
            await doors[0].lock(update_data=False)
            await client.aclose()
            assert not http.is_closed

    run(check())
    assert all(request.url.host == "local.example.test" for request in requests)


@pytest.mark.parametrize("path", [
    "https://cloud.example.test/pets/pets", "//cloud.example.test/pets/pets",
    "/pets/pets", "../pets/pets", "pets/../pets", "pets/%2e%2e/pets",
    "pets/%252e%252e/pets", "%2f%2fcloud.example.test/pets", r"pets\\..\\pets",
    "pets/pets#fragment",
])
def test_paths_fail_closed_before_network(path):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(ValueError):
                await petsafe.LocalPetSafeClient(BASE, TOKEN, client=http).api_get(path)

    run(check())
    assert calls == 0


def test_non_2xx_redirect_size_token_and_owned_lifecycle():
    outcomes = iter((
        httpx.Response(401, text=TOKEN),
        httpx.Response(307, headers={"location": "https://cloud.example.test/secret"}),
        httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
    ))
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return next(outcomes)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True,
        ) as http:
            client = petsafe.LocalPetSafeClient(BASE, TOKEN, client=http)
            assert TOKEN not in repr(client)
            for status in (401, 307):
                with pytest.raises(httpx.HTTPStatusError) as raised:
                    await client.api_get("pets/pets")
                assert raised.value.response.status_code == status
            with pytest.raises(httpx.HTTPError, match="too large"):
                await client.api_get("pets/pets")
        owned = petsafe.LocalPetSafeClient(BASE, TOKEN)
        http = owned._client
        await owned.aclose()
        assert http.is_closed

    run(check())
    assert calls == 3


@pytest.mark.parametrize("server_url", [
    "http://local.example.test", "https://user@local.example.test",
    "https://local.example.test/path", "https://local.example.test?query",
    "https://local.example.test#fragment",
])
def test_server_url_must_be_an_https_origin(server_url):
    with pytest.raises(ValueError):
        petsafe.LocalPetSafeClient(server_url, TOKEN)


def test_non_default_https_port_is_part_of_the_origin():
    client = petsafe.LocalPetSafeClient("https://local.example.test:8443", TOKEN)
    assert client._request_url("pets/pets") == "https://local.example.test:8443/local/pets/pets"
    run(client.aclose())


def test_compressed_response_is_returned_once_decoded():
    body = b'{"data":[]}'

    async def check():
        response = httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-length": "999"},
            content=gzip.compress(body),
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response),
        ) as http:
            returned = await petsafe.LocalPetSafeClient(BASE, TOKEN, client=http).api_get(
                "smartdoor/product/product"
            )
            assert returned.content == body
            assert "content-encoding" not in returned.headers
            assert returned.headers["content-length"] == str(len(body))

    run(check())


@pytest.mark.parametrize("payload", [{"data": {}}, {"data": ["not-a-device"]}, 1])
def test_smartdoor_collection_shape_is_validated(payload):
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        ) as http:
            with pytest.raises(ValueError, match="SmartDoor devices"):
                await petsafe.LocalPetSafeClient(BASE, TOKEN, client=http).get_smartdoors()

    run(check())


def test_iter_events_parses_snapshots_and_closes_stream():
    snapshot = {"version": 1, "revision": 7, "brokerConnected": True,
                "door": {"thingName": "door-test"}, "pets": [], "links": {},
                "activity": [], "override": None, "rfidScan": None,
                "setupSession": {"state": "expired"},
                "command": None, "firmware": {"state": "idle"},
                "futureField": {"addedWithoutClientRelease": True},
                "account": {"firstName": "Local"}, "notifications": {"enabled": False},
                "capabilities": {"rfidScan": True}}

    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            encoded = json.dumps(snapshot)
            split = encoded.index('"door"')
            yield (": heartbeat\n\nevent: snapshot\nid: 7\ndata: " + encoded[:split]
                   + "\ndata: " + encoded[split:] + "\n\n").encode()

        async def aclose(self):
            self.closed = True

    stream = Stream()

    async def handler(request):
        assert request.url == BASE + "/local/events"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["accept"] == "text/event-stream"
        assert request.extensions["timeout"]["read"] == 45
        return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"},
                              stream=stream)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            events = petsafe.LocalPetSafeClient(BASE, TOKEN, client=http).iter_events()
            assert await events.__anext__() == snapshot
            await events.aclose()
            assert stream.closed

    run(check())


@pytest.mark.parametrize("body, message", [
    (b"event: snapshot\ndata: {}\n\n", "schema"),
    (b"event: snapshot\ndata: {\"version\":NaN}\n\n", "JSON"),
    (b":" + b"x" * MAX_EVENT_BYTES + b"\n", "too large"),
])
def test_iter_events_rejects_invalid_or_oversized_frames(body, message):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                               content=body))) as http:
            with pytest.raises(ValueError, match=message):
                await petsafe.LocalPetSafeClient(BASE, TOKEN, client=http).iter_events().__anext__()

    run(check())


def test_iter_events_preserves_http_status_errors():
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(401))) as http:
            with pytest.raises(httpx.HTTPStatusError) as raised:
                await petsafe.LocalPetSafeClient(BASE, TOKEN, client=http).iter_events().__anext__()
            assert raised.value.response.status_code == 401

    run(check())


def test_iter_events_sanitizes_deep_schema_recursion():
    nested = "[]"
    for _ in range(500):
        nested = "[" + nested + "]"
    payload = ('{"version":1,"revision":0,"brokerConnected":true,'
               '"door":{"nested":' + nested
               + '},"pets":[],"links":{},"activity":[],"override":null}')
    with pytest.raises(ValueError, match="snapshot schema"):
        petsafe.LocalPetSafeClient._snapshot("snapshot", [payload])


@pytest.mark.parametrize("field,value", [
    ("rfidScan", []), ("setupSession", []), ("account", None),
    ("command", []), ("notifications", []), ("capabilities", []), ("firmware", None),
])
def test_snapshot_optional_management_fields_are_typed(field, value):
    snapshot = {"version": 1, "revision": 0, "brokerConnected": False,
                "door": {}, "pets": [], "links": {}, "activity": [], "override": None,
                field: value}
    with pytest.raises(ValueError, match="snapshot schema"):
        petsafe.LocalPetSafeClient._validate_snapshot(snapshot)


def test_local_management_helpers_cover_model_routes():
    calls = []
    pet_id, door_id, scan_id = "pet-id", "door-id", "scan-id"
    snapshot = {"version": 1, "revision": 3, "brokerConnected": False,
                "door": {"thingName": door_id}, "pets": [], "links": {},
                "activity": [], "override": None, "rfidScan": None,
                "setupSession": None,
                "account": {}, "notifications": {}, "capabilities": {"events": True}}

    async def handler(request):
        request_data = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, dict(request.url.params), request_data))
        route = request.url.path.removeprefix("/local/")
        if route == "state":
            return httpx.Response(200, json={"data": snapshot})
        if route == "capabilities":
            return httpx.Response(200, json={"data": {"events": True}})
        if route == "directory/products":
            return httpx.Response(200, json={"data": [{"productId": door_id}]})
        if route == "pets/pets" and request.method == "GET":
            return httpx.Response(200, json={"data": [{"petId": pet_id}]})
        if route == "pets/pets" and request.method == "POST":
            return httpx.Response(200, json={"data": {"petId": pet_id}})
        if route == "pets/pets/" + pet_id and request.method == "PATCH":
            return httpx.Response(200, json={"data": [{"petId": pet_id, "profile": request_data}]})
        if route == "pets/pets/" + pet_id and request.method == "DELETE":
            return httpx.Response(200, json={"data": None, "status": True})
        if route.endswith("/identifier") and request.method == "PUT":
            return httpx.Response(200, json={"data": {"petId": pet_id, "technologies": [request_data]}})
        if route.endswith("/identifier") and request.method == "DELETE":
            return httpx.Response(200, json={"data": "Identifier removed"})
        if route == "directory/petProduct" and request.method == "GET":
            return httpx.Response(200, json={"data": [{"petId": pet_id, "productId": door_id}]})
        if route == "directory/petProduct" and request.method == "PUT":
            return httpx.Response(200, json={"data": request_data})
        if route == "directory/petProduct" and request.method == "DELETE":
            return httpx.Response(200, json={"data": None, "status": True})
        if route == "directory/account" and request.method == "GET":
            return httpx.Response(200, json={"data": {"firstName": "Local"}})
        if route == "directory/account" and request.method == "PATCH":
            return httpx.Response(200, json={"data": request_data})
        if route == "directory/product-preferences/" + door_id:
            return httpx.Response(200, json={"data": {"productPreferences": {
                "thingName": door_id, "preferences": {"sendPetActivityNotifications": False}}}})
        if route == "directory/product-preferences" and request.method == "PUT":
            return httpx.Response(200, json={"data": request_data})
        command_route = "smartdoor/product/product/{}/commands".format(door_id)
        command = {"id": "command-id", "code": "REPORT_STATUS", "state": "submitted",
                   "confirmed": False, "responseReceived": False,
                   "submittedAt": "2026-10-24T12:00:00+00:00", "deadline": 123.0}
        if route == command_route and request.method == "GET":
            return httpx.Response(200, json={"data": command})
        if route == command_route and request.method == "POST":
            return httpx.Response(202, json={"data": dict(command, code=request_data["code"])})
        firmware_route = "smartdoor/product/product/{}/firmware".format(door_id)
        if route == firmware_route and request.method == "GET":
            return httpx.Response(200, json={"data": {"artifacts": [{"artifactId": "fw-id"}],
                                                        "job": None}})
        if route == firmware_route + "/artifacts" and request.method == "POST":
            return httpx.Response(201, json={"data": request_data})
        if route == firmware_route + "/jobs" and request.method == "POST":
            return httpx.Response(202, json={"data": {"id": "job-id", **request_data}})
        if route == firmware_route + "/artifacts/" + ("ab" * 32) and request.method == "DELETE":
            return httpx.Response(204)
        if route == firmware_route + "/jobs/job-id" and request.method == "DELETE":
            return httpx.Response(200, json={"data": {"id": "job-id", "state": "cancelled"}})
        scan_base = "smartdoor/product/product/{}/rfid-scans".format(door_id)
        if route == scan_base and request.method == "POST":
            return httpx.Response(202, json={"data": {"id": scan_id, "state": "scanning"}})
        if route in (scan_base, scan_base + "/" + scan_id) and request.method == "GET":
            return httpx.Response(200, json={"data": {"id": scan_id, "state": "captured"}})
        if route == scan_base + "/" + scan_id and request.method == "DELETE":
            return httpx.Response(200, json={"data": {"id": scan_id, "state": "cancelled"}})
        if route == scan_base + "/" + scan_id + "/assign":
            return httpx.Response(200, json={"data": {"id": scan_id, "state": "assigned"}})
        raise AssertionError((request.method, route))

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = petsafe.LocalPetSafeClient(BASE, TOKEN, client=http)
            assert await client.get_state() == snapshot
            assert await client.get_capabilities() == {"events": True}
            assert (await client.get_product_summaries())[0]["productId"] == door_id
            assert (await client.list_pets())[0]["petId"] == pet_id
            assert (await client.create_pet({"profile": {"name": "Pet"}}))["petId"] == pet_id
            assert (await client.update_pet(pet_id, {"profile": {"name": "New"}}))["petId"] == pet_id
            assert await client.delete_pet(pet_id) is None
            assert (await client.set_pet_identifier(
                pet_id, door_id, "123456789012345", "MICROCHIP"))["technologies"]
            assert await client.delete_pet_identifiers(pet_id, door_id) == "Identifier removed"
            assert (await client.get_pet_links(pet_id))[0]["productId"] == door_id
            assert (await client.get_door_links(door_id))[0]["petId"] == pet_id
            assert (await client.link_pet(pet_id, door_id, "smartdoor_1_large"))[0]["relationType"] == "use"
            assert await client.unlink_pet(pet_id, door_id) is None
            assert (await client.get_account())["firstName"] == "Local"
            account = await client.update_account({"receiveNotifications": True})
            assert account["receiveNotifications"] is True
            flags = await client.get_notifications(door_id)
            assert flags["sendPetActivityNotifications"] is False
            assert (await client.update_notifications(door_id, flags)) == flags
            assert (await client.get_last_command(door_id))["state"] == "submitted"
            assert (await client.submit_command(door_id, "REPORT_STATUS"))["confirmed"] is False
            assert (await client.submit_command(
                door_id, "MEMFAULT_CONFIG", {"mask": 0xffffffff, "time": 0}))["code"] == "MEMFAULT_CONFIG"
            assert (await client.submit_command(
                door_id, "CLI", {"TESTCMD": "status"}))["code"] == "CLI"
            digest = "aB" * 32
            assert (await client.get_firmware(door_id))["artifacts"][0]["artifactId"] == "fw-id"
            staged = await client.stage_firmware(door_id, "fw-id", "1.2.3", digest)
            assert staged["sha256"] == digest.lower()
            assert (await client.install_firmware(door_id, digest))["id"] == "job-id"
            assert await client.delete_firmware_artifact(door_id, digest) is None
            assert (await client.cancel_firmware_job(door_id, "job-id"))["state"] == "cancelled"
            assert (await client.start_rfid_scan(door_id, "MICROCHIP"))["id"] == scan_id
            assert (await client.get_rfid_scan(door_id))["state"] == "captured"
            assert (await client.get_rfid_scan(door_id, scan_id))["state"] == "captured"
            assert (await client.cancel_rfid_scan(door_id, scan_id))["state"] == "cancelled"
            assert (await client.assign_rfid_scan(door_id, scan_id, pet_id))["state"] == "assigned"

    run(check())
    put_calls = [call for call in calls if call[0] == "PUT"]
    assert any(isinstance(call[3], list) for call in put_calls)


def test_scan_rfid_consumes_push_until_candidate_is_restored():
    scan_id = "scan-id"
    initial = {"id": scan_id, "state": "scanning", "restored": False}

    def snapshot(revision, restored):
        return {"version": 1, "revision": revision, "brokerConnected": True,
                "door": {}, "pets": [], "links": {}, "activity": [], "override": None,
                "rfidScan": {"id": scan_id, "state": "captured", "restored": restored,
                             "tag": {"tagId": "123456789012345", "technology": "MICROCHIP",
                                     "frequency": "FDXB"}}}

    body = b"".join(b"event: snapshot\ndata: " + json.dumps(snapshot(revision, restored)).encode()
                    + b"\n\n" for revision, restored in ((1, False), (2, True)))

    async def handler(request):
        if request.url.path.endswith("/rfid-scans"):
            return httpx.Response(202, json={"data": initial})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        raise AssertionError(str(request.url))

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = petsafe.LocalPetSafeClient(BASE, TOKEN, client=http)
            updates = [update async for update in client.scan_rfid("door-id", "MICROCHIP")]
            assert [update["state"] for update in updates] == ["scanning", "captured", "captured"]
            assert updates[-1]["restored"] is True

    run(check())


def test_smartdoor_single_schedule_and_clear_override_helpers():
    async def handler(request):
        if request.method == "PUT":
            return httpx.Response(200, json={"data": [json.loads(request.content)]})
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"scheduleId": "schedule-id"}})
        assert request.method == "DELETE"
        return httpx.Response(204)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            door = DeviceSmartDoor(petsafe.LocalPetSafeClient(BASE, TOKEN, client=http),
                                   {"thingName": "door-id"})
            assert (await door.get_schedule("schedule-id"))["scheduleId"] == "schedule-id"
            updated = await door.update_schedule({"scheduleId": "schedule-id"}, update_data=False)
            assert updated[0]["thingName"] == "door-id"
            await door.clear_override_schedule(update_data=False)

    run(check())


@pytest.mark.parametrize("value", ["", ".", "..", "part/other", r"part\other"])
def test_management_route_segments_fail_closed(value):
    client = petsafe.LocalPetSafeClient(BASE, TOKEN)
    with pytest.raises(ValueError):
        client._scan_path(value)
    run(client.aclose())


@pytest.mark.parametrize("code,data", [
    ("UNKNOWN", {}), ("REBOOT", {"unexpected": True}),
    ("MEMFAULT_CONFIG", {"mask": -1, "time": 0}),
    ("MEMFAULT_CONFIG", {"mask": True, "time": 0}),
    ("CLI", {"TESTCMD": ""}), ("CLI", {"TESTCMD": "line\nfeed"}),
    ("CLI", {"TESTCMD": "x" * 65}),
])
def test_native_command_payloads_fail_before_network(code, data):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(202)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(ValueError, match="maintenance command"):
                await petsafe.LocalPetSafeClient(BASE, TOKEN, client=http).submit_command(
                    "door-id", code, data)

    run(check())
    assert calls == 0


@pytest.mark.parametrize("action", [
    lambda client: client.stage_firmware("door-id", "fw-id", "1.0", "not-a-hash"),
    lambda client: client.stage_firmware("door-id", "", "1.0", "a" * 64),
    lambda client: client.install_firmware("door-id", "a" * 63),
    lambda client: client.delete_firmware_artifact("door-id", "g" * 64),
    lambda client: client.cancel_firmware_job("door-id", "../job"),
])
def test_firmware_inputs_fail_before_network(action):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(ValueError):
                await action(petsafe.LocalPetSafeClient(BASE, TOKEN, client=http))

    run(check())
    assert calls == 0

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
                "activity": [], "override": None}

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

import asyncio
import json

import httpx
import pytest

from petsafe.setup import InvalidSetupResponse, SetupAPIError, SetupClient, SetupSession


BASE = "https://setup.example.test"
TOKEN = "synthetic-token"
DEVICE = "001122AABBCC"
SESSION = "87654321-4321-6789-a234-678943216789"


def payload(**changes):
    value = {
        "session_id": SESSION,
        "device_id": DEVICE,
        "mode": "provision",
        "endpoint": f"{BASE}/provision/{SESSION}",
        "expires_at": 2_000_000_000,
        "state": "pending",
        "provisioned": False,
        "online": False,
        "reported": False,
    }
    value.update(changes)
    return value


def run(awaitable):
    return asyncio.run(awaitable)


def session(**changes):
    return SetupSession(**payload(**changes))


def test_create_and_bound_confirmation_are_correlated():
    requests = []

    async def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if request.method == "POST":
            assert json.loads(request.content) == {"device_id": DEVICE, "mode": "provision"}
            return httpx.Response(201, json=payload())
        return httpx.Response(200, json=payload(
            state="complete", provisioned=True, online=True, reported=True))

    async def check():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = SetupClient(BASE, TOKEN, client=http, timeout=1)
            session = await client.create(DEVICE, "provision")
            assert session.session_id == SESSION and not session.confirmed
            assert await client.confirmation(session)() is True
            await client.aclose()
            assert not http.is_closed
        assert [request.method for request in requests] == ["POST", "GET"]
        assert requests[1].url == httpx.URL(f"{BASE}/setup/sessions/{SESSION}")

    run(check())


@pytest.mark.parametrize("mode,evidence,expected", [
    ("provision", (True, True, True), True),
    ("provision", (False, True, True), False),
    ("provision", (True, False, True), False),
    ("provision", (True, True, False), False),
    ("reconnect", (False, True, True), True),
    ("reconnect", (True, True, True), False),
    ("reconnect", (False, False, True), False),
])
def test_confirmation_requires_exact_boolean_mode_evidence(mode, evidence, expected):
    async def handler(request):
        return httpx.Response(200, json=payload(
            mode=mode, state="complete", provisioned=evidence[0],
            online=evidence[1], reported=evidence[2]))

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = SetupClient(BASE, TOKEN, client=http)
            assert await client.confirmation(session(mode=mode))() is expected

    run(check())


def test_pending_never_confirms():
    response = payload(state="pending", provisioned=True, online=True, reported=True)

    async def handler(request):
        return httpx.Response(200, json=response)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = SetupClient(BASE, TOKEN, client=http)
            assert await client.confirmation(session())() is False

    run(check())


@pytest.mark.parametrize("status", [400, 401, 404, 409, 410, 503])
def test_bad_status_is_bounded_and_does_not_disclose_credentials(status):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, text=TOKEN)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = SetupClient(BASE, TOKEN, client=http)
            with pytest.raises(SetupAPIError) as raised:
                await client.create(DEVICE, "provision")
            assert raised.value.status_code == status
            assert TOKEN not in str(raised.value) and TOKEN not in repr(client)
        assert calls == 1

    run(check())


def test_redirect_is_not_followed_or_accepted():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"location": "https://attacker.test/setup/sessions"})

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True,
        ) as http:
            client = SetupClient(BASE, TOKEN, client=http)
            with pytest.raises(SetupAPIError, match="HTTP 307"):
                await client.create(DEVICE, "provision")
        assert calls == 1

    run(check())


@pytest.mark.parametrize("change", [
    {"unexpected": True},
    {"session_id": "not-a-uuid"},
    {"device_id": DEVICE.lower()},
    {"endpoint": f"https://attacker.test/provision/{SESSION}"},
    {"endpoint": f"https://setup.example.test:8443/provision/{SESSION}"},
    {"endpoint": f"{BASE}/provision/{DEVICE}"},
    {"expires_at": -1},
    {"expires_at": True},
    {"state": "done"},
    {"mode": "repair"},
    {"reported": 1},
])
def test_malformed_responses_fail_closed(change):
    async def handler(request):
        return httpx.Response(201, json=payload(**change))

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(InvalidSetupResponse):
                await SetupClient(BASE, TOKEN, client=http).create(DEVICE, "provision")

    run(check())


@pytest.mark.parametrize("response", [
    httpx.Response(201, content=b"{}"),
    httpx.Response(201, headers={"content-type": "application/json"}, content=b"{"),
    httpx.Response(201, headers={"content-type": "application/json"}, content=b"x" * 16385),
    httpx.Response(201, headers={"content-type": "application/json"},
                   content=json.dumps(payload()).replace("2000000000", "Infinity").encode()),
])
def test_response_media_json_and_size_are_bounded(response):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as http:
            with pytest.raises(InvalidSetupResponse):
                await SetupClient(BASE, TOKEN, client=http).create(DEVICE, "provision")

    run(check())


@pytest.mark.parametrize("change", [
    {"session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
    {"device_id": "FFEEDDCCBBAA"},
    {"mode": "reconnect"},
])
def test_status_rejects_stale_or_cross_session_identity(change):
    async def handler(request):
        return httpx.Response(200, json=payload(**change))

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = SetupClient(BASE, TOKEN, client=http)
            with pytest.raises(InvalidSetupResponse, match="identity"):
                await client.status(session())

    run(check())


def test_cancel_and_owned_client_lifecycle():
    seen = []

    async def handler(request):
        seen.append((request.method, str(request.url)))
        return httpx.Response(204)

    async def check_cancel():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = SetupClient(BASE, TOKEN, client=http)
            assert await client.cancel(session()) is None
            await client.aclose()
            assert not http.is_closed

    async def check_owned():
        client = SetupClient(BASE, TOKEN)
        owned = client._client
        await client.aclose()
        assert owned.is_closed

    run(check_cancel())
    run(check_owned())
    assert seen == [("DELETE", f"{BASE}/setup/sessions/{SESSION}")]


@pytest.mark.parametrize("base", [
    "http://setup.example.test", "https://user@setup.example.test",
    "https://setup.example.test/path", "https://setup.example.test?",
    "https://setup.example.test#fragment", "https://setup.example.test:8443",
])
def test_base_must_be_an_https_origin(base):
    with pytest.raises(ValueError):
        SetupClient(base, TOKEN)


def test_request_inputs_are_validated_before_network_use():
    client = SetupClient(BASE, TOKEN)
    with pytest.raises(ValueError):
        run(client.create(DEVICE.lower(), "provision"))
    with pytest.raises(ValueError):
        run(client.status(session(device_id=DEVICE.lower())))
    with pytest.raises(ValueError):
        run(client.create(DEVICE, "other"))
    run(client.aclose())
    for token in ("", "space token", "secret\nvalue"):
        with pytest.raises(ValueError) as raised:
            SetupClient(BASE, token)
        assert not token or token not in str(raised.value)


def test_confirmation_waits_through_observer_outage_but_rejects_bad_auth():
    outcomes = iter((503, "timeout", 200, 401))

    async def handler(request):
        result = next(outcomes)
        if result == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(result, json=payload(
            state="complete", provisioned=True, online=True, reported=True))

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            confirm = SetupClient(BASE, TOKEN, client=http).confirmation(session())
            assert await confirm() is False
            assert await confirm() is False
            assert await confirm() is True
            with pytest.raises(SetupAPIError) as raised:
                await confirm()
            assert raised.value.status_code == 401

    run(check())

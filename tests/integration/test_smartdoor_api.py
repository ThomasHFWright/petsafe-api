import json
import sys
import time
from contextlib import asynccontextmanager
from urllib.parse import quote_plus

import httpx
import pytest

from petsafe.client import PetSafeClient
from petsafe.devices import DeviceSmartDoor


@asynccontextmanager
async def _create_client(mock_handler):
    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as httpx_client:
        client = PetSafeClient(
            email="user@example.com",
            id_token="test-id-token",
            refresh_token="test-refresh-token",
            access_token="test-access-token",
            client=httpx_client,
        )
        client._token_expires_time = time.time() + 3600  # noqa: SLF001 - test setup
        yield client


def _assert_response_printed(capsys, expected_text: str) -> None:
    captured = capsys.readouterr().out
    assert expected_text in captured
    # Re-emit the captured output so `pytest -s` users see the mocked payloads.
    sys.__stdout__.write(captured)
    sys.__stdout__.flush()


@pytest.mark.asyncio
async def test_get_smartdoors_prints_response_body(capsys):
    payload = {
        "data": [
            {
                "thingName": "door-1",
                "shadow": {"state": {"reported": {"door": {"mode": "SMART"}}}},
            },
            {
                "thingName": "door-2",
                "shadow": {"state": {"reported": {"door": {"mode": "MANUAL"}}}},
            },
        ]
    }
    body_text = json.dumps(payload)
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(request.url)
        return httpx.Response(200, text=body_text)

    async with _create_client(handler) as client:
        smartdoors = await client.get_smartdoors()
        assert [door.api_name for door in smartdoors] == ["door-1", "door-2"]
        print(body_text)

    _assert_response_printed(capsys, body_text)
    assert requested_urls and requested_urls[0].path == "/smartdoor/product/product"


@pytest.mark.asyncio
async def test_get_single_smartdoor_prints_response_body(capsys):
    payload = {
        "data": {
            "thingName": "primary-door",
            "shadow": {"state": {"reported": {"door": {"mode": "SMART"}}}},
        }
    }
    body_text = json.dumps(payload)
    requested_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(200, text=body_text)

    async with _create_client(handler) as client:
        smartdoor = await client.get_smartdoor("primary-door")
        assert smartdoor.api_name == "primary-door"
        print(body_text)

    _assert_response_printed(capsys, body_text)
    assert requested_paths == ["/smartdoor/product/product/primary-door/"]


@pytest.mark.asyncio
async def test_smartdoor_activity_api_prints_response_body(capsys):
    payload = {"data": [{"event": "LOCK"}, {"event": "UNLOCK"}]}
    body_text = json.dumps(payload)
    limit = 5
    since = "2023-08-01T12:00:00Z"
    expected_query = f"limit={limit}&since={quote_plus(since)}"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/smartdoor/product/product/door-1/activity"
        assert request.url.query.decode() == expected_query
        return httpx.Response(200, text=body_text)

    async with _create_client(handler) as client:
        smartdoor = DeviceSmartDoor(client, {"thingName": "door-1"})
        activity = await smartdoor.get_activity(limit=limit, since=since)
        assert activity == payload["data"]
        print(body_text)

    _assert_response_printed(capsys, body_text)

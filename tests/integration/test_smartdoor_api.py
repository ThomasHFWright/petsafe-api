"""Integration coverage for the SmartDoor API endpoints using live credentials."""
from __future__ import annotations

import json
from typing import Iterable

import pytest

from petsafe.client import PetSafeClient
from petsafe.devices import DeviceSmartDoor


def _dump_response(response_json: object) -> str:
    """Return a pretty JSON string for captured API responses."""

    return json.dumps(response_json, indent=2, sort_keys=True)


def _ensure_smartdoor_available(smartdoors: Iterable[DeviceSmartDoor]) -> DeviceSmartDoor:
    try:
        return next(iter(smartdoors))
    except StopIteration:
        pytest.skip("No SmartDoor devices are associated with this PetSafe account.")


@pytest.mark.asyncio
async def test_get_smartdoors_uses_live_api(authenticated_client: PetSafeClient) -> None:
    response = await authenticated_client.api_get("smartdoor/product/product")
    payload = response.json()
    print(_dump_response(payload))

    smartdoors = await authenticated_client.get_smartdoors()
    assert isinstance(smartdoors, list)
    if isinstance(payload, dict) and "data" in payload:
        assert len(smartdoors) == len(payload["data"])


@pytest.mark.asyncio
async def test_get_single_smartdoor_uses_live_api(authenticated_client: PetSafeClient) -> None:
    smartdoors = await authenticated_client.get_smartdoors()
    door = _ensure_smartdoor_available(smartdoors)

    response = await authenticated_client.api_get(f"smartdoor/product/product/{door.api_name}/")
    payload = response.json()
    print(_dump_response(payload))

    fresh = await authenticated_client.get_smartdoor(door.api_name)
    assert isinstance(fresh, DeviceSmartDoor)
    assert fresh.api_name == door.api_name


@pytest.mark.asyncio
async def test_smartdoor_activity_uses_live_api(authenticated_client: PetSafeClient) -> None:
    smartdoors = await authenticated_client.get_smartdoors()
    door = _ensure_smartdoor_available(smartdoors)

    limit = 5
    response = await authenticated_client.api_get(f"{door.api_path}activity?limit={limit}")
    payload = response.json()
    print(_dump_response(payload))

    activity = await door.get_activity(limit=limit)
    assert isinstance(activity, list)
    if isinstance(payload, dict) and "data" in payload:
        assert activity == payload["data"]

"""Pytest fixtures for PetSafe integration tests."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx
import pytest

from petsafe.client import InvalidCodeException, PetSafeClient

from .secret_store import SecretStore


@pytest.fixture(scope="session")
def secret_store() -> SecretStore:
    """Shared secret store for all integration tests."""

    return SecretStore()


async def _tokens_valid(client: PetSafeClient) -> bool:
    """Check whether the currently stored tokens allow API access."""

    if not client.id_token or not client.refresh_token or not client.access_token:
        return False

    try:
        await client.api_get("smart-feed/feeders")
    except httpx.HTTPStatusError as exc:  # pragma: no cover - network interaction
        if exc.response.status_code in {401, 403}:
            return False
        raise
    return True


async def _authenticate_with_code(
    client: PetSafeClient, store: SecretStore, *, force_new_code: bool = False
) -> None:
    await client.request_code()
    if force_new_code:
        # Delete a stale code so that we prompt the user again.
        store.data.pop("code", None)
    code = store.prompt(
        "code",
        prompt=f"Enter the PetSafe verification code sent to {client._email}: ",
        secret=True,
    )
    try:
        await client.request_tokens_from_code(code)
    except InvalidCodeException:
        # If the stored code is stale prompt the user again.
        store.data.pop("code", None)
        store.save()
        raise
    store.update(
        {
            "id_token": client.id_token,
            "refresh_token": client.refresh_token,
            "access_token": client.access_token,
        }
    )


@pytest.fixture(scope="session")
async def petsafe_client(secret_store: SecretStore) -> AsyncIterator[PetSafeClient]:
    """Ensure an authenticated PetSafe client for the integration tests."""

    email = secret_store.prompt("email", "Enter the PetSafe account email: ")
    client = PetSafeClient(
        email=email,
        id_token=secret_store.get("id_token"),
        refresh_token=secret_store.get("refresh_token"),
        access_token=secret_store.get("access_token"),
    )

    if not await _tokens_valid(client):
        for attempt in range(2):
            try:
                await _authenticate_with_code(client, secret_store, force_new_code=attempt > 0)
                break
            except InvalidCodeException:
                if attempt == 1:
                    raise
                print("The provided verification code was invalid. Please try again.")
        else:  # pragma: no cover - defensive programming
            raise AssertionError("Failed to authenticate with PetSafe API")

    yield client

    await client._client.aclose()


@pytest.fixture(scope="session")
def event_loop() -> AsyncIterator[asyncio.AbstractEventLoop]:
    """Create an event loop for the session-scoped async fixtures/tests."""

    loop = asyncio.new_event_loop()
    yield loop
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()

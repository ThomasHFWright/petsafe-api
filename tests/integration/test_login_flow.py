"""Integration tests that exercise the PetSafe login flow."""

from __future__ import annotations

import pytest

from petsafe import general
from petsafe.client import PetSafeClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tokens_available(petsafe_client: PetSafeClient) -> None:
    """Ensure that the login flow produced usable tokens."""

    assert petsafe_client.id_token, "An id token was not retrieved."
    assert petsafe_client.refresh_token, "A refresh token was not retrieved."
    assert petsafe_client.access_token, "An access token was not retrieved."


@pytest.mark.integration
@pytest.mark.asyncio
async def test_can_list_shared_products(petsafe_client: PetSafeClient) -> None:
    """The authenticated client should be able to access a basic API endpoint."""

    sharing = await general.list_product_sharing(petsafe_client)
    assert isinstance(sharing, list)

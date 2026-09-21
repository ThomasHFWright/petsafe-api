"""Regression check for unordered SmartFeed messages."""

import asyncio
from itertools import permutations
from unittest.mock import AsyncMock

from petsafe.devices import DeviceSmartFeed


def test_last_feeding_uses_latest_valid_timestamp():
    async def check():
        feeder = DeviceSmartFeed(None, {})
        oldest = {"message_type": "FEED_DONE", "payload": {"time": 100, "amount": 1}}
        middle = {"message_type": "FEED_DONE", "payload": {"time": 200, "amount": 2}}
        newest = {"message_type": "FEED_DONE", "payload": {"time": 300, "amount": 3}}
        invalid = [
            {},
            {"message_type": "FEED_START", "payload": {"time": 400}},
            {"message_type": "FEED_DONE"},
            {"message_type": "FEED_DONE", "payload": None},
            {"message_type": "FEED_DONE", "payload": []},
            {"message_type": "FEED_DONE", "payload": {}},
            {"message_type": "FEED_DONE", "payload": {"time": "400"}},
        ]
        for order in permutations((oldest, middle, newest)):
            messages = invalid + list(order)
            feeder.get_messages_since = AsyncMock(return_value=messages)
            assert await feeder.get_last_feeding() is newest
            assert messages == invalid + list(order)
        for messages, expected in (([], None), (invalid, None), ([oldest], oldest)):
            feeder.get_messages_since = AsyncMock(return_value=messages)
            assert await feeder.get_last_feeding() is expected

    asyncio.run(check())

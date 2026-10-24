"""Library-to-server contract checks using the real local handlers, without hardware."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

import httpx
import pytest

from petsafe import LocalPetSafeClient


@pytest.mark.asyncio
async def test_command_and_firmware_helpers_against_local_api(tmp_path):
    root = Path(__file__).resolve().parents[3]
    tools = root / "tools"
    image = root / "analysis/firmware/smartdoor_1_large-v2.1.12.bin"
    if not image.exists():
        pytest.skip("parent server firmware fixture is unavailable")
    sys.path.insert(0, str(tools))
    try:
        from local_api import LocalAPI
        from setup_sessions import SetupError, SetupSessions

        device = "AABBCCDDEEFF"
        token = "synthetic-token-000000000000000000000000"
        sessions = SetupSessions(
            {"device_id": device, "base_url": "https://door.example", "api_token": token},
            {"dataTopic": "test/data", "cmdTopic": "test/cmd"},
        )
        api = LocalAPI(sessions, tmp_path / "local.json", timeout=0, command_timeout=0)
        api.ready = True
        api.refresh = lambda desired=None: {"state": {"reported": {
            "door": {"mode": "SMART"}, "systemStatus": {"boots": 1}}}}
        api.publish = lambda *args: None

        metadata = json.loads(image.with_suffix(".bin.json").read_text())
        document = {"afr_ota": {"protocols": ["MQTT"], "streamname": metadata["stream"],
                                 "files": [metadata["file"]]}}
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        inbox = api.firmware.directory / "inbox"
        inbox.mkdir(parents=True)
        shutil.copyfile(image, inbox / "test.bin")
        (inbox / "test.json").write_text(json.dumps(document))
        api.firmware.signer = image.parent / metadata["signature_certificate"]

        async def handler(request):
            try:
                status, payload = api.api(
                    request.method, request.url.raw_path.decode(),
                    request.headers["authorization"], request.content)
            except SetupError as error:
                status, payload = error.status, {"error": error.message}
            return (httpx.Response(status, json=payload) if payload is not None
                    else httpx.Response(status))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = LocalPetSafeClient("https://door.example", token, client=http)
            command = await client.submit_command(device, "REPORT_STATUS")
            assert command["state"] == "completed" and command["confirmed"] is True
            assert type(command["submittedAt"]) is float
            assert (await client.get_last_command(device))["id"] == command["id"]

            assert await client.get_firmware(device) == {"artifacts": [], "job": None}
            artifact = await client.stage_firmware(device, "test", "v2.1.12", digest)
            assert artifact["sha256"] == digest
            job = await client.install_firmware(device, digest)
            assert job["status"] == "QUEUED" and job["jobId"]
            canceled = await client.cancel_firmware_job(device, job["jobId"])
            assert canceled["status"] == "CANCELED"
            assert await client.delete_firmware_artifact(device, digest) is None
            assert await client.get_firmware(device) == {"artifacts": [], "job": canceled}
    finally:
        sys.path.remove(str(tools))

"""fal.ai Seedream adapter tests — httpx client faked; no keys, no network."""
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tapnow.pricing import PricingTable
from tapnow.providers import ProviderError, ProviderRequest
from tapnow.providers.fal_seedream import FalSeedreamProvider

from .conftest import PROJECT_ROOT

REAL_PRICING = PricingTable.load(PROJECT_ROOT / "pricing.yaml")
MODEL = "fal-ai/bytedance/seedream/v4/text-to-image"
DATA_URI = "data:image/png;base64," + base64.b64encode(b"PNGBYTES").decode()


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, submit, statuses, result):
        self.submit_resp = submit
        self.statuses = list(statuses)
        self.result_resp = result
        self.posts = []
        self.gets = []

    async def post(self, path, json=None):
        self.posts.append(SimpleNamespace(path=path, body=json))
        return self.submit_resp

    async def get(self, path, **kwargs):
        self.gets.append(path)
        if path.endswith("/status"):
            return self.statuses.pop(0)
        return self.result_resp


def make_provider(client):
    return FalSeedreamProvider(pricing=REAL_PRICING, client=client,
                               poll_interval=0.01, poll_timeout=5.0)


def image_request(tmp_path, **overrides) -> ProviderRequest:
    defaults = dict(kind="image", model=MODEL, prompt="a lighthouse at dusk",
                    output_dir=tmp_path / "out")
    defaults.update(overrides)
    return ProviderRequest(**defaults)


STATUS_URL = "https://queue.fal.run/fal-ai/bytedance/requests/req1/status"
RESPONSE_URL = "https://queue.fal.run/fal-ai/bytedance/requests/req1"


async def test_queue_flow_and_data_uri_decode(tmp_path):
    client = FakeClient(
        submit=FakeResponse(payload={"status": "IN_QUEUE", "request_id": "req1",
                                     "status_url": STATUS_URL,
                                     "response_url": RESPONSE_URL}),
        statuses=[FakeResponse(payload={"status": "IN_PROGRESS"}),
                  FakeResponse(payload={"status": "COMPLETED"})],
        result=FakeResponse(payload={"images": [{"url": DATA_URI}], "seed": 42}))
    result = await make_provider(client).execute(image_request(tmp_path))

    assert client.posts[0].path == f"/{MODEL}"
    body = client.posts[0].body
    assert body["prompt"] == "a lighthouse at dusk"
    assert body["sync_mode"] is True
    assert body["image_size"] == "landscape_16_9"

    # polling must use the URLs the queue returned, not constructed paths
    assert client.gets == [STATUS_URL, STATUS_URL, RESPONSE_URL]
    assert result.files[0].read_bytes() == b"PNGBYTES"
    assert result.cost_usd == pytest.approx(0.03)  # verified fal price
    assert result.meta["seed"] == 42


async def test_submit_error_raises(tmp_path):
    client = FakeClient(
        submit=FakeResponse(status_code=422, payload={"detail": [{"msg": "bad size"}]}),
        statuses=[], result=None)
    with pytest.raises(ProviderError, match="submit error 422"):
        await make_provider(client).execute(image_request(tmp_path))


async def test_empty_result_raises(tmp_path):
    client = FakeClient(
        submit=FakeResponse(payload={"status": "IN_QUEUE", "request_id": "req1",
                                     "status_url": STATUS_URL,
                                     "response_url": RESPONSE_URL}),
        statuses=[FakeResponse(payload={"status": "COMPLETED"})],
        result=FakeResponse(payload={"images": []}))
    with pytest.raises(ProviderError, match="no images"):
        await make_provider(client).execute(image_request(tmp_path))


async def test_reference_images_route_to_edit_endpoint(tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\nrefbytes")
    client = FakeClient(
        submit=FakeResponse(payload={"status": "IN_QUEUE", "request_id": "req1",
                                     "status_url": STATUS_URL,
                                     "response_url": RESPONSE_URL}),
        statuses=[FakeResponse(payload={"status": "COMPLETED"})],
        result=FakeResponse(payload={"images": [{"url": DATA_URI}]}))
    result = await make_provider(client).execute(
        image_request(tmp_path, input_files=[ref]))

    assert client.posts[0].path == "/fal-ai/bytedance/seedream/v4/edit"
    uris = client.posts[0].body["image_urls"]
    assert len(uris) == 1 and uris[0].startswith("data:image/png;base64,")
    assert result.meta["endpoint"] == "fal-ai/bytedance/seedream/v4/edit"
    assert result.cost_usd == pytest.approx(0.03)


async def test_non_image_kind_rejected():
    provider = make_provider(FakeClient(None, [], None))
    with pytest.raises(ProviderError, match="kind 'image'"):
        await provider.execute(ProviderRequest(kind="video", model=MODEL))

"""fal.ai GPT Image 2 adapter tests — httpx client faked; no keys, no network."""
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tapnow.pricing import PricingTable
from tapnow.providers import ProviderError, ProviderRequest
from tapnow.providers.fal_gpt_image import FalGptImageProvider

from .conftest import PROJECT_ROOT

REAL_PRICING = PricingTable.load(PROJECT_ROOT / "pricing.yaml")
MODEL = "openai/gpt-image-2/edit"
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
    return FalGptImageProvider(pricing=REAL_PRICING, client=client,
                               poll_interval=0.01, poll_timeout=5.0)


def edit_request(tmp_path, **overrides) -> ProviderRequest:
    base = tmp_path / "base.png"
    base.write_bytes(b"\x89PNG\r\n\x1a\nbasebytes")
    defaults = dict(kind="image", model=MODEL, prompt="darken the fade",
                    input_files=[base], output_dir=tmp_path / "out")
    defaults.update(overrides)
    return ProviderRequest(**defaults)


STATUS_URL = "https://queue.fal.run/openai/gpt-image-2/edit/requests/r1/status"
RESPONSE_URL = "https://queue.fal.run/openai/gpt-image-2/edit/requests/r1"


def queue_client(result_payload):
    return FakeClient(
        submit=FakeResponse(payload={"status": "IN_QUEUE", "request_id": "r1",
                                     "status_url": STATUS_URL,
                                     "response_url": RESPONSE_URL}),
        statuses=[FakeResponse(payload={"status": "IN_PROGRESS"}),
                  FakeResponse(payload={"status": "COMPLETED"})],
        result=FakeResponse(payload=result_payload))


async def test_queue_flow_body_and_cost(tmp_path):
    client = queue_client({"images": [{"url": DATA_URI, "width": 2496, "height": 3328}]})
    result = await make_provider(client).execute(edit_request(tmp_path))

    assert client.posts[0].path == f"/{MODEL}"
    body = client.posts[0].body
    assert body["prompt"] == "darken the fade"
    assert body["quality"] == "high"          # spec default, pinned explicitly
    assert body["output_format"] == "png"     # letterforms: no lossy default
    assert body["image_size"] == "auto"
    assert body["sync_mode"] is True
    assert len(body["image_urls"]) == 1
    assert body["image_urls"][0].startswith("data:image/png;base64,")

    assert client.gets == [STATUS_URL, STATUS_URL, RESPONSE_URL]
    assert result.files[0].read_bytes() == b"PNGBYTES"
    assert result.cost_usd == pytest.approx(
        REAL_PRICING.get(MODEL).per_image)
    assert result.meta["reference_images"] == 1


async def test_params_pass_through(tmp_path):
    client = queue_client({"images": [{"url": DATA_URI}]})
    await make_provider(client).execute(edit_request(
        tmp_path, params={"image_size": {"width": 2496, "height": 3328},
                          "quality": "medium"}))
    body = client.posts[0].body
    assert body["image_size"] == {"width": 2496, "height": 3328}
    assert body["quality"] == "medium"


async def test_requires_an_input_image(tmp_path):
    provider = make_provider(queue_client({"images": []}))
    with pytest.raises(ProviderError, match="requires at least one input image"):
        await provider.execute(ProviderRequest(
            kind="image", model=MODEL, prompt="x", output_dir=tmp_path / "out"))


async def test_ref_cap_is_sixteen(tmp_path):
    refs = []
    for i in range(20):
        p = tmp_path / f"ref_{i:02d}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]))
        refs.append(p)
    client = queue_client({"images": [{"url": DATA_URI}]})
    await make_provider(client).execute(edit_request(tmp_path, input_files=refs))
    assert len(client.posts[0].body["image_urls"]) == 16


async def test_submit_error_raises(tmp_path):
    client = FakeClient(
        submit=FakeResponse(status_code=422, payload={"detail": [{"msg": "bad"}]}),
        statuses=[], result=None)
    with pytest.raises(ProviderError, match="submit error 422"):
        await make_provider(client).execute(edit_request(tmp_path))


async def test_empty_result_raises(tmp_path):
    client = queue_client({"images": []})
    with pytest.raises(ProviderError, match="no images"):
        await make_provider(client).execute(edit_request(tmp_path))


async def test_non_image_kind_rejected():
    provider = make_provider(FakeClient(None, [], None))
    with pytest.raises(ProviderError, match="kind 'image'"):
        await provider.execute(ProviderRequest(kind="video", model=MODEL))

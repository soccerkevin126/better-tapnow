"""Ark/Seedream adapter tests — httpx client is faked; no keys, no network."""
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tapnow.pricing import PricingTable
from tapnow.providers import ProviderError, ProviderRequest
from tapnow.providers.ark_seedream import ArkSeedreamProvider

from .conftest import PROJECT_ROOT

REAL_PRICING = PricingTable.load(PROJECT_ROOT / "pricing.yaml")
PNG_B64 = base64.b64encode(b"\x89PNGfakeimage").decode()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, path, json=None):
        self.calls.append(SimpleNamespace(path=path, body=json))
        return self.responses.pop(0)


def image_request(**overrides) -> ProviderRequest:
    defaults = dict(kind="image", model="seedream-5-0-lite-260128",
                    prompt="a lighthouse", output_dir=Path("."))
    defaults.update(overrides)
    return ProviderRequest(**defaults)


async def test_generates_image_and_prices_per_image(tmp_path):
    client = FakeClient([FakeResponse(payload={
        "model": "seedream-5-0-lite-260128", "data": [{"b64_json": PNG_B64}]})])
    provider = ArkSeedreamProvider(pricing=REAL_PRICING, client=client)
    result = await provider.execute(image_request(output_dir=tmp_path))

    assert result.files[0].read_bytes() == b"\x89PNGfakeimage"
    assert result.cost_usd == pytest.approx(0.035)  # verified lite price

    body = client.calls[0].body
    assert client.calls[0].path == "/images/generations"
    assert body["response_format"] == "b64_json"
    assert body["size"] == "2K" and body["watermark"] is False


async def test_reference_images_sent_as_data_uris(tmp_path):
    ref = tmp_path / "style.png"
    ref.write_bytes(b"refbytes")
    client = FakeClient([FakeResponse(payload={"data": [{"b64_json": PNG_B64}]})])
    provider = ArkSeedreamProvider(pricing=REAL_PRICING, client=client)
    await provider.execute(image_request(input_files=[ref], output_dir=tmp_path))

    sent = client.calls[0].body["image"]
    assert sent.startswith("data:image/png;base64,")


async def test_api_error_raises_with_detail(tmp_path):
    client = FakeClient([FakeResponse(status_code=400, payload={
        "error": {"code": "InvalidParameter", "message": "bad size"}})])
    provider = ArkSeedreamProvider(pricing=REAL_PRICING, client=client)
    with pytest.raises(ProviderError, match="InvalidParameter: bad size"):
        await provider.execute(image_request(output_dir=tmp_path))


async def test_retries_on_429_then_succeeds(tmp_path, monkeypatch):
    import tapnow.providers.ark_seedream as mod

    async def no_sleep(_):
        pass
    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)

    client = FakeClient([
        FakeResponse(status_code=429, payload={"error": {"message": "rate limited"}}),
        FakeResponse(payload={"data": [{"b64_json": PNG_B64}]}),
    ])
    provider = ArkSeedreamProvider(pricing=REAL_PRICING, client=client)
    result = await provider.execute(image_request(output_dir=tmp_path))
    assert len(client.calls) == 2 and result.files


async def test_non_image_kind_rejected():
    provider = ArkSeedreamProvider(pricing=REAL_PRICING, client=FakeClient([]))
    with pytest.raises(ProviderError, match="kind 'image'"):
        await provider.execute(ProviderRequest(kind="text", model="x"))

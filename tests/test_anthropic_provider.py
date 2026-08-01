"""Anthropic adapter tests — the SDK client is faked; no keys, no network."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from tapnow.pricing import PricingTable
from tapnow.providers import ProviderError, ProviderRequest
from tapnow.providers.anthropic import AnthropicProvider, structured_output_schema

from .conftest import PROJECT_ROOT

REAL_PRICING = PricingTable.load(PROJECT_ROOT / "pricing.yaml")


def fake_response(text="OK", model="claude-fable-5", stop_reason="end_turn",
                  tokens=(1000, 500)):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""),
                 SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
        model=model,
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1],
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )


class FakeClient:
    def __init__(self, response):
        self.kwargs = None

        async def create(**kwargs):
            self.kwargs = kwargs
            return response

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def make_provider(response) -> tuple[AnthropicProvider, FakeClient]:
    client = FakeClient(response)
    return AnthropicProvider(pricing=REAL_PRICING, client=client), client


def text_request(**overrides) -> ProviderRequest:
    defaults = dict(kind="text", model="claude-fable-5", prompt="hello")
    defaults.update(overrides)
    return ProviderRequest(**defaults)


async def test_text_call_shape_and_cost():
    provider, client = make_provider(fake_response())
    result = await provider.execute(text_request())

    assert result.text == "OK"
    # 1000 in @ $10/MTok + 500 out @ $50/MTok
    assert result.cost_usd == pytest.approx(0.035)
    assert result.meta["served_by"] == "claude-fable-5"

    k = client.kwargs
    assert k["model"] == "claude-fable-5"
    assert k["fallbacks"] == [{"model": "claude-opus-4-8"}]
    assert "server-side-fallback" in k["betas"][0]
    # claude-fable-5: thinking always on (param must be omitted), no sampling params
    for forbidden in ("thinking", "temperature", "top_p", "top_k"):
        assert forbidden not in k


async def test_json_schema_uses_structured_outputs():
    schema = {
        "type": "object",
        "required": ["scenes"],
        "properties": {"scenes": {
            "type": "array", "minItems": 2,
            "items": {"type": "object",
                      "properties": {"duration_s": {"type": "number", "minimum": 2}}},
        }},
    }
    provider, client = make_provider(fake_response(text='{"scenes": []}'))
    await provider.execute(text_request(json_schema=schema))

    sent = client.kwargs["output_config"]["format"]
    assert sent["type"] == "json_schema"
    s = sent["schema"]
    assert s["additionalProperties"] is False
    assert "minItems" not in s["properties"]["scenes"]
    assert "minimum" not in s["properties"]["scenes"]["items"]["properties"]["duration_s"]
    assert s["required"] == ["scenes"]  # supported keywords survive


async def test_refusal_raises_provider_error():
    provider, _ = make_provider(fake_response(stop_reason="refusal"))
    with pytest.raises(ProviderError, match="refusal.*cyber"):
        await provider.execute(text_request())


async def test_fallback_response_priced_by_served_model():
    provider, _ = make_provider(fake_response(model="claude-opus-4-8"))
    result = await provider.execute(text_request())
    # 1000 in @ $5/MTok + 500 out @ $25/MTok — opus-4-8 rates, not fable's
    assert result.cost_usd == pytest.approx(0.0175)
    assert result.meta["served_by"] == "claude-opus-4-8"


async def test_image_files_attached_for_vision(tmp_path: Path):
    img = tmp_path / "scene.png"
    img.write_bytes(b"\x89PNGfake")
    provider, client = make_provider(fake_response())
    await provider.execute(text_request(input_files=[img]))

    blocks = client.kwargs["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[-1] == {"type": "text", "text": "hello"}


async def test_non_text_kind_rejected():
    provider, _ = make_provider(fake_response())
    with pytest.raises(ProviderError, match="kind 'text'"):
        await provider.execute(ProviderRequest(kind="image", model="claude-fable-5"))


def test_schema_sanitizer_recurses_nested():
    schema = {"type": "object", "properties": {
        "a": {"type": "string", "maxLength": 5},
        "b": {"anyOf": [{"type": "integer", "maximum": 9}, {"type": "null"}]},
    }}
    out = structured_output_schema(schema)
    assert "maxLength" not in out["properties"]["a"]
    assert "maximum" not in out["properties"]["b"]["anyOf"][0]
    assert out["additionalProperties"] is False


def test_pricing_yaml_fable_entry_is_verified():
    p = REAL_PRICING.get("claude-fable-5")
    assert p.verified and p.input_per_mtok == 10.0 and p.output_per_mtok == 50.0
    assert REAL_PRICING.get("claude-opus-4-8").verified

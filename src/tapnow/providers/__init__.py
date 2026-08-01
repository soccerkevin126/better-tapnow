"""Provider registry. A new model service = one adapter module registered here."""
from __future__ import annotations

from .base import Provider, ProviderError, ProviderRequest, ProviderResult, poll_until
from .mock import MockProvider

# Real adapters land here one at a time. Until an adapter exists, resolving
# it without --mock fails loudly instead of inventing endpoints.
KNOWN_PROVIDERS = ("anthropic", "fal-seedream", "ark-seedream", "kling", "ffmpeg", "mock")


def get_provider(name: str, *, mock: bool = False, pricing=None) -> Provider:
    if mock or name == "mock":
        return MockProvider()
    if name == "anthropic":
        from .anthropic import AnthropicProvider
        return AnthropicProvider(pricing=pricing)
    if name == "ffmpeg":
        from .ffmpeg import FfmpegProvider
        return FfmpegProvider(pricing=pricing)
    if name == "ark-seedream":
        from .ark_seedream import ArkSeedreamProvider
        return ArkSeedreamProvider(pricing=pricing)
    if name == "fal-seedream":
        from .fal_seedream import FalSeedreamProvider
        return FalSeedreamProvider(pricing=pricing)
    if name == "kling":
        from .kling import KlingProvider
        return KlingProvider(pricing=pricing)
    if name in KNOWN_PROVIDERS:
        raise ProviderError(
            f"provider '{name}' has no real adapter yet — run with --mock, "
            "or add src/tapnow/providers/<name>.py"
        )
    raise ProviderError(f"unknown provider '{name}' (known: {', '.join(KNOWN_PROVIDERS)})")


__all__ = [
    "Provider", "ProviderError", "ProviderRequest", "ProviderResult",
    "poll_until", "MockProvider", "get_provider", "KNOWN_PROVIDERS",
]

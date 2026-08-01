"""Common provider interface. One adapter file per model service."""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable


@dataclass
class ProviderRequest:
    kind: str                      # text | image | video | assemble
    model: str
    prompt: str | None = None
    input_files: list[Path] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    output_dir: Path = Path(".")
    # For text steps expecting JSON: the schema, so providers can steer output.
    json_schema: dict | None = None


@dataclass
class ProviderResult:
    text: str | None = None
    files: list[Path] = field(default_factory=list)
    cost_usd: float = 0.0
    meta: dict = field(default_factory=dict)


class Provider(ABC):
    """Adapters implement execute(); the runner owns orchestration, retries,
    templating, spend accounting, and manifests."""

    name: str = "base"

    @abstractmethod
    async def execute(self, request: ProviderRequest) -> ProviderResult: ...


class ProviderError(RuntimeError):
    pass


def image_media_type(data: bytes) -> str | None:
    """Identify an image by magic bytes — extensions lie (e.g. fal returns
    JPEG data regardless of the filename you give it)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


async def poll_until(
    check: Callable[[], Awaitable[Any]],
    *,
    timeout_s: float = 900,
    initial_interval: float = 2.0,
    factor: float = 1.6,
    max_interval: float = 30.0,
) -> Any:
    """Poll an async job with exponential backoff. `check` returns a truthy
    result when done, None while pending, or raises on failure."""
    deadline = time.monotonic() + timeout_s
    interval = initial_interval
    while time.monotonic() < deadline:
        result = await check()
        if result is not None:
            return result
        await asyncio.sleep(min(interval, max(0, deadline - time.monotonic())))
        interval = min(interval * factor, max_interval)
    raise TimeoutError(f"job did not complete within {timeout_s}s")

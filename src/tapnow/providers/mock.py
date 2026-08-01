"""Deterministic offline provider. Stands in for every real adapter so the
whole pipeline — dry runs, full runs, tests — works with no keys and no spend.
"""
from __future__ import annotations

import json
from typing import Any

from .base import Provider, ProviderRequest, ProviderResult

# 1x1 red pixel PNG.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763f8cfc00000030101cf7f0fcc0000000049454e44ae426082"
)


def instance_from_schema(schema: dict) -> Any:
    """Build a minimal valid instance of a JSON schema, for canned responses."""
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type", "object")
    if t == "object":
        return {k: instance_from_schema(v) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        n = schema.get("minItems", 2)
        return [instance_from_schema(schema.get("items", {"type": "string"})) for _ in range(n)]
    if t == "string":
        return "mock value"
    if t in ("number", "integer"):
        # High enough to pass any sane quality-gate threshold by default.
        lo, hi = schema.get("minimum"), schema.get("maximum")
        val = 0.9 if t == "number" else 9
        if hi is not None and val > hi:
            val = hi
        if lo is not None and val < lo:
            val = lo
        return val
    if t == "boolean":
        return True
    return None


class MockProvider(Provider):
    name = "mock"

    def __init__(self, script: list[Any] | None = None):
        # Optional queue of canned results for tests: strings become text
        # responses, dicts are JSON-dumped. When exhausted, falls back to
        # deterministic defaults.
        self.script = list(script or [])
        self.calls: list[ProviderRequest] = []

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        self.calls.append(request)
        if self.script:
            scripted = self.script.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            if isinstance(scripted, ProviderResult):
                return scripted
            text = scripted if isinstance(scripted, str) else json.dumps(scripted)
            return ProviderResult(text=text, meta={"mock": "scripted"})

        if request.kind == "text":
            if request.json_schema:
                return ProviderResult(text=json.dumps(instance_from_schema(request.json_schema)))
            return ProviderResult(text=f"[mock:{request.model}] response to: {(request.prompt or '')[:80]}")

        request.output_dir.mkdir(parents=True, exist_ok=True)
        n = len(self.calls)
        if request.kind == "image":
            out = request.output_dir / f"mock_image_{n}.png"
            out.write_bytes(_PNG)
            return ProviderResult(files=[out])
        if request.kind == "video":
            out = request.output_dir / f"mock_video_{n}.mp4"
            out.write_bytes(b"MOCKVIDEO" + str(n).encode())
            return ProviderResult(files=[out])
        if request.kind == "assemble":
            out = request.output_dir / "assembled.mp4"
            blob = b"".join(p.read_bytes() for p in request.input_files)
            out.write_bytes(b"MOCKASSEMBLY" + blob[:64])
            return ProviderResult(files=[out])
        raise ValueError(f"unknown kind {request.kind}")

"""Anthropic text adapter (claude-fable-5).

API usage follows the official Anthropic SDK and current docs (verified
2026-08-01). Model notes for claude-fable-5:
- thinking is always on; the `thinking` param must be omitted entirely
- sampling params (temperature/top_p/top_k) are rejected — steer via prompt
- safety classifiers may return stop_reason "refusal"; a server-side fallback
  to claude-opus-4-8 is enabled so declined requests are re-run automatically
- structured outputs (`output_config.format`) constrain JSON responses; the
  runner still validates against the full original schema and retries
"""
from __future__ import annotations

import base64

import anthropic

from .base import (Provider, ProviderError, ProviderRequest, ProviderResult,
                   image_media_type)

FALLBACK_MODEL = "claude-opus-4-8"
FALLBACK_BETA = "server-side-fallback-2026-06-01"
DEFAULT_MAX_TOKENS = 16000

_IMAGE_MEDIA = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}

# JSON-schema keywords the structured-outputs API doesn't accept. Stripping
# them here is safe: the runner validates the full original schema and retries.
_UNSUPPORTED_KEYWORDS = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
}

# Cache-token pricing multipliers relative to base input price (official docs).
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, pricing=None, client: anthropic.AsyncAnthropic | None = None):
        self.pricing = pricing
        if client is None:
            try:
                # This network drops long uploads now and then; the SDK's
                # backoff retry handles it, give it more attempts than the
                # default 2.
                client = anthropic.AsyncAnthropic(max_retries=5)
            except anthropic.AnthropicError as e:
                raise ProviderError(
                    f"Anthropic credentials not found ({e}) — set ANTHROPIC_API_KEY in .env")
        self.client = client

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        if request.kind != "text":
            raise ProviderError(f"anthropic adapter handles kind 'text', not '{request.kind}'")

        content: list[dict] = []
        for f in request.input_files:
            if f.suffix.lower() in (".md", ".txt"):
                content.append({"type": "text",
                                "text": f"[Attached file {f.name}]\n{f.read_text()}"})
                continue
            data = f.read_bytes()
            # Trust the bytes, not the extension — generators mislabel files.
            media = image_media_type(data) or _IMAGE_MEDIA.get(f.suffix.lower())
            if media:
                data, media = _shrink_image(data, media)
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media,
                               "data": base64.standard_b64encode(data).decode()},
                })
        content.append({"type": "text", "text": request.prompt or ""})

        output_config: dict = {}
        if effort := request.params.get("effort"):
            output_config["effort"] = effort
        if request.json_schema:
            output_config["format"] = {
                "type": "json_schema",
                "schema": structured_output_schema(request.json_schema),
            }
        kwargs: dict = {}
        if output_config:
            kwargs["output_config"] = output_config

        try:
            response = await self.client.beta.messages.create(
                model=request.model,
                max_tokens=int(request.params.get("max_tokens", DEFAULT_MAX_TOKENS)),
                betas=[FALLBACK_BETA],
                fallbacks=[{"model": FALLBACK_MODEL}],
                messages=[{"role": "user", "content": content}],
                **kwargs,
            )
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"cannot reach the Anthropic API: {e}") from e

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ProviderError(
                "Anthropic declined this request (stop_reason=refusal"
                + (f", category={category}" if category else "") + ")")

        text = "".join(b.text for b in response.content if b.type == "text")
        usage = response.usage
        return ProviderResult(
            text=text,
            cost_usd=self._cost(response.model, request.model, usage),
            meta={
                "served_by": response.model,
                "stop_reason": response.stop_reason,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            },
        )

    def _cost(self, served_model: str, requested_model: str, usage) -> float:
        if self.pricing is None:
            return 0.0
        try:
            p = self.pricing.get(served_model)
        except KeyError:
            p = self.pricing.get(requested_model)
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        return round((
            usage.input_tokens * p.input_per_mtok
            + cache_write * p.input_per_mtok * CACHE_WRITE_MULT
            + cache_read * p.input_per_mtok * CACHE_READ_MULT
            + usage.output_tokens * p.output_per_mtok
        ) / 1_000_000, 6)


# High-resolution vision tier (Claude 4.7+, incl. claude-fable-5): images are
# 28x28-px patches, capped at 4784 visual tokens and a 2576px long edge —
# anything larger is resized server-side, so sending it only costs upload.
MAX_IMAGE_EDGE = 2576
MAX_IMAGE_TOKENS = 4784
_PATCH = 28


def _shrink_image(data: bytes, media: str) -> tuple[bytes, str]:
    """Downscale attachments just enough to fit both high-res-tier limits."""
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(data))
        w, h = img.size
        scale = min(
            1.0,
            MAX_IMAGE_EDGE / max(w, h),
            (MAX_IMAGE_TOKENS * _PATCH * _PATCH / (w * h)) ** 0.5,
        )
        if scale >= 1.0:
            return data, media
        img = img.convert("RGB")
        img.thumbnail((int(w * scale), int(h * scale)))
        buf = io.BytesIO()
        # Quality 95: the gate reads letterforms — compression artifacts on
        # thin strokes read as rendering defects.
        img.save(buf, "JPEG", quality=95)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return data, media  # unparseable → send as-is


def structured_output_schema(schema):
    """Adapt an arbitrary JSON schema to what structured outputs accepts."""
    if isinstance(schema, dict):
        out = {k: structured_output_schema(v)
               for k, v in schema.items() if k not in _UNSUPPORTED_KEYWORDS}
        if out.get("type") == "object":
            out.setdefault("additionalProperties", False)
        return out
    if isinstance(schema, list):
        return [structured_output_schema(v) for v in schema]
    return schema

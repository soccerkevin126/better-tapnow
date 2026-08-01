"""GPT Image 2 adapter via fal.ai (OpenAI's image model, fal-hosted).

Chosen 2026-08-01: #1 on the LMArena Image Edit leaderboard (Elo 1463,
28.5M votes) with forced high-fidelity input processing — the strongest
available engine for edits that must preserve exact letterforms.

API verified 2026-08-01 against fal's official OpenAPI spec
(fal.ai/api/openapi/queue/openapi.json?endpoint_id=openai/gpt-image-2/edit):
  submit: POST https://queue.fal.run/{model}
  auth:   Authorization: Key $FAL_KEY
  input:  prompt (required, 2-32000 chars), image_urls (required, max 16),
          num_images (1-4, default 1), output_format png|jpeg|webp (png),
          quality auto|low|medium|high (default high),
          image_size preset|{width,height} (default "auto", max 14142/side),
          mask_url (optional), sync_mode (default false)
  submit response: {status, request_id, status_url, response_url}
  result: {images: [{url, width, height, content_type, ...}]}
  No seed parameter exists on this endpoint.
With sync_mode=true images arrive as data URIs; URL fallback handled anyway.
"""
from __future__ import annotations

import asyncio
import base64
import os

import httpx

from .base import (Provider, ProviderError, ProviderRequest, ProviderResult,
                   image_media_type, poll_until)

DEFAULT_BASE_URL = "https://queue.fal.run"
_RETRYABLE = {429, 500, 502, 503}
MAX_REFERENCE_IMAGES = 16  # per the OpenAPI spec


class FalGptImageProvider(Provider):
    name = "fal-gpt-image"

    def __init__(self, pricing=None, client: httpx.AsyncClient | None = None,
                 poll_interval: float = 2.0, poll_timeout: float = 300.0):
        self.pricing = pricing
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        api_key = os.environ.get("FAL_KEY")
        if client is None and not api_key:
            raise ProviderError("FAL_KEY not set — add it to .env (fal.ai dashboard → Keys)")
        self.client = client or httpx.AsyncClient(
            base_url=os.environ.get("FAL_BASE_URL", DEFAULT_BASE_URL),
            headers={"Authorization": f"Key {api_key}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        if request.kind != "image":
            raise ProviderError(f"fal-gpt-image adapter handles kind 'image', not '{request.kind}'")

        refs = [f for f in request.input_files
                if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif")]
        if not refs:
            raise ProviderError(
                "gpt-image-2/edit requires at least one input image — "
                "attach the edit target via the step's inputs")
        uris = []
        for f in refs[:MAX_REFERENCE_IMAGES]:
            data = f.read_bytes()
            media = image_media_type(data) or "image/png"
            uris.append(f"data:{media};base64,"
                        + base64.standard_b64encode(data).decode())

        body: dict = {
            "prompt": request.prompt or "",
            "image_urls": uris,
            "image_size": request.params.get("image_size", "auto"),
            "quality": request.params.get("quality", "high"),
            "output_format": request.params.get("output_format", "png"),
            "sync_mode": True,
        }

        submitted = await self._submit(f"/{request.model}", body)
        rid = submitted.get("request_id")
        if not rid:
            raise ProviderError(f"fal queue returned no request_id: {str(submitted)[:200]}")
        status_url = submitted.get("status_url")
        response_url = submitted.get("response_url")
        if not status_url or not response_url:
            raise ProviderError(
                f"fal queue response missing status/response URLs: {str(submitted)[:200]}")

        await poll_until(
            lambda: self._check(status_url),
            timeout_s=self.poll_timeout,
            initial_interval=self.poll_interval, factor=1.4, max_interval=15.0)

        result = await self._get_json(response_url)
        images = result.get("images") or []
        if not images:
            raise ProviderError(f"fal returned no images: {str(result)[:300]}")

        request.output_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for i, item in enumerate(images):
            data = await self._image_bytes(item.get("url", ""))
            media = image_media_type(data) or "image/png"
            ext = {"image/jpeg": "jpg", "image/png": "png",
                   "image/webp": "webp", "image/gif": "gif"}[media]
            out = request.output_dir / f"image_{i:03d}.{ext}"
            out.write_bytes(data)
            files.append(out)

        cost = 0.0
        if self.pricing is not None:
            price = self.pricing.get(request.model)
            cost = round(price.per_image * len(files), 6)
        return ProviderResult(files=files, cost_usd=cost,
                              meta={"request_id": rid, "images": len(files),
                                    "endpoint": request.model,
                                    "reference_images": len(refs),
                                    "quality": body["quality"]})

    # -- API calls (same queue mechanics verified for fal-seedream) ----------

    async def _submit(self, path: str, body: dict) -> dict:
        delay = 2.0
        for attempt in range(3):
            try:
                resp = await self.client.post(path, json=body)
            except httpx.HTTPError as e:
                raise ProviderError(f"cannot reach fal.ai: {e}") from e
            if resp.status_code in _RETRYABLE and attempt < 2:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code not in (200, 201, 202):
                raise ProviderError(f"fal.ai submit error {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise ProviderError("fal.ai: retries exhausted")

    async def _check(self, status_url: str) -> bool | None:
        try:
            resp = await self.client.get(status_url)
        except httpx.HTTPError:
            return None
        if resp.status_code in _RETRYABLE:
            return None
        if resp.status_code not in (200, 202):
            raise ProviderError(f"fal.ai status error {resp.status_code}: {resp.text[:300]}")
        status = resp.json().get("status")
        return True if status == "COMPLETED" else None

    async def _get_json(self, path: str) -> dict:
        try:
            resp = await self.client.get(path)
        except httpx.HTTPError as e:
            raise ProviderError(f"cannot reach fal.ai: {e}") from e
        if resp.status_code != 200:
            raise ProviderError(f"fal.ai result error {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def _image_bytes(self, url: str) -> bytes:
        if url.startswith("data:"):
            try:
                return base64.b64decode(url.split(",", 1)[1])
            except (IndexError, ValueError) as e:
                raise ProviderError(f"malformed data URI from fal.ai: {e}") from e
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as dl:
            resp = await dl.get(url)
            if resp.status_code != 200:
                raise ProviderError(f"failed to download image ({resp.status_code}): {url[:120]}")
            return resp.content

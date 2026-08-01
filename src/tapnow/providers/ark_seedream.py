"""Seedream image adapter — BytePlus ModelArk / Volcano Engine Ark.

API reference verified 2026-08-01 against official docs
(docs.byteplus.com/en/docs/ModelArk/1541523):
  POST {base}/images/generations
  Authorization: Bearer $ARK_API_KEY
  body: model, prompt, size ("1K"|"1.5K"|"2K" or "WxH"), response_format
        ("url"|"b64_json"), output_format, watermark, image (reference
        images as data URIs or URLs)
  response: {"model", "created", "data": [{"url" | "b64_json"}], ...}

Base URLs: ap-southeast-1 https://ark.ap-southeast.bytepluses.com/api/v3,
eu-west-1 https://ark.eu-west.bytepluses.com/api/v3; the CN Volcano Engine
console uses https://ark.cn-beijing.volces.com/api/v3. Override with
ARK_BASE_URL to match where your key was issued.
"""
from __future__ import annotations

import asyncio
import base64
import os

import httpx

from .base import Provider, ProviderError, ProviderRequest, ProviderResult

DEFAULT_BASE_URL = "https://ark.ap-southeast.bytepluses.com/api/v3"
_RETRYABLE = {429, 500, 502, 503, 529}
_DATA_URI_MEDIA = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}


class ArkSeedreamProvider(Provider):
    name = "ark-seedream"

    def __init__(self, pricing=None, client: httpx.AsyncClient | None = None):
        self.pricing = pricing
        api_key = os.environ.get("ARK_API_KEY")
        if client is None and not api_key:
            raise ProviderError("ARK_API_KEY not set — add it to .env")
        self.client = client or httpx.AsyncClient(
            base_url=os.environ.get("ARK_BASE_URL", DEFAULT_BASE_URL),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(180.0, connect=10.0),
        )

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        if request.kind != "image":
            raise ProviderError(f"ark-seedream adapter handles kind 'image', not '{request.kind}'")

        body: dict = {
            "model": request.model,
            "prompt": request.prompt or "",
            "size": str(request.params.get("size", "2K")),
            "response_format": "b64_json",
            "output_format": "png",
            "watermark": bool(request.params.get("watermark", False)),
        }
        refs = [f for f in request.input_files if f.suffix.lower() in _DATA_URI_MEDIA]
        if refs:
            uris = [f"data:image/{_DATA_URI_MEDIA[f.suffix.lower()]};base64,"
                    + base64.standard_b64encode(f.read_bytes()).decode() for f in refs]
            body["image"] = uris if len(uris) > 1 else uris[0]

        data = await self._post_with_retry("/images/generations", body)

        images = data.get("data") or []
        if not images:
            raise ProviderError(f"Ark returned no images: {str(data)[:300]}")
        request.output_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for i, item in enumerate(images):
            if "b64_json" not in item:
                raise ProviderError(f"Ark response missing b64_json: {str(item)[:200]}")
            out = request.output_dir / f"image_{i:03d}.png"
            out.write_bytes(base64.b64decode(item["b64_json"]))
            files.append(out)

        cost = 0.0
        if self.pricing is not None:
            cost = round(self.pricing.get(request.model).per_image * len(files), 6)
        return ProviderResult(files=files, cost_usd=cost,
                              meta={"model": data.get("model", request.model),
                                    "images": len(files)})

    async def _post_with_retry(self, path: str, body: dict, attempts: int = 3) -> dict:
        delay = 2.0
        for attempt in range(attempts):
            try:
                resp = await self.client.post(path, json=body)
            except httpx.HTTPError as e:
                raise ProviderError(f"cannot reach Ark API: {e}") from e
            if resp.status_code in _RETRYABLE and attempt < attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code != 200:
                detail = _error_detail(resp)
                raise ProviderError(f"Ark API error {resp.status_code}: {detail}")
            return resp.json()
        raise ProviderError("Ark API: retries exhausted")


def _error_detail(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return f"{err.get('code', '?')}: {err.get('message', resp.text[:300])}"
    except Exception:
        return resp.text[:300]

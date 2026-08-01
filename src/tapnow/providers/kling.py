"""Kling image-to-video adapter (submit-then-poll).

API reference verified 2026-08-01 against official docs (kling.ai/document-api):
  submit: POST {base}/image-to-video/{model}           (model in path, e.g. kling-3.0)
  auth:   Authorization: Bearer <API Key>              (AK/SK JWT is legacy-only)
  body:   contents [{type: "prompt", text}, {type: "first_frame", url: <URL or Base64>}]
          settings {resolution: 720p|1080p|4k, duration: 3-13 (default 5),
                    audio: "off"|"native", multi_shot}
          options  {watermark_info: {enabled}}
  submit response: {code, message, request_id, data: {id, status}}
  poll:   GET {base}/tasks?task_ids={id}
          → data[0]: {status: submitted|processing|succeeded|failed, message,
                      outputs: [{type: "video", url, duration}],
                      billing: [{charge_type: cash|unit, amount, list_price}]}
  Result URLs expire after 30 days; the clip is downloaded immediately.
  First-frame constraints: jpg/jpeg/png, ≥300px each side, aspect 1:2.5-2.5:1.
"""
from __future__ import annotations

import asyncio
import base64
import os

import httpx

from .base import Provider, ProviderError, ProviderRequest, ProviderResult, poll_until

DEFAULT_BASE_URL = "https://api-singapore.klingai.com"
UNIT_PRICE_USD = 0.14  # 1 unit = $0.14 list price (kling.ai/dev/pricing, 2026-08-01)
_RETRYABLE = {429, 500, 502, 503}


class KlingProvider(Provider):
    name = "kling"

    def __init__(self, pricing=None, client: httpx.AsyncClient | None = None,
                 poll_interval: float = 5.0, poll_timeout: float = 1500.0):
        self.pricing = pricing
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        api_key = os.environ.get("KLING_API_KEY")
        if client is None and not api_key:
            raise ProviderError("KLING_API_KEY not set — add it to .env")
        self.client = client or httpx.AsyncClient(
            base_url=os.environ.get("KLING_BASE_URL", DEFAULT_BASE_URL),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        if request.kind != "video":
            raise ProviderError(f"kling adapter handles kind 'video', not '{request.kind}'")
        frames = [f for f in request.input_files
                  if f.suffix.lower() in (".png", ".jpg", ".jpeg")]
        if not frames:
            raise ProviderError("kling needs a first-frame image (png/jpg) as input")

        duration = int(float(request.params.get("duration_s", 5)))
        duration = max(3, min(duration, 10))
        resolution = str(request.params.get("resolution", "720p"))

        body = {
            "contents": [
                {"type": "prompt", "text": request.prompt or ""},
                {"type": "first_frame",
                 "url": base64.standard_b64encode(frames[0].read_bytes()).decode()},
            ],
            "settings": {"resolution": resolution, "duration": duration,
                         "audio": "off", "multi_shot": False},
            "options": {"watermark_info": {"enabled": False}},
        }
        submitted = await self._submit(f"/image-to-video/{request.model}", body)
        task_id = submitted["data"]["id"]

        task = await poll_until(
            lambda: self._check(task_id),
            timeout_s=float(request.params.get("timeout_s", self.poll_timeout)),
            initial_interval=self.poll_interval, factor=1.5, max_interval=30.0)

        videos = [o for o in task.get("outputs") or []
                  if o.get("type") == "video" and o.get("url")]
        if not videos:
            raise ProviderError(f"Kling task {task_id} succeeded but returned no video")
        out = await self._download(videos[0]["url"], request, task_id)
        return ProviderResult(
            files=[out],
            cost_usd=self._cost(task, request.model, duration),
            meta={"task_id": task_id, "duration_s": duration,
                  "resolution": resolution,
                  "reported_duration": videos[0].get("duration")},
        )

    # -- API calls -----------------------------------------------------------

    async def _submit(self, path: str, body: dict) -> dict:
        delay = 2.0
        for attempt in range(3):
            try:
                resp = await self.client.post(path, json=body)
            except httpx.HTTPError as e:
                raise ProviderError(f"cannot reach Kling API: {e}") from e
            if resp.status_code in _RETRYABLE and attempt < 2:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return _parse(resp, "submit")
        raise ProviderError("Kling API: retries exhausted")

    async def _check(self, task_id: str) -> dict | None:
        """One poll tick: task dict when done, None while pending/transient."""
        try:
            resp = await self.client.get(f"/tasks?task_ids={task_id}")
        except httpx.HTTPError:
            return None  # transient network issue — keep polling
        if resp.status_code in _RETRYABLE:
            return None
        data = _parse(resp, "poll")
        tasks = data.get("data") or []
        if not tasks:
            return None
        task = tasks[0]
        status = task.get("status")
        if status == "succeeded":
            return task
        if status == "failed":
            raise ProviderError(f"Kling task {task_id} failed: {task.get('message')}")
        return None  # submitted / processing

    async def _download(self, url: str, request: ProviderRequest, task_id: str):
        request.output_dir.mkdir(parents=True, exist_ok=True)
        out = request.output_dir / f"clip_{task_id}.mp4"
        try:
            resp = await self.client.get(url, timeout=300.0, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ProviderError(f"failed to download Kling result: {e}") from e
        out.write_bytes(resp.content)
        return out

    def _cost(self, task: dict, model: str, duration: int) -> float:
        # Prefer the actual deduction the API reports.
        total = 0.0
        for b in task.get("billing") or []:
            try:
                amount = float(b.get("amount") or 0)
            except (TypeError, ValueError):
                continue
            total += amount if b.get("charge_type") == "cash" else amount * UNIT_PRICE_USD
        if total:
            return round(total, 6)
        if self.pricing is not None:
            return round(self.pricing.get(model).per_second * duration, 6)
        return 0.0


def _parse(resp: httpx.Response, what: str) -> dict:
    if resp.status_code != 200:
        raise ProviderError(f"Kling API {what} error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if data.get("code") != 0:
        raise ProviderError(
            f"Kling API {what} error code {data.get('code')}: {data.get('message')} "
            f"(request_id {data.get('request_id')})")
    return data

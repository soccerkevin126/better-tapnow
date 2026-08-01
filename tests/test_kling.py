"""Kling adapter tests — submit-then-poll against a faked httpx client."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tapnow.pricing import PricingTable
from tapnow.providers import ProviderError, ProviderRequest
from tapnow.providers.kling import KlingProvider

from .conftest import PROJECT_ROOT

REAL_PRICING = PricingTable.load(PROJECT_ROOT / "pricing.yaml")


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


def ok(data):
    return FakeResponse(payload={"code": 0, "message": "ok", "request_id": "r1",
                                 "data": data})


class FakeClient:
    """Routes POST to a submit queue, GET /tasks to a poll queue,
    and any other GET to the download response."""

    def __init__(self, submit, polls, video=b"VIDEOBYTES"):
        self.submit_resp = submit
        self.polls = list(polls)
        self.video = video
        self.posts = []
        self.gets = []

    async def post(self, path, json=None):
        self.posts.append(SimpleNamespace(path=path, body=json))
        return self.submit_resp

    async def get(self, url, **kwargs):
        self.gets.append(url)
        if url.startswith("/tasks"):
            return self.polls.pop(0)
        return FakeResponse(content=self.video)


def make_provider(client):
    return KlingProvider(pricing=REAL_PRICING, client=client,
                         poll_interval=0.01, poll_timeout=5.0)


def video_request(tmp_path, **overrides) -> ProviderRequest:
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"frame")
    defaults = dict(kind="video", model="kling-3.0", prompt="camera pans right",
                    input_files=[frame], params={"duration_s": "7.0"},
                    output_dir=tmp_path / "out")
    defaults.update(overrides)
    return ProviderRequest(**defaults)


def succeeded_task(billing=None):
    return {"id": "task123", "status": "succeeded",
            "outputs": [{"type": "video", "url": "https://cdn.kling/video.mp4",
                         "duration": "7"}],
            "billing": billing or []}


async def test_submit_poll_download_flow(tmp_path):
    client = FakeClient(
        submit=ok({"id": "task123", "status": "submitted"}),
        polls=[ok([{"id": "task123", "status": "processing"}]),
               ok([succeeded_task()])])
    result = await make_provider(client).execute(video_request(tmp_path))

    body = client.posts[0].body
    assert client.posts[0].path == "/image-to-video/kling-3.0"
    assert body["contents"][0] == {"type": "prompt", "text": "camera pans right"}
    assert body["contents"][1]["type"] == "first_frame"
    assert body["settings"] == {"resolution": "720p", "duration": 7,
                                "audio": "off", "multi_shot": False}

    assert result.files[0].read_bytes() == b"VIDEOBYTES"
    # no billing info returned -> verified list price: 7s * $0.084
    assert result.cost_usd == pytest.approx(0.588)
    assert result.meta["task_id"] == "task123"


async def test_actual_billing_from_api_preferred(tmp_path):
    client = FakeClient(
        submit=ok({"id": "task123", "status": "submitted"}),
        polls=[ok([succeeded_task(billing=[
            {"charge_type": "unit", "amount": "4.2"}])])])
    result = await make_provider(client).execute(video_request(tmp_path))
    assert result.cost_usd == pytest.approx(4.2 * 0.14)  # units × $0.14


async def test_failed_task_raises_with_reason(tmp_path):
    client = FakeClient(
        submit=ok({"id": "task123", "status": "submitted"}),
        polls=[ok([{"id": "task123", "status": "failed",
                    "message": "content risk control"}])])
    with pytest.raises(ProviderError, match="content risk control"):
        await make_provider(client).execute(video_request(tmp_path))


async def test_api_error_code_raises(tmp_path):
    client = FakeClient(
        submit=FakeResponse(payload={"code": 1001, "message": "auth failed",
                                     "request_id": "r9"}),
        polls=[])
    with pytest.raises(ProviderError, match="code 1001: auth failed"):
        await make_provider(client).execute(video_request(tmp_path))


async def test_duration_clamped_to_supported_range(tmp_path):
    client = FakeClient(
        submit=ok({"id": "task123", "status": "submitted"}),
        polls=[ok([succeeded_task()])])
    await make_provider(client).execute(
        video_request(tmp_path, params={"duration_s": "99"}))
    assert client.posts[0].body["settings"]["duration"] == 10


async def test_missing_first_frame_rejected(tmp_path):
    provider = make_provider(FakeClient(submit=None, polls=[]))
    with pytest.raises(ProviderError, match="first-frame"):
        await provider.execute(video_request(tmp_path, input_files=[]))

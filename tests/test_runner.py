"""End-to-end pipeline runs against MockProvider — no keys, no network, no spend."""
from pathlib import Path

import pytest

from tapnow.config import StepConfig
from tapnow.manifest import Manifest
from tapnow.providers import ProviderResult
from tapnow.providers.mock import MockProvider
from tapnow.runner import Runner, SpendCapExceeded

from .conftest import PLAN_SCHEMA, make_workflow


def full_chain_workflow():
    return make_workflow([
        StepConfig(id="plan", kind="text", provider="anthropic", model="text-model",
                   output="json", json_schema=PLAN_SCHEMA,
                   prompt="Plan from: {{ inputs.text[0].content }}"),
        StepConfig(id="render", kind="image", provider="ark-seedream", model="image-model",
                   for_each="plan.scenes", prompt="{{ item.image_prompt }}", output="file"),
        StepConfig(id="animate", kind="video", provider="kling", model="video-model",
                   for_each="plan.scenes", prompt="{{ item.motion_prompt }}",
                   inputs=["render[{index}]"], output="file"),
        StepConfig(id="assemble", kind="assemble", provider="ffmpeg", model="ffmpeg",
                   inputs=["animate"], output="file"),
    ])


def make_runner(wf, input_folder, tmp_path, pricing, factory, cap=100.0):
    return Runner(wf, input_folder, pricing=pricing, run_dir=tmp_path / "run",
                  provider_factory=factory, spend_cap_usd=cap, log=lambda s: None)


async def test_full_chain_mock_run(input_folder, tmp_path, pricing, providers):
    runner = make_runner(full_chain_workflow(), input_folder, tmp_path, pricing, providers)
    manifest = await runner.run()

    steps = manifest.data["steps"]
    assert all(steps[s]["status"] == "done" for s in ("plan", "render", "animate", "assemble"))
    assert manifest.data["status"] == "done"

    # Mock plan has 2 scenes (schema minItems) -> 2 images -> 2 clips -> 1 video.
    assert len(steps["render"]["items"]) == 2
    assert len(steps["animate"]["items"]) == 2
    final = Path(steps["assemble"]["items"][0][0]["$file"])
    assert final.exists() and final.read_bytes().startswith(b"MOCKASSEMBLY")

    # The brief's content reached the planning prompt; each scene prompt
    # reached its image call; each clip got its scene's image as input.
    assert "lighthouse" in steps["plan"]["calls"][0]["prompt"]
    img_call = providers.cache["kling"].calls[0]
    assert img_call.input_files == [Path(steps["render"]["items"][0][0]["$file"])]

    # Intermediates saved per call, and total spend logged (mock = free).
    assert (tmp_path / "run/steps/plan/call_000/output.json").exists()
    assert (tmp_path / "run/steps/render/call_000/prompt.txt").exists()
    assert manifest.total_cost == 0.0


async def test_json_handoff_retries_then_succeeds(input_folder, tmp_path, pricing, providers):
    providers.cache["anthropic"] = MockProvider(script=[
        "this is not json",
        {"scenes": [{"image_prompt": "a", "motion_prompt": "b"},
                    {"image_prompt": "c", "motion_prompt": "d"}]},
    ])
    wf = make_workflow([full_chain_workflow().steps[0]])
    manifest = await make_runner(wf, input_folder, tmp_path, pricing, providers).run()

    calls = manifest.data["steps"]["plan"]["calls"]
    assert len(calls) == 2
    assert "json_error" in calls[0]
    assert "not valid JSON" in calls[1]["prompt"]


async def test_json_handoff_gives_up_after_max_attempts(input_folder, tmp_path, pricing, providers):
    providers.cache["anthropic"] = MockProvider(script=["nope", "nope", "nope"])
    wf = make_workflow([full_chain_workflow().steps[0]])
    with pytest.raises(ValueError, match="no schema-valid JSON after 3 attempts"):
        await make_runner(wf, input_folder, tmp_path, pricing, providers).run()


async def test_resume_skips_completed_steps(input_folder, tmp_path, pricing, providers):
    providers.cache["kling"] = MockProvider(script=[RuntimeError("provider outage")])
    runner = make_runner(full_chain_workflow(), input_folder, tmp_path, pricing, providers)
    with pytest.raises(RuntimeError, match="outage"):
        await runner.run()
    assert Manifest.load(tmp_path / "run").data["status"] == "aborted"

    # Fresh providers, resumed run: plan/render are reused, not re-executed.
    cache2: dict[str, MockProvider] = {}
    factory2 = lambda name: cache2.setdefault(name, MockProvider())
    runner2 = make_runner(full_chain_workflow(), input_folder, tmp_path, pricing, factory2)
    manifest = await runner2.run(resume=True)

    assert manifest.data["status"] == "done"
    assert "anthropic" not in cache2 and "ark-seedream" not in cache2
    assert len(cache2["kling"].calls) == 2


async def test_estimate_over_cap_aborts_before_any_call(input_folder, tmp_path, pricing, providers):
    wf = make_workflow([StepConfig(id="expensive", kind="text", provider="anthropic",
                                   model="pricey", output="text", prompt="hi")])
    with pytest.raises(SpendCapExceeded, match="nothing was run"):
        await make_runner(wf, input_folder, tmp_path, pricing, providers, cap=5.0).run()
    assert providers.cache == {}  # no provider was even constructed


async def test_actual_spend_hitting_cap_aborts_midrun(input_folder, tmp_path, pricing, providers):
    providers.cache["anthropic"] = MockProvider(script=[
        ProviderResult(text="fine", cost_usd=0.05)])
    wf = make_workflow([
        StepConfig(id="a", kind="text", provider="anthropic", model="text-model",
                   output="text", prompt="hi"),
        StepConfig(id="b", kind="text", provider="other", model="text-model",
                   output="text", prompt="hi"),
    ])
    with pytest.raises(SpendCapExceeded, match="reached cap"):
        await make_runner(wf, input_folder, tmp_path, pricing, providers, cap=0.04).run()
    assert "other" not in providers.cache
    assert Manifest.load(tmp_path / "run").total_cost == 0.05

from tapnow.config import QualityGate, StepConfig
from tapnow.providers.mock import MockProvider

from .conftest import make_workflow
from .test_runner import make_runner


def gated_image_step(threshold=0.6, max_retries=2):
    return StepConfig(
        id="render", kind="image", provider="img", model="image-model",
        prompt="a lighthouse at dusk", output="file",
        quality_gate=QualityGate(provider="critic", model="text-model",
                                 threshold=threshold, max_retries=max_retries,
                                 prompt="Score {{ output }} for: a lighthouse at dusk"))


async def test_low_score_triggers_regeneration_with_feedback(
        input_folder, tmp_path, pricing, providers):
    providers.cache["critic"] = MockProvider(script=[
        {"score": 0.2, "feedback": "too dark, no lighthouse visible"},
        {"score": 0.9, "feedback": "good"},
    ])
    wf = make_workflow([gated_image_step()])
    manifest = await make_runner(wf, input_folder, tmp_path, pricing, providers).run()

    img = providers.cache["img"]
    assert len(img.calls) == 2                      # original + one regeneration
    assert "too dark" in img.calls[1].prompt        # feedback fed back in
    assert len(providers.cache["critic"].calls) == 2
    # The passing attempt (second image) is what the step keeps.
    kept = manifest.data["steps"]["render"]["items"][0][0]["$file"]
    assert "mock_image_2" in kept


async def test_gate_sees_reference_inputs_before_output(
        input_folder, tmp_path, pricing, providers):
    step = gated_image_step()
    step.quality_gate.inputs = ["inputs.image"]
    providers.cache["critic"] = MockProvider(script=[
        {"score": 0.9, "feedback": "good"}])
    wf = make_workflow([step])
    await make_runner(wf, input_folder, tmp_path, pricing, providers).run()

    gate_call = providers.cache["critic"].calls[0]
    # reference image first, generated output last
    assert gate_call.input_files[0].name == "ref.png"
    assert "mock_image" in gate_call.input_files[-1].name


async def test_gate_retries_capped_and_best_attempt_kept(
        input_folder, tmp_path, pricing, providers):
    providers.cache["critic"] = MockProvider(script=[
        {"score": 0.2, "feedback": "bad"},
        {"score": 0.3, "feedback": "slightly less bad"},
        {"score": 0.1, "feedback": "worse"},
    ])
    wf = make_workflow([gated_image_step(threshold=0.9, max_retries=2)])
    manifest = await make_runner(wf, input_folder, tmp_path, pricing, providers).run()

    assert len(providers.cache["img"].calls) == 3   # original + max_retries
    kept = manifest.data["steps"]["render"]["items"][0][0]["$file"]
    assert "mock_image_2" in kept                   # best score (0.3) was attempt 2

import pytest
from pydantic import ValidationError

from tapnow.config import StepConfig, WorkflowConfig, load_workflow
from .conftest import PROJECT_ROOT


def test_storyboard_workflow_loads():
    wf = load_workflow(PROJECT_ROOT / "workflows" / "storyboard.yaml")
    assert wf.name == "storyboard"
    assert [s.id for s in wf.steps] == ["plan", "render", "animate", "assemble"]
    assert wf.steps[1].quality_gate is not None
    assert wf.steps[0].output == "json" and wf.steps[0].json_schema


def test_json_output_requires_schema():
    with pytest.raises(ValidationError, match="requires json_schema"):
        StepConfig(id="x", kind="text", provider="mock", output="json")


def test_duplicate_step_ids_rejected():
    step = {"id": "a", "kind": "text", "provider": "mock", "output": "text"}
    with pytest.raises(ValidationError, match="duplicate"):
        WorkflowConfig(name="w", inputs={"mode": "flat"}, steps=[step, step])


def test_reserved_step_id_rejected():
    with pytest.raises(ValidationError, match="reserved"):
        WorkflowConfig(name="w", inputs={"mode": "flat"},
                       steps=[{"id": "inputs", "kind": "text", "provider": "mock", "output": "text"}])


def test_flat_mode_rejects_roles():
    with pytest.raises(ValidationError, match="must not declare roles"):
        WorkflowConfig(name="w", steps=[],
                       inputs={"mode": "flat", "roles": {"r": {"type": "image"}}})

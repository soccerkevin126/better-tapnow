from pathlib import Path

import pytest

from tapnow.config import (CountSpec, InputsConfig, StepConfig, WorkflowConfig)
from tapnow.pricing import ModelPrice, PricingTable
from tapnow.providers.mock import MockProvider

PROJECT_ROOT = Path(__file__).parent.parent

PLAN_SCHEMA = {
    "type": "object",
    "required": ["scenes"],
    "properties": {
        "scenes": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "required": ["image_prompt", "motion_prompt"],
                "properties": {
                    "image_prompt": {"type": "string"},
                    "motion_prompt": {"type": "string"},
                },
            },
        }
    },
}


@pytest.fixture
def input_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "input"
    folder.mkdir()
    (folder / "brief.md").write_text("A short film about a lighthouse.")
    (folder / "ref.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return folder


@pytest.fixture
def pricing() -> PricingTable:
    return PricingTable({
        "mock": ModelPrice(unit="per_call", per_call=0.0, verified=True),
        "text-model": ModelPrice(unit="per_call", per_call=0.01, verified=True),
        "image-model": ModelPrice(unit="per_image", per_image=0.05, verified=True),
        "video-model": ModelPrice(unit="per_second", per_second=0.10, verified=True),
        "ffmpeg": ModelPrice(unit="per_call", per_call=0.0, verified=True),
        "pricey": ModelPrice(unit="per_call", per_call=50.0, verified=True),
    })


@pytest.fixture
def providers() -> dict[str, MockProvider]:
    """One shared MockProvider per provider name, injectable per test."""
    cache: dict[str, MockProvider] = {}

    def factory(name: str) -> MockProvider:
        return cache.setdefault(name, MockProvider())

    factory.cache = cache  # type: ignore[attr-defined]
    return factory


def make_workflow(steps: list[StepConfig], *, images_min: int = 0) -> WorkflowConfig:
    return WorkflowConfig(
        name="test-wf",
        inputs=InputsConfig(mode="flat", expects={
            "text": CountSpec(min=1, max=1),
            "image": CountSpec(min=images_min),
        }),
        steps=steps,
    )

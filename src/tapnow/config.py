"""Workflow definitions are data. This module is the schema they must satisfy."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

MediaType = Literal["text", "image", "audio", "video"]
StepKind = Literal["text", "image", "video", "assemble"]


class CountSpec(BaseModel):
    min: int = 0
    max: int | None = None


class RoleSpec(CountSpec):
    type: MediaType


class InputsConfig(BaseModel):
    """Hybrid convention: 'flat' classifies files in the folder root by media
    type; 'roles' requires one subfolder per declared role."""

    mode: Literal["flat", "roles"] = "flat"
    expects: dict[MediaType, CountSpec] = Field(default_factory=dict)
    roles: dict[str, RoleSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _mode_matches_fields(self) -> "InputsConfig":
        if self.mode == "flat" and self.roles:
            raise ValueError("mode 'flat' must not declare roles; use mode 'roles'")
        if self.mode == "roles":
            if not self.roles:
                raise ValueError("mode 'roles' requires at least one role")
            if self.expects:
                raise ValueError("mode 'roles' must not declare 'expects'")
        return self


class QualityGate(BaseModel):
    provider: str
    model: str | None = None
    prompt: str  # template; sees {{ output }} plus the step's own context
    threshold: float = 0.7
    max_retries: int = 2
    # Refs (e.g. exact artwork) attached to the scoring call BEFORE the
    # step's output, so the scorer can compare against ground truth.
    inputs: list[str] = Field(default_factory=list)


class StepConfig(BaseModel):
    id: str
    kind: StepKind
    provider: str
    model: str | None = None
    prompt: str | None = None
    # Dotted ref producing a list; the step runs once per item as {{ item }}.
    for_each: str | None = None
    # Refs to files fed to the provider (image/video/assemble steps).
    inputs: list[str] = Field(default_factory=list)
    output: Literal["text", "json", "file"] = "file"
    json_schema: dict | None = None
    # Dry-run fan-out estimate when for_each depends on a model output
    # that doesn't exist yet.
    expected_items: int | None = None
    params: dict = Field(default_factory=dict)
    quality_gate: QualityGate | None = None

    @model_validator(mode="after")
    def _json_needs_schema(self) -> "StepConfig":
        if self.output == "json" and not self.json_schema:
            raise ValueError(f"step '{self.id}': output 'json' requires json_schema")
        return self


class WorkflowConfig(BaseModel):
    name: str
    description: str = ""
    inputs: InputsConfig
    steps: list[StepConfig]

    @model_validator(mode="after")
    def _unique_step_ids(self) -> "WorkflowConfig":
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        reserved = {"inputs", "item", "index"}
        for s in self.steps:
            if s.id in reserved:
                raise ValueError(f"step id '{s.id}' is reserved")
        return self


def load_workflow(path: Path) -> WorkflowConfig:
    with open(path) as f:
        data = yaml.safe_load(f)
    return WorkflowConfig.model_validate(data)


def find_workflow(name: str, workflows_dir: Path) -> WorkflowConfig:
    for ext in (".yaml", ".yml"):
        p = workflows_dir / f"{name}{ext}"
        if p.exists():
            return load_workflow(p)
    available = sorted(p.stem for p in workflows_dir.glob("*.y*ml"))
    raise FileNotFoundError(
        f"no workflow '{name}' in {workflows_dir} (available: {', '.join(available) or 'none'})"
    )

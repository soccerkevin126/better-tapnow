"""Pricing table and cost estimation.

Prices live in pricing.yaml. Entries carry `verified: false` until confirmed
against current official provider docs — unverified prices are fine for
--dry-run (shown with a warning) but a real run refuses to start on them,
so a stale placeholder can never silently authorize spend.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class ModelPrice:
    unit: str  # per_call | per_image | per_second | per_mtok_in_out
    verified: bool
    per_call: float = 0.0
    per_image: float = 0.0
    per_second: float = 0.0
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    notes: str = ""


class PricingTable:
    def __init__(self, models: dict[str, ModelPrice]):
        self.models = models

    @classmethod
    def load(cls, path: Path) -> "PricingTable":
        data = yaml.safe_load(path.read_text())
        return cls({name: ModelPrice(**spec) for name, spec in data["models"].items()})

    def get(self, model: str) -> ModelPrice:
        if model not in self.models:
            raise KeyError(f"no pricing entry for model '{model}' — add it to pricing.yaml")
        return self.models[model]

    def estimate_call(self, model: str, *, est_tokens_in: int = 2000,
                      est_tokens_out: int = 1500, est_seconds: float = 5.0) -> float:
        p = self.get(model)
        if p.unit == "per_call":
            return p.per_call
        if p.unit == "per_image":
            return p.per_image
        if p.unit == "per_second":
            return p.per_second * est_seconds
        if p.unit == "per_mtok_in_out":
            return (est_tokens_in * p.input_per_mtok + est_tokens_out * p.output_per_mtok) / 1_000_000
        raise ValueError(f"unknown pricing unit '{p.unit}' for {model}")

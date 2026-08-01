"""Run manifest: every step's prompt, model, cost, duration, and outputs.
Written after every provider call, so a killed run resumes where it stopped."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"


def serialize_value(value: Any) -> Any:
    """Step results → JSON. Paths become {'$file': str}; lists recurse."""
    if isinstance(value, Path):
        return {"$file": str(value)}
    if isinstance(value, list):
        return [serialize_value(v) for v in value]
    return value


def deserialize_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$file"}:
        return Path(value["$file"])
    if isinstance(value, list):
        return [deserialize_value(v) for v in value]
    return value


class Manifest:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data

    @classmethod
    def create(cls, run_dir: Path, *, workflow: str, input_folder: Path,
               spend_cap_usd: float | None) -> "Manifest":
        run_dir.mkdir(parents=True, exist_ok=True)
        m = cls(run_dir / MANIFEST_NAME, {
            "workflow": workflow,
            "input_folder": str(input_folder),
            "created_at": datetime.now().astimezone().isoformat(),
            "spend_cap_usd": spend_cap_usd,
            "total_cost_usd": 0.0,
            "status": "in_progress",
            "steps": {},
        })
        m.save()
        return m

    @classmethod
    def load(cls, run_dir: Path) -> "Manifest":
        path = run_dir / MANIFEST_NAME
        return cls(path, json.loads(path.read_text()))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str))
        os.replace(tmp, self.path)

    # -- step bookkeeping ----------------------------------------------------

    def step(self, step_id: str) -> dict:
        return self.data["steps"].setdefault(
            step_id, {"status": "pending", "items": [], "calls": []})

    def step_done(self, step_id: str) -> bool:
        return self.data["steps"].get(step_id, {}).get("status") == "done"

    def completed_items(self, step_id: str) -> list[Any]:
        return [deserialize_value(v) for v in self.step(step_id)["items"]]

    def record_item(self, step_id: str, value: Any, calls: list[dict]) -> None:
        s = self.step(step_id)
        s["status"] = "in_progress"
        s["items"].append(serialize_value(value))
        s["calls"].extend(calls)
        self.data["total_cost_usd"] = round(
            self.data["total_cost_usd"] + sum(c.get("cost_usd", 0.0) for c in calls), 6)
        self.save()

    def finish_step(self, step_id: str) -> None:
        self.step(step_id)["status"] = "done"
        self.save()

    def finish_run(self, status: str = "done") -> None:
        self.data["status"] = status
        self.data["finished_at"] = datetime.now().astimezone().isoformat()
        self.save()

    @property
    def total_cost(self) -> float:
        return self.data["total_cost_usd"]

    def step_result(self, step_id: str, fan_out: bool) -> Any:
        items = self.completed_items(step_id)
        return items if fan_out else items[0]

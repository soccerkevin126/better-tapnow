"""The runner: plans, estimates, validates, executes, records.

Orchestration concerns (templating, JSON-handoff retries, quality gates,
spend accounting, manifests, resume) all live here; provider adapters only
turn one ProviderRequest into one ProviderResult.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import jsonschema

from .classify import ClassifiedInputs, InputFile, classify_folder, validate_inputs
from .config import QualityGate, StepConfig, WorkflowConfig
from .manifest import Manifest
from .pricing import PricingTable
from .providers import Provider, ProviderRequest
from .refs import render_template, resolve_ref

MAX_JSON_ATTEMPTS = 3

GATE_SCHEMA = {
    "type": "object",
    "required": ["score", "feedback"],
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "feedback": {"type": "string"},
    },
}


class ValidationFailed(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("input validation failed:\n  - " + "\n  - ".join(problems))


class SpendCapExceeded(Exception):
    pass


@dataclass
class PlannedStep:
    step: StepConfig
    n_calls: int
    calls_known: bool          # False when fan-out size is a guess
    gate_calls: int
    est_cost: float
    worst_cost: float
    unverified_pricing: bool


@dataclass
class Plan:
    steps: list[PlannedStep]
    problems: list[str]
    est_total: float = 0.0
    worst_total: float = 0.0
    unverified_models: list[str] = field(default_factory=list)


class Runner:
    def __init__(
        self,
        workflow: WorkflowConfig,
        input_folder: Path,
        *,
        pricing: PricingTable,
        run_dir: Path,
        provider_factory: Callable[[str], Provider],
        spend_cap_usd: float | None,
        log: Callable[[str], None] = print,
    ):
        self.workflow = workflow
        self.input_folder = input_folder
        self.pricing = pricing
        self.run_dir = run_dir
        self.spend_cap = spend_cap_usd
        self.log = log
        self._factory = provider_factory
        self._providers: dict[str, Provider] = {}
        self._spent = 0.0

    def provider(self, name: str) -> Provider:
        if name not in self._providers:
            self._providers[name] = self._factory(name)
        return self._providers[name]

    # -- planning / estimation (no providers touched) ------------------------

    def classify(self) -> ClassifiedInputs:
        return classify_folder(self.input_folder, self.workflow.inputs)

    def plan(self) -> Plan:
        inputs = self.classify()
        problems = validate_inputs(inputs, self.workflow.inputs)
        plan = Plan(steps=[], problems=problems)
        for step in self.workflow.steps:
            n_calls, known = self._fan_out_estimate(step, inputs)
            price = self.pricing.get(step.model or step.provider)
            per_call = self.pricing.estimate_call(step.model or step.provider)
            gate_calls, gate_per_call, worst_extra = 0, 0.0, 0.0
            unverified = not price.verified
            if step.quality_gate:
                g = step.quality_gate
                gate_model = g.model or g.provider
                gate_calls = n_calls
                gate_per_call = self.pricing.estimate_call(gate_model)
                unverified = unverified or not self.pricing.get(gate_model).verified
                # Worst case: every item exhausts its regeneration budget.
                worst_extra = n_calls * g.max_retries * (per_call + gate_per_call)
            est = n_calls * per_call + gate_calls * gate_per_call
            plan.steps.append(PlannedStep(
                step=step, n_calls=n_calls, calls_known=known,
                gate_calls=gate_calls, est_cost=est, worst_cost=est + worst_extra,
                unverified_pricing=unverified,
            ))
            plan.est_total += est
            plan.worst_total += est + worst_extra
            if unverified:
                for m in {step.model or step.provider,
                          (step.quality_gate.model or step.quality_gate.provider)
                          if step.quality_gate else None}:
                    if m and not self.pricing.get(m).verified and m not in plan.unverified_models:
                        plan.unverified_models.append(m)
        return plan

    def _fan_out_estimate(self, step: StepConfig, inputs: ClassifiedInputs) -> tuple[int, bool]:
        if not step.for_each:
            return 1, True
        if step.for_each.startswith("inputs."):
            try:
                items = resolve_ref(step.for_each, {"inputs": _inputs_ctx(inputs)})
                return max(len(items), 1), True
            except Exception:
                return 1, False
        return (step.expected_items, True) if step.expected_items else (1, False)

    # -- execution -----------------------------------------------------------

    async def run(self, *, resume: bool = False) -> Manifest:
        inputs = self.classify()
        problems = validate_inputs(inputs, self.workflow.inputs)
        if problems:
            raise ValidationFailed(problems)

        plan = self.plan()
        if self.spend_cap is not None and plan.est_total > self.spend_cap:
            raise SpendCapExceeded(
                f"estimated cost ${plan.est_total:.4f} exceeds cap "
                f"${self.spend_cap:.2f} (TAPNOW_MAX_SPEND_USD) — nothing was run")

        if resume and (self.run_dir / "manifest.json").exists():
            manifest = Manifest.load(self.run_dir)
            self._spent = manifest.total_cost
            self.log(f"resuming run in {self.run_dir} "
                     f"(spent so far: ${manifest.total_cost:.4f})")
        else:
            manifest = Manifest.create(
                self.run_dir, workflow=self.workflow.name,
                input_folder=self.input_folder, spend_cap_usd=self.spend_cap)

        ctx: dict[str, Any] = {"inputs": _inputs_ctx(inputs)}
        try:
            for step in self.workflow.steps:
                await self._run_step(step, ctx, manifest)
        except BaseException:
            manifest.finish_run("aborted")
            raise
        manifest.finish_run("done")
        return manifest

    async def _run_step(self, step: StepConfig, ctx: dict, manifest: Manifest) -> None:
        fan_out = step.for_each is not None
        if manifest.step_done(step.id):
            ctx[step.id] = manifest.step_result(step.id, fan_out)
            self.log(f"[{step.id}] already done — skipping")
            return

        items = resolve_ref(step.for_each, ctx) if fan_out else [None]
        if fan_out and not isinstance(items, list):
            raise ValueError(f"step '{step.id}': for_each '{step.for_each}' is not a list")

        done = manifest.completed_items(step.id)
        results: list[Any] = list(done)
        if done:
            self.log(f"[{step.id}] resuming at item {len(done) + 1}/{len(items)}")

        for index in range(len(done), len(items)):
            call_ctx = dict(ctx)
            if fan_out:
                call_ctx["item"] = items[index]
                call_ctx["index"] = index
            out_dir = self.run_dir / "steps" / step.id / f"call_{index:03d}"
            value, calls = await self._execute_gated(step, call_ctx, out_dir, index)
            results.append(value)
            manifest.record_item(step.id, value, calls)
            label = f"{step.id}[{index}]" if fan_out else step.id
            self.log(f"[{label}] done — ${sum(c['cost_usd'] for c in calls):.4f}")

        manifest.finish_step(step.id)
        ctx[step.id] = results if fan_out else results[0]

    async def _execute_gated(self, step: StepConfig, ctx: dict,
                             out_dir: Path, index: int) -> tuple[Any, list[dict]]:
        """Run one unit of a step; if it has a quality gate, score and
        regenerate up to gate.max_retries, keeping the best attempt."""
        calls: list[dict] = []
        prompt = render_template(step.prompt, ctx) if step.prompt else None
        value = await self._provider_call(step, prompt, ctx, out_dir, calls)
        gate = step.quality_gate
        if not gate:
            return value, calls

        best_value, best_score = value, None
        for attempt in range(gate.max_retries + 1):
            score, feedback = await self._score(gate, ctx, value, out_dir, calls)
            if best_score is None or score > best_score:
                best_value, best_score = value, score
            if score >= gate.threshold:
                break
            if attempt == gate.max_retries:
                self.log(f"[{step.id}] gate retries exhausted; keeping best "
                         f"attempt (score {best_score:.2f})")
                break
            retry_prompt = (
                f"{prompt}\n\nA previous attempt scored {score:.2f} "
                f"(threshold {gate.threshold}). Reviewer feedback:\n{feedback}\n"
                f"Generate an improved result.")
            value = await self._provider_call(
                step, retry_prompt, ctx, out_dir / f"retry_{attempt + 1}", calls)
        return best_value, calls

    async def _score(self, gate: QualityGate, ctx: dict, value: Any,
                     out_dir: Path, calls: list[dict]) -> tuple[float, str]:
        gate_ctx = dict(ctx)
        gate_ctx["output"] = [str(p) for p in value] if isinstance(value, list) else value
        gate_step = StepConfig(
            id="quality_gate", kind="text", provider=gate.provider,
            model=gate.model, prompt=gate.prompt, output="json",
            json_schema=GATE_SCHEMA, inputs=gate.inputs)
        files = [p for p in value if isinstance(p, Path)] if isinstance(value, list) else []
        result = await self._provider_call(
            gate_step, render_template(gate.prompt, gate_ctx), gate_ctx,
            out_dir / "gate", calls, extra_files=files)
        return float(result["score"]), result["feedback"]

    async def _provider_call(self, step: StepConfig, prompt: str | None, ctx: dict,
                             out_dir: Path, calls: list[dict],
                             extra_files: list[Path] | None = None) -> Any:
        self._check_cap()
        provider = self.provider(step.provider)
        input_files = self._resolve_files(step, ctx) + (extra_files or [])
        params = {k: render_template(v, ctx) if isinstance(v, str) else v
                  for k, v in step.params.items()}
        model = step.model or step.provider

        attempts: list[str] = []
        current_prompt = prompt
        for attempt in range(MAX_JSON_ATTEMPTS if step.output == "json" else 1):
            self._check_cap()
            request = ProviderRequest(
                kind=step.kind, model=model, prompt=current_prompt,
                input_files=input_files, params=params, output_dir=out_dir,
                json_schema=step.json_schema)
            start = time.monotonic()
            result = await provider.execute(request)
            record = {
                "step": step.id, "provider": step.provider, "model": model,
                "prompt": current_prompt, "cost_usd": result.cost_usd,
                "duration_s": round(time.monotonic() - start, 3),
                "output_files": [str(f) for f in result.files],
                "attempt": attempt + 1,
                "meta": result.meta,
            }
            calls.append(record)
            self._spent += result.cost_usd

            if step.output == "file":
                _save_text(out_dir, "prompt.txt", current_prompt)
                return result.files
            if step.output == "text":
                _save_text(out_dir, "output.txt", result.text or "")
                return result.text
            # output == "json": parse + schema-validate, retry with the error.
            try:
                parsed = _parse_json(result.text or "")
                jsonschema.validate(parsed, step.json_schema)
                _save_text(out_dir, "output.json", json.dumps(parsed, indent=2))
                return parsed
            except (ValueError, jsonschema.ValidationError) as e:
                attempts.append(str(e))
                record["json_error"] = str(e)[:500]
                current_prompt = (
                    f"{prompt}\n\nYour previous reply was not valid JSON for the "
                    f"required schema. Error: {e}\nReply with ONLY the corrected "
                    f"JSON object, no prose, no code fences.")
        raise ValueError(
            f"step '{step.id}': no schema-valid JSON after {MAX_JSON_ATTEMPTS} "
            f"attempts. Last error: {attempts[-1]}")

    def _resolve_files(self, step: StepConfig, ctx: dict) -> list[Path]:
        files: list[Path] = []
        for ref in step.inputs:
            concrete = ref.replace("{index}", str(ctx.get("index", 0)))
            _flatten_files(resolve_ref(concrete, ctx), files)
        return files

    def _check_cap(self) -> None:
        spent = self._spent
        if self.spend_cap is not None and spent >= self.spend_cap:
            raise SpendCapExceeded(
                f"actual spend ${spent:.4f} reached cap ${self.spend_cap:.2f} — "
                f"run aborted; resume with a higher cap to continue")


def _inputs_ctx(inputs: ClassifiedInputs) -> dict:
    ctx: dict[str, Any] = dict(inputs.by_type)
    ctx.update(inputs.by_role)
    return ctx


def _flatten_files(value: Any, out: list[Path]) -> None:
    if isinstance(value, InputFile):
        out.append(value.path)
    elif isinstance(value, Path):
        out.append(value)
    elif isinstance(value, str):
        out.append(Path(value))
    elif isinstance(value, list):
        for v in value:
            _flatten_files(v, out)
    else:
        raise ValueError(f"cannot use {type(value).__name__} as a file input")


def _parse_json(text: str) -> Any:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from e


def _save_text(out_dir: Path, name: str, content: str | None) -> None:
    if content is None:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(content)

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import click
from dotenv import load_dotenv

from .config import find_workflow
from .pricing import PricingTable
from .providers import get_provider
from .runner import Plan, Runner, SpendCapExceeded, ValidationFailed
from .classify import classify_folder, validate_inputs

EXIT_VALIDATION = 2
EXIT_SPEND_CAP = 3


def _project_paths(workflows_dir: str | None, pricing_path: str | None):
    root = Path.cwd()
    return (Path(workflows_dir) if workflows_dir else root / "workflows",
            Path(pricing_path) if pricing_path else root / "pricing.yaml")


def _spend_cap(mock: bool) -> float | None:
    raw = os.environ.get("TAPNOW_MAX_SPEND_USD")
    if raw:
        return float(raw)
    if mock:
        return 1000.0  # mock spend is always $0; keep the code path exercised
    return None


@click.group()
def main() -> None:
    """tapnow — configurable AI media-generation pipelines."""
    load_dotenv()


@main.command()
@click.option("--workflows-dir", default=None, help="Directory of workflow YAMLs (default ./workflows)")
def workflows(workflows_dir: str | None) -> None:
    """List available workflows."""
    wdir, _ = _project_paths(workflows_dir, None)
    for p in sorted(wdir.glob("*.y*ml")):
        from .config import load_workflow
        wf = load_workflow(p)
        click.echo(f"{wf.name:20s} {wf.description}")


@main.command()
@click.argument("workflow")
@click.option("--input", "input_folder", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--workflows-dir", default=None)
def validate(workflow: str, input_folder: str, workflows_dir: str | None) -> None:
    """Validate an input folder against a workflow, nothing else."""
    wdir, _ = _project_paths(workflows_dir, None)
    wf = find_workflow(workflow, wdir)
    inputs = classify_folder(Path(input_folder), wf.inputs)
    problems = validate_inputs(inputs, wf.inputs)
    _print_inputs(inputs)
    if problems:
        click.echo("\nProblems:")
        for p in problems:
            click.echo(f"  ✗ {p}")
        sys.exit(EXIT_VALIDATION)
    click.echo("\n✓ input folder is valid for this workflow")


@main.command()
@click.argument("workflow")
@click.option("--input", "input_folder", type=click.Path(exists=True, file_okay=False),
              help="Input folder (required unless --resume)")
@click.option("--dry-run", is_flag=True, help="Print plan + cost estimate; zero API calls.")
@click.option("--mock", is_flag=True, help="Run the full pipeline with the offline MockProvider.")
@click.option("--resume", "resume_dir", type=click.Path(exists=True, file_okay=False),
              help="Resume an interrupted run directory.")
@click.option("--workflows-dir", default=None)
@click.option("--pricing", "pricing_path", default=None)
@click.option("--runs-dir", default="runs")
def run(workflow: str, input_folder: str | None, dry_run: bool, mock: bool,
        resume_dir: str | None, workflows_dir: str | None,
        pricing_path: str | None, runs_dir: str) -> None:
    """Run WORKFLOW against an input folder."""
    wdir, ppath = _project_paths(workflows_dir, pricing_path)
    wf = find_workflow(workflow, wdir)
    pricing = PricingTable.load(ppath)

    if resume_dir:
        import json
        prev = json.loads((Path(resume_dir) / "manifest.json").read_text())
        input_folder = input_folder or prev["input_folder"]
        run_dir = Path(resume_dir)
    else:
        if not input_folder and Path("input").is_dir():
            input_folder = "input"  # the project's default drop folder
        if not input_folder:
            raise click.UsageError(
                "--input is required (or put your content in ./input, "
                "or --resume a previous run)")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = Path(runs_dir) / f"{wf.name}-{stamp}"

    cap = _spend_cap(mock)
    runner = Runner(
        wf, Path(input_folder), pricing=pricing, run_dir=run_dir,
        provider_factory=lambda name: get_provider(name, mock=mock, pricing=pricing),
        spend_cap_usd=cap, log=click.echo)

    plan = runner.plan()
    _print_plan(wf.name, Path(input_folder), plan, cap, runner)

    if plan.problems:
        click.echo("\n✗ input folder does not satisfy this workflow — nothing was run.")
        sys.exit(EXIT_VALIDATION)

    if dry_run:
        click.echo("\nDRY RUN — no API calls were made, nothing was spent.")
        if cap is not None and plan.est_total > cap:
            click.echo(f"⚠ estimate ${plan.est_total:.4f} exceeds the "
                       f"${cap:.2f} cap — a real run would abort.")
        return

    if not mock and plan.unverified_models:
        click.echo(f"\n✗ refusing a real run: pricing for "
                   f"{', '.join(plan.unverified_models)} is not verified against "
                   f"official docs yet (pricing.yaml). Use --mock or --dry-run.")
        sys.exit(EXIT_SPEND_CAP)
    if cap is None:
        raise click.UsageError("set TAPNOW_MAX_SPEND_USD in .env before a real run")

    try:
        manifest = asyncio.run(runner.run(resume=resume_dir is not None))
    except ValidationFailed as e:
        click.echo(f"\n✗ {e}")
        sys.exit(EXIT_VALIDATION)
    except SpendCapExceeded as e:
        click.echo(f"\n✗ {e}")
        sys.exit(EXIT_SPEND_CAP)

    click.echo(f"\n✓ run complete — actual spend ${manifest.total_cost:.4f}")
    click.echo(f"  outputs + manifest: {run_dir}")


def _testsrc(out: Path, spec: str, *extra: str) -> Path:
    import subprocess
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", spec, *extra, "-pix_fmt", "yuv420p", str(out)], check=True)
    return out


@main.command()
@click.argument("provider_name")
@click.option("--pricing", "pricing_path", default=None)
def smoke(provider_name: str, pricing_path: str | None) -> None:
    """Verify one real provider with a single minimal call.

    anthropic ~ $0.01 · ark-seedream $0.035 · kling ~ $0.25 · ffmpeg free.
    """
    import asyncio as _asyncio

    from .providers import ProviderRequest

    _, ppath = _project_paths(None, pricing_path)
    pricing = PricingTable.load(ppath)
    out_dir = Path("runs") / "smoke" / provider_name

    if provider_name == "anthropic":
        request = ProviderRequest(kind="text", model="claude-fable-5",
                                  prompt="Reply with exactly: OK", output_dir=out_dir)
    elif provider_name == "fal-seedream":
        request = ProviderRequest(kind="image",
                                  model="fal-ai/bytedance/seedream/v4/text-to-image",
                                  prompt="A single red apple on a white table, soft studio light",
                                  output_dir=out_dir)
    elif provider_name == "ark-seedream":
        request = ProviderRequest(kind="image", model="seedream-5-0-lite-260128",
                                  prompt="A single red apple on a white table, soft studio light",
                                  params={"size": "1K"}, output_dir=out_dir)
    elif provider_name == "kling":
        frame = _testsrc(out_dir / "frame.png",
                         "testsrc=duration=1:size=640x360:rate=1", "-frames:v", "1")
        request = ProviderRequest(kind="video", model="kling-3.0",
                                  prompt="The colorful test pattern slowly shimmers and drifts",
                                  input_files=[frame], params={"duration_s": 3},
                                  output_dir=out_dir)
    elif provider_name == "ffmpeg":
        clips = [_testsrc(out_dir / "a.mp4", "testsrc=duration=1:size=640x360:rate=24"),
                 _testsrc(out_dir / "b.mp4", "testsrc2=duration=1:size=1280x720:rate=30")]
        request = ProviderRequest(kind="assemble", model="ffmpeg",
                                  input_files=clips, output_dir=out_dir)
    else:
        raise click.UsageError(
            "no smoke test for '{}' (available: anthropic, fal-seedream, "
            "ark-seedream, kling, ffmpeg)".format(provider_name))

    provider = get_provider(provider_name, pricing=pricing)
    click.echo(f"calling {provider_name} ({request.model}) …")
    result = _asyncio.run(provider.execute(request))
    if result.text is not None:
        click.echo(f"response: {result.text!r}")
    for f in result.files:
        click.echo(f"file:     {f}")
    if result.meta:
        click.echo(f"meta:     {result.meta}")
    click.echo(f"cost:     ${result.cost_usd:.4f}")
    click.echo("✓ adapter works")


def _print_inputs(inputs) -> None:
    click.echo("Input folder contents:")
    for mtype, files in sorted(inputs.by_type.items()):
        names = ", ".join(f.name for f in files)
        click.echo(f"  {mtype:6s} ({len(files)}): {names}")
    for p in inputs.unclassified:
        click.echo(f"  ?      : {p.name} (unclassified)")


def _print_plan(name: str, folder: Path, plan: Plan, cap: float | None, runner: Runner) -> None:
    click.echo(f"Workflow: {name}")
    click.echo(f"Input:    {folder}")
    _print_inputs(runner.classify())
    if plan.problems:
        click.echo("\nProblems:")
        for p in plan.problems:
            click.echo(f"  ✗ {p}")
        return
    click.echo(f"\n{'step':<12} {'provider':<14} {'model':<18} {'calls':>5} {'gate':>4} {'est $':>8} {'worst $':>8}")
    for ps in plan.steps:
        calls = str(ps.n_calls) + ("" if ps.calls_known else "?")
        click.echo(f"{ps.step.id:<12} {ps.step.provider:<14} "
                   f"{(ps.step.model or '-'):<18} {calls:>5} "
                   f"{ps.gate_calls or '-':>4} {ps.est_cost:>8.4f} {ps.worst_cost:>8.4f}")
    click.echo(f"{'total':<51} {plan.est_total:>8.4f} {plan.worst_total:>8.4f}")
    cap_s = f"${cap:.2f}" if cap is not None else "NOT SET"
    click.echo(f"\nSpend cap (TAPNOW_MAX_SPEND_USD): {cap_s}")
    if plan.unverified_models:
        click.echo(f"⚠ pricing UNVERIFIED for: {', '.join(plan.unverified_models)} "
                   f"— estimates are placeholders until provider docs are fetched.")

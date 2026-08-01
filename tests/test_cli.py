"""CLI-level tests against the real storyboard workflow and sample input.
Everything here runs with zero API calls."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from tapnow.cli import main

from .conftest import PROJECT_ROOT

EXAMPLES = PROJECT_ROOT / "examples" / "input"


@pytest.fixture(autouse=True)
def in_project_root(monkeypatch):
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.delenv("TAPNOW_MAX_SPEND_USD", raising=False)


def test_dry_run_prints_plan_and_spends_nothing():
    result = CliRunner().invoke(main, ["run", "storyboard", "--input", str(EXAMPLES), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DRY RUN — no API calls" in result.output
    for step in ("plan", "render", "animate", "assemble"):
        assert step in result.output
    # every model in the default workflow now has doc-verified pricing
    assert "UNVERIFIED" not in result.output


def test_validation_failure_reports_whats_missing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = CliRunner().invoke(main, ["run", "storyboard", "--input", str(empty), "--dry-run"])
    assert result.exit_code == 2
    assert "at least 1 text" in result.output
    assert "nothing was run" in result.output


def test_real_run_requires_spend_cap():
    # Pricing is verified, so a real run proceeds to the next guard: it must
    # refuse to start when no spend cap is configured.
    result = CliRunner().invoke(main, ["run", "storyboard", "--input", str(EXAMPLES)])
    assert result.exit_code != 0
    assert "TAPNOW_MAX_SPEND_USD" in result.output


def test_full_mock_run_via_cli(tmp_path):
    result = CliRunner().invoke(main, [
        "run", "storyboard", "--input", str(EXAMPLES),
        "--mock", "--runs-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "run complete — actual spend $0.0000" in result.output

    run_dir = next(tmp_path.iterdir())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "done"
    assert set(manifest["steps"]) == {"plan", "render", "animate", "assemble"}
    assert Path(manifest["steps"]["assemble"]["items"][0][0]["$file"]).exists()


def test_validate_command():
    result = CliRunner().invoke(main, ["validate", "storyboard", "--input", str(EXAMPLES)])
    assert result.exit_code == 0, result.output
    assert "valid for this workflow" in result.output


def test_workflows_command_lists_storyboard():
    result = CliRunner().invoke(main, ["workflows"])
    assert result.exit_code == 0
    assert "storyboard" in result.output

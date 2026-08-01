# tapnow

Configurable AI media-generation pipelines. Define a workflow in YAML — a text
model plans, an image model renders, the text model critiques, a video model
animates, ffmpeg assembles — drop content into a folder, run one command, get
finished output.

```sh
uv sync
uv run tapnow run storyboard --input examples/input --dry-run   # plan + cost, zero API calls
uv run tapnow run storyboard --input examples/input --mock      # full offline pipeline
uv run pytest                                                   # no keys, no spend
```

## Status

**All four adapters are built**, each from current official docs, with
pricing verified and pinned in `pricing.yaml`:

| Provider | Model | Price | Verified against |
|---|---|---|---|
| `anthropic` | `claude-fable-5` (fallback `claude-opus-4-8`) | $10/$50 per MTok | platform.claude.com |
| `fal-seedream` (default) | Seedream V4 via fal.ai | $0.03/image | fal.ai model page |
| `ark-seedream` (alt) | `seedream-5-0-lite-260128` | $0.035/image | docs.byteplus.com — region-restricted |
| `kling` | `kling-3.0` (720p, audio off) | $0.084/s | kling.ai/dev/pricing |
| `ffmpeg` | local | free | — (verified with real runs) |

To go live: copy `.env.example` → `.env`, fill in the keys, then verify each
provider with one minimal call before trusting a full run:

```sh
uv run tapnow smoke anthropic      # ~$0.01
uv run tapnow smoke fal-seedream   # $0.03 — one image
uv run tapnow smoke kling          # ~$0.25 — one 3s 720p clip
uv run tapnow smoke ffmpeg         # free, works today
uv run tapnow run storyboard --input examples/input   # the real thing (~$2.28 est)
```

## Commands

```sh
tapnow workflows                              # list workflows
tapnow validate <workflow> --input <folder>   # check the folder, nothing else
tapnow run <workflow> --input <folder> [--dry-run | --mock]
tapnow run <workflow> --resume runs/<dir>     # continue an interrupted run
```

Exit codes: `2` input validation failed, `3` spend cap / unverified pricing.

## How the input folder works

Files are classified by media type (text / image / audio / video), never by
exact filename. A workflow declares one of two folder shapes:

**Flat** (the default) — files sit in the folder root and are bucketed by type:

```yaml
inputs:
  mode: flat
  expects:
    text:  { min: 1, max: 1 }   # exactly one brief
    image: { min: 0, max: 8 }   # optional references
```

```
input/
  brief.md          → text
  reference1.png    → image
```

In the default `storyboard` workflow, reference images (up to 8, any
filenames) actively shape the output: the planner **sees** them and writes
scene prompts consistent with their subject/palette/mood, and the renderer
switches to Seedream's image-editing endpoint with your references attached,
so every generated frame inherits their look. No references = plain
text-to-image from the brief alone.

**Roles** — for workflows where two files of the same type mean different
things (a style reference vs. the subject). One subfolder per declared role:

```yaml
inputs:
  mode: roles
  roles:
    reference: { type: image, min: 1, max: 1 }
    content:   { type: image, min: 1 }
```

```
input/
  reference/ style.jpg
  content/   a.jpg  b.jpg
```

The tradeoff: two shapes to know about, but each workflow uses exactly one —
simple workflows stay zero-ceremony, role-based ones stay unambiguous.
Validation runs before anything is spent; `tapnow validate` tells you exactly
what's missing or unclassifiable.

## How to write a workflow

One YAML file in `workflows/` = one workflow. No code. See
`workflows/storyboard.yaml` for the full example.

A workflow is `inputs` (above) plus ordered `steps`. Each step:

```yaml
- id: render                 # unique; later steps reference results by this id
  kind: image                # text | image | video | assemble
  provider: ark-seedream     # which adapter runs it
  model: seedream
  for_each: plan.scenes      # optional fan-out: run once per item
  expected_items: 4          # dry-run estimate when the list doesn't exist yet
  prompt: "{{ item.image_prompt }}"
  inputs: ["render[{index}]"]  # files to attach (refs; {index} = fan-out index)
  output: file               # text | json | file
  json_schema: { ... }       # required when output: json
  params: { duration_s: "{{ item.duration_s }}" }  # provider-specific knobs
```

**References.** `{{ ... }}` in prompts and dotted refs elsewhere resolve
against: `inputs.<type>` / `inputs.<role>` (classified files — `.content`
reads a text file), any prior step's `id` (its parsed JSON, text, or output
files), and inside a fan-out, `item` and `index`.

**Input/output shapes.** The workflow decides the mapping: `for_each` over a
model's output gives one→many; a final step whose `inputs` collect a whole
list gives many→one; combine them for many→many.

**JSON handoffs** (`output: json`) are schema-validated; on a parse or schema
failure the model is re-asked with the error, up to 3 attempts.

**Quality gate** (optional, per step): a text model scores the output 0–1;
below `threshold` the step regenerates with the reviewer's feedback, up to
`max_retries`; the best-scoring attempt is kept.

```yaml
quality_gate:
  provider: anthropic
  model: claude-fable-5
  threshold: 0.6
  max_retries: 2
  prompt: "Score the attached image against: {{ item.image_prompt }} ..."
```

## How to add a provider

One file in `src/tapnow/providers/`, implementing one method:

```python
from .base import Provider, ProviderRequest, ProviderResult, poll_until

class MyProvider(Provider):
    name = "my-provider"

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        # request: kind, model, prompt, input_files, params, output_dir, json_schema
        # Submit-then-poll APIs: use poll_until(check, timeout_s=..., ...)
        # Write output files into request.output_dir.
        return ProviderResult(files=[...], cost_usd=actual_cost)
```

Then register it in `providers/__init__.py` (`_REAL["my-provider"] = MyProvider`)
and add its model to `pricing.yaml`. Rules:

1. **No guessed API schemas.** Endpoints, params, and prices come from current
   official docs; if they can't be reached, stop — don't invent them.
2. Pricing entries start `verified: false`; the runner refuses real runs until
   the price is confirmed and flipped. Dry-run estimates show a warning.
3. Report **actual** cost in `ProviderResult.cost_usd`; the runner enforces
   the `TAPNOW_MAX_SPEND_USD` cap before every call and logs spend to the
   manifest.

Everything else — templating, JSON retries, quality gates, fan-out, resume,
manifests — lives in the runner; adapters stay dumb.

## Runs, manifests, resume

Each run writes `runs/<workflow>-<timestamp>/`:

```
manifest.json                 # per-call prompt, model, cost, duration, outputs
steps/<id>/call_NNN/          # every intermediate: prompts, JSON, images, clips
steps/<id>/call_NNN/gate/     # quality-gate verdicts
```

The manifest is saved after every provider call. If a run dies (timeout,
outage, spend cap), `--resume runs/<dir>` skips completed work — including
completed items inside a partially-finished fan-out step — and continues.

## Secrets and spend

Copy `.env.example` → `.env` (gitignored). `TAPNOW_MAX_SPEND_USD` is required
for real runs: the runner estimates first and aborts if the estimate exceeds
the cap, then tracks actual spend during the run and aborts if it reaches the
cap mid-flight.

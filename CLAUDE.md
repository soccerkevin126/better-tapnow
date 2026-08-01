# better-tapnow — notes for Claude instances

## ⚠ Machine-specific: iCloud breaks venvs in this repo's location

`~/Documents` on this Mac is iCloud-synced. iCloud's background sweep
(~1–2 min after file creation) sets the macOS `hidden` flag on dot-files,
dot-directories, **and everything inside them**. Python 3.14's `site.py`
silently skips hidden `.pth` files, so a normal `.venv` here breaks with
`ModuleNotFoundError: No module named 'tapnow'` minutes after `uv sync`.

**The setup that works (already in place — do not undo it):**

- The real venv is `venv.nosync/` (iCloud excludes `*.nosync` from sync).
- `.venv` is a symlink to `venv.nosync`. uv follows it.
- Both are gitignored.

**Every time** imports mysteriously fail here (or in any project under
`~/Documents`):

1. Check flags first, not the code: `stat -f %Sf .venv/lib/python3.14/site-packages/*.pth`
   — `hidden` means iCloud flagged it.
2. If `.venv` has become a real directory again (e.g. after deleting the venv),
   recreate the pair: `rm -rf .venv && mkdir venv.nosync && ln -s venv.nosync .venv && uv sync`.
3. Never create a plain `.venv` directory in this repo.

## Commands

```sh
uv sync                                                    # install (into venv.nosync via .venv symlink)
uv run pytest -q                                           # 28 tests, no keys, no network
uv run tapnow run storyboard --input examples/input --dry-run
uv run tapnow run storyboard --input examples/input --mock # full offline pipeline
```

## Project rules (from the original spec — do not relax)

- **Never guess API schemas or prices.** Real adapters are written only from
  current official provider docs; if docs are unreachable, stop and say so.
- `pricing.yaml` entries stay `verified: false` until confirmed against
  official docs; the CLI refuses real runs while any used model is unverified.
- Providers: one adapter file each in `src/tapnow/providers/`, registered in
  `_REAL` in `providers/__init__.py`. All orchestration (templating, JSON
  retries, quality gates, fan-out, resume, spend cap, manifests) stays in
  `runner.py` — adapters stay dumb.
- Workflows are YAML data in `workflows/`, never code.
- Tests must keep passing with no keys and no network.

## Prompting method (user-mandated — apply to EVERY model call)

The user requires Claude to hand-craft every prompt sent to generation
models. Follow the OFFICIAL Seedream 4.x prompt guide
(docs.byteplus.com/en/docs/ModelArk/1829186), which beats ad-hoc prompting:

1. Concise natural language: subject + action + environment. Do NOT stack
   dense ornate constraint walls — official guidance says concise beats
   ornate on Seedream 4.x.
2. State the application scenario explicitly ("Create a professional
   e-commerce product photograph: ...").
3. Multi-image input: name each reference's role explicitly — "Image 1 is
   the exact embroidery artwork...", "Image 2 is the garment ground
   truth...", "Image 3 shows the sun fade..." (attachment order = role
   dirs alphabetically, files alphabetical within each dir). Never attach
   references without telling the model what each one is.
4. Text that must render on the image goes in double quotes ("Said,").
5. State fixed elements explicitly ("keeping X unchanged"); state
   exclusions concretely (no neck labels/tags/red marks — models
   hallucinate garment-photo tropes).
6. Pin brand marks to exact artwork: render vectors from the source .ai
   at high res, crop cleanly (strip spec-box borders/leader lines), give
   each side only its own art (role subfolders).
7. Quality gates receive the refs (gate.inputs) and compare letterforms
   to Image 1 directly; encode measurable auto-fail rules (duplicated
   words < 0.5, ombre/orange fade < 0.6, chunky strokes < 0.65).
8. Domain lesson (refined per user across v1→v7): "sun fade" = organic
   UV bleach on the BODY PANEL ONLY — from collar through the embroidery
   zone down to about ARMPIT level, light/pale, dispersed and mottled.
   NEVER an ombre/two-tone band, and NO fade on the sleeves (tech pack
   2D mockup shows sleeves uniform dark).
9. VERSIONS ITERATE (user mandate, "what Tapnow does"): v(n+1) is a
   targeted EDIT of v(n)'s actual output image, not a regeneration.
   Pattern (see workflows/mockup-v5.yaml): copy the previous best into
   input/front_base & input/back_base, attach as Image 1, give ONE
   concise modification instruction + an explicit keep-unchanged list
   (per the official Seedream image-editing guide), and gate against
   both the previous version (preservation) and the exact art
   (letterforms). This locked lettering consistency and passed gates on
   the first attempt where full regeneration kept failing.

## Status

- Phase 1 done: scaffold, config, CLI, runner, MockProvider, `storyboard`
  workflow, dry-run + mock verified end to end.
- Phase 2 done — all four adapters built from official docs (2026-08-01),
  all pricing verified in pricing.yaml, 50 tests green, full mock pipeline
  and ffmpeg smoke verified for real:
  - `anthropic.py` — official SDK, claude-fable-5: thinking param omitted
    (always on), no sampling params, refusal → server-side fallback to
    claude-opus-4-8 (`server-side-fallback-2026-06-01` beta), structured
    outputs for JSON steps, cost from usage × price of the serving model.
  - `ark_seedream.py` — POST {base}/images/generations, Bearer ARK_API_KEY,
    b64_json responses, model seedream-5-0-lite-260128 @ $0.035/image.
    Default base is BytePlus ap-southeast-1; ARK_BASE_URL overrides (CN
    Volcano Engine: https://ark.cn-beijing.volces.com/api/v3).
  - `kling.py` — POST {base}/image-to-video/{model} (kling-3.0), plain
    API-key Bearer (KLING_API_KEY; AK/SK JWT is legacy), first_frame as
    raw base64 (needs ≥300px, aspect 1:2.5–2.5:1), poll GET /tasks?task_ids=
    with backoff, duration clamped 3–10s, actual cost read from the task's
    billing array (units × $0.14) with list-price fallback ($0.084/s 720p).
  - `ffmpeg.py` — local filter-graph concat with normalization; free.
  - `fal_seedream.py` — Seedream V4 via fal.ai queue API (submit POST
    queue.fal.run/{model} → poll /requests/{id}/status → result), auth
    `Authorization: Key $FAL_KEY`, sync_mode=true so images arrive as data
    URIs. $0.03/image verified on the fal model page. Built from fal's
    official OpenAPI spec. This is the DEFAULT image provider — BytePlus
    ModelArk signup is region-blocked for this user (ark adapter kept for
    accounts that have Ark access).
- Verified against live APIs: anthropic smoke test passed 2026-08-01
  (response 'OK', $0.0012, served by claude-fable-5). ffmpeg verified with
  real local runs.
- Live verification status (2026-08-01): anthropic ✅ (smoke, $0.0012),
  fal-seedream ✅ (smoke $0.03 + 4 real renders), ffmpeg ✅ (real runs),
  kling: adapter+auth ✅ but account has NO CREDITS (API code 1102).
  Live-API lessons already fixed: fal poll URLs must come from the queue
  response (status_url/response_url, NOT constructed paths); fal status
  returns HTTP 202 while pending; fal serves JPEG bytes regardless of
  filename — all adapters now sniff image magic bytes, never extensions.
- PAUSED REAL RUN awaiting Kling credits:
  `runs/storyboard-20260801-020221` — plan + all 4 renders done (gate
  scores 0.85/0.75/0.95/0.92), $0.3983 spent, resumes with
  `uv run tapnow run storyboard --resume runs/storyboard-20260801-020221`
  (goes straight to animate → assemble, ~$1.68 more).
- Reference images now flow through the whole chain: planner sees them
  (vision), renderer uses fal's /edit endpoint with them as data URIs
  (same $0.03, verified). Drop refs next to brief.md in the input folder.

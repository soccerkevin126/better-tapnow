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
models. **PROMPTING.md is the full playbook** (researched 2026-08-01 from
official Seedream/Kling/Anthropic docs, tech reports, a TapNow teardown,
and the VLM-judge literature — sources indexed there). Load-bearing rules:

1. Concise natural language: subject + action + environment; state the
   application scenario ("Create a professional e-commerce product
   photograph: ..."). Concise beats ornate (official). Hard cap 600 words;
   aim < 200. A server-side prompt rewriter always runs — clear semantics
   survive it, token tricks don't.
2. Multi-image input: name each reference's role explicitly — "Image 1 is
   the exact embroidery artwork..." (attachment order = role dirs
   alphabetically, files alphabetical within each dir). Image 1 = first
   array element. fal edit keeps only the LAST 10 image_urls if over —
   never exceed 10 refs. Add a tight zoomed crop of the wordmark as an
   extra labeled ref (front_zoom/, back_zoom/ role dirs).
3. Text that must render goes in double quotes, ALWAYS with position:
   'the chest embroidery reads "Said," exactly as in Image 1'. Consistent
   terminology: reuse the same noun for anything that must persist —
   never synonym-shuffle "the embroidery"/"the script".
4. Colors as hex + name pairs ("body #1B2A3C, deep faded navy"). Positive
   framing first ("a plain blank inner collar"), then a SHORT exclusion
   tail (no neck labels, no hang tags) — never a negative wall.
5. Edit prompts use Lock / Change / Scope / Output: keep-unchanged list
   (may come first), ONE concise change, where the edit is allowed
   ("body panel only"), output format. Region fixes without masks: draw a
   marker box onto the input image and reference it (official 4.x).
6. Pin brand marks to exact artwork: render vectors from the source .ai
   at high res, crop cleanly (strip spec-box borders/leader lines), give
   each side only its own art (role subfolders).
7. Resolution IS fidelity: image_size auto_2K for iteration, auto_4K for
   finals — small type collapses at 1K and upscaling never fixes broken
   text. Log fal's echoed seed for cheap re-rolls.
8. Quality gates (see PROMPTING.md §4): label all images; force
   character-by-character transcription of rendered text "exactly as
   rendered, including any errors" BEFORE comparison (VLMs silently
   auto-correct mangled text); binary checklist with cited evidence; then
   the score with measurable auto-fail caps (duplicated words < 0.5,
   ombre/orange fade < 0.6, chunky strokes < 0.65). Feedback = a
   directional edit instruction from the failed items. Don't ask Claude
   to count occurrences (unreliable) — that's an OCR/code job (roadmap).
9. Domain lesson (refined per user across v1→v7): "sun fade" = organic
   UV bleach on the BODY PANEL ONLY — from collar through the embroidery
   zone down to about ARMPIT level, light/pale, dispersed and mottled.
   NEVER an ombre/two-tone band, and NO fade on the sleeves (tech pack
   2D mockup shows sleeves uniform dark).
10. VERSIONS ITERATE (user mandate, "what Tapnow does"): v(n+1) is a
    targeted EDIT of v(n)'s actual output, not a regeneration — editing
    is Seedream's native format. BUT chains degrade (documented
    "prior-induced editing drift", ~5-10 edits; caused the v7 warm
    drift): cap chains at 3-4 hops from a pristine base, then RE-BASE on
    the best early version (v8 did this from v5). Gate every version
    against v1 for global tint/softness drift, not only against v(n-1).
    Template: workflows/mockup-template.yaml.
11. Kling i2v: the image is the scene — prompt ONLY the motion, 15-40
    words. Name the subject minimally to bind it; ONE slow camera move
    with an endpoint ("...then the camera settles") or fixed camera +
    fabric motion. kling-3.0 text-lock: name the mark ("The camera
    remains fixed on the "Said," embroidery; the lettering stays sharp").
    No negative_prompt field on 2.6/3.0 — exclusions as prose sentences.
    5-7s sweet spot. Loops: same image as first+last frame.
12. Fidelity ladder when lettering won't lock (PROMPTING.md §0): edit
    with refs → mechanically composite exact art then harmonize-only edit
    → marker-scoped region repair → live-type overlay in post. Industry
    tools never let the model draw letterforms from scratch.

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
- 2026-08-01 ENGINE SWITCH (user decision): mockup image edits now run on
  GPT IMAGE 2 via fal (`openai/gpt-image-2/edit`, adapter
  `fal_gpt_image.py`, provider `fal-gpt-image`) — #1 LMArena image-edit
  Elo (1463, 28.5M votes), forced high-fidelity input processing.
  Token-billed (~$0.80/edit conservative at pinned 1536x2048 high; see
  pricing.yaml notes). fal-seedream stays registered for storyboard t2i.
  Landscape research + fal schema dumps: scratchpad research-sota-models.md
  + the schema agent's spec_*.json files.
- Mockup v9 (Seedream v4, runs/mockup-v9-20260801-161255): BACK passed 0.9
  (subtle body-panel fade per the tech pack's page-8 reference photo —
  which is now correctly extracted to input/style/; the old style file was
  a mis-cropped measurement page). FRONT rejected at 0.62: Seedream redrew
  the "Said," script while editing the fade (its known failure mode).
  v9_back archived in outputs/sun-fade-tee/. Art refs re-rendered from the
  .ai at 600dpi, spec-box borders/leader lines scrubbed (input/front[0],
  input/back[0]). v10 = same edit on GPT Image 2, exact art attached to
  the RENDER as Image 3 (workflows/mockup-v10.yaml).
- v10 SHIPPED (GPT Image 2 first fire, runs/mockup-v10-20260801-164404):
  front 0.88 + back 0.92, BOTH first attempt, no retries, $1.93 total.
  Letterforms held during the fade edit — the failure Seedream v4 couldn't
  clear. Outputs: outputs/sun-fade-tee/v10_{front,back}.png. These are the
  current best pair; v5 bases remain the pristine edit roots.

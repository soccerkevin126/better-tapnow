# PROMPTING.md — the prompting playbook

Distilled 2026-08-01 from five parallel research streams: official Seedream
docs (BytePlus/Volcano/fal/tech reports), official Kling docs + API markdown,
a TapNow product teardown, a community/competitor letterform-fidelity sweep
(25+ sources), and VLM-as-judge literature. Every model call this pipeline
makes is hand-crafted per this playbook. CLAUDE.md carries the short version;
this file is the reference with reasoning and sources.

---

## 0. Strategy: why we win

**TapNow is an aggregator.** It calls the same public models we do (Seedream
5.0 Lite ~$0.04/img, Nano Banana Pro, Kling 3.0) at a markup. Its accuracy
comes from orchestration: role-labeled references with keep-unchanged lists,
edit chains where each version consumes the previous output, region-scoped
spot repair, and an agent QC pass for "copy accuracy". All of which this
runner already does or does better. TapNow cannot ingest vector artwork at
all (JPG/PNG only) — the tech pack is our structural advantage.

**The industry's dirty secret on letterform fidelity:** every commercial tool
that guarantees exact artwork (Recraft depth-warps your art onto the garment;
print-on-demand uses PSD displacement maps; try-on research warps real pixels
then synthesizes; Magic Hour overlays real logo assets post-gen) does NOT let
the model draw the letterforms. Independent benchmarks: Seedream 4.0 renders
usable text from scratch only ~38% of the time; 4.5 turned "LUNA BAKERY" into
"LUNRA" 1 run in 12.

**Fidelity ladder** (try the cheapest rung that passes the gate):
1. Reference-and-regenerate (current method): exact art as Image 1, quoted
   wordmark, keep-unchanged list. Works when the base image already carries
   correct lettering (edit mode preserves it).
2. Compose-then-harmonize: mechanically composite the rendered vector art
   onto the garment (Pillow warp at tech-pack placement), then one Seedream
   edit: "make the printed artwork follow the fabric folds and lighting;
   keep every letterform exactly as shown." The model never invents glyphs.
3. Region-scoped repair: draw a visible box/marker onto the input image over
   the flawed zone and edit only that ("replace the area marked by the red
   box…, keep the rest of the layout unchanged") — official 4.x technique;
   real coordinates (`<bbox>`) need 5.0-pro.
4. Give up on generation for the smallest text: composite live type/art in
   post. This is what fidelity-critical commercial tools actually ship.

**Edit chains have a documented ceiling.** "Prior-Induced Editing Drift":
untouched regions drift toward model priors; visible degradation by ~5-10
sequential edits (this is what made v6→v7 drift warm). Rules: cap chains at
3-4 hops from a pristine base, then RE-BASE (fresh edit from the best early
version, as v8 did from v5); gate every version against v1 for global
tint/softness drift, not just against v(n-1).

---

## 1. Seedream (fal `.../seedream/v4/*`, Ark 4.x/5.0-lite)

Primary official source: BytePlus ModelArk doc 1829186 (confirmed current
2026-07-05; the Volcano twin extends identical rules to 5.0-lite). fal
guidance from blog.fal.ai + model OpenAPI schemas. Tech facts from the
Seedream 4.0 report (arXiv 2509.20427).

### Prompt construction
1. **Natural-language sentences**: subject + action + environment. Concise
   beats ornate on 4.x/5.x (official, verbatim-confirmed). fal suggests
   <200 words; official hard guidance: **keep prompts under 600 words** —
   longer scatters attention and elements go missing.
2. **State the application scenario** ("Create a professional e-commerce
   product photograph: …") — official pattern.
3. **A server-side prompt rewriter always runs** (the PE model does task
   routing + prompt rewriting; `enhance_prompt_mode` on fal only picks its
   mode). Clear semantic statements survive rewriting; brittle token-order
   tricks don't.
4. **Consistent terminology** (tech report §2.2): reuse the SAME noun for
   anything that must persist ("the embroidery" everywhere — never rotate
   synonyms like "the stitching" / "the script").
5. **Quote text that must render**, unconditionally, with its position:
   `the chest embroidery reads "Said," exactly as in Image 1`. Without
   quotes the model treats words as descriptive keywords (fal, official).
   Keep rendered text short — accuracy drops after ~2-3 dense lines; error
   density scales inversely with type size.
6. **Colors as hex + name pairs**: "body color #1B2A3C, deep faded navy"
   (fal official for the 5.x lineage). Name the fade zone's color too.
7. **Positive framing first, short exclusion tail second**: state the
   desired state ("a plain blank inner collar, clean hem"), then at most a
   couple of hard exclusions ("no neck labels, no hang tags"). Never a
   negative wall. There is NO negative_prompt parameter — exclusions are
   prose.

### Multi-reference input
8. **Name every image's role**: "Image 1 is the exact embroidery artwork…"
   ("Figure N" also works). Image 1 = FIRST element of `image_urls` —
   proven by official demo code. Our attachment order: role dirs
   alphabetical, files alphabetical within each.
9. **fal edit keeps only the LAST 10 `image_urls`** when given more — which
   would silently drop Image 1 first. The adapter pre-truncates to the
   first 10; never design a workflow needing more than 10 refs on fal.
   (Ark 4.5/5.0-lite accept 14; in+out ≤ 15.)
10. **Reference recipe** (official): state (a) the Reference Target — what
    to extract/retain from each ref; (b) the Generated Scene Description.
11. **Add a tight zoomed crop of just the wordmark** as an extra labeled
    reference alongside the full art (OmniRefiner mechanism: zoomed
    reference patches restore high-frequency detail). Role dir convention:
    `front_zoom/`, `back_zoom/`.
12. **Sketch/tech-pack inputs**: official phrasing for stripping spec
    clutter — "Do not include any text or hand-drawn edges from the
    original sketch" (adapted: no spec boxes, leader lines, or dimension
    callouts).

### Editing (the v(n)→v(n+1) iteration mandate)
13. Editing is the model's NATIVE format (training data = ref image + one
    imperative instruction). One edit = ONE change.
14. **Lock / Change / Scope / Output** structure (community-validated;
    failures trace to a missing lock or vague scope):
    - Lock: "Keep the embroidered "Said," script (same letterforms, size,
      position), the ribbed collar, garment shape, fabric texture, folds,
      lighting, shadows, camera angle and the concrete floor unchanged."
      (Official examples put the keep-list first as often as last.)
    - Change: the single modification, concise and unambiguous.
    - Scope: where the edit is allowed ("the body panel only; no changes
      to sleeves, collar, or the embroidery").
    - Output: format/aspect if it matters.
15. Region designation without masks (4.x/5.0-lite): draw arrows/boxes onto
    the input image itself, then reference them ("replace the area marked
    by the red box"). Coordinate tags (`<point>`/`<bbox>`, 0-999
    normalized) are 5.0-pro only — including the official protect-region
    trick (bbox around the embroidery + "keep unchanged").

### Parameters (fal)
16. `image_size`: **pin `auto_2K` for iteration, `auto_4K` (or explicit
    ~4K WxH) for finals** — small type collapses at 1K; never rely on
    upscaling to fix broken text (fix prompt/composition instead). The
    edit endpoint's floor is only ~0.92MP; presets like `portrait_4_3`
    waste the detail budget.
17. `seed` is exposed and echoed back (log it; reuse for cheap re-rolls) —
    similarity is not guaranteed identical. Ark has no seed on 4.0+.
18. On Ark only: `watermark` defaults TRUE — always set false.

## 2. Kling image-to-video (kling-3.0, API 2.0)

Official sources: kling.ai/quickstart/* guides, blog posts, and the API
markdown (append `.md` to doc URLs; index at kling.ai/document-api/llms.txt).

1. **The image is the scene; prompt ONLY the motion.** Official formula:
   Subject + Movement (list multiple subject+movement pairs sequentially).
   Re-describing what the frame shows wastes words and can fight the image.
   15-40 words is the community sweet spot. Under-described → static video;
   motion that contradicts the image → camera cut.
2. **Name the subject minimally to bind it**: "the dark washed tee with the
   chest embroidery" — bare verbs ("wear sunglasses") fail; named subjects
   ("Mona Lisa puts on sunglasses with her hand") work.
3. **One slow camera move per clip, with an endpoint**: "slow dolly-in
   toward the chest embroidery, then the camera settles." Open-ended motion
   causes frozen/hung endings. Two moves in one prompt = confusion. Slow
   beats fast (fast warps). Prefer a 90° orbit over a full 360 unless
   supplying the back view (last_frame or Elements).
4. Official camera vocabulary: Pan, Tilt, Dolly In (logo/texture focus),
   Orbit (product showcase), Static Shot (fabric/liquid motion), "The
   camera remains fixed on…", "locked-off camera", "tripod", "fixed lens";
   speed words "ultra-slow motion / slow and deliberate / moderate pace".
5. **kling-3.0 has native text-lock** (built for e-commerce): it detects
   text/logos in the input frame and holds them stable — bind it by naming
   the mark: "The camera remains fixed on the "Said," embroidery; the
   lettering stays sharp and unchanged throughout."
6. **No negative_prompt field on the 2.6/3.0 endpoints** — write exclusions
   as sentences in the prompt: "No morphing, no warping of the embroidery
   or lettering, no color shift of the garment, no camera shake, no scene
   change, no new objects or people." (cfg_scale is legacy 1.x-only.)
7. Physics-plausible motions only. For garment stills the reliable pattern:
   fixed camera + fabric motion — "gentle fabric sway as if from a soft
   breeze, sleeves hang still, then the fabric settles."
8. Duration: 5-7s sweet spot; 5s hangs less than 10s. kling-3.0 accepts
   3-15s (adapter clamps 3-10 — a valid subset). Loops: same image as
   first_frame AND last_frame + "continuous motion without snapping or
   reversing"; very different start/end frames cause a lens switch.
9. Multi-shot (3.0): "shot n, m, words;" template (shots 1-6, seconds must
   sum to total, ≤512 chars each); we default `multi_shot: false`.

## 3. Claude as planner

- Storyboard `image_prompt`s must follow §1 (they are Seedream prompts):
  full sentences, scenario framing, quoted rendered text, ≤120 words.
- `motion_prompt`s must follow §2: motion-only, one camera move with an
  endpoint, exclusion sentences included.
- The planner sees the reference images (vision) — tell it to treat them as
  ground truth and to carry consistent terminology into every scene prompt.

## 4. Claude as quality gate (VLM-as-judge)

Known failure modes of VLM judges, from the literature:
- **Leniency**: false PASSES are the expected error direction.
- **Auto-correction**: VLMs report the text that SHOULD be there — they
  silently miss mangled letterforms (arXiv 2509.17418). Counter: force a
  character-by-character transcription "exactly as rendered, including any
  errors" BEFORE any comparison.
- **Score clustering** (everything lands 0.7-0.85): binary checklists beat
  scalar scores (CheckEval: +0.45 kappa); our measurable auto-fail caps do
  real work — keep them.
- **Counting is unreliable**: don't ask Claude to count word occurrences;
  that's an OCR/code job.
- Absolute scoring is weak; **pairwise comparison is far stronger** — for
  preservation checks, show v(n) and v(n+1) and ask which is more faithful
  and what changed outside the requested edit (run both orders if it's
  load-bearing).

Vision facts (official Anthropic docs, current):
- claude-fable-5 is on the high-res tier: max long edge 2576px, max 4784
  visual tokens/image (28×28-px patches; cost = ⌈w/28⌉×⌈h/28⌉). The adapter
  resizes to fit BOTH limits client-side (a 2048² render loses only ~6%).
- Images before text; label every image ("Image 1: the exact artwork…") —
  both official recommendations, already our convention.
- >20 images in one request triggers a stricter ~2000px per-image cap.
- Avoid heavy JPEG compression (text legibility); <200px content invites
  hallucination.

Gate prompt structure (encode in workflow YAML):
1. Label all images and their roles up front.
2. Force transcription of rendered text first, "exactly as rendered,
   including any errors or malformed letters."
3. Then a binary checklist of observable yes/no items, each requiring cited
   evidence ("name the letter if letterforms differ").
4. Then the score, with anchored bands and hard caps (duplicated word →
   <0.5; fade/placement violation → <0.6; letterform mismatch → <0.65).
5. Feedback must be a directional edit instruction built from FAILED items
   (the runner appends it to the retry prompt), not a restatement of the
   score: "The letter 'a' in "Said," has chunky merged strokes; re-render
   only the embroidery to match Image 1, keeping everything else unchanged."

Roadmap (not yet implemented — needs runner changes, not just prompts):
- OCR pre-gate on a tight crop of the text zone (exact match + edit
  distance + word-occurrence counts in code; duplicated-word detection
  becomes deterministic). OCR failure on stylized embroidery is
  "unverified", not auto-fail.
- Attach a native-resolution crop of the embroidery zone as an extra gate
  image (crop the original render, not the resized one).
- Checklist-as-schema with the numeric score computed in runner code.
- CLIP/phash similarity of the logo crop vs the exact art as a cheap
  pre-filter.
- Compose-then-harmonize render step (fidelity ladder rung 2).

## 5. Source index (for re-verification)

- Seedream prompt guide: docs.byteplus.com/en/docs/ModelArk/1829186 (+
  Volcano twin docs.volcengine.com/docs/82379/1829186); API ref
  ModelArk/1541523; 5.0-pro editing ModelArk/2582775; tech report arXiv
  2509.20427; fal schemas fal.ai/models/fal-ai/bytedance/seedream/v4/{edit,
  text-to-image}/api; fal guides blog.fal.ai (seedream-5-0-lite-prompting-
  guide, seedream-4-0-on-fal).
- Kling: kling.ai/quickstart/image-to-video-guide, ai-camera-control-guide,
  klingai-video-3-model-user-guide; blogs 4k-ecommerce-product-video-ai-
  guide, fix-ai-video-drift-consistency-guide; API markdown at
  kling.ai/document-api/api/video/3-0-omni/image-to-video.md.
- Claude vision: platform.claude.com/docs/en/build-with-claude/vision.
- VLM-judge: arXiv 2402.04788 (MLLM-as-a-Judge), 2509.17418 (VLMs miss
  misspellings), 2511.19820 (CropVLM), 2601.15286 (iterative refinement >
  best-of-N), CheckEval (EMNLP 2025); edit-drift: arXiv 2512.01755, 2509.13399.
- Compose-then-harmonize precedents: recraft.ai/mockup-generator,
  dynamicmockups.com, tanzijian.com ComfyUI embroidery pipeline, DualFit
  arXiv 2508.12131, OmniRefiner arXiv 2511.19990.
- TapNow: docs.tapnow.ai (agent, canvas, generation-mode pages),
  app.tapnow.ai/pricing, future-stack-reviews.com/tapnow-ai-review.
- Full research dumps: scratchpad research-*.md files (session-local) and
  .playwright-mcp/ page captures.
